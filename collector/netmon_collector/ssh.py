"""One long-lived SSH connection per router node."""

from __future__ import annotations

import asyncio
import logging

import asyncssh

from .commands import Command

log = logging.getLogger(__name__)


class RouterConnection:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        key_path: str,
        known_hosts: str | None,
        timeout: float = 20.0,
        max_parallel: int = 3,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.key_path = key_path
        self.known_hosts = known_hosts
        self.timeout = timeout
        self._conn: asyncssh.SSHClientConnection | None = None
        self._connect_lock = asyncio.Lock()
        # Dropbear on the router handles a few channels fine; keep it gentle.
        self._sem = asyncio.Semaphore(max_parallel)

    async def _connection(self) -> asyncssh.SSHClientConnection:
        async with self._connect_lock:
            if self._conn is None:
                log.info("connecting to %s@%s:%s", self.username, self.host, self.port)
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(
                        self.host,
                        port=self.port,
                        username=self.username,
                        client_keys=[self.key_path],
                        known_hosts=self.known_hosts,
                        keepalive_interval=30,
                        agent_path=None,
                    ),
                    self.timeout,
                )
            return self._conn

    async def run(self, command: Command) -> str:
        if not isinstance(command, Command):
            raise TypeError("only commands from commands.py may be run")
        conn = await self._connection()
        try:
            async with self._sem:
                result = await asyncio.wait_for(conn.run(command.text, check=False), self.timeout)
        except (TimeoutError, OSError, asyncssh.Error):
            self.reset()
            raise
        out = result.stdout if isinstance(result.stdout, str) else (result.stdout or b"").decode(errors="replace")
        return out

    def reset(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
