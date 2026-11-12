#!/usr/bin/env python3
"""Weekly digest: a few lines on how the network did, sent to ntfy (or printed).

    scripts/digest.py                 print the last 7 days
    scripts/digest.py --send          send to NTFY_URL (prints when it isn't set)
    scripts/digest.py --schedule "mon 08:00"   run forever, send once a week (for the compose `digest` profile)

Settings (environment): PROMETHEUS_URL (default http://localhost:9090), NTFY_URL (e.g. https://ntfy.sh/<topic>
or your own ntfy server), NTFY_TOKEN (optional access token), DIGEST_DAYS (default 7), DIGEST_NAMES (true to list
the names of new devices; off by default because a public ntfy.sh topic can be read by anyone who knows its name).

Standard library only, so it runs anywhere Python 3.9+ does.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Callable

Query = Callable[[str], list]

WAN = "(max(asus_wan_up))"
RTT = '(avg(asus_ping_rtt_avg_ms{role="internet"}))'
LOSS = '(avg(asus_ping_loss_ratio{role="internet"}))'
GRADES = [(5, "A+"), (30, "A"), (60, "B"), (200, "C"), (400, "D")]  # same as the dashboards


def prom_query(base: str, timeout: float = 30) -> Query:
    def query(expr: str) -> list:
        url = f"{base.rstrip('/')}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.load(resp)
        if body.get("status") != "success":
            raise RuntimeError(f"Prometheus: {body.get('error', body)}")
        return body["data"]["result"]

    return query


def scalar(query: Query, expr: str) -> float | None:
    result = query(expr)
    if not result:
        return None
    value = float(result[0]["value"][1])
    return None if value != value else value  # NaN


def grade(ms: float) -> str:
    return next((g for limit, g in GRADES if ms <= limit), "F")


def mbit(bits: float) -> str:
    return f"{bits / 1e6:.0f} Mbit/s"


def collect(query: Query, days: int = 7, names: bool = False) -> list[str]:
    """The digest as lines of text (Markdown, as ntfy renders it)."""
    r = f"{days}d"
    lines: list[str] = []

    up = scalar(query, f"avg_over_time({WAN}[{r}:1m])")
    if up is None:
        lines.append("**Internet:** no data (is the collector running?)")
    else:
        outages = scalar(query, f"ceil(changes({WAN}[{r}:1m]) / 2)") or 0
        down = scalar(query, f"count_over_time({WAN}[{r}:1m]) - sum_over_time({WAN}[{r}:1m])") or 0
        text = f"**Internet:** up {up * 100:.2f}%"
        if outages:
            text += f", {outages:.0f} outage{'s' if outages != 1 else ''} ({down:.0f} min down)"
        else:
            text += ", no outages"
        lines.append(text)

    p50 = scalar(query, f"quantile_over_time(0.5, {RTT}[{r}:1m])")
    p95 = scalar(query, f"quantile_over_time(0.95, {RTT}[{r}:1m])")
    loss = scalar(query, f"avg_over_time({LOSS}[{r}:1m])")
    if p50 is not None:
        text = f"**Latency:** {p50:.0f} ms typical, {p95 or 0:.0f} ms p95"
        if loss is not None:
            text += f", {loss * 100:.2f}% loss"
        lines.append(text)

    dl = scalar(query, f"quantile_over_time(0.5, max(speedtest_tracker_download_bits)[{r}:5m])")
    ul = scalar(query, f"quantile_over_time(0.5, max(speedtest_tracker_upload_bits)[{r}:5m])")
    if dl is not None:
        text = f"**Speed (median):** {mbit(dl)} down"
        plan = scalar(query, 'max(netmonx_isp_plan_bits{direction="download"})')
        if plan:
            text += f" ({dl / plan * 100:.0f}% of plan)"
        if ul is not None:
            text += f", {mbit(ul)} up"
            plan = scalar(query, 'max(netmonx_isp_plan_bits{direction="upload"})')
            if plan:
                text += f" ({ul / plan * 100:.0f}% of plan)"
        lines.append(text)
    bloat = scalar(
        query,
        "clamp_min((max(speedtest_tracker_download_latency_iqm_ms) > max(speedtest_tracker_upload_latency_iqm_ms)"
        " or max(speedtest_tracker_upload_latency_iqm_ms)) - max(speedtest_tracker_ping_ms), 0)",
    )
    if bloat is not None:
        lines.append(f"**Bufferbloat:** grade {grade(bloat)} (+{bloat:.0f} ms under load, last test)")

    online = scalar(query, "count(asus_client_online == 1)")
    # asus_client_new is 1 for an hour after a device first shows up (not for the collector's first-poll baseline).
    new = query(f"max by (mac, hostname) (max_over_time(asus_client_new[{r}])) == 1")
    if online is not None:
        text = f"**Devices:** {online:.0f} online now, {len(new)} new this period"
        if names and new:
            shown = sorted({d["metric"].get("hostname") or d["metric"].get("mac", "?") for d in new})
            text += ": " + ", ".join(shown[:15]) + (f" and {len(shown) - 15} more" if len(shown) > 15 else "")
        lines.append(text)

    reboots = query(f"resets(asus_router_uptime_seconds[{r}]) > 0")
    if reboots:
        nodes = ", ".join(sorted(d["metric"].get("node", "?") for d in reboots))
        lines.append(f"**Reboots:** {nodes}")
    events = {
        d["metric"].get("event"): float(d["value"][1])
        for d in query(f"sum by (event) (increase(asus_syslog_events_total[{r}]))")
    }
    failed = sum(v for k, v in events.items() if k and k.endswith("_failed"))
    if events:
        text = f"**Router log:** {failed:.0f} failed logins"
        if events.get("dfs_radar", 0) >= 1:
            text += f", {events['dfs_radar']:.0f} radar channel changes"
        if events.get("out_of_memory", 0) + events.get("crash", 0) >= 1:
            text += ", out-of-memory or crash events (see Router & Mesh)"
        lines.append(text)
    return lines


def send(url: str, title: str, body: str, token: str = "") -> None:
    req = urllib.request.Request(url, data=body.encode(), method="POST")
    req.add_header("Title", title)
    req.add_header("Markdown", "yes")
    req.add_header("Tags", "bar_chart")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def next_run(spec: str, now: datetime) -> datetime:
    """Next time matching "mon 08:00" (weekday names in English, 24 h clock)."""
    day, _, hm = spec.strip().lower().partition(" ")
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    if day[:3] not in days:
        raise ValueError(f"bad schedule {spec!r}: want e.g. 'mon 08:00'")
    hour, _, minute = (hm or "08:00").partition(":")
    at = now.replace(hour=int(hour), minute=int(minute or 0), second=0, microsecond=0)
    at += timedelta(days=(days.index(day[:3]) - now.weekday()) % 7)
    return at if at > now else at + timedelta(days=7)


def run_once(args: argparse.Namespace) -> None:
    query = prom_query(os.environ.get("PROMETHEUS_URL", "http://localhost:9090"))
    days = int(os.environ.get("DIGEST_DAYS", "7"))
    names = os.environ.get("DIGEST_NAMES", "false").lower() in ("1", "true", "yes")
    body = "\n".join(collect(query, days, names))
    title = f"netmonX: last {days} days"
    url = os.environ.get("NTFY_URL", "")
    if args.send and url:
        send(url, title, body, os.environ.get("NTFY_TOKEN", ""))
        print(f"sent to {urllib.parse.urlsplit(url).netloc}", file=sys.stderr)
    else:
        print(f"# {title}\n{body}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--send", action="store_true", help="send to NTFY_URL instead of printing")
    parser.add_argument("--schedule", help='keep running and send every week at this time, e.g. "mon 08:00"')
    args = parser.parse_args()
    if not args.schedule:
        run_once(args)
        return
    args.send = True
    while True:
        at = next_run(args.schedule, datetime.now())
        print(f"next digest {at:%a %Y-%m-%d %H:%M}", file=sys.stderr, flush=True)
        time.sleep(max(1, (at - datetime.now()).total_seconds()))
        try:
            run_once(args)
        except Exception as exc:  # keep the loop alive; try again next week
            print(f"digest failed: {exc}", file=sys.stderr, flush=True)
        time.sleep(61)


if __name__ == "__main__":
    main()
