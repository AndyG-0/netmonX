"""Every shell command the collector is allowed to run on a router.

The SSH layer only accepts `Command` objects, and only this module builds them.
Anything that ends up inside a command (IPs, hostnames, interface names) is
validated first, so config values or router output can't inject extra shell.
Every command here only reads state; nothing writes nvram, files or settings.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

_IFACE_RE = re.compile(r"^[A-Za-z0-9._-]{1,15}$")
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$"
)

NVRAM_KEYS = (
    "productid",
    "model",
    "firmver",
    "buildno",
    "extendno",
    "lan_ipaddr",
    "lan_hwaddr",
    "wan0_ifname",
    "wan0_proto",
    "wan0_state_t",
    "wan0_ipaddr",
    "wan0_gateway",
    "wan0_dns",
    "link_wan",
    "wl_ifnames",
    "wl0_ifname",
    "wl1_ifname",
    "wl2_ifname",
    "wl3_ifname",
    "cfg_device_list",
    "custom_clientlist",
    # DHCP pool, for the "pool almost full" alert.
    "dhcp_start",
    "dhcp_end",
    # Virtual interfaces: guest networks, and the main SSIDs on mesh nodes.
    *(f"wl{u}_vifs" for u in range(4)),
    # SSID names, to label clients in clientlist.json (wlN = main, wlN.M = guest network M).
    *(f"wl{u}_ssid" for u in range(4)),
    *(f"wl{u}.{i}_ssid" for u in range(4) for i in range(1, 6)),
)


@dataclass(frozen=True)
class Command:
    name: str
    text: str


def safe_iface(name: str) -> str:
    if not _IFACE_RE.match(name):
        raise ValueError(f"unsafe interface name: {name!r}")
    return name


def safe_host(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(host):
        raise ValueError(f"unsafe host: {host!r}")
    return host


def fast_snapshot() -> Command:
    """Counters and health values read every fast interval."""
    return Command(
        "fast_snapshot",
        "echo @@net_dev; cat /proc/net/dev; "
        "echo @@loadavg; cat /proc/loadavg; "
        "echo @@meminfo; cat /proc/meminfo; "
        "echo @@uptime; cat /proc/uptime; "
        "echo @@stat; head -n 1 /proc/stat; "
        "echo @@conntrack; cat /proc/sys/net/netfilter/nf_conntrack_count "
        "/proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null; "
        "echo @@wan_state; nvram get wan0_state_t; nvram get link_wan",
    )


def slow_snapshot() -> Command:
    """Config-like values and tables read every slow interval."""
    keys = " ".join(NVRAM_KEYS)
    return Command(
        "slow_snapshot",
        f'echo @@nvram; for k in {keys}; do printf \'%s=%s\\n\' "$k" "$(nvram get $k)"; done; '
        "echo @@thermal; for f in /sys/class/thermal/thermal_zone*/temp; do "
        '[ -r "$f" ] && echo "$f $(cat $f)"; done; '
        "echo @@leases; cat /var/lib/misc/dnsmasq.leases 2>/dev/null; "
        "echo @@arp; cat /proc/net/arp",
    )


def wifi_snapshot(radios: list[str], vifs: list[str] = ()) -> Command:
    """Per interface: SSID and associated clients with signal and rates.

    Radios also report temperature, channel, noise floor and airtime (chanim_stats).
    """
    radio_names = " ".join(safe_iface(i) for i in radios)
    all_names = " ".join(safe_iface(i) for i in [*radios, *vifs])
    return Command(
        "wifi_snapshot",
        f"for i in {radio_names}; do "
        'echo "@@wl_temp $i"; wl -i $i phy_tempsense 2>/dev/null; '
        'echo "@@chanspec $i"; wl -i $i chanspec 2>/dev/null; '
        'echo "@@noise $i"; wl -i $i noise 2>/dev/null; '
        'echo "@@chanim $i"; wl -i $i chanim_stats 2>/dev/null; done; '
        f"for i in {all_names}; do "
        'echo "@@ssid $i"; wl -i $i ssid 2>/dev/null; '
        "for m in $(wl -i $i assoclist 2>/dev/null | awk '{print $2}'); do "
        'echo "@@sta $i $m"; wl -i $i rssi $m 2>/dev/null; wl -i $i sta_info $m 2>/dev/null; '
        "done; done",
    )


def clients_snapshot() -> Command:
    """AiMesh's own client list (main router only): every device, wired or Wi-Fi, by node and band."""
    return Command("clients_snapshot", "cat /tmp/clientlist.json 2>/dev/null")


def ping(host: str, count: int = 5) -> Command:
    count = max(1, min(int(count), 20))
    # -w caps total runtime so a dead WAN can't stall the loop.
    return Command("ping", f"ping -c {count} -w {count + 2} {safe_host(host)} 2>&1")


def traceroute(host: str, max_hops: int = 5) -> Command:
    max_hops = max(1, min(int(max_hops), 15))
    return Command("traceroute", f"traceroute -n -m {max_hops} -w 1 -q 1 {safe_host(host)} 2>&1")
