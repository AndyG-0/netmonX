"""Optional MQTT bridge: WAN state and device presence for Home Assistant and other automations.

Off unless MQTT_URL is set. Only publishes; never subscribes to anything or acts on messages, so nothing on
MQTT can reach the router. A tiny MQTT 3.1.1 client (QoS 0, retained messages, last will) keeps the image free
of another dependency.

Topics (prefix MQTT_TOPIC, default "netmonx"):
    netmonx/status                  online / offline (last will), for HA availability
    netmonx/wan                     online / offline
    netmonx/device/<id>             home / not_home     (<id>: MAC without colons, lower case)
    netmonx/device/<id>/attributes  {"mac", "name", "node", "band", "last_seen"}
With MQTT_DISCOVERY (default true) it also publishes Home Assistant discovery configs under
homeassistant/, so a WAN connectivity sensor and a device_tracker per device appear by themselves.
Which devices: MQTT_DEVICES=named (default: the ones in devices.yml) or all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import struct
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

log = logging.getLogger(__name__)

DISCOVERY_PREFIX = "homeassistant"


# --- MQTT 3.1.1 packets (only what a publisher needs) ------------------------


def _str(s: str) -> bytes:
    b = s.encode()
    return struct.pack("!H", len(b)) + b


def _length(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n % 128, n // 128
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def connect_packet(
    client_id: str, keepalive: int, username: str = "", password: str = "", will: tuple[str, str] | None = None
) -> bytes:
    flags = 0x02  # clean session
    payload = _str(client_id)
    if will:
        flags |= 0x04 | 0x20  # will flag, will retain (QoS 0)
        payload += _str(will[0]) + _str(will[1])
    if username:
        flags |= 0x80
        payload += _str(username)
        if password:
            flags |= 0x40
            payload += _str(password)
    body = _str("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload
    return bytes([0x10]) + _length(len(body)) + body


def publish_packet(topic: str, payload: str, retain: bool = True) -> bytes:
    body = _str(topic) + payload.encode()
    return bytes([0x30 | (1 if retain else 0)]) + _length(len(body)) + body


PINGREQ = b"\xc0\x00"
DISCONNECT = b"\xe0\x00"


@dataclass
class MqttSettings:
    host: str
    port: int
    username: str = ""
    password: str = ""
    tls: bool = False
    prefix: str = "netmonx"
    discovery: bool = True
    devices: str = "named"  # "named" (devices.yml) or "all"

    @classmethod
    def from_url(cls, url: str, prefix: str = "netmonx", discovery: bool = True, devices: str = "named"):
        u = urllib.parse.urlsplit(url if "://" in url else f"mqtt://{url}")
        if u.scheme not in ("mqtt", "mqtts"):
            raise ValueError(f"MQTT_URL must start with mqtt:// or mqtts://, not {u.scheme}://")
        tls = u.scheme == "mqtts"
        return cls(
            host=u.hostname or "localhost",
            port=u.port or (8883 if tls else 1883),
            username=urllib.parse.unquote(u.username or ""),
            password=urllib.parse.unquote(u.password or ""),
            tls=tls,
            prefix=prefix.strip("/") or "netmonx",
            discovery=discovery,
            devices="all" if devices.strip().lower() == "all" else "named",
        )


class MqttClient:
    """Publish-only client that reconnects by itself. publish() never raises."""

    def __init__(self, settings: MqttSettings, client_id: str = "netmonx-collector", keepalive: int = 60) -> None:
        self.s = settings
        self.client_id = client_id
        self.keepalive = keepalive
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._next_try = 0.0
        self._last_write = 0.0
        self.connects = 0  # bumped on each successful connect, so callers can republish everything

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def _connect(self) -> bool:
        if self.connected:
            return True
        if time.monotonic() < self._next_try:
            return False
        self._next_try = time.monotonic() + 30  # back off after a failure
        status = f"{self.s.prefix}/status"
        try:
            ctx = ssl.create_default_context() if self.s.tls else None
            reader, writer = await asyncio.wait_for(asyncio.open_connection(self.s.host, self.s.port, ssl=ctx), 10)
            writer.write(
                connect_packet(self.client_id, self.keepalive, self.s.username, self.s.password, (status, "offline"))
            )
            await writer.drain()
            ack = await asyncio.wait_for(reader.readexactly(4), 10)
            if ack[0] != 0x20 or ack[3] != 0:
                codes = {1: "bad protocol", 2: "client id rejected", 3: "unavailable", 4: "bad login", 5: "not allowed"}
                raise ConnectionError(f"broker refused: {codes.get(ack[3], ack[3])}")
        except (TimeoutError, OSError, asyncio.IncompleteReadError, ConnectionError, ssl.SSLError) as exc:
            log.warning("MQTT %s:%s: %s", self.s.host, self.s.port, exc or type(exc).__name__)
            return False
        self._writer = writer
        self._reader_task = asyncio.create_task(self._drain(reader))
        self.connects += 1
        log.info("MQTT connected to %s:%s", self.s.host, self.s.port)
        self._write(publish_packet(status, "online"))
        return True

    async def _drain(self, reader: asyncio.StreamReader) -> None:
        """Read and drop what the broker sends (PINGRESP); notice when it hangs up."""
        try:
            while await reader.read(1024):
                pass
        except (OSError, asyncio.IncompleteReadError):
            pass
        self._drop()

    def _drop(self) -> None:
        if self._writer is not None:
            self._writer.close()
            log.warning("MQTT connection to %s:%s lost", self.s.host, self.s.port)
        self._writer = None
        self._next_try = 0.0  # try again right away once

    def _write(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(data)
        self._last_write = time.monotonic()

    async def publish(self, messages: list[tuple[str, str]]) -> bool:
        if not messages:
            return True
        if not await self._connect():
            return False
        try:
            for topic, payload in messages:
                self._write(publish_packet(topic, payload))
            await self._writer.drain()
        except (OSError, AttributeError):
            self._drop()
            return False
        return True

    async def ping(self) -> None:
        if self.connected and time.monotonic() - self._last_write > self.keepalive / 2:
            try:
                self._write(PINGREQ)
                await self._writer.drain()
            except (OSError, AttributeError):
                self._drop()

    async def close(self) -> None:
        if self.connected:
            try:
                self._write(publish_packet(f"{self.s.prefix}/status", "offline"))
                self._write(DISCONNECT)
                await self._writer.drain()
            except OSError:
                pass
            self._writer.close()
        self._writer = None
        if self._reader_task:
            self._reader_task.cancel()


# --- what to publish (pure) -------------------------------------------------


def device_id(mac: str) -> str:
    return mac.replace(":", "").lower()


def state_messages(prefix: str, wan_up: bool | None, devices: dict[str, dict]) -> dict[str, str]:
    """topic -> payload for the current state. devices: MAC -> {"home", "name", "node", "band", "last_seen"}."""
    out: dict[str, str] = {}
    if wan_up is not None:
        out[f"{prefix}/wan"] = "online" if wan_up else "offline"
    for mac, d in devices.items():
        base = f"{prefix}/device/{device_id(mac)}"
        out[base] = "home" if d["home"] else "not_home"
        attrs = {"mac": mac, "name": d.get("name", ""), "node": d.get("node", ""), "band": d.get("band", "")}
        attrs["last_seen"] = int(d.get("last_seen", 0))
        out[f"{base}/attributes"] = json.dumps(attrs, sort_keys=True)
    return out


def discovery_messages(prefix: str, devices: dict[str, dict]) -> dict[str, str]:
    """Home Assistant MQTT discovery configs for the WAN sensor and each device tracker."""
    avail = {"availability_topic": f"{prefix}/status"}
    hub = {"identifiers": [prefix], "name": "netmonX", "manufacturer": "netmonX"}
    out = {
        f"{DISCOVERY_PREFIX}/binary_sensor/{prefix}/wan/config": json.dumps(
            {
                "name": "Internet",
                "unique_id": f"{prefix}_wan",
                "state_topic": f"{prefix}/wan",
                "payload_on": "online",
                "payload_off": "offline",
                "device_class": "connectivity",
                "device": hub,
                **avail,
            },
            sort_keys=True,
        )
    }
    for mac, d in devices.items():
        did = device_id(mac)
        out[f"{DISCOVERY_PREFIX}/device_tracker/{prefix}_{did}/config"] = json.dumps(
            {
                "name": d.get("name") or mac,
                "unique_id": f"{prefix}_{did}",
                "state_topic": f"{prefix}/device/{did}",
                "json_attributes_topic": f"{prefix}/device/{did}/attributes",
                "payload_home": "home",
                "payload_not_home": "not_home",
                "source_type": "router",
                **avail,
            },
            sort_keys=True,
        )
    return out


def changed(new: dict[str, str], old: dict[str, str]) -> list[tuple[str, str]]:
    """Messages whose payload differs from what was last sent."""
    return [(t, p) for t, p in new.items() if old.get(t) != p]


class Bridge:
    """Publishes on change; everything again after a (re)connect, as the broker may have lost retained state."""

    def __init__(self, client: MqttClient, snapshot: Callable[[], tuple[bool | None, dict[str, dict]]]) -> None:
        self.client = client
        self.snapshot = snapshot
        self._sent: dict[str, str] = {}
        self._connects = 0

    async def step(self) -> None:
        wan_up, devices = self.snapshot()
        prefix = self.client.s.prefix
        want = state_messages(prefix, wan_up, devices)
        if self.client.s.discovery:
            want = {**discovery_messages(prefix, devices), **want}
        if self.client.connects != self._connects or not self.client.connected:
            self._sent = {}  # new connection: send it all
        msgs = changed(want, self._sent)
        # Attributes change every poll (last_seen); only send them with a state change or once a full refresh.
        state_changed = {t for t, _ in msgs if not t.endswith("/attributes")}
        msgs = [
            (t, p)
            for t, p in msgs
            if not t.endswith("/attributes") or t.removesuffix("/attributes") in state_changed or t not in self._sent
        ]
        if await self.client.publish(msgs):
            self._connects = self.client.connects
            self._sent.update(msgs)
        await self.client.ping()

    async def run(self, interval: float) -> None:
        while True:
            try:
                await self.step()
            except Exception:  # never take the collector down
                log.exception("MQTT bridge step failed")
            await asyncio.sleep(interval)


def snapshot_from(shared, mode: str) -> tuple[bool | None, dict[str, dict]]:
    """Current WAN state and devices to publish, from the poller's SharedState."""
    meta = shared.devices.get()
    tracker = shared.tracker
    macs = list(tracker.devices) if mode == "all" else [m for m in meta if m in tracker.devices]
    devices = {}
    for mac in macs:
        seen = tracker.devices[mac]
        m = meta.get(mac)
        devices[mac] = {
            "home": mac in tracker.online,
            "name": (m.name if m else "") or seen.name,
            "node": seen.node,
            "band": seen.band,
            "last_seen": seen.last_seen,
        }
    return shared.wan_up, devices
