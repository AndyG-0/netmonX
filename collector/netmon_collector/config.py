"""Settings read from environment variables (see .env.example)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from .commands import safe_host

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    return default if not raw else raw.lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


@dataclass
class Config:
    router_host: str
    router_port: int
    router_user: str
    key_path: str
    known_hosts: str | None
    ping_targets: list[str]
    ping_count: int
    isp_hop: str  # "auto", "none" or an address
    mesh_nodes: list[str]  # ["auto"], [] or addresses
    mesh_ssh: bool
    fast_interval: float
    slow_interval: float
    listen_port: int
    listen_addr: str = "0.0.0.0"
    state_dir: str | None = None  # saves device history here so it survives restarts
    devices_file: str | None = None  # devices.yml: names, owners, types, offline alerts
    oui_file: str | None = None  # Wireshark `manuf` file for MAC vendor names
    client_retention_days: float = 30
    ping6_targets: list[str] = field(default_factory=list)  # used only if the router has IPv6 on

    @classmethod
    def from_env(cls) -> Config:
        host = _env("ROUTER_HOST")
        if not host:
            raise SystemExit("ROUTER_HOST is required")
        known_hosts = _env("ROUTER_KNOWN_HOSTS") or None
        if known_hosts and not os.path.exists(known_hosts):
            log.warning("ROUTER_KNOWN_HOSTS %s not found; host key checking is off", known_hosts)
            known_hosts = None
        if known_hosts is None:
            log.warning("router host key is not verified; set ROUTER_KNOWN_HOSTS to enable checking")
        isp_hop = _env("ISP_HOP", "auto")
        if isp_hop not in ("auto", "none"):
            safe_host(isp_hop)
        mesh = _list("MESH_NODES", "auto")
        if mesh in (["none"], ["off"]):
            mesh = []
        for m in mesh:
            if m != "auto":
                safe_host(m)
        targets = _list("ROUTER_PING_TARGETS", "1.1.1.1,8.8.8.8")
        for t in targets:
            safe_host(t)
        targets6 = _list("ROUTER_PING6_TARGETS", "2606:4700:4700::1111,2001:4860:4860::8888")
        if targets6 in (["none"], ["off"]):
            targets6 = []
        for t in targets6:
            if ":" not in safe_host(t):
                raise ValueError(f"ROUTER_PING6_TARGETS must be IPv6 addresses: {t!r}")
        return cls(
            router_host=safe_host(host),
            router_port=int(_env("ROUTER_PORT", "22")),
            router_user=_env("ROUTER_USER", "admin"),
            key_path=_env("ROUTER_KEY_PATH", "/run/secrets/router_key"),
            known_hosts=known_hosts,
            ping_targets=targets,
            ping6_targets=targets6,
            ping_count=int(_env("PING_COUNT", "5")),
            isp_hop=isp_hop,
            mesh_nodes=mesh,
            mesh_ssh=_bool("MESH_SSH", True),
            fast_interval=float(_env("FAST_INTERVAL", "15")),
            slow_interval=float(_env("SLOW_INTERVAL", "300")),
            listen_port=int(_env("LISTEN_PORT", "9877")),
            listen_addr=_env("LISTEN_ADDR", "0.0.0.0"),
            state_dir=_env("STATE_DIR") or None,
            devices_file=_env("DEVICES_FILE") or None,
            oui_file=_env("OUI_FILE") or None,
            client_retention_days=float(_env("CLIENT_RETENTION_DAYS", "30")),
        )
