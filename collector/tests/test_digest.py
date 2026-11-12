"""scripts/digest.py: weekly digest text, schedule and ntfy request."""

import importlib.util
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_path = Path(__file__).resolve().parents[2] / "scripts" / "digest.py"
_spec = importlib.util.spec_from_file_location("digest", _path)
digest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(digest)


def vec(value, **labels):
    return [{"metric": labels, "value": [0, str(value)]}]


def fake(answers):
    """A Prometheus stand-in: the first key found in the expression decides the answer."""

    def query(expr):
        for key, result in answers.items():
            if key in expr:
                return result
        return []

    return query


def test_digest_text():
    q = fake(
        {
            "avg_over_time((max(asus_wan_up))": vec(0.995),
            "ceil(changes": vec(2),
            "count_over_time((max(asus_wan_up))": vec(70),
            "quantile_over_time(0.5, (avg(asus_ping_rtt": vec(11.4),
            "quantile_over_time(0.95": vec(30),
            "asus_ping_loss_ratio": vec(0.001),
            "speedtest_tracker_download_bits)[": vec(450e6),
            "speedtest_tracker_upload_bits)[": vec(40e6),
            'direction="download"': vec(500e6),
            "clamp_min": vec(45),
            "count(asus_client_online": vec(30),
            "asus_client_new": vec(1, mac="AA:00:00:00:00:01", hostname="TV")
            + vec(1, mac="AA:00:00:00:00:02", hostname=""),
            "resets(": vec(1, node="Office"),
            "asus_syslog_events_total": vec(3, event="ssh_login_failed") + vec(1, event="dfs_radar"),
        }
    )
    lines = digest.collect(q, 7)
    assert lines[0] == "**Internet:** up 99.50%, 2 outages (70 min down)"
    assert lines[1] == "**Latency:** 11 ms typical, 30 ms p95, 0.10% loss"
    assert lines[2] == "**Speed (median):** 450 Mbit/s down (90% of plan), 40 Mbit/s up"
    assert lines[3].startswith("**Bufferbloat:** grade B")
    assert lines[4] == "**Devices:** 30 online now, 2 new this period"
    assert lines[5] == "**Reboots:** Office"
    assert lines[6] == "**Router log:** 3 failed logins, 1 radar channel changes"
    named = digest.collect(q, 7, names=True)
    assert named[4].endswith(": AA:00:00:00:00:02, TV")


def test_digest_without_data():
    assert digest.collect(fake({}), 7) == ["**Internet:** no data (is the collector running?)"]


def test_grade_and_schedule():
    assert [digest.grade(ms) for ms in (3, 30, 31, 250, 1000)] == ["A+", "A", "B", "D", "F"]
    wed = datetime(2026, 11, 11, 9, 0)  # a Wednesday
    assert digest.next_run("mon 08:00", wed) == datetime(2026, 11, 16, 8, 0)
    assert digest.next_run("wed 09:30", wed) == datetime(2026, 11, 11, 9, 30)
    assert digest.next_run("wed 09:00", wed) == datetime(2026, 11, 18, 9, 0)  # not twice in the same minute


def test_send_to_ntfy():
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got["headers"] = dict(self.headers)
            got["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.handle_request)
    thread.start()
    digest.send(f"http://127.0.0.1:{server.server_port}/topic", "netmonX", "**hi**", token="tk")
    thread.join(5)
    server.server_close()
    assert got["body"] == "**hi**"
    assert got["headers"]["Title"] == "netmonX" and got["headers"]["Markdown"] == "yes"
    assert got["headers"]["Authorization"] == "Bearer tk"
