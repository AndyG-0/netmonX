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
