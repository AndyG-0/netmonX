#!/usr/bin/env python3
"""Generate the Grafana dashboards in dashboards/*.json.

Run after changing this file or the URLs in .env:
    python3 scripts/build_dashboards.py

Environment variables override .env, and --out writes somewhere else (./dev.sh uses both for local links):
    SPEEDTEST_APP_URL=http://localhost:18765 python3 scripts/build_dashboards.py --out dev/.dashboards
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DS = {"type": "prometheus", "uid": "prometheus"}
PRIVATE_HOSTS = r'host=~"(10|192\\.168|172\\.(1[6-9]|2[0-9]|3[01]))\\..*"'
PUBLIC_HOSTS = r'host!~"(10|192\\.168|172\\.(1[6-9]|2[0-9]|3[01]))\\..*"'
SPEED = lambda m: f'speedtest_tracker_{m}'  # noqa: E731  (names from Speedtest Tracker's PrometheusMetricsService)


def read_env() -> dict[str, str]:
    env = {}
    path = ROOT / ".env"
    if not path.exists():
        path = ROOT / ".env.example"
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.startswith("#"):
            env[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("NETALERTX_URL", "SPEEDTEST_APP_URL"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


# --- panel helpers --------------------------------------------------------


class Layout:
    """Places panels left to right, wrapping at 24 columns."""

    def __init__(self) -> None:
        self.x = self.y = self.row_h = 0
        self.next_id = 1
        self.panels: list[dict] = []

    def add(self, panel: dict, w: int, h: int) -> None:
        if self.x + w > 24:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        panel["id"] = self.next_id
        panel["gridPos"] = {"x": self.x, "y": self.y, "w": w, "h": h}
        self.next_id += 1
        self.x += w
        self.row_h = max(self.row_h, h)
        self.panels.append(panel)

    def row(self, title: str) -> None:
        if self.x:
            self.x, self.y, self.row_h = 0, self.y + self.row_h, 0
        self.panels.append(
            {"type": "row", "title": title, "id": self.next_id, "collapsed": False,
             "gridPos": {"x": 0, "y": self.y, "w": 24, "h": 1}, "panels": []}
        )
        self.next_id += 1
        self.y += 1


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False, table: bool = False) -> dict:
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        t["instant"], t["range"] = True, False
    if table:
        t["format"] = "table"
    return t


def thresholds(*steps: tuple[float | None, str]) -> dict:
    return {"mode": "absolute", "steps": [{"value": v, "color": c} for v, c in steps]}


def stat(title, expr, unit="none", legend="", steps=None, mappings=None, decimals=None, text_mode="auto",
         description="", graph=False, color_mode="value", reduce="lastNotNull"):
    defaults = {"unit": unit, "thresholds": steps or thresholds((None, "green"))}
    if mappings:
        defaults["mappings"] = mappings
    if decimals is not None:
        defaults["decimals"] = decimals
    return {
        "type": "stat", "title": title, "description": description, "datasource": DS,
        "targets": [target(expr, legend)],
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"reduceOptions": {"calcs": [reduce], "fields": "", "values": False},
                    "colorMode": color_mode, "graphMode": "area" if graph else "none",
                    "textMode": text_mode, "justifyMode": "center"},
    }


def timeseries(title, targets, unit="none", description="", stack=False, min_=None, fill=10,
               overrides=None, legend_calcs=("mean", "max")):
    custom = {"lineWidth": 1, "fillOpacity": fill, "showPoints": "never", "spanNulls": False,
              "stacking": {"mode": "normal" if stack else "none"}}
    defaults = {"unit": unit, "custom": custom}
    if min_ is not None:
        defaults["min"] = min_
    return {
        "type": "timeseries", "title": title, "description": description, "datasource": DS,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": overrides or []},
        "options": {"legend": {"displayMode": "table", "placement": "bottom", "calcs": list(legend_calcs)},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
    }


def state_timeline(title, targets, description="", up_down=True):
    if up_down:
        defaults = {
            "mappings": [{"type": "value", "options": {
                "0": {"text": "Down", "color": "red"}, "1": {"text": "Up", "color": "green"}}}],
            "thresholds": thresholds((None, "red"), (1, "green")),
            "color": {"mode": "thresholds"}}
    else:  # plain values (e.g. channel numbers), one colour per value
        defaults = {"color": {"mode": "continuous-BlPu"}}
    return {
        "type": "state-timeline", "title": title, "description": description, "datasource": DS,
        "targets": targets,
        "fieldConfig": {"defaults": defaults, "overrides": []},
        "options": {"showValue": "never" if up_down else "auto", "mergeValues": True, "rowHeight": 0.8},
    }


def heatmap(title, expr, description=""):
    return {
        "type": "heatmap", "title": title, "description": description, "datasource": DS,
        "targets": [{**target(expr, "{{le}}"), "format": "heatmap"}],
        "options": {"calculate": False, "yAxis": {"unit": "s"},
                    "color": {"mode": "scheme", "scheme": "Spectral", "reverse": True, "exponent": 0.5},
                    "cellGap": 0, "tooltip": {"mode": "single", "yHistogram": True}},
    }


def table(title, targets, organize=None, overrides=None, description="", sort=None):
    transformations = [{"id": "merge", "options": {}}]
    if organize:
        transformations.append({"id": "organize", "options": organize})
    panel = {
        "type": "table", "title": title, "description": description, "datasource": DS,
        "targets": targets, "transformations": transformations,
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "filterable": True}}, "overrides": overrides or []},
        "options": {"showHeader": True, "cellHeight": "sm"},
    }
    if sort:
        panel["options"]["sortBy"] = [{"displayName": sort[0], "desc": sort[1]}]
    return panel


def bargauge(title, expr, legend, description=""):
    return {
        "type": "bargauge", "title": title, "description": description, "datasource": DS,
        "targets": [target(expr, legend, instant=True)],
        "fieldConfig": {"defaults": {"unit": "none", "min": 0, "decimals": 0,
                                     "color": {"mode": "palette-classic"},
                                     "thresholds": thresholds((None, "green"))}, "overrides": []},
        "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                    "valueMode": "text", "namePlacement": "left", "sizing": "auto",
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
    }


def text(title, content):
    return {"type": "text", "title": title, "options": {"mode": "markdown", "content": content}}


def override(name, **props):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": k.replace("_", "."), "value": v} for k, v in props.items()]}


def dashboard(uid, title, layout: Layout, links, variables=(), refresh="30s", time_from="now-24h"):
    return {
        "uid": uid, "title": title, "tags": ["netmonx"], "timezone": "browser",
        "schemaVersion": 39, "version": 1, "editable": False, "graphTooltip": 1,
        "refresh": refresh, "time": {"from": time_from, "to": "now"},
        "templating": {"list": list(variables)}, "links": links, "panels": layout.panels,
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "name": "Annotations & Alerts", "type": "dashboard"},
                                 *EVENT_ANNOTATIONS]},
    }


def prom_annotation(name, expr, color, title, text=""):
    """A Prometheus annotation: a marker wherever `expr` returns a series."""
    return {"name": name, "datasource": DS, "enable": True, "hide": False, "iconColor": color,
            "expr": expr, "step": "60s", "titleFormat": title, "textFormat": text, "tagKeys": "node",
            "useValueForTime": False,
            "target": {"expr": expr, "refId": "Anno", "interval": "60s"}}


# Shown on every dashboard, so a dip in a chart can be matched to its cause.
EVENT_ANNOTATIONS = [
    prom_annotation("WAN down", "max by (node) (asus_wan_up) == 0", "red", "WAN down"),
    prom_annotation("Reboots", "resets(asus_router_uptime_seconds[2m]) > 0", "orange", "{{node}} rebooted"),
    prom_annotation("WAN IP changes", "increase(asus_wan_ip_changes_total[2m]) > 0", "blue", "WAN IP changed"),
]


def query_var(name, label, query, multi=False, include_all=False, regex=""):
    v = {
        "name": name, "label": label, "type": "query", "datasource": DS,
        "query": {"query": query, "refId": "v"}, "definition": query, "refresh": 2,
        "multi": multi, "includeAll": include_all, "sort": 1, "current": {},
    }
    if regex:
        v["regex"] = regex
    return v


def relabel(expr, **labels):
    """Copy labels in PromQL: relabel(x, id="mac") sets id from mac. A tuple tries sources in order (last non-empty wins)."""
    for new, src in labels.items():
        for i, one in enumerate(src if isinstance(src, tuple) else (src,)):
            expr = f'label_replace({expr}, "{new}", "$1", "{one}", "{"(.*)" if i == 0 else "(.+)"}")'
    return expr


def topology():
    """Node Graph of router -> mesh nodes -> devices, from two instant table queries (nodes, edges)."""
    infra = ("(max by (node, role) (asus_router_info) * on (node) group_left (node_name) "
             "max by (node, node_name) (asus_client_info)) or max by (node, role) (asus_router_info)")
    infra = relabel(f"({infra})", id="node", title=("node", "node_name"), subtitle="role")
    devices = 'label_replace(max by (mac, hostname, band) (asus_client_info), "subtitle", "wired", "band", "")'
    devices = relabel(f'label_replace({devices}, "subtitle", "$1", "band", "(.+)")', id="mac", title=("mac", "hostname"))
    nodes = f"topk by (id) (1, max by (id, title, subtitle) ({infra} or {devices}))"  # one row per id
    links = relabel("max by (mac, node) (asus_client_info)", id="mac", source="node", target="mac")
    mesh = relabel("max by (ip, node) (asus_mesh_node_info)", id="ip", source="node", target="ip")
    edges = f"topk by (id) (1, max by (id, source, target) ({links} or {mesh}))"
    return {
        "type": "nodeGraph", "title": "Topology", "datasource": DS,
        "description": "Main router, mesh nodes and every device attached to each, from the AiMesh client "
                       "list. Scroll to zoom, drag to pan, click a node for its details.",
        "targets": [target(nodes, ref="nodes", instant=True, table=True),
                    target(edges, ref="edges", instant=True, table=True)],
        "transformations": [{"id": "organize", "options": {"excludeByName": {
            "Time": True, "Value": True, "Value #nodes": True, "Value #edges": True}}}],
        "options": {"nodes": {}, "edges": {}},
    }


UP_MAP = [{"type": "value", "options": {"0": {"text": "DOWN", "color": "red"}, "1": {"text": "UP", "color": "green"}}}]
LAN_RTT = (
    "1000 * sum by (host) (rate(smokeping_response_duration_seconds_sum{{{sel}}}[$__rate_interval]))"
    " / sum by (host) (rate(smokeping_response_duration_seconds_count{{{sel}}}[$__rate_interval]))"
)
LAN_LOSS = (
    "1 - sum by (host) (rate(smokeping_response_duration_seconds_count{{{sel}}}[$__rate_interval]))"
    " / sum by (host) (rate(smokeping_requests_total{{{sel}}}[$__rate_interval]))"
)
CPU_BUSY = (
    "1 - sum by (node) (rate(asus_router_cpu_seconds_total{{mode=\"idle\"{sel}}}[$__rate_interval]))"
    " / sum by (node) (rate(asus_router_cpu_seconds_total{{{sel0}}}[$__rate_interval]))"
)


# --- dashboards -----------------------------------------------------------


SCORE_STEPS = thresholds((None, "red"), (50, "orange"), (70, "yellow"), (85, "green"))
SCORE_HELP = ("0-100 per Wi-Fi device, worked out by the collector: 50% signal (-90 dBm scores 0, -50 dBm "
              "scores full), 30% link rate against what the channel width allows (2-stream Wi-Fi 6), 20% "
              "retries since the device connected (half of frames retried scores 0). Above 85 is great, "
              "below 50 is a poor link.")


def overview(links) -> dict:
    L = Layout()
    L.add(stat("Internet", "max(asus_wan_up)", mappings=UP_MAP,
               steps=thresholds((None, "red"), (1, "green")), color_mode="background",
               description="WAN state reported by the router (with dual WAN: up if either WAN is up)."), 4, 4)
    L.add(stat("Latency (router → internet)", 'avg(asus_ping_rtt_avg_ms{role="internet"})', "ms", decimals=1,
               steps=thresholds((None, "green"), (40, "yellow"), (100, "red")), graph=True,
               description="Average of the router's pings to the internet targets. No home Wi-Fi in the path."), 4, 4)
    L.add(stat("Packet loss (15m)", 'avg(avg_over_time(asus_ping_loss_ratio{role="internet"}[15m]))', "percentunit",
               decimals=2, steps=thresholds((None, "green"), (0.005, "yellow"), (0.02, "red")), graph=True), 4, 4)
    L.add(stat("Jitter", 'avg(asus_ping_jitter_ms{role="internet"})', "ms", decimals=1,
               steps=thresholds((None, "green"), (10, "yellow"), (30, "red")), graph=True), 4, 4)
    L.add(stat("Last download", f"max(last_over_time({SPEED('download_bits')}[1d]))", "bps", decimals=0,
               description="From Speedtest Tracker."), 4, 4)
    L.add(stat("Last upload", f"max(last_over_time({SPEED('upload_bits')}[1d]))", "bps", decimals=0), 4, 4)

    L.add(stat("Devices online", "sum(asus_clients)",
               description="Every device on the mesh, wired or Wi-Fi, from the main router's AiMesh client list."), 4, 4)
    L.add(stat("Wi-Fi devices", 'sum(asus_clients{connection="wifi"}) or vector(0)',
               description="Devices on Wi-Fi, on any node and any SSID (main, guest, IoT)."), 4, 4)
    L.add(stat("Wi-Fi experience", "avg(asus_wifi_client_score)", decimals=0, steps=SCORE_STEPS,
               graph=True, description=SCORE_HELP + " Per node: Clients & Wi-Fi dashboard."), 4, 4)
    L.add(stat("Mesh nodes up", "sum(asus_up)", description="Nodes the collector can reach over SSH.",
               legend="", steps=thresholds((None, "orange"))), 3, 4)
    L.add(stat("Router uptime", 'max(asus_router_uptime_seconds * on(node) group_left group by (node) (asus_router_info{role="primary"}))',
               "s", decimals=0), 3, 4)
    L.add(stat("WAN IP", "asus_wan_info", legend="{{ip}}", text_mode="name", steps=thresholds((None, "blue")),
               color_mode="none"), 3, 4)
    L.add(stat("Router CPU", "(" + CPU_BUSY.format(sel="", sel0="")
               + ') * on(node) group_left group by (node) (asus_router_info{role="primary"})', "percentunit", decimals=0,
               steps=thresholds((None, "green"), (0.7, "yellow"), (0.9, "red"))), 3, 4)

    L.add(timeseries(
        "Internet latency: router vs. LAN",
        [target('asus_ping_rtt_avg_ms{role="internet"}', "router → {{target}}"),
         target('asus_ping_rtt_avg_ms{role="isp_gateway"}', "router → ISP gateway", "B"),
         target(LAN_RTT.format(sel=PUBLIC_HOSTS), "LAN → {{host}}", "C")],
        "ms", min_=0, fill=0,
        description="Router pings leave out your Wi-Fi and LAN. If the LAN lines spike but the router lines "
                    "don't, the problem is inside your home network."), 16, 9)
    L.add(timeseries("Packet loss",
                     [target('asus_ping_loss_ratio{role!=""}', "router → {{target}}"),
                      target(LAN_LOSS.format(sel=PUBLIC_HOSTS), "LAN → {{host}}", "B")],
                     "percentunit", min_=0, fill=20), 8, 9)
    L.add(timeseries(
        "WAN throughput",
        [target("sum(rate(asus_wan_receive_bytes_total[$__rate_interval])) * 8", "download"),
         target("sum(rate(asus_wan_transmit_bytes_total[$__rate_interval])) * -8", "upload", "B")],
        "bps", fill=25, description="Upload is drawn below zero."), 16, 8)
    L.add(state_timeline("Internet connection",
                         [target("max by (wan) (asus_wan_up)", "WAN {{wan}}"),
                          target("min(asus_up)", "router SSH", "B")]), 8, 8)
    return dashboard("netmonx-overview", "Network Overview", L, links)

# Latency the last speed test added under load; same as netmonx:bufferbloat_ms in rules/isp.yml.
BLOAT = (f"clamp_min((max({SPEED('download_latency_iqm_ms')}) > max({SPEED('upload_latency_iqm_ms')}) "
         f"or max({SPEED('upload_latency_iqm_ms')})) - max({SPEED('ping_ms')}), 0)")
BLOAT_HELP = ("Extra latency under load in the last speed test (worse of download and upload, minus idle ping). "
              "Graded like Waveform's bufferbloat test: A+ up to 5 ms, A 30, B 60, C 200, D 400, F above. "
              "B or worse means calls and games stutter while someone downloads; turning on QoS / Smart Queue "
              "(Adaptive QoS on Asus) usually fixes it.")
BLOAT_GRADES = [{"type": "range", "options": {"from": lo, "to": hi, "result": {"text": grade, "color": color}}}
                for lo, hi, grade, color in [(0, 5, "A+", "green"), (5, 30, "A", "green"), (30, 60, "B", "yellow"),
                                             (60, 200, "C", "orange"), (200, 400, "D", "red"),
                                             (400, 1e9, "F", "dark-red")]]


def bloat_grade(title="Bufferbloat"):
    return stat(title, BLOAT, mappings=BLOAT_GRADES, color_mode="background", description=BLOAT_HELP)


def isp(links) -> dict:
    L = Layout()
    L.row("Latency")
    L.add(heatmap("LAN latency distribution: $host",
                  'sum by (le) (increase(smokeping_response_duration_seconds_bucket{host="$host"}[$__rate_interval]))',
                  "SmokePing-style view. A tight band is good; smear or speckle above it means jitter."), 12, 10)
    L.add(timeseries(
        "Router ping range (min / avg / max)",
        [target('asus_ping_rtt_min_ms{role!=""}', "min {{target}}"),
         target('asus_ping_rtt_avg_ms{role!=""}', "avg {{target}}", "B"),
         target('asus_ping_rtt_max_ms{role!=""}', "max {{target}}", "C")],
        "ms", min_=0, fill=0), 12, 10)
    L.add(timeseries("Jitter (router)", [target('asus_ping_jitter_ms{role!=""}', "{{target}}")], "ms", min_=0), 12, 8)
    L.add(timeseries(
        "Long-run loss rate (router)",
        [target("1 - rate(asus_ping_packets_received_total[1h]) / rate(asus_ping_packets_sent_total[1h])",
                "{{target}}")],
        "percentunit", min_=0, description="Share of pings lost over the past hour, per target."), 12, 8)
    L.row("Speed tests")
    L.add(timeseries("Speed test results",
                     [target(f"max({SPEED('download_bits')})", "download"),
                      target(f"max({SPEED('upload_bits')})", "upload", "B")],
                     "bps", min_=0, fill=0, legend_calcs=("lastNotNull", "mean", "min")), 12, 8)
    L.add(timeseries("Speed test latency",
                     [target(f"max({SPEED('ping_ms')})", "idle ping"),
                      target(f"max({SPEED('download_latency_iqm_ms')})", "loaded: download (IQM)", "B"),
                      target(f"max({SPEED('upload_latency_iqm_ms')})", "loaded: upload (IQM)", "C"),
                      target(BLOAT, "added under load", "D")],
                     "ms", min_=0, fill=0,
                     description="A big rise under load points to bufferbloat."), 8, 8)
    L.add(bloat_grade(), 4, 8)
    L.row("Path & addressing")
    L.add(table("Route to first ping target (latest traceroute)",
                [target("asus_traceroute_hop_rtt_ms", ref="A", instant=True, table=True),
                 target("asus_traceroute_hop_loss_ratio", ref="B", instant=True, table=True)],
                organize={"excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True,
                                            "node": True},
                          "renameByName": {"hop": "Hop", "ip": "Address", "Value #A": "RTT (ms)",
                                           "Value #B": "Loss"}},
                overrides=[override("Loss", unit="percentunit", decimals=0)],
                sort=("Hop", False),
                description="3 probes per hop, every SLOW_INTERVAL. Many routers on the path don't answer "
                            "traceroute at all (100% loss with no address): that is normal, not a fault."), 8, 8)
    L.add(table("WAN details",
                [target("asus_wan_info", ref="A", instant=True, table=True),
                 target("asus_isp_hop_info", ref="B", instant=True, table=True)],
                organize={"excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True,
                                            "Value #A": True, "Value #B": True, "node": True},
                          "renameByName": {"ip": "WAN IP", "gateway": "Gateway", "dns": "ISP DNS",
                                           "proto": "Type", "iface": "Interface", "wan": "WAN",
                                           "wan_type": "Port"}}), 8, 8)
    L.add(timeseries("WAN IP changes per day", [target("increase(asus_wan_ip_changes_total[1d])", "changes")],
                     "none", min_=0), 8, 8)
    L.add(timeseries(
        "Latency per hop", [target("max by (hop) (asus_traceroute_hop_rtt_ms)", "hop {{hop}}")], "ms", min_=0,
        fill=0, legend_calcs=("mean", "max"),
        description="The first hop that gets slow, and stays slow for every hop after it, is where the delay starts."),
        12, 8)
    L.add(timeseries(
        "Loss per hop (1h)",
        [target("1 - increase(asus_traceroute_hop_probes_received_total[1h]) / "
                "increase(asus_traceroute_hop_probes_sent_total[1h]) < 1", "hop {{hop}}")],
        "percentunit", min_=0, fill=0, legend_calcs=("mean", "max"),
        description="Hops that never answer are left out. Loss at one hop that later hops don't show is just "
                    "that router ignoring traceroute; loss that carries on to the last hop is real."), 12, 8)
    L.row("DNS & websites")
    L.add(timeseries("DNS lookup time", [target('probe_duration_seconds{job="dns"}', "{{resolver}}")], "s",
                     min_=0, fill=0), 12, 8)
    L.add(timeseries("Website response time",
                     [target('probe_duration_seconds{job="http"}', "{{instance}}"),
                      target('probe_success{job="http"} == 0', "{{instance}} FAILED", "B")],
                     "s", min_=0, fill=0), 12, 8)
    host = query_var("host", "LAN ping target", "label_values(smokeping_response_duration_seconds_count, host)")
    return dashboard("netmonx-isp", "ISP & Internet", L, links, [host])


def isp_report(links) -> dict:
    """Printable ISP report card for any time range (default 30 days). Same maths as rules/isp.yml."""
    L = Layout()
    up = "(max(asus_wan_up))[$__range:1m]"
    rtt = '(avg(asus_ping_rtt_avg_ms{role="internet"}))[$__range:1m]'
    loss = '(avg(asus_ping_loss_ratio{role="internet"}))[$__range:1m]'
    down, upl = SPEED("download_bits"), SPEED("upload_bits")
    plan = 'max(netmonx_isp_plan_bits{{direction="{}"}})'

    def tile(title, expr, unit="none", **kw):
        panel = stat(title, expr, unit, **kw)
        panel["targets"][0].update(instant=True, range=False)
        return panel

    pct = thresholds((None, "red"), (0.99, "orange"), (0.999, "green"))
    L.add(tile("Uptime", f"avg_over_time({up})", "percentunit", decimals=3, steps=pct,
               description="Share of minutes in the time range with a working WAN, as the router reports it. "
                           "Minutes when netmonX itself was down are left out."), 4, 5)
    L.add(tile("Outages", f"ceil(changes({up}) / 2)", decimals=0,
               steps=thresholds((None, "green"), (1, "orange"), (5, "red"))), 4, 5)
    L.add(tile("Total downtime", f"60 * (count_over_time({up}) - sum_over_time({up}))", "s",
               steps=thresholds((None, "green"), (60, "orange"), (3600, "red"))), 4, 5)
    L.add(tile("Latency p95", f"quantile_over_time(0.95, {rtt})", "ms", decimals=1,
               steps=thresholds((None, "green"), (50, "orange"), (100, "red")),
               description="95% of pings from the router to the internet targets were faster than this."), 4, 5)
    L.add(tile("Packet loss", f"avg_over_time({loss})", "percentunit", decimals=2,
               steps=thresholds((None, "green"), (0.005, "orange"), (0.02, "red"))), 4, 5)
    L.add(tile("Speed tests", f"changes((max({SPEED('result_id')}))[$__range:1m])", decimals=0,
               description="New Speedtest Tracker results in the time range."), 4, 5)
    vs = thresholds((None, "red"), (0.6, "orange"), (0.8, "green"))
    L.add(tile("Median download", f"quantile_over_time(0.5, (max({down}))[$__range:5m])", "bps", decimals=0), 5, 5)
    L.add(tile("Download vs. plan", f"quantile_over_time(0.5, (max({down}))[$__range:5m]) / on() {plan.format('download')}",
               "percentunit", decimals=0, steps=vs,
               description="Median download over the plan speed (ISP_PLAN_DOWN in .env). Empty until it is set."), 5, 5)
    L.add(tile("Median upload", f"quantile_over_time(0.5, (max({upl}))[$__range:5m])", "bps", decimals=0), 5, 5)
    L.add(tile("Upload vs. plan", f"quantile_over_time(0.5, (max({upl}))[$__range:5m]) / on() {plan.format('upload')}",
               "percentunit", decimals=0, steps=vs,
               description="Median upload over the plan speed (ISP_PLAN_UP in .env). Empty until it is set."), 5, 5)
    L.add(bloat_grade("Bufferbloat (last test)"), 4, 5)

    L.add(state_timeline("Internet connection", [target("max(asus_wan_up)", "WAN")]), 24, 4)
    daily = timeseries("Uptime per day", [target("avg_over_time((max(asus_wan_up))[1d:1m])", "uptime")],
                       "percentunit", min_=0, fill=30, legend_calcs=("min", "mean"))
    daily["interval"] = "1d"
    daily["fieldConfig"]["defaults"]["custom"]["drawStyle"] = "bars"
    L.add(daily, 12, 8)
    L.add(timeseries("Speed tests vs. plan",
                     [target(f"max({down})", "download"), target(f"max({upl})", "upload", "B"),
                      target(plan.format("download"), "plan download", "C"),
                      target(plan.format("upload"), "plan upload", "D")],
                     "bps", min_=0, fill=0, legend_calcs=("min", "mean", "max")), 12, 8)
    L.add(timeseries("Internet latency (router pings)",
                     [target('avg(asus_ping_rtt_avg_ms{role="internet"})', "average"),
                      target('max(asus_ping_rtt_max_ms{role="internet"})', "worst ping", "B")],
                     "ms", min_=0, fill=0, legend_calcs=("mean", "max")), 12, 8)
    L.add(timeseries("Packet loss (router pings)",
                     [target('avg(asus_ping_loss_ratio{role="internet"})', "loss")],
                     "percentunit", min_=0, legend_calcs=("mean", "max")), 12, 8)
    L.add(text("How this is measured", (
        "Pick the time range at the top (default: last 30 days), then print or export with "
        "**Share → Export → PDF** (or the browser's print). Uptime and outages come from the router's own WAN "
        "status, read every 15 seconds. Latency and loss are pings sent from the router to the internet targets "
        "(`ROUTER_PING_TARGETS`), so Wi-Fi can't skew them. Speeds are Speedtest Tracker results. "
        "Set `ISP_PLAN_DOWN` and `ISP_PLAN_UP` (Mbit/s) in .env to compare with what you pay for. "
        "The same 30-day numbers are kept as Prometheus recording rules (`netmonx:*`, see "
        "config/prometheus/rules/isp.yml) for alerts and scripts.")), 24, 4)
    return dashboard("netmonx-isp-report", "ISP report card", L, links, refresh="", time_from="now-30d")


def router(links) -> dict:
    L = Layout()
    sel = 'node=~"$node"'
    L.add(table("Nodes", [target("asus_router_info", ref="A", instant=True, table=True),
                          target("asus_router_uptime_seconds", ref="B", instant=True, table=True),
                          target("asus_up", ref="C", instant=True, table=True),
                          target("asus_firmware_update_available", ref="D", instant=True, table=True)],
                organize={"excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True,
                                            "Value #A": True},
                          "renameByName": {"node": "Node", "model": "Model", "firmware": "Firmware",
                                           "mac": "MAC", "role": "Role", "Value #B": "Uptime",
                                           "Value #C": "Reachable", "latest": "Latest firmware",
                                           "Value #D": "Update"}},
                overrides=[override("Uptime", unit="s"),
                           override("Update", custom_cellOptions={"type": "color-text"}, mappings=[
                               {"type": "value", "options": {"0": {"text": "up to date", "color": "green"},
                                                             "1": {"text": "available", "color": "orange"}}}]),
                           override("Reachable", mappings=UP_MAP,
                                    custom_cellOptions={"type": "color-text"})]), 24, 6)
    L.add(timeseries("CPU busy", [target(CPU_BUSY.format(sel=", " + sel, sel0=sel), "{{node}}")],
                     "percentunit", min_=0, fill=0), 8, 8)
    L.add(timeseries("Memory used",
                     [target(f"1 - asus_router_memory_available_bytes{{{sel}}} / asus_router_memory_total_bytes{{{sel}}}",
                             "{{node}}")], "percentunit", min_=0, fill=0), 8, 8)
    L.add(timeseries("Temperature", [target(f"asus_router_temperature_celsius{{{sel}}}", "{{node}} {{sensor}}")],
                     "celsius", fill=0,
                     description="Radio sensors are raw driver values; some older chips report in a different "
                                 "scale."), 8, 8)
    L.add(timeseries("Load average (1m)", [target(f"asus_router_load1{{{sel}}}", "{{node}}")], "none",
                     min_=0, fill=0), 8, 8)
    L.add(timeseries("Tracked connections",
                     [target(f"asus_router_conntrack_entries{{{sel}}}", "{{node}}"),
                      target(f"asus_router_conntrack_max{{{sel}}}", "{{node}} limit", "B")], "none", min_=0, fill=0),
          8, 8)
    L.add(timeseries("Mesh node ping from LAN",
                     [target(LAN_RTT.format(sel=PRIVATE_HOSTS), "{{host}}")], "ms", min_=0, fill=0,
                     description="Smokeping pings to router and mesh node IPs."), 8, 8)
    L.add(timeseries(
        "Interface traffic",
        [target(f'rate(asus_net_receive_bytes_total{{{sel}, iface=~"$iface"}}[$__rate_interval]) * 8',
                "{{node}} {{iface}} in"),
         target(f'rate(asus_net_transmit_bytes_total{{{sel}, iface=~"$iface"}}[$__rate_interval]) * -8',
                "{{node}} {{iface}} out", "B")],
        "bps", fill=5), 16, 9)
    L.add(timeseries(
        "Errors & drops",
        [target(f"sum by (node, iface) (rate(asus_net_receive_errors_total{{{sel}}}[$__rate_interval]) + "
                f"rate(asus_net_transmit_errors_total{{{sel}}}[$__rate_interval])) > 0", "{{node}} {{iface}} errors"),
         target(f"sum by (node, iface) (rate(asus_net_receive_drops_total{{{sel}}}[$__rate_interval]) + "
                f"rate(asus_net_transmit_drops_total{{{sel}}}[$__rate_interval])) > 0", "{{node}} {{iface}} drops",
                "B")],
        "pps", min_=0, fill=0), 8, 9)
    L.add(timeseries(
        "Ping from the server (router, nodes, ISP gateway)",
        [target('probe_icmp_duration_seconds{job="lan_icmp", phase="rtt"} * 1000', "{{role}} {{instance}}"),
         target('1 - avg_over_time(probe_success{job="lan_icmp"}[5m])', "{{instance}} loss", "B")],
        "ms", min_=0, fill=0,
        overrides=[{"matcher": {"id": "byRegexp", "options": ".* loss"},
                    "properties": [{"id": "unit", "value": "percentunit"},
                                   {"id": "custom.axisPlacement", "value": "right"}]}],
        description="Blackbox ICMP every 15 s to the targets the collector lists at `/sd/nodes`: new mesh "
                    "nodes appear on their own. A node that's slow here but fine in smokeping points at "
                    "the backhaul."), 24, 8)
    L.row("Wi-Fi radios")
    rinst = lambda expr, ref: target(expr, ref=ref, instant=True, table=True)  # noqa: E731
    L.add(table(
        "Radios",
        [rinst(f"asus_wifi_radio_info{{{sel}}}", "A"), rinst(f"asus_wifi_channel_width_mhz{{{sel}}}", "B"),
         rinst(f"asus_wifi_noise_dbm{{{sel}}}", "C"),
         rinst(f'100 - asus_wifi_airtime_percent{{{sel}, stat="idle"}}', "D"),
         rinst(f"increase(asus_wifi_channel_changes_total{{{sel}}}[24h])", "E")],
        organize={"excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True, "Value #A": True,
                                    "stat": True},
                  "renameByName": {"node": "Node", "iface": "Interface", "radio": "Radio", "band": "Band",
                                   "chanspec": "Channel", "dfs": "DFS", "Value #B": "Width",
                                   "Value #C": "Noise floor", "Value #D": "Busy", "Value #E": "Channel changes (24h)"}},
        overrides=[override("Width", unit="MHz"), override("Noise floor", unit="dBm"),
                   override("Busy", unit="percent", custom_cellOptions={"type": "color-text"},
                            thresholds=thresholds((None, "green"), (50, "yellow"), (75, "red")),
                            color={"mode": "thresholds"}),
                   override("DFS", mappings=[{"type": "value", "options": {
                       "0": {"text": "no"}, "1": {"text": "yes", "color": "orange"}}}]),
                   override("Channel changes (24h)", decimals=0)],
        sort=("Node", False),
        description="From `wl chanspec`, `wl noise` and `wl chanim_stats` on each node. DFS channels (5 GHz 52-144) "
                    "must move when the radio detects radar. Busy is 100 minus the driver's idle airtime share."),
        24, 7)
    L.add(timeseries(
        "Airtime busy",
        [target(f'100 - asus_wifi_airtime_percent{{{sel}, stat="idle"}}', "{{node}} {{iface}}")],
        "percent", min_=0, fill=0,
        description="Share of time the channel was not idle: our traffic, other networks and non-Wi-Fi noise."), 8, 8)
    L.add(timeseries(
        "Other networks on the channel",
        [target(f'asus_wifi_airtime_percent{{{sel}, stat="obss"}}', "{{node}} {{iface}}")],
        "percent", min_=0, fill=0,
        description="`obss` airtime: frames from neighbouring networks on the same channel. High values mean "
                    "a less crowded channel would help."), 8, 8)
    L.add(timeseries(
        "Noise floor", [target(f"asus_wifi_noise_dbm{{{sel}}}", "{{node}} {{iface}}")], "dBm", fill=0,
        description="Lower (more negative) is better. A rise of 5-10 dB points at nearby interference."), 8, 8)
    L.add(state_timeline(
        "Channel", [target(f"asus_wifi_channel{{{sel}}}", "{{node}} {{iface}}")],
        description="Primary channel per radio over time; a change is a DFS move, auto channel or a manual change.",
        up_down=False),
        24, 7)
    L.row("Security & VPN")
    onoff = [{"type": "value", "options": {"0": {"text": "off", "color": "green"},
                                           "1": {"text": "ON", "color": "orange"}}}]
    L.add(table(
        "Exposure",
        [rinst("max by (setting) (asus_exposure_enabled)", "A")],
        organize={"excludeByName": {"Time": True},
                  "renameByName": {"setting": "Setting", "Value": "State"}},
        overrides=[override("State", mappings=onoff, custom_cellOptions={"type": "color-text"})],
        sort=("State", True),
        description="Settings that open the router or LAN to the outside, read from nvram. `ssh_wan` and "
                    "`webui_wan` (router admin reachable from the internet) should stay off. UPnP lets any "
                    "device open ports; WPS push-button/PIN pairing is a known weak spot."),
        8, 8)
    L.add(stat("Settings last changed", "max(asus_config_last_change_timestamp_seconds) * 1000", "dateTimeFromNow",
               steps=thresholds((None, "blue")),
               description="When the collector saw the router's settings change: a hash of `nvram show` (taken on "
                           "the router, values never leave it) without keys that change on their own. Empty "
                           "until the first change after the collector started."), 4, 4)
    L.add(stat("Settings changes (7d)", "sum(increase(asus_config_changes_total[7d]))", decimals=0,
               steps=thresholds((None, "green"), (1, "orange"))), 4, 4)
    L.add(table(
        "Open ports",
        [rinst("asus_port_forward_info", "A"), rinst("asus_upnp_mapping_info", "B")],
        organize={"excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True, "node": True,
                                    "Value #A": True, "Value #B": True},
                  "renameByName": {"rule": "Port forward", "desc": "UPnP (device says)", "ext_port": "WAN port",
                                   "ip": "LAN device", "int_port": "LAN port", "proto": "Protocol"}},
        description="Port forwarding rules (set by you in the web UI) and UPnP mappings (opened by devices on "
                    "their own). Each is a way in from the internet."),
        16, 8)
    L.add(table(
        "VPN clients",
        [rinst("time() - asus_vpn_client_connected_since_timestamp_seconds", "A"),
         rinst("asus_vpn_client_receive_bytes_total", "B"), rinst("asus_vpn_client_transmit_bytes_total", "C")],
        organize={"excludeByName": {"Time": True, "__name__": True, "job": True, "instance": True, "node": True},
                  "renameByName": {"server": "Server", "client": "Client", "real_address": "From",
                                   "virtual_address": "VPN address", "Value #A": "Connected for",
                                   "Value #B": "Received", "Value #C": "Sent"}},
        overrides=[override("Connected for", unit="s"), override("Received", unit="bytes"),
                   override("Sent", unit="bytes")],
        description="Connected OpenVPN clients from the server status file. WireGuard peers only show traffic: "
                    "see the chart for their last handshake."),
        16, 8)
    L.add(timeseries(
        "VPN clients connected",
        [target("sum by (server) (asus_vpn_clients)", "{{server}}"),
         target("max by (peer) (time() - asus_wireguard_peer_last_handshake_timestamp_seconds)",
                "WireGuard {{peer}}: s since handshake", "B")],
        "none", min_=0, fill=0,
        description="WireGuard counts a peer as connected while it handshook in the last 3 minutes."), 8, 8)
    L.row("Topology")
    L.add(topology(), 24, 16)

    L.row("Router events (from the router log)")
    ev = 'sum by (event) (increase(asus_syslog_events_total{{node=~"$node", event=~"{}"}}[$__range]))'
    L.add(bargauge("Events in range", ev.format(".*") + " > 0", "{{event}}",
                   description="Counted from /tmp/syslog.log on each node, from lines written after the collector "
                               "started. ssh_login_key includes the collector's own logins."), 8, 10)
    hourly = 'sum by (event) (increase(asus_syslog_events_total{{node=~"$node", event=~"{}"}}[1h]))'
    L.add(timeseries("Logins and failures per hour",
                     [target(hourly.format("ssh_.*|vpn_.*|web_.*"), "{{event}}")],
                     "none", min_=0, fill=0, legend_calcs=("sum", "max"),
                     description="ssh_login_password: something logged in to the router with the admin password "
                                 "(the Asus app, a script, or a person). Failures are worth a look."), 8, 10)
    L.add(timeseries("Wi-Fi and WAN events per hour",
                     [target(hourly.format("wifi_.*|roam_.*|weak_.*|band_.*|wan_.*|dfs_.*|out_of_.*|crash"),
                             "{{event}}")],
                     "none", min_=0, fill=0, legend_calcs=("sum", "max"),
                     description="roam_assist / weak_signal_kick: the router pushed a device off a weak link. "
                                 "dfs_radar: radar forced a 5 GHz channel change."), 8, 10)

    L.row("Collector")
    L.add(timeseries(
        "SSH command time (p95)",
        [target("histogram_quantile(0.95, sum by (node, command, le) "
                f"(rate(netmonx_collector_ssh_command_duration_seconds_bucket{{{sel}}}[$__rate_interval])))",
                "{{node}} {{command}}")],
        "s", min_=0, fill=0, description="How long each router command takes over SSH."), 12, 8)
    L.add(timeseries(
        "Collector problems",
        [target(f"sum by (node) (increase(netmonx_collector_ssh_command_errors_total{{{sel}}}[1h]))",
                "{{node}} command errors"),
         target(f"sum by (node) (increase(netmonx_collector_ssh_connects_total{{{sel}}}[1h]))",
                "{{node}} SSH connects", "B"),
         target(f"sum by (node, section) (increase(netmonx_collector_parse_failures_total{{{sel}}}[1h]))",
                "{{node}} unparsed {{section}}", "C")],
        "none", min_=0, fill=0,
        description="Per hour. One connect per node is normal; more means the connection keeps dropping."), 12, 8)
    node = query_var("node", "Node", "label_values(asus_up, node)", multi=True, include_all=True)
    iface = query_var("iface", "Interface", 'label_values(asus_net_receive_bytes_total{node=~"$node"}, iface)',
                      multi=True, include_all=True)
    return dashboard("netmonx-router", "Router & Mesh", L, links, [node, iface])


def clients(links, netalertx_url) -> dict:
    L = Layout()
    wired_or_wifi = "Counts come from the main router's AiMesh client list, so every node is included."
    L.add(stat("Devices", "sum(asus_clients)", description=wired_or_wifi, graph=True), 4, 4)
    L.add(stat("Wi-Fi", 'sum(asus_clients{connection="wifi"}) or vector(0)', graph=True,
               description="Wi-Fi devices on every node and SSID."), 4, 4)
    L.add(stat("Wired", 'sum(asus_clients{connection="wired"}) or vector(0)', graph=True,
               description="Devices on an Ethernet port of the router or a mesh node."), 4, 4)
    L.add(stat("Guest network", 'sum(asus_clients{ssid=~"(?i).*guest.*"}) or vector(0)',
               description="Devices on an SSID with \"guest\" in its name."), 4, 4)
    L.add(stat("Weak signal (< -70 dBm)", "count(asus_client_rssi_dbm < -70) or vector(0)",
               steps=thresholds((None, "green"), (1, "yellow"), (5, "red"))), 4, 4)
    L.add(stat("DHCP leases", "sum(asus_dhcp_leases)",
               description="Leases include devices that left recently, so this is usually higher than Devices."), 4, 4)

    L.add(bargauge("Devices by mesh node", 'sum by (node_name, connection) (asus_clients)',
                   "{{node_name}} · {{connection}}"), 8, 9)
    L.add(bargauge("Wi-Fi devices by band", 'sum by (band) (asus_clients{connection="wifi"})', "{{band}}"), 8, 9)
    L.add(bargauge("Wi-Fi devices by SSID", 'sum by (ssid) (asus_clients{connection="wifi"})', "{{ssid}}"), 8, 9)

    L.add(timeseries("Devices by mesh node", [target("sum by (node_name) (asus_clients)", "{{node_name}}")],
                     "none", stack=True, min_=0, fill=40, legend_calcs=("lastNotNull", "max")), 12, 8)
    L.add(timeseries("Wired vs. Wi-Fi", [target("sum by (connection) (asus_clients)", "{{connection}}")],
                     "none", stack=True, min_=0, fill=40, legend_calcs=("lastNotNull", "max")), 12, 8)

    inst = lambda expr, ref: target(expr, ref=ref, instant=True, table=True)  # noqa: E731
    hide = {"Time": True, "__name__": True, "job": True, "instance": True}
    names = {"hostname": "Name", "mac": "MAC", "ip": "IP", "node_name": "Mesh node", "connection": "Connection",
             "band": "Band", "ssid": "SSID", "vendor": "Vendor", "owner": "Owner", "type": "Type"}
    signal = override("Signal", unit="dBm", custom_cellOptions={"type": "color-background"},
                      thresholds=thresholds((None, "red"), (-75, "orange"), (-67, "yellow"), (-55, "green")),
                      color={"mode": "thresholds"})
    order = {"Name": 0, "IP": 1, "Mesh node": 2, "Connection": 3, "Band": 4, "SSID": 5, "Signal": 6, "MAC": 7,
             "Vendor": 8, "Owner": 9, "Type": 10}
    L.row("All devices")
    L.add(table(
        "All devices",
        [inst("asus_client_info", "A"),
         inst("asus_client_rssi_dbm * on (mac) group_left (ip, vendor, owner, type) asus_client_info", "B")],
        organize={"excludeByName": {**hide, "node": True, "Value #A": True},
                  "renameByName": {**names, "Value #B": "Signal"}, "indexByName": order},
        overrides=[signal, override("MAC", links=DEVICE_LINK)], sort=("Mesh node", False),
        description="One row per device. Click a MAC for its details page. Filter a column with the funnel icon in its header. " + wired_or_wifi),
        24, 14)
    L.add(table(
        "Wired devices", [inst('asus_client_info{connection="wired"}', "A")],
        organize={"excludeByName": {**hide, "node": True, "Value": True, "connection": True, "band": True,
                                    "ssid": True},
                  "renameByName": names},
        sort=("Mesh node", False),
        description="Devices behind a mesh node show under that node, not the main router."), 12, 10)
    L.add(table("DHCP leases", [inst("asus_dhcp_lease_info", "A")],
                organize={"excludeByName": {**hide, "Value": True, "node": True},
                          "renameByName": {"hostname": "Name", "ip": "IP", "mac": "MAC"}},
                sort=("IP", False)), 12, 10)

    L.row("Presence & roaming")
    L.add(stat("New devices (24h)", "count(max_over_time(asus_client_new[24h])) or vector(0)",
               steps=thresholds((None, "green"), (1, "yellow")),
               description="Devices the collector had never seen before. The collector's first poll after a fresh "
                           "install is the baseline, so those devices don't count."), 4, 5)
    L.add(stat("Roams (24h)", "sum(increase(asus_client_roams_total[24h])) or vector(0)", decimals=0,
               description="Moves to another mesh node or band while connected."), 4, 5)
    L.add(stat("Known devices offline", "count(asus_client_online == 0) or vector(0)",
               description="Seen in the last CLIENT_RETENTION_DAYS days but not connected now."), 4, 5)
    L.add(timeseries("Roams per hour", [target("sum(increase(asus_client_roams_total[1h]))", "roams")],
                     "none", min_=0, fill=20, legend_calcs=("sum", "max")), 12, 5)
    when = lambda name: override(name, unit="dateTimeFromNow")  # noqa: E731
    L.add(table(
        "Recent roams (24h)",
        [inst("1000 * (asus_client_last_roam_timestamp_seconds > time() - 86400)", "A")],
        organize={"excludeByName": hide,
                  "renameByName": {"hostname": "Name", "mac": "MAC", "from_node": "From node", "to_node": "To node",
                                   "from_band": "From band", "to_band": "To band", "Value": "When"}},
        overrides=[when("When")], sort=("When", True),
        description="The latest roam of each device that roamed in the past day."), 12, 10)
    L.add(table(
        "Last seen",
        [inst("asus_client_online", "A"), inst("1000 * asus_client_last_seen_timestamp_seconds", "B"),
         inst("1000 * asus_client_first_seen_timestamp_seconds", "C")],
        organize={"excludeByName": hide,
                  "renameByName": {**names, "Value #A": "Online", "Value #B": "Last seen", "Value #C": "First seen"}},
        overrides=[override("Online", mappings=UP_MAP, custom_cellOptions={"type": "color-text"}),
                   when("Last seen"), when("First seen"), override("MAC", links=DEVICE_LINK)],
        sort=("Last seen", True),
        description="Every device seen in the retention window, online or not."), 12, 10)
    L.add(state_timeline("Online timeline",
                         [target('asus_client_online{hostname!=""}', "{{hostname}}")],
                         description="Named devices only (name them in the Asus app or devices.yml)."), 24, 12)

    L.row("Wi-Fi link details (from each node's radios)")
    L.add(table(
        "Wi-Fi link details",
        [inst("asus_wifi_client_rssi_dbm", "A"), inst("asus_wifi_client_tx_rate_mbps", "B"),
         inst("asus_wifi_client_rx_rate_mbps", "C"), inst("asus_wifi_client_connected_seconds", "D"),
         inst("rate(asus_wifi_client_transmit_bytes_total[10m]) * 8", "E"), inst("asus_wifi_client_snr_db", "F"),
         inst("asus_wifi_client_score", "G"),
         inst("100 * asus_wifi_client_tx_retries_total / (asus_wifi_client_tx_packets_total "
              "+ asus_wifi_client_tx_retries_total > 0)", "H")],
        organize={"excludeByName": {**hide, "iface": True},
                  "renameByName": {"hostname": "Name", "mac": "MAC", "node": "Node", "radio": "Radio",
                                   "ssid": "SSID", "Value #A": "Signal", "Value #B": "Tx rate",
                                   "Value #C": "Rx rate", "Value #D": "Connected",
                                   "Value #E": "Download (10m avg)", "Value #F": "SNR", "Value #G": "Score",
                                   "Value #H": "Retries"}},
        overrides=[override("Score", decimals=0, custom_cellOptions={"type": "color-background"},
                            thresholds=SCORE_STEPS, color={"mode": "thresholds"}),
                   override("Retries", unit="percent", decimals=1),
                   signal, override("SNR", unit="dB", custom_cellOptions={"type": "color-text"},
                                    thresholds=thresholds((None, "red"), (15, "orange"), (25, "yellow"), (40, "green")),
                                    color={"mode": "thresholds"}), override("Tx rate", unit="Mbits"), override("Rx rate", unit="Mbits"),
                   override("Connected", unit="s"), override("Download (10m avg)", unit="bps")],
        sort=("Signal", False),
        description="Link rate and traffic per Wi-Fi device, read with `wl` on each node that accepts SSH."),
        24, 12)
    L.add(timeseries(
        "Wi-Fi experience by node",
        [target("avg by (node) (asus_wifi_client_score)", "{{node}}"),
         target("avg(asus_wifi_client_score)", "whole network", "B")],
        "none", min_=0, fill=0, legend_calcs=("mean", "min", "lastNotNull"),
        description=SCORE_HELP), 12, 9)
    L.add(table(
        "Sticky clients (weak signal for an hour, no roam)",
        [inst('max_over_time(asus_wifi_client_rssi_dbm[1h]) < -75 and on(mac) '
              'asus_wifi_client_connected_seconds > 3600', "A")],
        organize={"excludeByName": {**hide, "iface": True, "radio": True},
                  "renameByName": {"hostname": "Name", "mac": "MAC", "node": "Node", "ssid": "SSID",
                                   "Value": "Best signal (1h)"}},
        overrides=[override("Best signal (1h)", unit="dBm")],
        description="Devices that stayed below -75 dBm for the last hour on the same node. A closer node "
                    "would serve them better: they hold on to a weak link instead of roaming. Turning Wi-Fi "
                    "off and on, or moving the node, usually helps."), 6, 9)
    L.add(table(
        "On 2.4 GHz but 5 GHz capable",
        [inst('asus_client_info{band="2.4 GHz"} * on(mac) group_left asus_client_5ghz_capable', "A")],
        organize={"excludeByName": {**hide, "Value": True, "ip": True, "connection": True, "ssid_key": True,
                                    "vendor": True, "owner": True, "type": True},
                  "renameByName": {"hostname": "Name", "mac": "MAC", "node": "Node", "node_name": "Node name",
                                   "band": "Band", "ssid": "SSID"}},
        description="Devices on 2.4 GHz now that have used 5 or 6 GHz before (within the device history). "
                    "2.4 GHz is slower and more crowded; these usually end up there from distance or "
                    "smart-connect band steering."), 6, 9)
    L.add(timeseries("Signal strength", [target("asus_client_rssi_dbm", "{{hostname}} {{mac}}")],
                     "dBm", fill=0, legend_calcs=("lastNotNull", "min")), 12, 10)
    L.add(timeseries("Top Wi-Fi clients by traffic",
                     [target("topk(10, rate(asus_wifi_client_transmit_bytes_total[$__rate_interval]) * 8)",
                             "{{hostname}} {{mac}}")], "bps", min_=0, fill=0), 12, 10)
    L.row("Device ping (LAN_CLIENT_PING)")
    L.add(timeseries(
        "Ping to each device",
        [target('probe_icmp_duration_seconds{job="lan_clients", phase="rtt"} * 1000', "{{hostname}} {{mac}}")],
        "ms", min_=0, fill=0, legend_calcs=("mean", "max", "lastNotNull"),
        description="Empty unless `LAN_CLIENT_PING=true` in .env. One ping a minute from the server to each "
                    "online device on the main LAN. Phones in power-save mode answer late or not at all."), 16, 9)
    L.add(table(
        "Not answering (last 15 min)",
        [target('1 - avg_over_time(probe_success{job="lan_clients"}[15m]) > 0.5', ref="A", instant=True,
                table=True)],
        organize={"excludeByName": {"Time": True, "__name__": True, "job": True},
                  "renameByName": {"instance": "IP", "hostname": "Device", "mac": "MAC", "Value": "Lost"}},
        overrides=[override("Lost", unit="percentunit")],
        description="Online per the router but losing most pings: often a firewall on the device (Windows "
                    "blocks ping by default), sometimes a bad Wi-Fi link."), 8, 9)
    L.row("LAN speed tests (iperf3 profile)")
    L.add(timeseries(
        "LAN speed by client",
        [target('max by (client, direction) (netmonx_lan_speed_bits{job="lan_speed"})', "{{client}} {{direction}}")],
        "bps", min_=0, fill=0, legend_calcs=("lastNotNull", "max"),
        description="From `scripts/lan-speed.sh <server> [name]` on a laptop or desktop (COMPOSE_PROFILES=iperf3). "
                    "Each line holds its last result until the next test. Wi-Fi vs. wired, or one room vs. "
                    "another, tells you whether a slow internet complaint is really the Wi-Fi."), 16, 8)
    L.add(table(
        "Last test per client",
        [target('max by (client, direction) (netmonx_lan_speed_bits{job="lan_speed"})', ref="A", instant=True,
                table=True),
         target('max by (client) (netmonx_lan_speed_timestamp_seconds{job="lan_speed"}) * 1000', ref="B",
                instant=True, table=True)],
        organize={"excludeByName": {"Time": True},
                  "renameByName": {"client": "Client", "direction": "Direction", "Value #A": "Speed",
                                   "Value #B": "When"}},
        overrides=[override("Speed", unit="bps"), override("When", unit="dateTimeFromNow")]), 8, 8)
    if netalertx_url:
        note = (f"Full device inventory, new-device alerts and presence history are in "
                f"**[NetAlertX]({netalertx_url})**.")
    else:
        note = ("NetAlertX (device inventory and new-device alerts) isn't running here. It needs host "
                "networking for ARP scans, so it only runs on the Linux server.")
    L.add(text("Device discovery", note + "\n\nNames come from the Asus app and DHCP. Name devices in the "
               "Asus app to make this page easier to read."), 24, 3)
    return dashboard("netmonx-clients", "Clients & Wi-Fi", L, links)

DEVICE_LINK = [{"title": "Device details", "url": "/d/netmonx-device?var-mac=${__value.raw}&${__url_time_range}"}]


def device(links) -> dict:
    """Everything about one device, picked by MAC (linked from the MAC columns on Clients & Wi-Fi)."""
    L = Layout()
    m = 'mac="$mac"'
    when = lambda name: override(name, unit="dateTimeFromNow")  # noqa: E731
    L.add(stat("Online", f"max(asus_client_online{{{m}}})", mappings=UP_MAP, color_mode="background",
               steps=thresholds((None, "red"), (1, "green"))), 4, 4)
    L.add(stat("Signal", f"max(asus_client_rssi_dbm{{{m}}})", "dBm",
               steps=thresholds((None, "red"), (-75, "orange"), (-67, "yellow"), (-55, "green")),
               description="As seen by the mesh node it is attached to. Empty for wired devices."), 4, 4)
    L.add(stat("Wi-Fi score", f"max(asus_wifi_client_score{{{m}}})", steps=SCORE_STEPS, decimals=0,
               description=SCORE_HELP), 4, 4)
    L.add(stat("Last seen", f"1000 * max(asus_client_last_seen_timestamp_seconds{{{m}}})", "dateTimeFromNow"), 4, 4)
    L.add(stat("First seen", f"1000 * max(asus_client_first_seen_timestamp_seconds{{{m}}})", "dateTimeFromNow",
               description="First sighting within the retention window (CLIENT_RETENTION_DAYS)."), 4, 4)
    L.add(stat("Roams (range)", f"sum(increase(asus_client_roams_total{{{m}}}[$__range])) or vector(0)", decimals=0,
               description="Moves to another node or band while connected, in the selected time range."), 4, 4)

    inst = lambda expr, ref: target(expr, ref=ref, instant=True, table=True)  # noqa: E731
    hide = {"Time": True, "__name__": True, "job": True, "instance": True, "Value": True}
    L.add(table(
        "Device", [inst(f"asus_client_info{{{m}}}", "A")],
        organize={"excludeByName": {**hide, "node": True},
                  "renameByName": {"hostname": "Name", "mac": "MAC", "ip": "IP", "node_name": "Mesh node",
                                   "connection": "Connection", "band": "Band", "ssid": "SSID",
                                   "vendor": "Vendor", "owner": "Owner", "type": "Type"}},
        description="Current entry in the AiMesh client list. Empty while the device is offline."), 16, 4)
    L.add(table(
        "DHCP lease", [inst(f"asus_dhcp_lease_info{{{m}}}", "A")],
        organize={"excludeByName": {**hide, "node": True, "mac": True},
                  "renameByName": {"hostname": "Lease name", "ip": "IP"}},
        description="Static (manually assigned) addresses have no lease."), 8, 4)

    L.row("Link")
    L.add(timeseries(
        "Signal",
        [target(f"asus_client_rssi_dbm{{{m}}}", "client list ({{node_name}})"),
         target(f"asus_wifi_client_rssi_dbm{{{m}}}", "radio {{node}} {{radio}}", "B"),
         target(f"asus_wifi_client_snr_db{{{m}}}", "SNR {{node}} {{radio}}", "C")],
        "dBm", fill=0, legend_calcs=("lastNotNull", "min", "mean")), 12, 9)
    L.add(timeseries(
        "Link rate",
        [target(f"asus_wifi_client_tx_rate_mbps{{{m}}}", "to device {{radio}}"),
         target(f"asus_wifi_client_rx_rate_mbps{{{m}}}", "from device {{radio}}", "B")],
        "Mbits", min_=0, fill=0, legend_calcs=("lastNotNull", "min", "mean")), 12, 9)
    L.add(timeseries(
        "Traffic",
        [target(f"sum(rate(asus_wifi_client_transmit_bytes_total{{{m}}}[$__rate_interval])) * 8", "download"),
         target(f"sum(rate(asus_wifi_client_receive_bytes_total{{{m}}}[$__rate_interval])) * 8", "upload", "B")],
        "bps", min_=0, legend_calcs=("mean", "max"),
        description="Wi-Fi only: the router doesn't count traffic per wired device."), 12, 9)
    L.add(timeseries(
        "Wi-Fi score and retries",
        [target(f"max(asus_wifi_client_score{{{m}}})", "score"),
         target(f"100 * sum(rate(asus_wifi_client_tx_retries_total{{{m}}}[$__rate_interval])) / "
                f"(sum(rate(asus_wifi_client_tx_packets_total{{{m}}}[$__rate_interval])) + "
                f"sum(rate(asus_wifi_client_tx_retries_total{{{m}}}[$__rate_interval])) > 0)", "retries %", "B")],
        "none", min_=0, fill=0, legend_calcs=("mean", "min", "lastNotNull"), description=SCORE_HELP), 12, 9)

    L.row("Where and when")
    L.add(state_timeline("Online", [target(f"max(asus_client_online{{{m}}})", "online")]), 24, 4)
    L.add(state_timeline(
        "Attached to", [target(f"max by (node_name, band) (asus_client_info{{{m}}})", "{{node_name}} {{band}}")],
        description="One row per mesh node and band the device used in the time range. Gaps are offline time, "
                    "a jump to another row is a roam or reconnect."), 24, 6)
    L.add(table(
        "Latest roam", [inst(f"1000 * asus_client_last_roam_timestamp_seconds{{{m}}}", "A")],
        organize={"excludeByName": {**hide, "Value": False, "mac": True, "hostname": True},
                  "renameByName": {"from_node": "From node", "to_node": "To node", "from_band": "From band",
                                   "to_band": "To band", "Value": "When"}},
        overrides=[when("When")]), 12, 4)
    L.add(timeseries(
        "Ping from the server",
        [target(f'probe_icmp_duration_seconds{{job="lan_clients", phase="rtt", {m}}} * 1000', "rtt")],
        "ms", min_=0, fill=0, legend_calcs=("mean", "max"),
        description="Empty unless `LAN_CLIENT_PING=true` in .env (main-LAN devices only)."), 12, 8)
    named = 'max by (hostname, mac) (asus_client_online{hostname!=""})'
    unnamed = 'label_replace(max by (hostname, mac) (asus_client_online{hostname=""}), "hostname", "$1", "mac", "(.*)")'
    mac = query_var("mac", "Device", f"query_result({named} or {unnamed})",
                    regex='/hostname="(?<text>[^"]*)",\\s*mac="(?<value>[^"]*)"/')
    return dashboard("netmonx-device", "Device", L, links, [mac], time_from="now-7d")


def status(links) -> dict:
    """Big tiles for a wall display: open /d/netmonx-status?kiosk (see GRAFANA_ANONYMOUS in .env)."""
    L = Layout()
    L.add(stat("Internet", "max(asus_wan_up)", mappings=UP_MAP,
               steps=thresholds((None, "red"), (1, "green")), color_mode="background"), 6, 6)
    L.add(stat("Latency", 'avg(asus_ping_rtt_avg_ms{role="internet"})', "ms", decimals=0,
               steps=thresholds((None, "green"), (40, "yellow"), (100, "red")), color_mode="background",
               graph=True), 6, 6)
    L.add(stat("Packet loss (15m)", 'avg(avg_over_time(asus_ping_loss_ratio{role="internet"}[15m]))',
               "percentunit", decimals=1, steps=thresholds((None, "green"), (0.005, "yellow"), (0.02, "red")),
               color_mode="background"), 6, 6)
    L.add(stat("Mesh nodes up", "sum(asus_up)", steps=thresholds((None, "blue")), color_mode="background"), 6, 6)
    L.add(stat("Devices online", "sum(asus_clients)", steps=thresholds((None, "blue")), graph=True), 6, 5)
    L.add(stat("Last download", f"max(last_over_time({SPEED('download_bits')}[1d]))", "bps", decimals=0,
               steps=thresholds((None, "blue"))), 6, 5)
    L.add(stat("Last upload", f"max(last_over_time({SPEED('upload_bits')}[1d]))", "bps", decimals=0,
               steps=thresholds((None, "blue"))), 6, 5)
    L.add(stat("Router temperature", "max(asus_router_temperature_celsius)", "celsius", decimals=0,
               steps=thresholds((None, "green"), (75, "yellow"), (85, "red"))), 6, 5)
    L.add({"type": "alertlist", "title": "Firing alerts", "datasource": None,
           "options": {"viewMode": "list", "groupMode": "default", "maxItems": 10, "sortOrder": 1,
                       "dashboardAlerts": False, "alertInstanceLabelFilter": "", "showInstances": True,
                       "stateFilter": {"firing": True, "pending": True, "noData": False, "normal": False,
                                       "error": True}}}, 12, 8)
    L.add(timeseries("Internet latency (6h)", [target('avg(asus_ping_rtt_avg_ms{role="internet"})', "latency")],
                     "ms", min_=0, fill=20, legend_calcs=()), 12, 8)
    return dashboard("netmonx-status", "Status", L, links, refresh="1m", time_from="now-6h")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=ROOT / "dashboards", help="output folder (default: dashboards/)")
    out = ap.parse_args().out
    env = read_env()
    netalertx = env.get("NETALERTX_URL", "http://localhost:20211")  # empty: no NetAlertX link
    speedtest = env.get("SPEEDTEST_APP_URL", "http://localhost:18765")
    links = [{"title": "netmonX", "type": "dashboards", "tags": ["netmonx"], "asDropdown": False,
              "includeVars": False, "keepTime": True}]
    if netalertx:
        links.append({"title": "NetAlertX", "type": "link", "url": netalertx, "targetBlank": True,
                      "icon": "external link"})
    if speedtest:
        links.append({"title": "Speedtest Tracker", "type": "link", "url": speedtest, "targetBlank": True,
                      "icon": "external link"})
    out.mkdir(parents=True, exist_ok=True)
    for name, dash in {"overview": overview(links), "isp": isp(links), "router": router(links),
                       "clients": clients(links, netalertx), "status": status(links),
                       "device": device(links), "isp-report": isp_report(links)}.items():
        (out / f"{name}.json").write_text(json.dumps(dash, indent=2) + "\n")
        print(f"wrote {out / name}.json ({len(dash['panels'])} panels)")


if __name__ == "__main__":
    main()
