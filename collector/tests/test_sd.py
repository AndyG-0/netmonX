"""HTTP service discovery endpoints."""

import io
import json

from prometheus_client.registry import CollectorRegistry

from netmon_collector import parsers, sd
from netmon_collector.poller import SharedState, lan_cidr

A, B, G = "AA:00:00:00:00:01", "AA:00:00:00:00:02", "AA:00:00:00:00:03"


def shared():
    s = SharedState(hostnames={A: "Pixel"})
    s.mesh_nodes = [parsers.MeshNode("XT8", "192.168.50.2", "04:D9:F5:AA:BB:02")]
    s.isp_hop = "73.12.34.1"
    s.client_ips = {A: "192.168.50.23", B: "192.168.50.41", G: "192.168.101.20"}
    s.lan_cidr = "192.168.50.0/24"
    s.tracker.online = {A, G}
    return s


def test_sd_nodes_and_dns():
    assert sd.sd_nodes(shared(), "192.168.50.1") == [
        {"targets": ["192.168.50.1"], "labels": {"node": "192.168.50.1", "role": "router"}},
        {"targets": ["192.168.50.2"], "labels": {"node": "192.168.50.2", "role": "mesh", "model": "XT8"}},
        {"targets": ["73.12.34.1"], "labels": {"role": "isp_gateway"}},
    ]
    assert sd.sd_dns("10.0.0.1") == [{"targets": ["10.0.0.1:53"], "labels": {"resolver": "router"}}]


def test_sd_clients_opt_in_online_lan_only_and_limited():
    s = shared()
    assert sd.sd_clients(s, False, 50) == []
    # B is offline, G is on the guest network: neither is pinged.
    assert sd.sd_clients(s, True, 50) == [{"targets": ["192.168.50.23"], "labels": {"mac": A, "hostname": "Pixel"}}]
    s.tracker.online = {A, B}
    assert len(sd.sd_clients(s, True, 1)) == 1


def test_lan_cidr():
    assert lan_cidr("192.168.50.1", "255.255.255.0") == "192.168.50.0/24"
    assert lan_cidr("10.1.2.3", "") == "10.1.2.0/24"
    assert lan_cidr("", "") == ""


def call(app, path):
    status = {}
    body = app({"PATH_INFO": path, "REQUEST_METHOD": "GET", "QUERY_STRING": "", "wsgi.input": io.BytesIO()},
               lambda st, headers: status.update(st=st, headers=dict(headers)))  # fmt: skip
    return status, b"".join(body)


def test_app_routes_sd_and_metrics():
    app = sd.make_app(CollectorRegistry(), {"/sd/dns": lambda: sd.sd_dns("192.168.50.1")})
    st, body = call(app, "/sd/dns")
    assert st["headers"]["Content-Type"] == "application/json"
    assert json.loads(body)[0]["targets"] == ["192.168.50.1:53"]
    st, _ = call(app, "/metrics")
    assert st["st"].startswith("200")
