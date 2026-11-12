"""Device presence and roaming, tracked across AiMesh client list polls.

The router only says who is connected right now. This module remembers each
device's first and last sighting and where it was attached, so we can export
an online gauge, "last seen" times, new devices and roams between nodes/bands.

State lives in memory. With STATE_DIR set, it is also saved as JSON so it
survives collector restarts (otherwise every device looks new after a restart,
which is why new-device detection waits for a baseline poll).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass

log = logging.getLogger(__name__)

STATE_FILE = "clients.json"


@dataclass
class Seen:
    first_seen: float
    last_seen: float
    node: str = ""  # node label (IP) the device was last attached to
    band: str = ""  # "" for wired
    roams: int = 0
    new: bool = False  # first seen after the baseline poll
    name: str = ""  # last known name, so offline devices keep theirs
    last_roam: dict | None = None  # {"ts", "from_node", "to_node", "from_band", "to_band"}


@dataclass
class Roam:
    mac: str
    from_node: str
    to_node: str
    from_band: str
    to_band: str
    ts: float


class ClientTracker:
    def __init__(self, state_dir: str | None = None, retention_days: float = 30) -> None:
        self.state_dir = state_dir or None
        self.retention = retention_days * 86400
        self.devices: dict[str, Seen] = {}
        self.online: set[str] = set()
        # Without saved state, the first poll is the baseline: those devices aren't "new".
        self.baseline_done = False
        self._load()

    # --- persistence -----------------------------------------------------

    def _path(self) -> str | None:
        return os.path.join(self.state_dir, STATE_FILE) if self.state_dir else None

    def _load(self) -> None:
        path = self._path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path) as f:
                raw = json.load(f)
            self.devices = {mac: Seen(**v) for mac, v in raw.get("devices", {}).items()}
            self.baseline_done = bool(self.devices)
            log.info("loaded %d known devices from %s", len(self.devices), path)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("ignoring unreadable state file %s: %s", path, exc)

    def save(self) -> None:
        path = self._path()
        if not path:
            return
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.state_dir, prefix=".clients.")
            with os.fdopen(fd, "w") as f:
                json.dump({"devices": {m: asdict(s) for m, s in self.devices.items()}}, f)
            os.replace(tmp, path)
        except OSError as exc:
            log.warning("could not save state to %s: %s", path, exc)

    # --- updates ---------------------------------------------------------

    def update(self, attached: dict[str, tuple[str, str]], now: float) -> list[Roam]:
        """attached: MAC -> (node label, band) for every device in this poll. Returns new roams."""
        roams: list[Roam] = []
        for mac, (node, band) in attached.items():
            seen = self.devices.get(mac)
            if seen is None:
                self.devices[mac] = Seen(now, now, node, band, new=self.baseline_done)
                continue
            # A device that was offline and comes back elsewhere hasn't roamed; it reconnected.
            if mac in self.online and (seen.node, seen.band) != (node, band) and seen.node:
                roam = Roam(mac, seen.node, node, seen.band, band, now)
                roams.append(roam)
                seen.roams += 1
                seen.last_roam = {
                    "ts": now,
                    "from_node": roam.from_node,
                    "to_node": roam.to_node,
                    "from_band": roam.from_band,
                    "to_band": roam.to_band,
                }
            seen.last_seen, seen.node, seen.band = now, node, band
        self.online = set(attached)
        self.baseline_done = True
        cutoff = now - self.retention
        for mac in [m for m, s in self.devices.items() if s.last_seen < cutoff]:
            del self.devices[mac]
        return roams


# --- devices.yml ------------------------------------------------------------


@dataclass
class DeviceMeta:
    name: str = ""
    owner: str = ""
    type: str = ""
    alert_offline_hours: float | None = None


def _norm_mac(mac: str) -> str:
    return str(mac).strip().upper().replace("-", ":")


def parse_devices(data) -> dict[str, DeviceMeta]:
    """`{"devices": {MAC: {name, owner, type, alert_offline_hours}}}`; bad entries are skipped."""
    out: dict[str, DeviceMeta] = {}
    devices = (data or {}).get("devices") if isinstance(data, dict) else None
    if not isinstance(devices, dict):
        return out
    for mac, attrs in devices.items():
        attrs = attrs if isinstance(attrs, dict) else {}
        hours = attrs.get("alert_offline_hours")
        try:
            hours = float(hours) if hours not in (None, "") else None
        except (TypeError, ValueError):
            hours = None
        out[_norm_mac(mac)] = DeviceMeta(
            name=str(attrs.get("name", "") or ""),
            owner=str(attrs.get("owner", "") or ""),
            type=str(attrs.get("type", "") or ""),
            alert_offline_hours=hours,
        )
    return out


class DevicesFile:
    """Reloads the devices file when it changes; keeps the last good copy if it breaks."""

    def __init__(self, path: str | None) -> None:
        self.path = path or None
        self.devices: dict[str, DeviceMeta] = {}
        self._mtime: float | None = None

    def get(self) -> dict[str, DeviceMeta]:
        if not self.path:
            return self.devices
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            if self._mtime is not None:
                log.warning("devices file %s is gone; keeping the last copy", self.path)
                self._mtime = None
            return self.devices
        if mtime != self._mtime:
            self._mtime = mtime
            try:
                import yaml

                with open(self.path) as f:
                    self.devices = parse_devices(yaml.safe_load(f))
                log.info("loaded %d devices from %s", len(self.devices), self.path)
            except Exception as exc:  # a typo in the file must not stop polling
                log.warning("could not read devices file %s: %s", self.path, exc)
        return self.devices


# --- MAC vendor (OUI) ---------------------------------------------------------

PRIVATE_VENDOR = "Private (random) MAC"


def is_locally_administered(mac: str) -> bool:
    """Phones use random "private" MACs; those set bit 1 of the first octet."""
    try:
        return bool(int(mac.split(":")[0], 16) & 0x02)
    except (ValueError, IndexError):
        return False


class OuiTable:
    """Vendor names from a Wireshark `manuf` file (`OUI<TAB>short<TAB>long`, with /28 and /36 blocks)."""

    def __init__(self, path: str | None = None) -> None:
        self.prefixes: dict[int, dict[int, str]] = {}  # prefix bits -> {prefix value: vendor}
        if path:
            self.load(path)

    def load(self, path: str) -> None:
        if not os.path.exists(path):
            log.info("no OUI file at %s; vendor names are off (run scripts/update-oui.sh)", path)
            return
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    self._add_line(line)
            log.info("loaded %d OUI prefixes from %s", sum(len(v) for v in self.prefixes.values()), path)
        except OSError as exc:
            log.warning("could not read OUI file %s: %s", path, exc)

    def _add_line(self, line: str) -> None:
        line = line.strip()
        if not line or line.startswith("#"):
            return
        parts = line.split("\t") if "\t" in line else line.split(None, 2)
        if len(parts) < 2:
            return
        prefix, _, bits_s = parts[0].partition("/")
        hexs = prefix.replace(":", "").replace("-", "").replace(".", "")
        try:
            value = int(hexs, 16)
        except ValueError:
            return
        bits = int(bits_s) if bits_s.isdigit() else len(hexs) * 4
        if bits <= 0 or bits > 48:
            return
        value >>= len(hexs) * 4 - bits if len(hexs) * 4 >= bits else 0
        name = (parts[2] if len(parts) > 2 and parts[2].strip() else parts[1]).strip()
        self.prefixes.setdefault(bits, {})[value] = name

    def vendor(self, mac: str) -> str:
        if is_locally_administered(mac):
            return PRIVATE_VENDOR
        try:
            value = int(mac.replace(":", ""), 16)
        except ValueError:
            return ""
        for bits in sorted(self.prefixes, reverse=True):  # most specific block first
            name = self.prefixes[bits].get(value >> (48 - bits))
            if name:
                return name
        return ""
