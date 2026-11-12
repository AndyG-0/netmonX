from pathlib import Path

import pytest

from netmon_collector import commands, parsers

FIX = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIX / name).read_text()


def test_fast_sections():
    sec = parsers.section_map(fixture("fast_snapshot.txt"))
    dev = parsers.parse_net_dev(sec["net_dev"])
    assert dev["eth0"]["rx_bytes"] == 98213876123
    assert dev["eth0"]["tx_bytes"] == 12398712311
    assert dev["eth6"]["tx_drop"] == 7
    assert parsers.parse_loadavg(sec["loadavg"]) == (0.42, 0.37, 0.31)
    mem = parsers.parse_meminfo(sec["meminfo"])
    assert mem["MemTotal"] == 506904 * 1024
    assert parsers.mem_available(mem) == 213448 * 1024
    assert parsers.parse_uptime(sec["uptime"]) == 864123.45
    cpu = parsers.parse_cpu_stat(sec["stat"])
    assert cpu["user"] == 12345.0 and cpu["idle"] == 987654.0
    assert parsers.parse_ints(sec["conntrack"]) == [812, 65536]


def test_mem_available_fallback():
    mem = {"MemFree": 10, "Buffers": 5, "Cached": 2}
    assert parsers.mem_available(mem) == 17


def test_ping_busybox():
    r = parsers.parse_ping(fixture("ping_busybox.txt"), expected=5)
    assert (r.sent, r.received) == (5, 4)
    assert r.loss_ratio == pytest.approx(0.2)
    assert r.min_ms == 11.904 and r.max_ms == 14.502
    assert r.avg_ms == pytest.approx(12.934, abs=1e-3)
    # |14.502-12.311| + |11.904-14.502| + |13.020-11.904|, over 3
    assert r.jitter_ms == pytest.approx((2.191 + 2.598 + 1.116) / 3, abs=1e-3)


def test_ping_iputils():
    text = (
        "64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=10.1 ms\n"
        "64 bytes from 8.8.8.8: icmp_seq=2 ttl=117 time=10.5 ms\n"
        "2 packets transmitted, 2 received, 0% packet loss, time 1001ms\n"
    )
    r = parsers.parse_ping(text, expected=2)
    assert (r.sent, r.received, r.loss_ratio) == (2, 2, 0.0)


def test_ping_unreachable_counts_as_total_loss():
    r = parsers.parse_ping(fixture("ping_unreachable.txt"), expected=5)
    assert (r.sent, r.received, r.loss_ratio) == (5, 0, 1.0)
    assert r.avg_ms is None and r.jitter_ms is None


def test_traceroute_and_isp_hop():
    hops = parsers.parse_traceroute(fixture("traceroute.txt"))
    assert [h.ttl for h in hops] == [1, 2, 3, 4, 5]
    assert hops[0].ip == "73.12.34.1" and hops[0].rtt_ms == 8.412
    assert hops[2].ip is None and hops[2].rtt_ms is None
    assert parsers.pick_isp_hop("73.12.34.1", hops) == "73.12.34.1"


def test_isp_hop_behind_modem_nat():
    hops = [parsers.Hop(1, "192.168.0.1", 1.0), parsers.Hop(2, "100.72.0.1", 9.0)]
    # Private gateway (modem in router mode): use first ISP-side hop, CGNAT counts.
    assert parsers.pick_isp_hop("192.168.0.1", hops) == "100.72.0.1"
    assert parsers.pick_isp_hop("192.168.0.1", []) == "192.168.0.1"
    assert parsers.pick_isp_hop(None, []) is None


def test_slow_sections():
    sec = parsers.section_map(fixture("slow_snapshot.txt"))
    nv = parsers.parse_kv(sec["nvram"])
    assert nv["productid"] == "XT8" and nv["wan0_dns"] == "75.75.75.75 75.75.76.76"
    assert parsers.parse_wl_ifnames(nv) == {"eth4": "wl0", "eth5": "wl1", "eth6": "wl2"}
    nodes = parsers.parse_cfg_device_list(nv["cfg_device_list"])
    assert [n.ip for n in nodes] == ["192.168.50.1", "192.168.50.2", "192.168.50.3", "192.168.50.4"]
    assert nodes[1].mac == "04:D9:F5:AA:BB:02"
    assert parsers.parse_thermal(sec["thermal"]) == {"thermal_zone0": 67.542}
    leases = parsers.parse_leases(sec["leases"])
    assert [(x.mac, x.hostname) for x in leases] == [("7C:D9:5C:11:22:33", "Pixel-8"), ("A4:83:E7:44:55:66", "")]
    arp = parsers.parse_proc_arp(sec["arp"])
    assert len(arp) == 3 and {e.device for e in arp} == {"br0", "eth0"}


