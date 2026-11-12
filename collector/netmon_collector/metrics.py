"""Metric definitions and the in-memory store that Prometheus reads from.

Each poll replaces one "group" of samples (for example node X's fast
snapshot). If a poll fails, that group is dropped, so Prometheus never sees
stale values that look current.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from importlib import metadata

from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from prometheus_client.registry import Collector

# name -> (type, help). Counter names leave off `_total`; the client adds it.
METRICS: dict[str, tuple[str, str]] = {
    "asus_up": ("gauge", "1 if the last SSH poll of this node worked."),
    "asus_last_poll_timestamp_seconds": ("gauge", "Unix time of the last successful poll, per loop."),
    "asus_poll_errors": ("counter", "Failed polls, per loop."),
    "asus_router_info": ("gauge", "Router model and firmware (value is always 1)."),
    "asus_router_uptime_seconds": ("gauge", "Router uptime."),
    "asus_router_load1": ("gauge", "1-minute load average."),
    "asus_router_load5": ("gauge", "5-minute load average."),
    "asus_router_load15": ("gauge", "15-minute load average."),
    "asus_router_cpu_seconds": ("counter", "CPU time spent in each mode, all cores."),
    "asus_router_memory_total_bytes": ("gauge", "Total RAM."),
    "asus_router_memory_available_bytes": ("gauge", "RAM available to programs."),
    "asus_router_conntrack_entries": ("gauge", "Tracked connections (NAT table size)."),
    "asus_router_conntrack_max": ("gauge", "Connection table limit."),
    "asus_router_temperature_celsius": ("gauge", "Temperature sensors (CPU zones and Wi-Fi radios)."),
    "asus_net_receive_bytes": ("counter", "Bytes received per interface."),
    "asus_net_transmit_bytes": ("counter", "Bytes sent per interface."),
    "asus_net_receive_packets": ("counter", "Packets received per interface."),
    "asus_net_transmit_packets": ("counter", "Packets sent per interface."),
    "asus_net_receive_errors": ("counter", "Receive errors per interface."),
    "asus_net_transmit_errors": ("counter", "Transmit errors per interface."),
    "asus_net_receive_drops": ("counter", "Dropped incoming packets per interface."),
    "asus_net_transmit_drops": ("counter", "Dropped outgoing packets per interface."),
    "asus_wan_up": ("gauge", "1 if the router reports the WAN as connected (wan0_state_t == 2)."),
    "asus_wan_link_up": ("gauge", "1 if the WAN port has a cable link."),
    "asus_wan_info": ("gauge", "WAN addressing (value is always 1)."),
    "asus_wan_receive_bytes": ("counter", "Bytes received on the WAN interface."),
    "asus_wan_transmit_bytes": ("counter", "Bytes sent on the WAN interface."),
    "asus_dualwan_info": ("gauge", "Dual WAN is on: which WAN types and the mode (value is always 1)."),
    "asus_ipv6_info": ("gauge", "IPv6 service type, prefix and router address (value is always 1)."),
    "asus_ipv6_enabled": ("gauge", "1 if IPv6 is turned on in the router."),
    "asus_firmware_update_available": ("gauge", "1 if the router's own update check found newer firmware."),
    "asus_wan_ip_changes": ("counter", "Times the WAN IP changed since the collector started."),
    "asus_ping_rtt_min_ms": ("gauge", "Lowest round trip in the last ping burst from the router."),
    "asus_ping_rtt_avg_ms": ("gauge", "Average round trip in the last ping burst from the router."),
    "asus_ping_rtt_max_ms": ("gauge", "Highest round trip in the last ping burst from the router."),
    "asus_ping_jitter_ms": ("gauge", "Average change between consecutive round trips."),
    "asus_ping_loss_ratio": ("gauge", "Share of pings lost in the last burst (0-1)."),
    "asus_ping_packets_sent": ("counter", "Pings sent from the router."),
    "asus_ping_packets_received": ("counter", "Ping replies received by the router."),
    "asus_traceroute_hop_rtt_ms": ("gauge", "Round trip to each hop of the latest traceroute."),
    "asus_isp_hop_info": ("gauge", "Address used as the ISP's first hop (value is always 1)."),
    "asus_dhcp_leases": ("gauge", "Active DHCP leases."),
    "asus_dhcp_pool_size": ("gauge", "Addresses in the DHCP pool (dhcp_start to dhcp_end)."),
    "asus_dhcp_lease_info": ("gauge", "One series per DHCP lease (value is always 1)."),
    "asus_arp_entries": ("gauge", "Devices with a complete ARP entry, per interface."),
    "asus_mesh_node_info": ("gauge", "AiMesh nodes known to the main router (value is always 1)."),
    "asus_wifi_clients": ("gauge", "Associated Wi-Fi clients per interface (radio or virtual SSID)."),
    "asus_clients": ("gauge", "Devices by mesh node, connection, band and SSID (AiMesh client list)."),
    "asus_client_info": ("gauge", "One series per device in the AiMesh client list (value is always 1)."),
    "asus_client_online": ("gauge", "1 if the device is in the latest AiMesh client list, 0 if it was seen recently."),
    "asus_client_last_seen_timestamp_seconds": ("gauge", "Unix time the device was last in the client list."),
    "asus_client_first_seen_timestamp_seconds": ("gauge", "Unix time the collector first saw the device."),
    "asus_client_new": ("gauge", "1 for an hour after a device shows up for the first time."),
    "asus_client_roams": ("counter", "Times the device moved to another mesh node or band while online."),
    "asus_client_last_roam_timestamp_seconds": ("gauge", "Unix time of the device's latest roam, labelled from/to."),
    "asus_device_offline_alert_hours": ("gauge", "Offline alert threshold from devices.yml, per device."),
    "asus_client_rssi_dbm": ("gauge", "Device signal strength as seen by its mesh node (AiMesh client list)."),
    "asus_wifi_client_rssi_dbm": ("gauge", "Client signal strength."),
    "asus_wifi_client_snr_db": ("gauge", "Client signal minus its radio's noise floor."),
    "asus_wifi_radio_info": ("gauge", "Radio band, chanspec and whether the channel is DFS (value is always 1)."),
    "asus_wifi_channel": ("gauge", "Primary channel of the radio."),
    "asus_wifi_channel_width_mhz": ("gauge", "Channel width of the radio."),
    "asus_wifi_channel_changes": ("counter", "Channel or width changes seen since the collector started."),
    "asus_wifi_noise_dbm": ("gauge", "Noise floor of the radio."),
    "asus_wifi_airtime_percent": ("gauge", "Share of airtime per state from `wl chanim_stats` (tx, obss, idle, ...)."),
    "asus_wifi_client_tx_rate_mbps": ("gauge", "Link rate of the last frame sent to the client."),
    "asus_wifi_client_rx_rate_mbps": ("gauge", "Link rate of the last frame received from the client."),
    "asus_wifi_client_connected_seconds": ("gauge", "How long the client has been associated."),
    "asus_wifi_client_transmit_bytes": ("counter", "Bytes sent to the client."),
    "asus_wifi_client_receive_bytes": ("counter", "Bytes received from the client."),
}


@dataclass(frozen=True)
class Sample:
    name: str
    value: float
    labels: tuple[tuple[str, str], ...] = ()


def sample(name: str, value: float | int | None, **labels: str) -> Sample | None:
    if name not in METRICS:
        raise KeyError(f"undefined metric {name}")
    if value is None:
        return None
    return Sample(name, float(value), tuple(sorted((k, str(v)) for k, v in labels.items())))


class Store:
    def __init__(self) -> None:
        self._groups: dict[tuple[str, str], list[Sample]] = {}
        self._lock = threading.Lock()

    def set(self, node: str, group: str, samples: list[Sample | None]) -> None:
        clean = [s for s in samples if s is not None]
        with self._lock:
            self._groups[(node, group)] = clean

    def drop(self, node: str, group: str) -> None:
        with self._lock:
            self._groups.pop((node, group), None)

    def snapshot(self) -> list[Sample]:
        with self._lock:
            return [s for group in self._groups.values() for s in group]


class StoreCollector(Collector):
    def __init__(self, store: Store) -> None:
        self.store = store

    def collect(self):
        by_name: dict[str, list[Sample]] = {}
        for s in self.store.snapshot():
            by_name.setdefault(s.name, []).append(s)
        for name, samples in sorted(by_name.items()):
            kind, help_text = METRICS[name]
            label_names = sorted({k for s in samples for k, _ in s.labels})
            family_cls = CounterMetricFamily if kind == "counter" else GaugeMetricFamily
            family = family_cls(name, help_text, labels=label_names)
            seen: set[tuple[str, ...]] = set()
            for s in samples:
                labels = dict(s.labels)
                values = tuple(labels.get(k, "") for k in label_names)
                if values in seen:
                    continue
                seen.add(values)
                family.add_metric(list(values), s.value)
            yield family


def collector_version() -> str:
    """COLLECTOR_VERSION (set by the image build), else the installed package version."""
    if version := os.environ.get("COLLECTOR_VERSION", "").strip():
        return version
    try:
        return metadata.version("netmon-collector")
    except metadata.PackageNotFoundError:
        return "unknown"


class BuildInfoCollector(Collector):
    """netmonx_collector_build_info{version="..."} 1, to see which release is running."""

    def __init__(self, version: str | None = None) -> None:
        self.version = version or collector_version()

    def collect(self):
        family = GaugeMetricFamily(
            "netmonx_collector_build_info", "netmonX collector version, always 1", labels=["version"]
        )
        family.add_metric([self.version], 1)
        yield family
