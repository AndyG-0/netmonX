"""Run the poller against a fake connection that replays fixture output."""

import asyncio
from pathlib import Path

from prometheus_client import generate_latest
from prometheus_client.registry import CollectorRegistry

from netmon_collector.metrics import Store, StoreCollector
from netmon_collector.poller import NodePoller, SharedState

FIX = Path(__file__).parent / "fixtures"


class FakeConn:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.commands: list[str] = []

    async def run(self, command):
        self.commands.append(command.name)
        if self.fail:
            raise OSError("connection refused")
        name = {
            "fast_snapshot": "fast_snapshot.txt",
            "slow_snapshot": "slow_snapshot.txt",
            "wifi_snapshot": "wifi_snapshot.txt",
            "clients_snapshot": "clientlist.json",
            "ping": "ping_busybox.txt",
            "traceroute": "traceroute.txt",
        }[command.name]
        return (FIX / name).read_text()


def make(conn, primary=True):
    store, shared = Store(), SharedState()
    poller = NodePoller(
        "192.168.50.1",
        conn,
        store,
        shared,
        is_primary=primary,
        ping_targets=["1.1.1.1", "8.8.8.8"],
        ping_count=5,
        isp_hop_setting="auto",
        fast_interval=15,
        slow_interval=300,
    )
    return poller, store, shared


def render(store) -> str:
    reg = CollectorRegistry()
    reg.register(StoreCollector(store))
    return generate_latest(reg).decode()


def test_full_cycle_primary():
    conn = FakeConn()
    poller, store, shared = make(conn)

    async def cycle():
        await poller._guard("slow", poller.poll_slow, ["slow", "wifi", "clients"])
        await poller._guard("fast", poller.poll_fast, ["fast", "ping"])
        await poller._guard("fast", poller.poll_fast, ["fast", "ping"])

    asyncio.run(cycle())
    text = render(store)

    assert poller.wan_iface == "eth0" and poller.isp_hop == "73.12.34.1"
    assert [n.ip for n in shared.mesh_nodes] == ["192.168.50.2", "192.168.50.3", "192.168.50.4"]
    assert shared.hostnames["7C:D9:5C:11:22:33"] == "Pixel-8"
    assert 'asus_up{node="192.168.50.1"} 1.0' in text
    assert 'asus_wan_up{node="192.168.50.1",wan="0"} 1.0' in text
    assert 'wan="1"' not in text  # single WAN: wan1 state is ignored
    assert 'asus_firmware_update_available{latest="3.0.0.4.388_25000-gabcdef0",node="192.168.50.1"} 1.0' in text
    assert 'asus_ipv6_enabled{node="192.168.50.1"} 0.0' in text
    assert "asus_dualwan_info" not in text
    assert 'asus_wan_receive_bytes_total{iface="eth0",node="192.168.50.1"} 9.8213876123e+010' in text
    assert 'asus_ping_loss_ratio{node="192.168.50.1",role="isp_gateway",target="73.12.34.1"} 0.2' in text
    # Two bursts of 5 sent / 4 received accumulate.
    assert 'asus_ping_packets_sent_total{node="192.168.50.1",role="internet",target="8.8.8.8"} 10.0' in text
    assert 'asus_ping_packets_received_total{node="192.168.50.1",role="internet",target="8.8.8.8"} 8.0' in text
    assert 'asus_router_info{firmware="3.0.0.4.388_24353-g1a2b3c4"' in text
    assert 'hostname="Pixel-8"' in text
    assert 'asus_wifi_clients{iface="eth5",node="192.168.50.1",radio="wl1",ssid="HomeNet"} 0.0' in text
    assert (
        'asus_wifi_radio_info{band="5 GHz",chanspec="100/80",dfs="1",iface="eth5",node="192.168.50.1",radio="wl1"} 1.0'
        in text
    )
    assert 'asus_wifi_noise_dbm{iface="eth4",node="192.168.50.1",radio="wl0"} -87.0' in text
    assert 'asus_wifi_airtime_percent{iface="eth4",node="192.168.50.1",radio="wl0",stat="obss"} 56.0' in text
    assert 'asus_wifi_channel_changes_total{iface="eth6",node="192.168.50.1",radio="wl2"} 0.0' in text
    # eth6 client at -52 dBm over a -89 dBm noise floor
    assert 'asus_wifi_client_snr_db{hostname="Pixel-8",iface="eth6",mac="7C:D9:5C:11:22:33",' in text
    assert 'radio="wl2",ssid="HomeNet"} 37.0' in text
    assert 'asus_wifi_clients{iface="wl0.1",node="192.168.50.1",radio="wl0",ssid="HomeNet_Guest"} 0.0' in text
    assert 'iface="wl2.5"' not in text  # hidden backhaul with no clients
    # Mesh-wide client list: 6 devices, named nodes, guest SSIDs resolved.
    assert 'asus_clients{band="",connection="wired",node="192.168.50.2",node_name="Office",ssid=""} 1.0' in text
    assert (
        'asus_clients{band="",connection="wired",node="192.168.50.1",node_name="main 192.168.50.1",ssid=""} 1.0' in text
    )
    assert (
        'asus_clients{band="2.4 GHz",connection="wifi",node="192.168.50.1",'
        'node_name="main 192.168.50.1",ssid="HomeNet_Guest"} 1.0'
    ) in text
    assert text.count("asus_client_info{") == 6
    assert 'hostname="Living room TV"' in text
    assert 'asus_client_rssi_dbm{band="5 GHz-2",connection="wifi",hostname=""' in text
    assert 'asus_router_temperature_celsius{node="192.168.50.1",sensor="radio_eth6"} 61.0' in text
    assert 'asus_traceroute_hop_rtt_ms{hop="3",ip="*",node="192.168.50.1"}' not in text  # no rtt -> no sample
    assert 'asus_dhcp_leases{node="192.168.50.1"} 2.0' in text
    assert 'asus_dhcp_pool_size{node="192.168.50.1"} 253.0' in text
    # Presence: every device is online; the first poll is the baseline, so nothing is "new".
    assert text.count("asus_client_online{") == 6 and "asus_client_new{" not in text
    assert 'vendor="Private (random) MAC"' in text  # 5E:... is a locally administered MAC