def test_wifi_snapshot():
    snap = parsers.parse_wifi_snapshot(fixture("wifi_snapshot.txt"))
    assert snap.temps == {"eth4": 58.0, "eth6": 61.0}
    assert snap.ssids["wl0.1"] == "HomeNet_Guest" and snap.ssids["wl2.5"] == ""
    a, b = snap.clients
    assert (a.iface, a.mac, a.rssi_dbm) == ("eth6", "7C:D9:5C:11:22:33", -52)
    assert a.tx_rate_mbps == 1201.0 and a.rx_rate_mbps == 864.8
    assert a.connected_seconds == 3612 and a.tx_bytes == 1123412341 and a.rx_bytes == 51234123
    # `wl rssi` printed 0, so fall back to smoothed rssi.
    assert (b.iface, b.rssi_dbm, b.tx_bytes) == ("eth4", -71, None)


def test_commands_reject_injection():
    with pytest.raises(ValueError):
        commands.ping("1.1.1.1; reboot")
    with pytest.raises(ValueError):
        commands.wifi_snapshot(["eth6 && nvram set x=1"])
    with pytest.raises(ValueError):
        commands.traceroute("$(id)")
    assert commands.ping("one.one.one.one", 3).text.startswith("ping -c 3 -w 5 one.one.one.one")


def test_commands_are_read_only():
    texts = [
        commands.fast_snapshot().text,
        commands.slow_snapshot().text,
        commands.wifi_snapshot(["eth4", "eth5"], ["wl0.1"]).text,
        commands.clients_snapshot().text,
        commands.ping("1.1.1.1").text,
        commands.traceroute("1.1.1.1").text,
    ]
    for t in texts:
        for bad in ("nvram set", "nvram commit", "nvram unset", "rm ", "reboot", "service ", ">/jffs", "> /"):
            assert bad not in t


def test_band_info():
    assert parsers.band_info("2G") == ("2.4 GHz", "wl0")
    assert parsers.band_info("5G_1") == ("5 GHz", "wl1.1")
    assert parsers.band_info("5G1") == ("5 GHz-2", "wl2")
    assert parsers.band_info("60G") is None and parsers.band_info("5G_x") is None


def test_wl_vifs():
    assert parsers.parse_wl_vifs({"wl0_vifs": "wl0.1 wl0.2", "wl1_vifs": ""}) == {"wl0.1": "wl0", "wl0.2": "wl0"}


def test_clientlist_and_dedupe():
    raw = parsers.parse_clientlist(fixture("clientlist.json"))
    # Bad node keys and unknown bands are skipped.
    assert {c.node_mac for c in raw} == {"04:D9:F5:AA:BB:01", "04:D9:F5:AA:BB:02"}
    assert len(raw) == 9
    clients = parsers.dedupe_clients(raw, "04:D9:F5:AA:BB:01", {"04:D9:F5:AA:BB:01", "04:D9:F5:AA:BB:02"})
    by_mac = {c.mac: c for c in clients}
    assert len(clients) == 6 and "04:D9:F5:AA:BB:02" not in by_mac
    # The NAS shows as wired on the main router but is on the node's Wi-Fi: Wi-Fi wins.
    nas = by_mac["D0:50:99:00:00:09"]
    assert (nas.node_mac, nas.wired, nas.band, nas.ssid_key, nas.rssi_dbm) == (
        "04:D9:F5:AA:BB:02",
        False,
        "5 GHz-2",
        "wl2",
        -62,
    )
    # Wired on both: the mesh node wins.
    assert by_mac["B8:27:EB:00:00:05"].node_mac == "04:D9:F5:AA:BB:02"
    assert by_mac["A4:83:E7:44:55:66"].rssi_dbm is None
    assert by_mac["5E:11:22:33:44:55"].ssid_key == "wl0.1"
    assert parsers.parse_clientlist("") == [] and parsers.parse_clientlist("not json") == []


def test_custom_clientlist():
    value = "<Living room TV>a4:83:e7:44:55:66>0>0>>>><>11:22:33:44:55:66>0>0>>>><junk"
    assert parsers.parse_custom_clientlist(value) == {"A4:83:E7:44:55:66": "Living room TV"}


def test_dhcp_pool_size():
    assert parsers.dhcp_pool_size("192.168.50.2", "192.168.50.254") == 253
    assert parsers.dhcp_pool_size("192.168.50.9", "192.168.50.2") is None
    assert parsers.dhcp_pool_size("", "192.168.50.254") is None
