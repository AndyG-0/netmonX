"""Polling loops for one router node, and the code that turns parsed output into samples."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass, field

from . import commands, parsers, presence
from .metrics import Sample, Store, sample
from .selfmetrics import SELF
from .ssh import RouterConnection

log = logging.getLogger(__name__)


@dataclass
class SharedState:
    """Facts learned by the main router that other pollers use."""

    hostnames: dict[str, str] = field(default_factory=dict)  # MAC -> name (app name, else DHCP hostname)
    mesh_nodes: list[parsers.MeshNode] = field(default_factory=list)
    # node -> {"wl0.2": "Guest", "wl0": "Home", ...}: SSIDs as each node's own radios report them.
    node_ssids: dict[str, dict[str, str]] = field(default_factory=dict)
    tracker: presence.ClientTracker = field(default_factory=presence.ClientTracker)
    devices: presence.DevicesFile = field(default_factory=lambda: presence.DevicesFile(None))
    oui: presence.OuiTable = field(default_factory=presence.OuiTable)
    # For /sd/* service discovery (read from the HTTP thread; only ever replaced, never mutated).
    isp_hop: str | None = None
    client_ips: dict[str, str] = field(default_factory=dict)  # MAC -> IP of clients in the last poll
    lan_cidr: str = ""  # e.g. 192.168.50.0/24


def ssid_keys(snap: parsers.WifiSnapshot, ifaces: dict[str, str]) -> dict[str, str]:
    """SSIDs by clientlist key: virtual ifaces by name (wl0.2), physical radios by unit (wl0)."""
    out = {}
    for iface, ssid in snap.ssids.items():
        if not ssid:
            continue
        out[iface if iface.startswith("wl") else ifaces.get(iface, iface)] = ssid
    return out


# --- sample builders (pure) -----------------------------------------------


def fast_samples(
    node: str, text: str, wan_iface: str | None, is_primary: bool, dual_wan: bool = False
) -> list[Sample | None]:
    sec = parsers.section_map(text)
    out: list[Sample | None] = []
    for iface, c in parsers.parse_net_dev(sec.get("net_dev", "")).items():
        if iface == "lo":
            continue
        lab = {"node": node, "iface": iface}
        out += [
            sample("asus_net_receive_bytes", c["rx_bytes"], **lab),
            sample("asus_net_transmit_bytes", c["tx_bytes"], **lab),
            sample("asus_net_receive_packets", c["rx_packets"], **lab),
            sample("asus_net_transmit_packets", c["tx_packets"], **lab),
            sample("asus_net_receive_errors", c["rx_errs"], **lab),
            sample("asus_net_transmit_errors", c["tx_errs"], **lab),
            sample("asus_net_receive_drops", c["rx_drop"], **lab),
            sample("asus_net_transmit_drops", c["tx_drop"], **lab),
        ]
        if is_primary and iface == wan_iface:
            out += [
                sample("asus_wan_receive_bytes", c["rx_bytes"], node=node, iface=iface),
                sample("asus_wan_transmit_bytes", c["tx_bytes"], node=node, iface=iface),
            ]
    load = parsers.parse_loadavg(sec.get("loadavg", ""))
    if load:
        out += [
            sample("asus_router_load1", load[0], node=node),
            sample("asus_router_load5", load[1], node=node),
            sample("asus_router_load15", load[2], node=node),
        ]
    mem = parsers.parse_meminfo(sec.get("meminfo", ""))
    out.append(sample("asus_router_memory_total_bytes", mem.get("MemTotal"), node=node))
    out.append(sample("asus_router_memory_available_bytes", parsers.mem_available(mem), node=node))
    out.append(sample("asus_router_uptime_seconds", parsers.parse_uptime(sec.get("uptime", "")), node=node))
    for mode, secs in parsers.parse_cpu_stat(sec.get("stat", "")).items():
        out.append(sample("asus_router_cpu_seconds", secs, node=node, mode=mode))
    ct = parsers.parse_ints(sec.get("conntrack", ""))
    if ct:
        out.append(sample("asus_router_conntrack_entries", ct[0], node=node))
    if len(ct) > 1:
        out.append(sample("asus_router_conntrack_max", ct[1], node=node))
    if is_primary:
        # wan0 state and link, then wan1's (only meaningful with dual WAN on).
        state = sec.get("wan_state", "").splitlines()
        for wan in ("0", "1") if dual_wan else ("0",):
            i = 2 * int(wan)
            st = state[i].strip() if len(state) > i else ""
            link = state[i + 1].strip() if len(state) > i + 1 else ""
            if st:
                out.append(sample("asus_wan_up", 1 if st == "2" else 0, node=node, wan=wan))
            if link:
                out.append(sample("asus_wan_link_up", 1 if link == "1" else 0, node=node, wan=wan))
    return out


def ping_samples(node: str, target: str, role: str, result: parsers.PingResult) -> list[Sample | None]:
    lab = {"node": node, "target": target, "role": role}
    return [
        sample("asus_ping_rtt_min_ms", result.min_ms, **lab),
        sample("asus_ping_rtt_avg_ms", result.avg_ms, **lab),
        sample("asus_ping_rtt_max_ms", result.max_ms, **lab),
        sample("asus_ping_jitter_ms", result.jitter_ms, **lab),
        sample("asus_ping_loss_ratio", result.loss_ratio, **lab),
    ]


# Settings that open the router or LAN to the outside, from nvram: (setting, key, values meaning "on").
EXPOSURE_SETTINGS = (
    ("ssh_wan", "sshd_enable", ("1",)),  # 1 = LAN and WAN, 2 = LAN only
    ("webui_wan", "misc_http_x", ("1",)),
    ("port_forwarding", "vts_enable_x", ("1",)),
    ("upnp", "upnp_enable", ("1",)),
    ("wps", "wps_enable", ("1",)),
    ("openvpn_server1", "vpn_server1_state", ("1", "2")),  # 1 = starting, 2 = running
    ("openvpn_server2", "vpn_server2_state", ("1", "2")),
    ("wireguard_server", "wgs_enable", ("1",)),
)
WG_ACTIVE_SECONDS = 180  # WireGuard re-handshakes every 2 minutes while traffic flows


def security_samples(node: str, nv: dict[str, str], text: str, now: float) -> list[Sample | None]:
    out: list[Sample | None] = []
    for setting, key, on in EXPOSURE_SETTINGS:
        if key in nv and nv[key] != "":
            out.append(sample("asus_exposure_enabled", 1 if nv[key] in on else 0, node=node, setting=setting))
    if "vts_rulelist" in nv:
        rules = parsers.parse_vts_rulelist(nv["vts_rulelist"])
        out.append(sample("asus_port_forwards", len(rules), node=node))
        for f in rules:
            out.append(
                sample(
                    "asus_port_forward_info",
                    1,
                    node=node,
                    rule=f.name,
                    ext_port=f.ext_port,
                    ip=f.ip,
                    int_port=f.int_port,
                    proto=f.proto,
                )
            )
    sec = parsers.section_map(text)
    if "upnp" in sec:
        maps = parsers.parse_upnp_leases(sec["upnp"])
        out.append(sample("asus_upnp_mappings", len(maps), node=node))
        for m in maps:
            out.append(
                sample(
                    "asus_upnp_mapping_info",
                    1,
                    node=node,
                    proto=m.proto,
                    ext_port=m.ext_port,
                    ip=m.ip,
                    int_port=m.int_port,
                    desc=m.desc,
                )
            )
    for n in ("1", "2"):
        if nv.get(f"vpn_server{n}_state", "") not in ("1", "2"):
            continue  # server off: no status file worth reading
        server = f"openvpn{n}"
        clients = parsers.parse_openvpn_status(sec.get(f"openvpn {n}", ""))
        out.append(sample("asus_vpn_clients", len(clients), node=node, server=server))
        for c in clients:
            lab = {
                "node": node,
                "server": server,
                "client": c.name,
                "real_address": c.real_address,
                "virtual_address": c.virtual_address,
            }
            out += [
                sample("asus_vpn_client_connected_since_timestamp_seconds", c.since, **lab),
                sample("asus_vpn_client_receive_bytes", c.rx_bytes, **lab),
                sample("asus_vpn_client_transmit_bytes", c.tx_bytes, **lab),
            ]
    peers = parsers.parse_wg(sec.get("wg_handshakes", ""), sec.get("wg_transfer", ""))
    if peers or nv.get("wgs_enable") == "1":
        active = [p for p in peers if p.handshake and now - p.handshake < WG_ACTIVE_SECONDS]
        out.append(sample("asus_vpn_clients", len(active), node=node, server="wireguard"))
        for p in peers:
            lab = {"node": node, "server": "wireguard", "iface": p.iface, "peer": p.peer[:8]}
            out += [
                sample("asus_wireguard_peer_last_handshake_timestamp_seconds", p.handshake or None, **lab),
                sample("asus_vpn_client_receive_bytes", p.rx_bytes, **lab),
                sample("asus_vpn_client_transmit_bytes", p.tx_bytes, **lab),
            ]
    return out


def lan_cidr(ip: str, netmask: str) -> str:
    try:
        return str(ipaddress.ip_network(f"{ip}/{netmask or '255.255.255.0'}", strict=False))
    except ValueError:
        return ""


# Wi-Fi score: link rate a good 2-stream Wi-Fi 6 client reaches at each channel width (Mbps).
SCORE_RATE_REF = {20: 287, 40: 574, 80: 1201, 160: 2402, 320: 4804}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def wifi_score(
    rssi: int | None,
    tx_rate: float | None,
    width_mhz: int | None,
    tx_pkts: int | None,
    tx_retries: int | None,
) -> int | None:
    """0-100: 50% signal (-90 dBm = 0, -50 dBm = 1), 30% link rate vs. what the channel width allows,
    20% retries (none = 1, half of all frames retried = 0). Missing parts are left out and the
    rest reweighted. Retries count since the client associated."""
    parts: list[tuple[float, float]] = []
    if rssi is not None and rssi < 0:
        parts.append((0.5, _clamp01((rssi + 90) / 40)))
    if tx_rate and width_mhz in SCORE_RATE_REF:
        parts.append((0.3, _clamp01(tx_rate / SCORE_RATE_REF[width_mhz])))
    if tx_pkts is not None and tx_retries is not None and tx_pkts + tx_retries > 0:
        parts.append((0.2, _clamp01(1 - 2 * tx_retries / (tx_pkts + tx_retries))))
    if not parts or parts[0][0] != 0.5:
        return None  # no signal reading: not enough to go on
    return round(100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts))


def snr(rssi: float | None, noise: float | None) -> float | None:
    if rssi is None or noise is None or rssi >= 0:
        return None
    return rssi - noise


class ChannelTracker:
    """Counts channel/width changes per radio between polls (DFS radar, auto channel, manual)."""

    def __init__(self) -> None:
        self.last: dict[str, str] = {}
        self.changes: dict[str, int] = {}

    def update(self, iface: str, chanspec: str) -> int:
        prev = self.last.get(iface)
        if prev is not None and prev != chanspec:
            self.changes[iface] = self.changes.get(iface, 0) + 1
            log.info("%s changed channel %s -> %s", iface, prev, chanspec)
        self.last[iface] = chanspec
        return self.changes.setdefault(iface, 0)


def wifi_samples(
    node: str,
    text: str,
    bands: dict[str, str],
    hostnames: dict[str, str],
    channels: ChannelTracker | None = None,
) -> list[Sample | None]:
    """`bands` maps every polled interface (radios and virtual SSIDs) to its radio unit."""
    snap = parsers.parse_wifi_snapshot(text)
    out: list[Sample | None] = []
    # Noise floor per radio unit, so clients on a guest SSID (wl0.1) get their radio's (wl0) noise.
    unit_noise = {bands.get(iface, "wl?"): r.noise_dbm for iface, r in snap.radios.items() if r.noise_dbm is not None}
    for iface, r in snap.radios.items():
        lab = {"node": node, "iface": iface, "radio": bands.get(iface, "wl?")}
        cs = r.chanspec
        if cs:
            out += [
                sample("asus_wifi_radio_info", 1, **lab, band=cs.band, chanspec=cs.raw, dfs="1" if cs.dfs else "0"),
                sample("asus_wifi_channel", cs.channel, **lab),
                sample("asus_wifi_channel_width_mhz", cs.width_mhz, **lab),
            ]
            if channels is not None:
                out.append(sample("asus_wifi_channel_changes", channels.update(iface, cs.raw), **lab))
        out.append(sample("asus_wifi_noise_dbm", r.noise_dbm, **lab))
        for stat, pct in r.chanim.items():
            out.append(sample("asus_wifi_airtime_percent", pct, **lab, stat=stat))
    unit_width = {
        bands.get(iface, "wl?"): r.chanspec.width_mhz for iface, r in snap.radios.items() if r.chanspec is not None
    }
    counts: dict[str, int] = {iface: 0 for iface in bands}
    for c in snap.clients:
        counts[c.iface] = counts.get(c.iface, 0) + 1
        lab = {
            "node": node,
            "iface": c.iface,
            "radio": bands.get(c.iface, "wl?"),
            "ssid": snap.ssids.get(c.iface, ""),
            "mac": c.mac,
            "hostname": hostnames.get(c.mac, ""),
        }
        out += [
            sample("asus_wifi_client_rssi_dbm", c.rssi_dbm, **lab),
            sample("asus_wifi_client_snr_db", snr(c.rssi_dbm, unit_noise.get(lab["radio"])), **lab),
            sample("asus_wifi_client_tx_rate_mbps", c.tx_rate_mbps, **lab),
            sample("asus_wifi_client_rx_rate_mbps", c.rx_rate_mbps, **lab),
            sample("asus_wifi_client_connected_seconds", c.connected_seconds, **lab),
            sample("asus_wifi_client_transmit_bytes", c.tx_bytes, **lab),
            sample("asus_wifi_client_receive_bytes", c.rx_bytes, **lab),
            sample("asus_wifi_client_tx_packets", c.tx_pkts, **lab),
            sample("asus_wifi_client_tx_retries", c.tx_retries, **lab),
            sample("asus_wifi_client_tx_failures", c.tx_failed, **lab),
            sample(
                "asus_wifi_client_score",
                wifi_score(c.rssi_dbm, c.tx_rate_mbps, unit_width.get(lab["radio"]), c.tx_pkts, c.tx_retries),
                **lab,
            ),
        ]
    for iface, n in counts.items():
        ssid = snap.ssids.get(iface, "")
        if not ssid and not n:
            continue  # unused interface, e.g. a mesh node's backhaul radio
        out.append(sample("asus_wifi_clients", n, node=node, iface=iface, radio=bands.get(iface, "wl?"), ssid=ssid))
    for iface, t in snap.temps.items():
        out.append(sample("asus_router_temperature_celsius", t, node=node, sensor=f"radio_{iface}"))
    return out


def client_samples(
    clients: list[parsers.MeshClient],
    nodes: dict[str, str],
    node_names: dict[str, str],
    ssids: dict[str, str],
    names: dict[str, str],
    node_ssids: dict[str, dict[str, str]] | None = None,
    meta: dict[str, presence.DeviceMeta] | None = None,
    vendor=None,
) -> list[Sample | None]:
    """Samples for the deduplicated AiMesh client list.

    nodes: node MAC -> node label (the IP used as `node` elsewhere); node_names: node label -> display name;
    ssids: nvram SSID key (wl0, wl1.1, ...) -> SSID on the main router; names: device MAC -> name.
    node_ssids: per node label, the SSIDs its own radios report. They win, because a band key like
    "2G_2" means interface wl0.2 on the node the client is attached to, and nodes number guest
    networks differently from the main router.
    meta: devices.yml entries (owner, type labels); vendor: MAC -> vendor name.
    """
    out: list[Sample | None] = []
    counts: dict[tuple[str, str, str, str], int] = {}
    for c in clients:
        node = nodes.get(c.node_mac, c.node_mac)
        conn = "wired" if c.wired else "wifi"
        ssid = ""
        if not c.wired:
            ssid = (node_ssids or {}).get(node, {}).get(c.ssid_key) or ssids.get(c.ssid_key, "")
        key = (node, conn, c.band, ssid)
        counts[key] = counts.get(key, 0) + 1
        lab = {
            "node": node,
            "node_name": node_names.get(node, node),
            "mac": c.mac,
            "hostname": names.get(c.mac, ""),
            "connection": conn,
            "band": c.band,
            "ssid": ssid,
        }
        m = (meta or {}).get(c.mac) or presence.DeviceMeta()
        extra = {"vendor": vendor(c.mac) if vendor else "", "owner": m.owner, "type": m.type}
        out.append(sample("asus_client_info", 1, ip=c.ip, **lab, **extra))
        out.append(sample("asus_client_rssi_dbm", c.rssi_dbm, **lab))
    for (node, conn, band, ssid), n in counts.items():
        out.append(
            sample(
                "asus_clients",
                n,
                node=node,
                node_name=node_names.get(node, node),
                connection=conn,
                band=band,
                ssid=ssid,
            )
        )
    return out


def presence_samples(
    tracker: presence.ClientTracker,
    meta: dict[str, presence.DeviceMeta],
    now: float,
    new_window: float = 3600,
) -> list[Sample | None]:
    """Online state, first/last seen, new devices and roams for every device seen in the retention window."""
    out: list[Sample | None] = []
    for mac, seen in tracker.devices.items():
        m = meta.get(mac) or presence.DeviceMeta()
        lab = {"mac": mac, "hostname": m.name or seen.name}
        out.append(sample("asus_client_online", 1 if mac in tracker.online else 0, **lab, owner=m.owner, type=m.type))
        out.append(sample("asus_client_last_seen_timestamp_seconds", seen.last_seen, **lab))
        out.append(sample("asus_client_first_seen_timestamp_seconds", seen.first_seen, **lab))
        out.append(sample("asus_client_roams", seen.roams, **lab))
        if any(b.startswith(("5", "6")) for b in seen.bands):
            out.append(sample("asus_client_5ghz_capable", 1, **lab))
        if seen.new and now - seen.first_seen < new_window:
            out.append(sample("asus_client_new", 1, **lab))
        if seen.last_roam:
            r = seen.last_roam
            out.append(
                sample(
                    "asus_client_last_roam_timestamp_seconds",
                    r["ts"],
                    **lab,
                    from_node=r["from_node"],
                    to_node=r["to_node"],
                    from_band=r["from_band"],
                    to_band=r["to_band"],
                )
            )
    for mac, m in meta.items():
        if m.alert_offline_hours:
            out.append(sample("asus_device_offline_alert_hours", m.alert_offline_hours, mac=mac, hostname=m.name))
    return out


def node_labels(primary: str, primary_mac: str, mesh: list[parsers.MeshNode], names: dict[str, str]):
    """(node MAC -> node label, node label -> display name). Names set in the Asus app win if they're unique."""
    nodes = {primary_mac: primary, **{n.mac: n.ip for n in mesh}}
    custom = {label: names.get(mac, "") for mac, label in nodes.items()}
    taken = [v for v in custom.values() if v]
    display = {}
    for label, name in custom.items():
        if name and taken.count(name) == 1:
            display[label] = name
        else:
            display[label] = f"main {label}" if label == primary else f"node {label}"
    return nodes, display


