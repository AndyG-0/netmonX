"""Pure functions that turn router command output into plain data.

Kept free of I/O so they can be tested against captured router output.
Router builds differ, so every parser tolerates missing or odd lines.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field

_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")
_IPV4_RE = re.compile(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b")


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split output that uses `@@name` header lines into (header, body) pairs."""
    sections: list[tuple[str, str]] = []
    header: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        if line.startswith("@@"):
            if header is not None:
                sections.append((header, "\n".join(body)))
            header, body = line[2:].strip(), []
        elif header is not None:
            body.append(line)
    if header is not None:
        sections.append((header, "\n".join(body)))
    return sections


def section_map(text: str) -> dict[str, str]:
    return dict(split_sections(text))


# --- /proc ----------------------------------------------------------------

NET_DEV_FIELDS = (
    "rx_bytes",
    "rx_packets",
    "rx_errs",
    "rx_drop",
    "rx_fifo",
    "rx_frame",
    "rx_compressed",
    "rx_multicast",
    "tx_bytes",
    "tx_packets",
    "tx_errs",
    "tx_drop",
    "tx_fifo",
    "tx_colls",
    "tx_carrier",
    "tx_compressed",
)


def parse_net_dev(text: str) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        if ":" not in line or "|" in line:
            continue
        name, _, rest = line.partition(":")
        values = rest.split()
        if len(values) < len(NET_DEV_FIELDS):
            continue
        try:
            nums = [int(v) for v in values[: len(NET_DEV_FIELDS)]]
        except ValueError:
            continue
        result[name.strip()] = dict(zip(NET_DEV_FIELDS, nums, strict=False))
    return result


def parse_loadavg(text: str) -> tuple[float, float, float] | None:
    parts = text.split()
    try:
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (IndexError, ValueError):
        return None


def parse_meminfo(text: str) -> dict[str, int]:
    """Return memory values in bytes, keyed by the /proc/meminfo name."""
    result: dict[str, int] = {}
    for line in text.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)(?:\s+kB)?", line)
        if m:
            result[m.group(1)] = int(m.group(2)) * 1024
    return result


def mem_available(mem: dict[str, int]) -> int | None:
    if "MemAvailable" in mem:
        return mem["MemAvailable"]
    if "MemFree" in mem:
        return mem["MemFree"] + mem.get("Buffers", 0) + mem.get("Cached", 0)
    return None


def parse_uptime(text: str) -> float | None:
    try:
        return float(text.split()[0])
    except (IndexError, ValueError):
        return None


CPU_MODES = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")


