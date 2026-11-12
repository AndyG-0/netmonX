"""Entry point: start the /metrics server and a poller per router node."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from prometheus_client.registry import CollectorRegistry

from . import mqtt, sd
from .config import Config
from .metrics import BuildInfoCollector, IspPlanCollector, Store, StoreCollector, collector_version
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
    registry.register(IspPlanCollector(cfg.isp_plan_down, cfg.isp_plan_up))
    SELF.register(registry)
    shared = SharedState(
        tracker=ClientTracker(cfg.state_dir, cfg.client_retention_days),
        devices=DevicesFile(cfg.devices_file),
        oui=OuiTable(cfg.oui_file),
    )
    routes = {
        "/sd/nodes": lambda: sd.sd_nodes(shared, cfg.router_host),
        "/sd/dns": lambda: sd.sd_dns(cfg.router_host),
        "/sd/clients": lambda: sd.sd_clients(shared, cfg.lan_client_ping, cfg.lan_client_ping_max),
    }
    sd.serve(sd.make_app(registry, routes), cfg.listen_addr, cfg.listen_port)
    log.info("collector %s serving metrics on %s:%s/metrics", collector_version(), cfg.listen_addr, cfg.listen_port)
    tasks = [_poller(cfg, cfg.router_host, store, shared, primary=True).run()]
    if cfg.mesh_ssh and cfg.mesh_nodes:
        tasks.append(_watch_mesh(cfg, store, shared))
    client = None
    try:
        settings = mqtt.MqttSettings.from_url(cfg.mqtt_url, cfg.mqtt_topic, cfg.mqtt_discovery, cfg.mqtt_devices)
    except ValueError as exc:
        log.error("MQTT bridge off: %s", exc)
        settings = None
    if cfg.mqtt_url and settings:
        client = mqtt.MqttClient(settings)
        bridge = mqtt.Bridge(client, lambda: mqtt.snapshot_from(shared, settings.devices))
        tasks.append(bridge.run(min(cfg.fast_interval, 30)))
        log.info("MQTT bridge to %s:%s, topic %s/", settings.host, settings.port, settings.prefix)
    # Stop promptly on Ctrl-C, `docker stop` or a dev reload.
    main_task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main_task.cancel)
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        log.info("stopping")
        if client:
            await client.close()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    asyncio.run(run())


if __name__ == "__main__":
    main()