def test_mesh_node_skips_wan_and_ping():
    conn = FakeConn()
    poller, store, _ = make(conn, primary=False)

    async def cycle():
        await poller.poll_slow()
        await poller.poll_fast()

    asyncio.run(cycle())
    text = render(store)
    assert "ping" not in conn.commands and "traceroute" not in conn.commands
    assert "asus_wan_up" not in text and "asus_dhcp_leases" not in text
    assert 'role="mesh"' in text


def test_failure_drops_values_and_marks_down():
    conn = FakeConn()
    poller, store, _ = make(conn)
    asyncio.run(poller._guard("slow", poller.poll_slow, ["slow", "wifi", "clients"]))
    conn.fail = True
    asyncio.run(poller._guard("slow", poller.poll_slow, ["slow", "wifi", "clients"]))
    text = render(store)
    assert 'asus_up{node="192.168.50.1"} 0.0' in text
    assert 'asus_poll_errors_total{loop="slow",node="192.168.50.1"} 1.0' in text
    assert "asus_wan_info" not in text


def test_node_labels_fall_back_when_names_repeat():
    from netmon_collector.parsers import MeshNode
    from netmon_collector.poller import node_labels

    mesh = [MeshNode(ip="192.168.50.2", mac="B", model="XT8"), MeshNode(ip="192.168.50.3", mac="C", model="XT8")]
    nodes, display = node_labels("192.168.50.1", "A", mesh, {"B": "XT8", "C": "XT8"})
    assert nodes == {"A": "192.168.50.1", "B": "192.168.50.2", "C": "192.168.50.3"}
    assert display == {
        "192.168.50.1": "main 192.168.50.1",
        "192.168.50.2": "node 192.168.50.2",
        "192.168.50.3": "node 192.168.50.3",
    }


def test_client_ssid_comes_from_the_nodes_own_radios():
    from netmon_collector.parsers import MeshClient, WifiSnapshot
    from netmon_collector.poller import client_samples, ssid_keys

    # On the main router wl0.2 is "Stream"; on the node, wl0.2 is the guest network.
    main = {"wl0": "Home", "wl0.2": "Stream"}
    node = ssid_keys(WifiSnapshot(ssids={"eth4": "", "wl0.1": "Home", "wl0.2": "Guest"}), {"eth4": "wl0"})
    assert node == {"wl0.1": "Home", "wl0.2": "Guest"}
    clients = [
        MeshClient("N", "AA:00:00:00:00:01", "", False, "2.4 GHz", "wl0.2"),
        MeshClient("N", "AA:00:00:00:00:02", "", False, "2.4 GHz", "wl0"),  # node backhaul has no SSID: use main
    ]
    out = client_samples(clients, {"N": "10.0.0.2"}, {}, main, {}, {"10.0.0.2": node})
    info = [dict(s.labels) for s in out if s and s.name == "asus_client_info"]
    ssids = {lab["mac"]: lab["ssid"] for lab in info}
    assert ssids == {"AA:00:00:00:00:01": "Guest", "AA:00:00:00:00:02": "Home"}


def test_channel_tracker_counts_changes():
    from netmon_collector.poller import ChannelTracker

    t = ChannelTracker()
    assert t.update("eth5", "44/80") == 0
    assert t.update("eth5", "44/80") == 0
    assert t.update("eth5", "100/80") == 1
    assert t.update("eth4", "6") == 0


def test_dual_wan_and_ipv6():
    from netmon_collector import poller as P

    text = "@@wan_state\n2\n1\n0\n1\n"
    out = [x for x in P.fast_samples("r", text, None, True, dual_wan=True) if x]
    up = {dict(x.labels)["wan"]: x.value for x in out if x.name == "asus_wan_up"}
    assert up == {"0": 1, "1": 0}

    conn = FakeConn()
    poller, store, _ = make(conn)
    slow = (FIX / "slow_snapshot.txt").read_text()
    slow = (
        slow.replace("wans_dualwan=wan none", "wans_dualwan=wan usb")
        .replace("ipv6_service=disabled", "ipv6_service=dhcp6")
        .replace("ipv6_prefix=\n", "ipv6_prefix=2001:db8:1::\n")
    )
    conn.run = _fixed(conn.run, "slow_snapshot", slow)
    asyncio.run(poller._guard("slow", poller.poll_slow, ["slow"]))
    rendered = render(store)
    assert 'asus_dualwan_info{mode="failover",node="192.168.50.1",wans="wan usb"} 1.0' in rendered
    assert 'wan="1",wan_type="usb"' in rendered
    assert poller.dual_wan and poller.ipv6
    assert 'asus_ipv6_info{node="192.168.50.1",prefix="2001:db8:1::",prefix_length="64"' in rendered


def _fixed(run, name, text):
    async def wrapped(command):
        if command.name == name:
            return text
        return await run(command)

    return wrapped


def test_ping6_command():
    from netmon_collector import commands

    assert commands.ping("2606:4700:4700::1111", 3).text.startswith("ping6 -c 3")
    assert commands.ping("1.1.1.1", 3).text.startswith("ping -c 3")