def parse_cpu_stat(text: str, user_hz: int = 100) -> dict[str, float]:
    """Total CPU seconds by mode from the first line of /proc/stat."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            values = parts[1 : 1 + len(CPU_MODES)]
            return {mode: int(v) / user_hz for mode, v in zip(CPU_MODES, values, strict=False) if v.isdigit()}
    return {}


def parse_ints(text: str) -> list[int]:
    return [int(t) for t in text.split() if t.lstrip("-").isdigit()]


# --- ping / traceroute ----------------------------------------------------


@dataclass
class PingResult:
    sent: int
    received: int
    rtts_ms: list[float] = field(default_factory=list)

    @property
    def loss_ratio(self) -> float:
        return 1.0 if self.sent == 0 else max(0, self.sent - self.received) / self.sent

    @property
    def min_ms(self) -> float | None:
        return min(self.rtts_ms) if self.rtts_ms else None

    @property
    def max_ms(self) -> float | None:
        return max(self.rtts_ms) if self.rtts_ms else None

    @property
    def avg_ms(self) -> float | None:
        return sum(self.rtts_ms) / len(self.rtts_ms) if self.rtts_ms else None

    @property
    def jitter_ms(self) -> float | None:
        """Mean difference between consecutive replies (RFC 3550 style, unsmoothed)."""
        if len(self.rtts_ms) < 2:
            return None
        diffs = [abs(b - a) for a, b in zip(self.rtts_ms, self.rtts_ms[1:], strict=False)]
        return sum(diffs) / len(diffs)


def parse_ping(text: str, expected: int) -> PingResult:
    """Parse BusyBox or iputils ping output.

    If the summary line is missing (for example "Network unreachable"), every
    expected packet counts as lost.
    """
    rtts = [float(m) for m in re.findall(r"time[=<]([\d.]+)\s*ms", text)]
    m = re.search(r"(\d+) packets transmitted, (\d+) (?:packets )?received", text)
    if m:
        return PingResult(int(m.group(1)), int(m.group(2)), rtts)
    return PingResult(expected, len(rtts), rtts)


@dataclass
class Hop:
    ttl: int
    ip: str | None
    rtt_ms: float | None


def parse_traceroute(text: str) -> list[Hop]:
    hops: list[Hop] = []
    for line in text.splitlines():
        m = re.match(r"^\s*(\d+)\s+(.*)$", line)
        if not m:
            continue
        ttl, rest = int(m.group(1)), m.group(2)
        ip_m = _IPV4_RE.search(rest)
        rtt_m = re.search(r"([\d.]+)\s*ms", rest)
        hops.append(Hop(ttl, ip_m.group(1) if ip_m else None, float(rtt_m.group(1)) if rtt_m else None))
    return hops


_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_isp_side(ip: str) -> bool:
    """True for public or carrier-grade NAT addresses (both belong to the ISP)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if addr in _CGNAT:
        return True
    return addr.is_global


def pick_isp_hop(wan_gateway: str | None, hops: list[Hop]) -> str | None:
    """The first address on the ISP's side of the router.

    Usually that's the WAN gateway. If the gateway is private (a modem doing
    its own NAT in front of the router), use the first ISP-side traceroute hop.
    """
    if wan_gateway and is_isp_side(wan_gateway):
        return wan_gateway
    for hop in hops:
        if hop.ip and is_isp_side(hop.ip):
            return hop.ip
    return wan_gateway or None


# --- nvram / tables -------------------------------------------------------


