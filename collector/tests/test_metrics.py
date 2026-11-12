"""The build-info metric reports the installed collector version."""

from prometheus_client import generate_latest
from prometheus_client.registry import CollectorRegistry

from netmon_collector.metrics import BuildInfoCollector, collector_version


def test_build_info_reports_version():
    registry = CollectorRegistry()
    registry.register(BuildInfoCollector("1.2.3-rc.1"))
    assert 'netmonx_collector_build_info{version="1.2.3-rc.1"} 1.0' in generate_latest(registry).decode()


def test_collector_version_is_installed_version(monkeypatch):
    monkeypatch.delenv("COLLECTOR_VERSION", raising=False)
    assert collector_version() not in ("", "unknown")


def test_collector_version_prefers_image_build_version(monkeypatch):
    monkeypatch.setenv("COLLECTOR_VERSION", "0.1.0+edge.abc1234")
    assert collector_version() == "0.1.0+edge.abc1234"


def test_self_metrics_count_commands_and_errors():
    import asyncio

    import pytest

    from netmon_collector.commands import fast_snapshot
    from netmon_collector.selfmetrics import SelfMetrics
    from netmon_collector.ssh import RouterConnection

    class Result:
        stdout = "ok"

    class Conn:
        fail = False

        async def run(self, text, check):
            if self.fail:
                raise OSError("gone")
            return Result()

        def close(self):
            pass

    m = SelfMetrics()
    rc = RouterConnection("10.0.0.1", 22, "admin", "/key", None, metrics=m)
    rc._conn = Conn()
    assert asyncio.run(rc.run(fast_snapshot())) == "ok"
    rc._conn.fail = True
    with pytest.raises(OSError):
        asyncio.run(rc.run(fast_snapshot()))
    assert rc._conn is None  # reset after the error
    registry = CollectorRegistry()
    m.register(registry)
    text = generate_latest(registry).decode()
    assert 'netmonx_collector_ssh_command_duration_seconds_count{command="fast_snapshot",node="10.0.0.1"} 1.0' in text
    assert 'netmonx_collector_ssh_command_errors_total{command="fast_snapshot",node="10.0.0.1"} 1.0' in text


def test_unparsed_sections():
    from netmon_collector.poller import FAST_REQUIRED, unparsed_sections

    assert unparsed_sections("@@net_dev\n@@loadavg\n0.1 0.2 0.3 1/2 3\n", FAST_REQUIRED) == [
        "net_dev",
        "meminfo",
        "uptime",
    ]


def test_isp_plan_metric(monkeypatch):
    from netmon_collector.config import Config
    from netmon_collector.metrics import IspPlanCollector

    monkeypatch.setenv("ROUTER_HOST", "192.0.2.1")
    monkeypatch.setenv("ISP_PLAN_DOWN", "500")
    monkeypatch.setenv("ISP_PLAN_UP", "fast")
    cfg = Config.from_env()
    assert (cfg.isp_plan_down, cfg.isp_plan_up) == (500, 0)
    registry = CollectorRegistry()
    registry.register(IspPlanCollector(cfg.isp_plan_down, cfg.isp_plan_up))
    text = generate_latest(registry).decode()
    assert 'netmonx_isp_plan_bits{direction="download"} 5e+08' in text and "upload" not in text