# Store groups the slow loop fills; all are dropped if a slow poll fails.
SLOW_GROUPS = ["slow", "wifi", "clients", "presence", "security", "config"]

# Sections every router returns; if one comes back empty or unparsable, it counts as a parse failure.
FAST_REQUIRED = {
    "net_dev": parsers.parse_net_dev,
    "loadavg": parsers.parse_loadavg,
    "meminfo": parsers.parse_meminfo,
    "uptime": parsers.parse_uptime,
}
SLOW_REQUIRED = {"nvram": parsers.parse_kv}


def unparsed_sections(text: str, required: dict) -> list[str]:
    sec = parsers.section_map(text)
    return [name for name, parse in required.items() if not parse(sec.get(name, ""))]


# --- poller ---------------------------------------------------------------


class NodePoller:
    """Polls one node. The primary node also measures the WAN and ISP."""

    def __init__(
        self,
        node: str,
        conn: RouterConnection,
        store: Store,
        shared: SharedState,
        *,
        is_primary: bool,
        ping_targets: list[str],
        ping_count: int,
        isp_hop_setting: str,
        fast_interval: float,
        slow_interval: float,
        ping6_targets: list[str] = (),
    ) -> None:
        self.node = node
        self.conn = conn
        self.store = store
        self.shared = shared
        self.is_primary = is_primary
        self.ping_targets = ping_targets
        self.ping6_targets = list(ping6_targets)  # pinged only when the router has IPv6 on
        self.ping_count = ping_count
        self.isp_hop_setting = isp_hop_setting
        self.fast_interval = fast_interval
        self.slow_interval = slow_interval
        self.wan_iface: str | None = None
        self.wifi_bands: dict[str, str] = {}  # radio iface -> unit
        self.wifi_vifs: dict[str, str] = {}  # virtual iface -> unit
        self.isp_hop: str | None = None if isp_hop_setting in ("auto", "none") else isp_hop_setting
        self._last_wan_ip: str | None = None
        self._ip_changes = 0
        self.channels = ChannelTracker()
        self._config_hash: str | None = None
        self._config_changes = 0
        self._config_changed_at: float | None = None
        self.dual_wan = False
        self.ipv6 = False
        self._errors: dict[str, int] = {}
        self._ping_totals: dict[str, list[int]] = {}  # target -> [sent, received]
        self._hop_totals: dict[int, list[int]] = {}  # traceroute hop -> [probes sent, answered]
        self._ok: dict[str, bool] = {}
        self.metrics = SELF

    def _count_unparsed(self, text: str, required: dict) -> None:
        for name in unparsed_sections(text, required):
            self.metrics.parse_failures.labels(self.node, name).inc()

    # Each loop reports its own health; the node is up only if every loop is.
    def _mark(self, loop: str, ok: bool) -> None:
        self._ok[loop] = ok
        samples = [sample("asus_up", 1 if all(self._ok.values()) else 0, node=self.node)]
        for name, count in self._errors.items():
            samples.append(sample("asus_poll_errors", count, node=self.node, loop=name))
        self.store.set(self.node, "health", samples)
        if ok:
            self.store.set(
                self.node,
                f"ts_{loop}",
                [sample("asus_last_poll_timestamp_seconds", time.time(), node=self.node, loop=loop)],
            )

    async def _guard(self, loop: str, fn, groups: list[str]) -> None:
        try:
            await fn()
            self._mark(loop, True)
        except Exception as exc:  # keep polling whatever goes wrong
            self._errors[loop] = self._errors.get(loop, 0) + 1
            log.warning("%s %s poll failed: %s", self.node, loop, exc or type(exc).__name__)
            for g in groups:
                self.store.drop(self.node, g)
            self._mark(loop, False)

    async def poll_slow(self) -> None:
        text = await self.conn.run(commands.slow_snapshot())
        self._count_unparsed(text, SLOW_REQUIRED)
        sec = parsers.section_map(text)
        nv = parsers.parse_kv(sec.get("nvram", ""))
        self.wan_iface = nv.get("wan0_ifname") or self.wan_iface
        self.wifi_bands = parsers.parse_wl_ifnames(nv)
        self.wifi_vifs = parsers.parse_wl_vifs(nv)
        fw = f"{nv.get('firmver', '')}.{nv.get('buildno', '')}_{nv.get('extendno', '')}".strip("._")
        out: list[Sample | None] = [
            sample(
                "asus_router_info",
                1,
                node=self.node,
                model=nv.get("productid") or nv.get("model", ""),
                firmware=fw,
                mac=nv.get("lan_hwaddr", "").upper(),
                role="primary" if self.is_primary else "mesh",
            )
        ]
        for zone, temp in parsers.parse_thermal(sec.get("thermal", "")).items():
            out.append(sample("asus_router_temperature_celsius", temp, node=self.node, sensor=zone))
        flag = nv.get("webs_state_flag", "").strip()
        if flag.isdigit():
            # 1 = new firmware, 2 = new firmware the router marks as important; 0 = up to date.
            latest = parsers.firmware_version(nv.get("webs_state_info", ""))
            out.append(
                sample("asus_firmware_update_available", 1 if flag in ("1", "2") else 0, node=self.node, latest=latest)
            )

        if self.is_primary:
            out += await self._slow_primary(nv, sec)

        self.store.set(self.node, "slow", out)
        if self.is_primary:
            await self._clients(nv)
            security = await self.conn.run(commands.security_snapshot())
            self.store.set(self.node, "security", security_samples(self.node, nv, security, time.time()))
            self.store.set(self.node, "config", self._config(await self.conn.run(commands.config_hash())))
        if self.wifi_bands or self.wifi_vifs:
            wifi = await self.conn.run(commands.wifi_snapshot(list(self.wifi_bands), list(self.wifi_vifs)))
            ifaces = {**self.wifi_bands, **self.wifi_vifs}
            self.store.set(
                self.node, "wifi", wifi_samples(self.node, wifi, ifaces, self.shared.hostnames, self.channels)
            )
            self.shared.node_ssids[self.node] = ssid_keys(parsers.parse_wifi_snapshot(wifi), ifaces)

    def _config(self, text: str) -> list[Sample | None]:
        """Config drift: a hash of the router's settings; a change counts once per new hash."""
        parsed = parsers.parse_config_hash(text)
        if parsed is None:
            if text.strip():
                self.metrics.parse_failures.labels(self.node, "config_hash").inc()
            return []
        digest, keys = parsed
        if self._config_hash is not None and digest != self._config_hash:
            self._config_changes += 1
            self._config_changed_at = time.time()
            log.info("router settings changed (config hash %s -> %s)", self._config_hash[:8], digest[:8])
        self._config_hash = digest
        return [
            sample("asus_config_hash_info", 1, node=self.node, hash=digest[:12]),
            sample("asus_config_keys", keys, node=self.node),
            sample("asus_config_changes", self._config_changes, node=self.node),
            sample("asus_config_last_change_timestamp_seconds", self._config_changed_at, node=self.node),
        ]

    async def _clients(self, nv: dict[str, str]) -> None:
        """Every device on the mesh, wired or Wi-Fi, from the main router's AiMesh client list."""
        app_names = parsers.parse_custom_clientlist(nv.get("custom_clientlist", ""))
        meta = self.shared.devices.get()
        own_names = {mac: m.name for mac, m in meta.items() if m.name}
        # devices.yml beats names set in the Asus app, which beat DHCP hostnames.
        self.shared.hostnames = {**self.shared.hostnames, **app_names, **own_names}
        primary_mac = nv.get("lan_hwaddr", "").upper()
        nodes, display = node_labels(self.node, primary_mac, self.shared.mesh_nodes, app_names)
        raw_text = await self.conn.run(commands.clients_snapshot())
        raw = parsers.parse_clientlist(raw_text)
        if raw_text.strip() and not raw:
            self.metrics.parse_failures.labels(self.node, "clientlist").inc()
        clients = parsers.dedupe_clients(raw, primary_mac, set(nodes))
        self.shared.client_ips = {c.mac: c.ip for c in clients if c.ip}
        self.shared.lan_cidr = lan_cidr(nv.get("lan_ipaddr", ""), nv.get("lan_netmask", ""))
        ssids = {k[: -len("_ssid")]: v for k, v in nv.items() if k.endswith("_ssid") and k.startswith("wl")}
        names = self.shared.hostnames
        self.store.set(
            self.node,
            "clients",
            client_samples(clients, nodes, display, ssids, names, self.shared.node_ssids, meta, self.shared.oui.vendor),
        )
        # Presence and roaming. Use display names so "from"/"to" read like the rest of the dashboards.
        tracker = self.shared.tracker
        now = time.time()
        attached = {c.mac: (display.get(nodes.get(c.node_mac, c.node_mac), c.node_mac), c.band) for c in clients}
        for roam in tracker.update(attached, now):
            log.info("%s roamed %s %s -> %s %s", roam.mac, roam.from_node, roam.from_band, roam.to_node, roam.to_band)
        for mac in attached:
            if names.get(mac):
                tracker.devices[mac].name = names[mac]
        tracker.save()
        self.store.set(self.node, "presence", presence_samples(tracker, meta, now))

    async def _slow_primary(self, nv: dict[str, str], sec: dict[str, str]) -> list[Sample | None]:
        out: list[Sample | None] = []
        wan_ip = nv.get("wan0_ipaddr", "")
        if self._last_wan_ip is not None and wan_ip and wan_ip != self._last_wan_ip:
            self._ip_changes += 1
            log.info("WAN IP changed from %s to %s", self._last_wan_ip, wan_ip)
        if wan_ip:
            self._last_wan_ip = wan_ip
        out.append(sample("asus_wan_ip_changes", self._ip_changes, node=self.node))
        wans = parsers.parse_dualwan(nv.get("wans_dualwan", ""))
        self.dual_wan = len(wans) > 1
        for wan in ("0", "1") if self.dual_wan else ("0",):
            out.append(
                sample(
                    "asus_wan_info",
                    1,
                    node=self.node,
                    wan=wan,
                    wan_type=wans[int(wan)] if len(wans) > int(wan) else "wan",
                    iface=nv.get(f"wan{wan}_ifname", ""),
                    proto=nv.get(f"wan{wan}_proto", ""),
                    ip=nv.get(f"wan{wan}_ipaddr", ""),
                    gateway=nv.get(f"wan{wan}_gateway", ""),
                    dns=nv.get(f"wan{wan}_dns", ""),
                )
            )
        if self.dual_wan:
            mode = {"fo": "failover", "fb": "failback", "lb": "load balance"}.get(nv.get("wans_mode", ""), "")
            out.append(sample("asus_dualwan_info", 1, node=self.node, wans=" ".join(wans), mode=mode))

        service = nv.get("ipv6_service", "").strip()
        self.ipv6 = service not in ("", "disabled")
        if service:
            out.append(
                sample(
                    "asus_ipv6_info",
                    1,
                    node=self.node,
                    service=service,
                    prefix=nv.get("ipv6_prefix", ""),
                    prefix_length=nv.get("ipv6_prefix_length", "") if self.ipv6 else "",
                    router_addr=nv.get("ipv6_rtr_addr", ""),
                )
            )
            out.append(sample("asus_ipv6_enabled", 1 if self.ipv6 else 0, node=self.node))

        leases = parsers.parse_leases(sec.get("leases", ""))
        self.shared.hostnames = {lease.mac: lease.hostname for lease in leases if lease.hostname}
        out.append(sample("asus_dhcp_leases", len(leases), node=self.node))
        pool = parsers.dhcp_pool_size(nv.get("dhcp_start", ""), nv.get("dhcp_end", ""))
        if pool:
            out.append(sample("asus_dhcp_pool_size", pool, node=self.node))
        for lease in leases:
            out.append(
                sample("asus_dhcp_lease_info", 1, node=self.node, mac=lease.mac, ip=lease.ip, hostname=lease.hostname)
            )

        arp_counts: dict[str, int] = {}
        for e in parsers.parse_proc_arp(sec.get("arp", "")):
            arp_counts[e.device] = arp_counts.get(e.device, 0) + 1
        for dev, n in arp_counts.items():
            out.append(sample("asus_arp_entries", n, node=self.node, iface=dev))

        self.shared.mesh_nodes = [
            n for n in parsers.parse_cfg_device_list(nv.get("cfg_device_list", "")) if n.ip != nv.get("lan_ipaddr")
        ]
        for n in self.shared.mesh_nodes:
            out.append(sample("asus_mesh_node_info", 1, node=self.node, ip=n.ip, mac=n.mac, model=n.model))

        # Traceroute to the first ping target shows the path and finds the ISP hop.
        hops: list[parsers.Hop] = []
        if self.ping_targets:
            hops = parsers.parse_traceroute(await self.conn.run(commands.traceroute(self.ping_targets[0])))
            for h in hops:
                lab = {"node": self.node, "hop": str(h.ttl)}
                totals = self._hop_totals.setdefault(h.ttl, [0, 0])
                totals[0] += h.sent
                totals[1] += len(h.rtts)
                out += [
                    sample("asus_traceroute_hop_rtt_ms", h.rtt_ms, **lab, ip=h.ip or "*"),
                    sample("asus_traceroute_hop_loss_ratio", h.loss_ratio, **lab, ip=h.ip or "*"),
                    sample("asus_traceroute_hop_probes_sent", totals[0], **lab),
                    sample("asus_traceroute_hop_probes_received", totals[1], **lab),
                ]
        if self.isp_hop_setting == "auto":
            self.isp_hop = parsers.pick_isp_hop(nv.get("wan0_gateway"), hops)
        self.shared.isp_hop = self.isp_hop
        if self.isp_hop:
            out.append(sample("asus_isp_hop_info", 1, node=self.node, ip=self.isp_hop))
        return out

    async def poll_fast(self) -> None:
        tasks = [self.conn.run(commands.fast_snapshot())]
        targets: list[tuple[str, str]] = []
        if self.is_primary:
            if self.isp_hop:
                targets.append((self.isp_hop, "isp_gateway"))
            targets += [(t, "internet") for t in self.ping_targets if t != self.isp_hop]
            if self.ipv6:
                targets += [(t, "internet_v6") for t in self.ping6_targets]
            tasks += [self.conn.run(commands.ping(t, self.ping_count)) for t, _ in targets]
        results = await asyncio.gather(*tasks)
        self._count_unparsed(results[0], FAST_REQUIRED)
        self.store.set(
            self.node, "fast", fast_samples(self.node, results[0], self.wan_iface, self.is_primary, self.dual_wan)
        )

        ping_out: list[Sample | None] = []
        for (target, role), text in zip(targets, results[1:], strict=False):
            res = parsers.parse_ping(text, self.ping_count)
            totals = self._ping_totals.setdefault(target, [0, 0])
            totals[0] += res.sent
            totals[1] += res.received
            ping_out += ping_samples(self.node, target, role, res)
            lab = {"node": self.node, "target": target, "role": role}
            ping_out.append(sample("asus_ping_packets_sent", totals[0], **lab))
            ping_out.append(sample("asus_ping_packets_received", totals[1], **lab))
        if self.is_primary:
            self.store.set(self.node, "ping", ping_out)

    async def run(self) -> None:
        # Slow poll first: it learns the WAN interface and ISP hop the fast poll needs.
        await self._guard("slow", self.poll_slow, SLOW_GROUPS)
        # If that first try failed, retry soon instead of waiting a full slow interval.
        first_wait = self.slow_interval if self._ok.get("slow") else min(30.0, self.slow_interval)
        await asyncio.gather(
            self._every(self.fast_interval, "fast", self.poll_fast, ["fast", "ping"]),
            self._every(
                self.slow_interval,
                "slow",
                self.poll_slow,
                SLOW_GROUPS,
                first_wait=first_wait,
            ),
        )

    async def _every(self, interval: float, loop: str, fn, groups: list[str], first_wait: float = 0) -> None:
        loop_clock = asyncio.get_running_loop()
        await asyncio.sleep(first_wait)
        while True:
            start = loop_clock.time()
            await self._guard(loop, fn, groups)
            await asyncio.sleep(max(1.0, interval - (loop_clock.time() - start)))