def parse_kv(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


@dataclass
class MeshNode:
    model: str
    ip: str
    mac: str


def parse_cfg_device_list(value: str) -> list[MeshNode]:
    """Parse nvram `cfg_device_list`, e.g. `<XT8>192.168.50.1>AA:BB:..>1<XT8>...`."""
    nodes: list[MeshNode] = []
    for entry in value.split("<"):
        fields = entry.split(">")
        if len(fields) < 3:
            continue
        model, ip, mac = fields[0].strip(), fields[1].strip(), fields[2].strip().upper()
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        nodes.append(MeshNode(model, ip, mac))
    return nodes


def parse_wl_ifnames(nvram: dict[str, str]) -> dict[str, str]:
    """Map each Wi-Fi interface to its radio unit (wl0, wl1, ...)."""
    result: dict[str, str] = {}
    for unit in range(4):
        iface = nvram.get(f"wl{unit}_ifname", "").strip()
        if iface:
            result[iface] = f"wl{unit}"
    for iface in nvram.get("wl_ifnames", "").split():
        result.setdefault(iface, "wl?")
    return result


@dataclass
class Lease:
    mac: str
    ip: str
    hostname: str
    expires: int


def parse_leases(text: str) -> list[Lease]:
    leases: list[Lease] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4 or not _MAC_RE.match(parts[1]):
            continue
        host = "" if parts[3] == "*" else parts[3]
        try:
            expires = int(parts[0])
        except ValueError:
            expires = 0
        leases.append(Lease(parts[1].upper(), parts[2], host, expires))
    return leases


@dataclass
class ArpEntry:
    ip: str
    mac: str
    device: str


def parse_proc_arp(text: str) -> list[ArpEntry]:
    """Complete entries from /proc/net/arp (flag 0x2 set)."""
    entries: list[ArpEntry] = []
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            complete = int(parts[2], 16) & 0x2
        except ValueError:
            continue
        if complete and parts[3] != "00:00:00:00:00:00":
            entries.append(ArpEntry(parts[0], parts[3].upper(), parts[5]))
    return entries


def parse_thermal(text: str) -> dict[str, float]:
    """`/sys/class/thermal/thermal_zoneN/temp <millideg>` lines -> {zoneN: celsius}."""
    result: dict[str, float] = {}
    for line in text.splitlines():
        m = re.search(r"(thermal_zone\d+)/temp\s+(-?\d+)", line)
        if m:
            raw = int(m.group(2))
            result[m.group(1)] = raw / 1000 if abs(raw) > 200 else float(raw)
    return result


# --- Wi-Fi (Broadcom `wl`) ------------------------------------------------


@dataclass
class WifiClient:
    iface: str
    mac: str
    rssi_dbm: int | None = None
    tx_rate_mbps: float | None = None
    rx_rate_mbps: float | None = None
    connected_seconds: int | None = None
    tx_bytes: int | None = None
    rx_bytes: int | None = None


def parse_phy_tempsense(text: str) -> float | None:
    m = re.match(r"^\s*(-?\d+)", text)
    return float(m.group(1)) if m else None


def _int_after(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def parse_sta(iface: str, mac: str, body: str) -> WifiClient:
    client = WifiClient(iface=iface, mac=mac.upper())
    lines = body.splitlines()
    # `wl rssi` prints a bare number on its own line, before sta_info output.
    if lines and re.fullmatch(r"\s*-\d+\s*", lines[0]):
        client.rssi_dbm = int(lines[0])
    if client.rssi_dbm is None or client.rssi_dbm == 0:
        client.rssi_dbm = _int_after(r"smoothed rssi:\s*(-\d+)", body)
    if client.rssi_dbm is None:
        m = re.search(r"per antenna average rssi of rx data frames:\s*([-\d\s]+)", body)
        if m:
            vals = [int(v) for v in m.group(1).split() if v.startswith("-")]
            client.rssi_dbm = max(vals) if vals else None
    tx = _int_after(r"rate of last tx pkt:\s*(\d+)\s*kbps", body)
    rx = _int_after(r"rate of last rx pkt:\s*(\d+)\s*kbps", body)
    client.tx_rate_mbps = tx / 1000 if tx is not None else None
    client.rx_rate_mbps = rx / 1000 if rx is not None else None
    client.connected_seconds = _int_after(r"in network\s+(\d+)\s+seconds", body)
    client.tx_bytes = _int_after(r"tx total bytes:\s*(\d+)", body)
    client.rx_bytes = _int_after(r"rx data bytes:\s*(\d+)", body)
    return client


def parse_ssid(text: str) -> str:
    """`wl ssid` prints `Current SSID: "name"`."""
    m = re.search(r'SSID:\s*"(.*)"', text)
    return m.group(1) if m else ""


@dataclass
class WifiSnapshot:
    clients: list[WifiClient] = field(default_factory=list)
    temps: dict[str, float] = field(default_factory=dict)  # radio iface -> celsius
    ssids: dict[str, str] = field(default_factory=dict)  # iface -> SSID ("" if none)


def parse_wifi_snapshot(text: str) -> WifiSnapshot:
    snap = WifiSnapshot()
    for header, body in split_sections(text):
        parts = header.split()
        if parts[0] == "wl_temp" and len(parts) == 2:
            t = parse_phy_tempsense(body)
            if t is not None:
                snap.temps[parts[1]] = t
        elif parts[0] == "ssid" and len(parts) == 2:
            snap.ssids[parts[1]] = parse_ssid(body)
        elif parts[0] == "sta" and len(parts) == 3 and _MAC_RE.match(parts[2]):
            snap.clients.append(parse_sta(parts[1], parts[2], body))
    return snap


def parse_wl_vifs(nvram: dict[str, str]) -> dict[str, str]:
    """Map virtual Wi-Fi interfaces (guest networks, and the main SSIDs on mesh nodes) to their radio unit."""
    result: dict[str, str] = {}
    for unit in range(4):
        for vif in nvram.get(f"wl{unit}_vifs", "").split():
            result[vif] = f"wl{unit}"
    return result


# --- AiMesh client list (/tmp/clientlist.json on the main router) ---------------

# Band keys in clientlist.json, mapped to a label and the radio unit that carries it.
# A suffix like "_1" means guest network 1 on that band (interface wlN.1).
_BANDS = {"2G": ("2.4 GHz", 0), "5G": ("5 GHz", 1), "5G1": ("5 GHz-2", 2), "6G": ("6 GHz", 2), "6G1": ("6 GHz-2", 3)}


@dataclass
class MeshClient:
    node_mac: str  # the AiMesh node the client is attached to
    mac: str
    ip: str
    wired: bool
    band: str = ""  # "2.4 GHz", "5 GHz", ... ("" for wired)
    ssid_key: str = ""  # nvram SSID key for the network, e.g. "wl0" or "wl0.1"
    rssi_dbm: int | None = None


def band_info(key: str) -> tuple[str, str] | None:
    """`5G_1` -> ("5 GHz", "wl1.1"). None for unknown keys."""
    base, _, idx = key.partition("_")
    if base not in _BANDS or (idx and not idx.isdigit()):
        return None
    label, unit = _BANDS[base]
    return label, f"wl{unit}.{idx}" if idx else f"wl{unit}"


def parse_clientlist(text: str) -> list[MeshClient]:
    """Every entry in clientlist.json, as the router reports it (may contain duplicates)."""
    try:
        data = json.loads(text) if text.strip() else {}
    except ValueError:
        return []
    out: list[MeshClient] = []
    if not isinstance(data, dict):
        return out
    for node_mac, bands in data.items():
        if not isinstance(bands, dict) or not _MAC_RE.match(node_mac):
            continue
        for key, clients in bands.items():
            if not isinstance(clients, dict):
                continue
            wired = key == "wired_mac"
            info = None if wired else band_info(key)
            if not wired and info is None:
                continue
            for mac, attrs in clients.items():
                if not _MAC_RE.match(mac):
                    continue
                attrs = attrs if isinstance(attrs, dict) else {}
                rssi = None
                try:
                    rssi = int(attrs.get("rssi", "")) or None
                except (TypeError, ValueError):
                    pass
                out.append(
                    MeshClient(
                        node_mac=node_mac.upper(),
                        mac=mac.upper(),
                        ip=str(attrs.get("ip", "") or ""),
                        wired=wired,
                        band=info[0] if info else "",
                        ssid_key=info[1] if info else "",
                        rssi_dbm=rssi,
                    )
                )
    return out


def dedupe_clients(clients: list[MeshClient], primary_mac: str, skip_macs: set[str]) -> list[MeshClient]:
    """One entry per device.

    The main router also lists devices behind a mesh node as "wired" (their traffic arrives over the
    backhaul), so a Wi-Fi entry beats a wired one and a mesh node beats the main router. AiMesh nodes
    themselves are left out.
    """
    best: dict[str, MeshClient] = {}
    rank = lambda c: (c.wired, c.node_mac == primary_mac)  # noqa: E731  (lower is better)
    for c in clients:
        if c.mac in skip_macs:
            continue
        if c.mac not in best or rank(c) < rank(best[c.mac]):
            best[c.mac] = c
    return list(best.values())


def parse_custom_clientlist(value: str) -> dict[str, str]:
    """nvram `custom_clientlist`: names set in the Asus app, `<name>MAC>...<name>MAC>...`."""
    names: dict[str, str] = {}
    for entry in value.split("<"):
        fields = entry.split(">")
        if len(fields) > 1 and _MAC_RE.match(fields[1].strip()) and fields[0].strip():
            names[fields[1].strip().upper()] = fields[0].strip()
    return names
