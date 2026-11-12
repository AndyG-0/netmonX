"""Prometheus HTTP service discovery, served next to /metrics.

`/sd/nodes`: the main router, AiMesh nodes and the ISP hop, for a blackbox ICMP job.
`/sd/dns`: the router's DNS server (dnsmasq), so prometheus.yml needs no hard-coded IP.
`/sd/clients`: online LAN clients for per-client pings; empty unless LAN_CLIENT_PING is on.

Format: https://prometheus.io/docs/prometheus/latest/http_sd/
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Callable, Iterable
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from prometheus_client import make_wsgi_app


def _group(targets: Iterable[str], **labels: str) -> dict:
    return {"targets": list(targets), "labels": {k: v for k, v in labels.items() if v}}


def sd_nodes(shared, router_host: str) -> list[dict]:
    out = [_group([router_host], node=router_host, role="router")]
    for n in shared.mesh_nodes:
        if n.ip and n.ip != router_host:
            out.append(_group([n.ip], node=n.ip, role="mesh", model=n.model))
    if shared.isp_hop:
        out.append(_group([shared.isp_hop], role="isp_gateway"))
    return out


def sd_dns(router_host: str) -> list[dict]:
    return [_group([f"{router_host}:53"], resolver="router")]


def in_lan(ip: str, lan: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(lan, strict=False)
    except ValueError:
        return False


def sd_clients(shared, enabled: bool, limit: int) -> list[dict]:
    """One group per client so each keeps its own mac/hostname labels. Main LAN only (not guest networks)."""
    if not enabled:
        return []
    out = []
    online = shared.tracker.online
    for mac, ip in sorted(shared.client_ips.items()):
        if mac not in online or (shared.lan_cidr and not in_lan(ip, shared.lan_cidr)):
            continue
        out.append(_group([ip], mac=mac, hostname=shared.hostnames.get(mac, "")))
        if len(out) >= limit:
            break
    return out


class _Server(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):  # noqa: A002 - Prometheus scrapes would flood the log
        pass


def make_app(registry, routes: dict[str, Callable[[], list[dict]]]):
    metrics = make_wsgi_app(registry)

    def app(environ, start_response):
        route = routes.get(environ.get("PATH_INFO", ""))
        if route is None:
            return metrics(environ, start_response)
        body = json.dumps(route()).encode()
        start_response("200 OK", [("Content-Type", "application/json"), ("Content-Length", str(len(body)))])
        return [body]

    return app


def serve(app, addr: str, port: int):
    """Start the HTTP server in a daemon thread; returns the server."""
    import threading

    httpd = make_server(addr, port, app, server_class=_Server, handler_class=_QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd
