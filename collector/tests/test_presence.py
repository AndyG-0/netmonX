"""Presence, roaming, devices.yml and MAC vendor lookup."""

from netmon_collector import presence
from netmon_collector.poller import presence_samples

A, B = "AA:00:00:00:00:01", "AA:00:00:00:00:02"


def labels(samples, name):
    return [(dict(s.labels), s.value) for s in samples if s and s.name == name]


def test_first_poll_is_baseline_then_new_devices_and_roams():
    t = presence.ClientTracker()
    assert t.update({A: ("main", "5 GHz")}, 100) == []
    assert not t.devices[A].new  # part of the baseline

    roams = t.update({A: ("Office", "5 GHz"), B: ("main", "2.4 GHz")}, 200)
    assert [(r.from_node, r.to_node) for r in roams] == [("main", "Office")]
    assert t.devices[B].new and t.devices[A].roams == 1

    # Offline, then back on another node: a reconnect, not a roam.
    t.update({B: ("main", "2.4 GHz")}, 300)
    assert t.update({A: ("main", "2.4 GHz"), B: ("main", "2.4 GHz")}, 400) == []
    assert t.devices[A].last_seen == 400 and t.online == {A, B}


def test_band_change_counts_as_roam():
    t = presence.ClientTracker()
    t.update({A: ("main", "2.4 GHz")}, 1)
    roams = t.update({A: ("main", "5 GHz")}, 2)
    assert [(r.from_band, r.to_band) for r in roams] == [("2.4 GHz", "5 GHz")]


def test_retention_forgets_old_devices():
    t = presence.ClientTracker(retention_days=1)
    t.update({A: ("main", "")}, 0)
    t.update({B: ("main", "")}, 2 * 86400)
    assert set(t.devices) == {B}


def test_state_survives_restart(tmp_path):
    t = presence.ClientTracker(str(tmp_path))
    t.update({A: ("main", "")}, 10)
    t.save()
    t2 = presence.ClientTracker(str(tmp_path))
    assert t2.devices[A].first_seen == 10
    assert t2.baseline_done  # so a device appearing after restart is new
    t2.update({B: ("main", "")}, 20)
    assert t2.devices[B].new


def test_broken_state_file_is_ignored(tmp_path):
    (tmp_path / presence.STATE_FILE).write_text("{nope")
    assert presence.ClientTracker(str(tmp_path)).devices == {}


def test_presence_samples():
    t = presence.ClientTracker()
    t.update({A: ("main", "5 GHz")}, 100)
    t.update({A: ("Office", "5 GHz"), B: ("main", "")}, 200)
    t.update({A: ("Office", "5 GHz")}, 300)
    t.devices[A].name = "Pixel"
    meta = {A: presence.DeviceMeta(name="Andy phone", owner="andy", type="phone", alert_offline_hours=12)}
    out = presence_samples(t, meta, now=400)
    online = {lab["mac"]: (lab["hostname"], lab["owner"], v) for lab, v in labels(out, "asus_client_online")}
    assert online == {A: ("Andy phone", "andy", 1), B: ("", "", 0)}
    assert labels(out, "asus_client_new") == [({"hostname": "", "mac": B}, 1)]
    ((roam, ts),) = labels(out, "asus_client_last_roam_timestamp_seconds")
    assert roam["from_node"] == "main" and roam["to_node"] == "Office" and ts == 200
    assert labels(out, "asus_device_offline_alert_hours") == [({"hostname": "Andy phone", "mac": A}, 12)]
    assert presence_samples(t, {}, now=200 + 3600) and not labels(presence_samples(t, {}, 3800), "asus_client_new")


def test_parse_devices():
    meta = presence.parse_devices(
        {
            "devices": {
                "aa-00-00-00-00-01": {"name": "TV", "owner": "family", "type": "tv", "alert_offline_hours": "6"},
                "AA:00:00:00:00:02": None,
                "AA:00:00:00:00:03": {"alert_offline_hours": "soon"},
            }
        }
    )
    assert meta[A] == presence.DeviceMeta("TV", "family", "tv", 6.0)
    assert meta[B] == presence.DeviceMeta()
    assert meta["AA:00:00:00:00:03"].alert_offline_hours is None
    assert presence.parse_devices(None) == {} and presence.parse_devices({"devices": []}) == {}


def test_devices_file_reload_and_bad_yaml(tmp_path):
    path = tmp_path / "devices.yml"
    path.write_text("devices:\n  AA:00:00:00:00:01: {name: TV}\n")
    f = presence.DevicesFile(str(path))
    assert f.get()[A].name == "TV"
    path.write_text("devices: [unclosed\n")
    import os

    os.utime(path, (1, 1))  # make sure the mtime changes
    assert f.get()[A].name == "TV"  # keeps the last good copy
    assert presence.DevicesFile(str(tmp_path / "missing.yml")).get() == {}


def test_oui_lookup(tmp_path):
    manuf = tmp_path / "manuf"
    manuf.write_text(
        "# comment\n"
        "00:00:0C\tCisco\tCisco Systems, Inc\n"
        "7C:D9:5C\tGoogle\tGoogle, Inc.\n"
        "00:1B:C5:00:00:00/36\tConverg\tConverging Systems Inc.\n"
        "00:1B:C5\tIEEERegi\tIEEE Registration Authority\n"
        "B8:27:EB\tRaspberr\n"
    )
    oui = presence.OuiTable(str(manuf))
    assert oui.vendor("7C:D9:5C:11:22:33") == "Google, Inc."
    assert oui.vendor("00:1B:C5:00:00:01") == "Converging Systems Inc."
    assert oui.vendor("00:1B:C5:FF:00:01") == "IEEE Registration Authority"
    assert oui.vendor("B8:27:EB:00:00:05") == "Raspberr"
    assert oui.vendor("5E:11:22:33:44:55") == presence.PRIVATE_VENDOR
    assert oui.vendor("04:D9:F5:AA:BB:01") == ""
    assert presence.OuiTable(str(tmp_path / "missing")).vendor("00:00:0C:00:00:00") == ""
