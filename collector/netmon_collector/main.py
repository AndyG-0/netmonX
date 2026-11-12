"""Entry point: start the /metrics server and a poller per router node."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from prometheus_client import start_http_server
from prometheus_client.registry import CollectorRegistry

from .config import Config
from .metrics import BuildInfoCollector, Store, StoreCollector, collector_version
from .poller import NodePoller, SharedState
from .presence import ClientTracker, DevicesFile, OuiTable
from .selfmetrics import SELF
from .ssh import RouterConnection

log = logging.getLogger("netmon_collector")


def _poller(cfg: Config, host: str, store: Store, shared: SharedState, primary: bool) -> NodePoller:
    conn = RouterConnection(host, cfg.router_port, cfg.router_user, cfg.key_path, cfg.known_hosts)
    return NodePoller(
        host,
        conn,
        store,
        shared,
        is_primary=primary,
        ping_targets=cfg.ping_targets,
        ping6_targets=cfg.ping6_targets,
        ping_count=cfg.ping_count,
        isp_hop_setting=cfg.isp_hop,
        fast_interval=cfg.fast_interval,
        slow_interval=cfg.slow_interval,
    )


async def _watch_mesh(cfg: Config, store: Store, shared: SharedState) -> None:
    """Start a poller for each mesh node, from config or as the main router reports them."""
    started: dict[str, asyncio.Task] = {}
    while True:
        if "auto" in cfg.mesh_nodes:
            hosts = [n.ip for n in shared.mesh_nodes]
        else:
            hosts = cfg.mesh_nodes
        for host in hosts:
            if host != cfg.router_host and host not in started:
                log.info("starting poller for mesh node %s", host)
                started[host] = asyncio.create_task(_poller(cfg, host, store, shared, primary=False).run())
        await asyncio.sleep(min(cfg.slow_interval, 60))


async def run() -> None:
    cfg = Config.from_env()
    store = Store()
    registry = CollectorRegistry(auto_describe=True)
    registry.register(StoreCollector(store))
    registry.register(BuildInfoCollector())
    SELF.register(registry)
    start_http_server(cfg.listen_port, addr=cfg.listen_addr, registry=registry)
    log.info("collector %s serving metrics on %s:%s/metrics", collector_version(), cfg.listen_addr, cfg.listen_port)

    shared = SharedState(
        tracker=ClientTracker(cfg.state_dir, cfg.client_retention_days),
        devices=DevicesFile(cfg.devices_file),
        oui=OuiTable(cfg.oui_file),
    )
    tasks = [_poller(cfg, cfg.router_host, store, shared, primary=True).run()]
    if cfg.mesh_ssh and cfg.mesh_nodes:
        tasks.append(_watch_mesh(cfg, store, shared))
    # Stop promptly on Ctrl-C, `docker stop` or a dev reload.
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("stopping")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
