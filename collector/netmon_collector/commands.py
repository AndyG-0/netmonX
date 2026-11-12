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
    "lan_netmask",
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
    # Firmware check result from the router's own update checker (no request is made by us).
    "webs_state_update",
    "webs_state_info",
    "webs_state_flag",
    # Dual WAN: second WAN and the failover mode.
    "wans_dualwan",
    "wans_mode",
    "wan1_ifname",
    "wan1_proto",
    "wan1_ipaddr",
    "wan1_gateway",
    "wan1_dns",
    # Things reachable from outside: port forwards, admin access from the WAN, UPnP, WPS, VPN servers.
    "vts_enable_x",
    "vts_rulelist",
    "sshd_enable",
    "misc_http_x",
    "upnp_enable",
    "wps_enable",
    "vpn_server1_state",
    "vpn_server2_state",
    "wgs_enable",
    # IPv6 settings.
    "ipv6_service",
    "ipv6_rtr_addr",
    "ipv6_prefix",
    "ipv6_prefix_length",
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
        "echo @@wan_state; nvram get wan0_state_t; nvram get link_wan; nvram get wan1_state_t; nvram get link_wan1",
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


def security_snapshot() -> Command:
    """Main router only: UPnP port mappings and connected VPN clients (status files and `wg show`).

    `wg show all latest-handshakes` and `transfer` print public keys only, never private or preshared keys.
    """
    return Command(
        "security_snapshot",
        "echo @@upnp; cat /tmp/upnp.leases 2>/dev/null; "
        'for n in 1 2; do echo "@@openvpn $n"; cat /etc/openvpn/server$n/status 2>/dev/null; done; '
        "echo @@wg_handshakes; wg show all latest-handshakes 2>/dev/null; "
        "echo @@wg_transfer; wg show all transfer 2>/dev/null",
    )


# nvram keys that change on their own (clocks, leases, link and service state, AiMesh sync, update checks).
# Found by hashing every key twice 15 minutes apart on an XT8, plus the families those keys belong to.
CONFIG_VOLATILE_KEYS = (
    "^("
    "asus_device_list|cfg_device_list|cfg_(ob|re)count|sys_uptime_[a-z]+|rc_service[a-z_]*|"
    "[A-Za-z0-9_.]*_(state|state_t|sbstate_t|auxstate_t|realip_state|realip_ip)|"
    "[A-Za-z0-9_.]*_(time|timestamp|ts|ts_w|expire|expires|expire_tmp|lease|cnt|count)|"
    "[A-Za-z0-9_.]*login[A-Za-z0-9_.]*|ntp_[a-z_]+|webs_[A-Za-z_]+|link_[a-z0-9]+|apps_state_[a-z]+|"
    "(wan|wan[0-9]|ipv6|ipv61|lan|lan1)_(ipaddr|gateway|dns|get_dns|dhcp_lifetime|expires)|ddns_ipaddr|"
    "sysstate_[a-z_]+|wandog_[a-z_]+|fb_[a-z_]+|amas_[a-z0-9_]+|ookla_[a-z_]+"
    ")="
)


def config_hash() -> Command:
    """Main router only: md5 of `nvram show` minus volatile keys, and the key count.

    Hashed on the router, so no nvram value (passwords, Wi-Fi keys) ever leaves it.
    """
    return Command(
        "config_hash",
        f"nvram show 2>/dev/null | grep -Ev '{CONFIG_VOLATILE_KEYS}' | sort | md5sum; "
        f"nvram show 2>/dev/null | grep -cEv '{CONFIG_VOLATILE_KEYS}'",
    )


SYSLOG_PATH = "/tmp/syslog.log"


def syslog_tail(lines: int = 500) -> Command:
    """The end of the router's log; the poller counts only lines it hasn't seen yet."""
    lines = max(1, min(int(lines), 2000))
    return Command("syslog", f"tail -n {lines} {SYSLOG_PATH} 2>/dev/null")


def clients_snapshot() -> Command:
    """AiMesh's own client list (main router only): every device, wired or Wi-Fi, by node and band."""
    return Command("clients_snapshot", "cat /tmp/clientlist.json 2>/dev/null")


def ping(host: str, count: int = 5) -> Command:
    count = max(1, min(int(count), 20))
    # -w caps total runtime so a dead WAN can't stall the loop.
    host = safe_host(host)
    # Busybox `ping` is IPv4-only on stock firmware; `ping6` is a separate applet.
    tool = "ping6" if ":" in host else "ping"
    return Command("ping", f"{tool} -c {count} -w {count + 2} {host} 2>&1")


def traceroute(host: str, max_hops: int = 5, probes: int = 3) -> Command:
    max_hops, probes = max(1, min(int(max_hops), 15)), max(1, min(int(probes), 5))
    return Command("traceroute", f"traceroute -n -m {int(max_hops)} -w 1 -q {int(probes)} {safe_host(host)} 2>&1")
