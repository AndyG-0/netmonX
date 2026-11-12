"""MQTT bridge: packets, messages, publish-on-change, and a round trip through a fake broker."""

import asyncio
import json

from netmon_collector import mqtt, presence
from netmon_collector.poller import SharedState

A, B = "AA:00:00:00:00:01", "AA:00:00:00:00:02"


def test_settings_from_url():
    s = mqtt.MqttSettings.from_url("mqtts://ha:p%40ss@broker.lan", prefix="/home/")
    assert (s.host, s.port, s.username, s.password, s.tls, s.prefix) == ("broker.lan", 8883, "ha", "p@ss", True, "home")
    assert mqtt.MqttSettings.from_url("10.0.0.5").port == 1883
    assert mqtt.MqttSettings.from_url("mqtt://x", devices="ALL").devices == "all"
    try:
        mqtt.MqttSettings.from_url("http://x")
    except ValueError:
        pass
    else:
        raise AssertionError("http:// accepted")


def test_packets():
    assert mqtt._length(127) == b"\x7f" and mqtt._length(128) == b"\x80\x01" and mqtt._length(16383) == b"\xff\x7f"
    pkt = mqtt.publish_packet("a/b", "on")
    assert pkt == b"\x31\x07\x00\x03a/bon"
    c = mqtt.connect_packet("id", 60, "u", "p", ("s", "offline"))
    assert c[0] == 0x10 and c[2:8] == b"\x00\x04MQTT" and c[8] == 4
    assert c[9] == 0x02 | 0x04 | 0x20 | 0x80 | 0x40  # clean, will, will retain, user, password


def test_state_and_discovery_messages():
    devices = {A: {"home": True, "name": "TV", "node": "192.168.50.1", "band": "5 GHz", "last_seen": 10.5}}
    st = mqtt.state_messages("netmonx", False, devices)
    assert st["netmonx/wan"] == "offline" and st["netmonx/device/aa0000000001"] == "home"
    assert json.loads(st["netmonx/device/aa0000000001/attributes"])["last_seen"] == 10
    assert "netmonx/wan" not in mqtt.state_messages("netmonx", None, {})
    disc = mqtt.discovery_messages("netmonx", devices)
    tracker = json.loads(disc["homeassistant/device_tracker/netmonx_aa0000000001/config"])
    assert tracker["name"] == "TV" and tracker["state_topic"] == "netmonx/device/aa0000000001"
    assert json.loads(disc["homeassistant/binary_sensor/netmonx/wan/config"])["device_class"] == "connectivity"


def test_snapshot_named_and_all(tmp_path):
    path = tmp_path / "devices.yml"
    path.write_text(f"devices:\n  {A}: {{name: TV}}\n")
    shared = SharedState(devices=presence.DevicesFile(str(path)))
    shared.tracker.update({A: ("main", "5 GHz"), B: ("main", "")}, 100)
    shared.tracker.update({B: ("main", "")}, 200)
    shared.wan_up = True
    wan, named = mqtt.snapshot_from(shared, "named")
    assert wan is True and list(named) == [A] and named[A]["home"] is False and named[A]["name"] == "TV"
    assert set(mqtt.snapshot_from(shared, "all")[1]) == {A, B}


class FakeClient:
    def __init__(self):
        self.s = mqtt.MqttSettings("x", 1883, discovery=False)
        self.connects, self.connected, self.sent = 1, True, []

    async def publish(self, msgs):
        self.sent.append(list(msgs))
        return True

    async def ping(self):
        pass


def test_bridge_publishes_changes_only():
    state = {"wan": True, "devices": {A: {"home": True, "last_seen": 1}}}
    client = FakeClient()
    bridge = mqtt.Bridge(client, lambda: (state["wan"], state["devices"]))
    asyncio.run(bridge.step())
    assert len(client.sent[-1]) == 3  # wan, device, attributes
    state["devices"][A]["last_seen"] = 2
    asyncio.run(bridge.step())
    assert client.sent[-1] == []  # only last_seen moved: not worth a message
    state["devices"][A] = {"home": False, "last_seen": 3}
    asyncio.run(bridge.step())
    assert [t for t, _ in client.sent[-1]] == ["netmonx/device/aa0000000001", "netmonx/device/aa0000000001/attributes"]
    client.connects = 2  # reconnected: everything again
    asyncio.run(bridge.step())
    assert len(client.sent[-1]) == 3


def test_client_against_fake_broker():
    got = []

    async def broker(reader, writer):
        got.append(await reader.read(2))  # CONNECT header
        await reader.read(1024)
        writer.write(b"\x20\x02\x00\x00")  # CONNACK accepted
        await writer.drain()
        while data := await reader.read(1024):
            got.append(data)
        writer.close()

    async def main():
        server = await asyncio.start_server(broker, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        client = mqtt.MqttClient(mqtt.MqttSettings("127.0.0.1", port))
        assert await client.publish([("netmonx/wan", "online")])
        await asyncio.sleep(0.05)
        await client.close()
        await asyncio.sleep(0.05)
        server.close()

    asyncio.run(main())
    blob = b"".join(got[1:])
    assert got[0][0] == 0x10 and b"netmonx/statusonline" in blob and b"netmonx/wanonline" in blob
    assert blob.endswith(mqtt.DISCONNECT)


def test_client_refused_login():
    async def broker(reader, writer):
        await reader.read(1024)
        writer.write(b"\x20\x02\x00\x04")  # bad user name or password
        await writer.drain()
        writer.close()

    async def main():
        server = await asyncio.start_server(broker, "127.0.0.1", 0)
        client = mqtt.MqttClient(mqtt.MqttSettings("127.0.0.1", server.sockets[0].getsockname()[1]))
        ok = await client.publish([("t", "p")])
        server.close()
        return ok, client.connected

    assert asyncio.run(main()) == (False, False)
