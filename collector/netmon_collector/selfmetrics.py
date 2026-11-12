"""The collector's own health: SSH timings, reconnects and output that didn't parse.

These live in prometheus_client objects (not the Store) because they are
cumulative over the process lifetime and include a histogram.
"""

from __future__ import annotations

from prometheus_client import Counter, Histogram
from prometheus_client.registry import CollectorRegistry

# Router commands take 0.1 s (fast snapshot) to ~10 s (traceroute through a dead hop).
_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30)


class SelfMetrics:
    def __init__(self) -> None:
        self.command_seconds = Histogram(
            "netmonx_collector_ssh_command_duration_seconds",
            "Time to run one router command over SSH.",
            ["node", "command"],
            buckets=_BUCKETS,
            registry=None,
        )
        self.command_errors = Counter(
            "netmonx_collector_ssh_command_errors",
            "Router commands that failed (timeout, SSH error or dropped connection).",
            ["node", "command"],
            registry=None,
        )
        self.connects = Counter(
            "netmonx_collector_ssh_connects",
            "SSH connections opened (more than one per node means reconnects).",
            ["node"],
            registry=None,
        )
        self.parse_failures = Counter(
            "netmonx_collector_parse_failures",
            "Command output sections that came back but couldn't be parsed.",
            ["node", "section"],
            registry=None,
        )

    def register(self, registry: CollectorRegistry) -> None:
        for m in (self.command_seconds, self.command_errors, self.connects, self.parse_failures):
            registry.register(m)


# One instance per process; tests can make their own.
SELF = SelfMetrics()
