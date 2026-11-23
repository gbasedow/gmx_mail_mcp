"""Tests for the IDLE watcher (renewal, BYE, lost connection, backoff, auth failure)."""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

import pytest

from custom_components.gmx_mail_mcp.client.idle import IdleWatcher
from custom_components.gmx_mail_mcp.client.imap import Backoff, ConnectionLimiter, ImapSettings
from custom_components.gmx_mail_mcp.const import PushState, Security

from .fakes import EMAIL, PASSWORD, FakeIdleClient, FakeIdleServer

SETTINGS = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password=PASSWORD)


class Harness:
    """Runs a watcher against the fake client and records callbacks and backoff sleeps."""

    def __init__(self, server: FakeIdleServer, limiter: ConnectionLimiter | None = None) -> None:
        """Initialise."""
        self.server = server
        self.limiter = limiter or ConnectionLimiter()
        self.changes = 0
        self.auth_failures = 0
        self.sleeps: list[float] = []
        self.clock = 0.0
        self.watcher = IdleWatcher(
            SETTINGS,
            ssl_context=self._context,
            limiter=self.limiter,
            on_change=self._changed,
            on_auth_failed=self._auth_failed,
            renew_seconds=24 * 60,
            client_factory=server,
            backoff=Backoff(rng=lambda: 0.5, clock=lambda: self.clock),
            sleep=self._sleep,
        )
        self.task: asyncio.Task[None] | None = None

    async def _context(self) -> ssl.SSLContext:
        return ssl.create_default_context()

    def _changed(self) -> None:
        self.changes += 1

    def _auth_failed(self) -> None:
        self.auth_failures += 1

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds  # time passes instantly ...
        await asyncio.sleep(0)  # ... but the event loop must get control, or a retry loop would never yield

    def start(self) -> None:
        """Run the watcher in the background."""
        self.task = asyncio.create_task(self.watcher.async_run())

    async def until(self, condition: Any, steps: int = 200) -> None:
        """Let the loop run until ``condition()`` is true."""
        for _ in range(steps):
            try:
                if condition():
                    return
            except IndexError:  # no client created yet
                pass
            await asyncio.sleep(0)
        raise AssertionError("condition not reached")

    def client(self, index: int = -1) -> FakeIdleClient:
        """Return a created client."""
        return self.server.clients[index]

    async def stop(self) -> None:
        """Cancel the watcher."""
        assert self.task is not None
        self.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.task


@pytest.fixture
def server() -> FakeIdleServer:
    """A fresh fake IDLE server."""
    return FakeIdleServer()


async def test_connects_examines_and_reports_changes(server: FakeIdleServer) -> None:
    """EXAMINE (read-only) the inbox; EXISTS/EXPUNGE/FETCH trigger on_change, other lines do not."""
    harness = Harness(server)
    harness.start()
    await harness.until(lambda: harness.watcher.connected and harness.client().pending_idle)
    client = harness.client()
    assert client.selected == ['"INBOX"']
    assert harness.changes == 1  # catch-up after connecting
    assert harness.limiter.in_use == 1

    client.push(b"5 EXISTS")
    await harness.until(lambda: harness.changes == 2)
    client.push(b"OK Still here")
    await harness.until(lambda: client.idle_starts == 3)
    assert harness.changes == 2
    client.push(b"3 EXPUNGE")
    await harness.until(lambda: harness.changes == 3)
    client.push(b"2 FETCH (FLAGS (\\Seen))")
    await harness.until(lambda: harness.changes == 4)
    assert client.done_calls == 4  # DONE after every push

    await harness.stop()
    assert client.logged_out
    assert harness.limiter.in_use == 0
    assert harness.watcher.state is PushState.STOPPED


async def test_renewal_restarts_idle_without_reconnect(server: FakeIdleServer) -> None:
    """The renewal timer (stop marker) sends DONE and starts a new IDLE on the same connection."""
    harness = Harness(server)
    harness.start()
    await harness.until(lambda: harness.client().pending_idle)
    for expected in (2, 3, 4):
        harness.client().renew()
        await harness.until(lambda e=expected: harness.client().idle_starts == e)
    assert len(server.clients) == 1
    assert harness.changes == 1
    await harness.stop()


async def test_bye_timeout_reconnects_with_backoff(server: FakeIdleServer) -> None:
    """GMX sends '* BYE timeout' after ~30 min: reconnect (5 s backoff), no error state left behind."""
    harness = Harness(server)
    harness.start()
    await harness.until(lambda: harness.client().pending_idle)
    harness.client().push(b"BYE timeout")
    await harness.until(lambda: len(server.clients) == 2 and harness.watcher.connected)
    assert harness.sleeps == [5.0]
    assert harness.watcher.reconnects == 1
    # After BYE the server closes the socket: no DONE and no LOGOUT (both would only wait for timeouts).
    assert server.clients[0].done_calls == 0
    assert not server.clients[0].logged_out
    assert harness.limiter.in_use == 1  # slot released and taken again
    assert harness.changes == 2  # catch-up after the reconnect
    await harness.stop()


async def test_lost_connection_wakes_the_wait(server: FakeIdleServer) -> None:
    """A dropped socket is noticed at once, not only at the next renewal."""
    harness = Harness(server)
    harness.start()
    await harness.until(lambda: harness.client().pending_idle)
    harness.client().lose_connection()
    await harness.until(lambda: len(server.clients) == 2 and harness.watcher.connected)
    assert harness.watcher.last_error is None
    assert server.clients[0].done_calls == 0 and not server.clients[0].logged_out
    await harness.stop()


async def test_backoff_grows_while_server_unreachable(server: FakeIdleServer) -> None:
    """Failed connects back off exponentially; success resets the backoff."""
    server.connect_error = ConnectionRefusedError()
    harness = Harness(server)
    harness.start()
    await harness.until(lambda: len(harness.sleeps) >= 3)
    assert harness.sleeps[:3] == [5.0, 10.0, 20.0]
    assert harness.watcher.state is PushState.RECONNECTING
    assert harness.watcher.last_error == "ConnectionRefusedError"
    assert harness.limiter.in_use == 0
    server.connect_error = None
    await harness.until(lambda: harness.watcher.connected)
    await harness.stop()


async def test_auth_failure_stops_the_watcher(server: FakeIdleServer) -> None:
    """A rejected login is reported once and the watcher ends (no retry loop against GMX)."""
    server.password = "revoked"
    harness = Harness(server)
    harness.start()
    assert harness.task is not None
    await asyncio.wait_for(harness.task, 1)
    assert harness.auth_failures == 1
    assert harness.watcher.state is PushState.AUTH_FAILED
    assert harness.limiter.in_use == 0
    assert len(server.clients) == 1


async def test_without_idle_capability_the_watcher_stops(server: FakeIdleServer) -> None:
    """No IDLE after login: state 'unsupported', polling continues elsewhere."""
    server.idle_capability = False
    harness = Harness(server)
    harness.start()
    assert harness.task is not None
    await asyncio.wait_for(harness.task, 1)
    assert harness.watcher.state is PushState.UNSUPPORTED
    assert harness.limiter.in_use == 0


async def test_respects_connection_limit(server: FakeIdleServer) -> None:
    """With both connection slots taken the watcher backs off instead of opening a third connection."""
    limiter = ConnectionLimiter(limit=2)
    limiter.acquire()
    limiter.acquire()
    harness = Harness(server, limiter)
    harness.start()
    await harness.until(lambda: len(harness.sleeps) >= 1)
    assert server.clients == []
    assert harness.watcher.last_error == "GmxConnectionLimitError"
    await harness.stop()


def test_mailbox_argument_encoding() -> None:
    """aioimaplib sends mailbox names unchanged, so they are encoded (modified UTF-7) and quoted here."""
    from custom_components.gmx_mail_mcp.client.idle import mailbox_argument  # noqa: PLC0415

    assert mailbox_argument("INBOX") == '"INBOX"'
    assert mailbox_argument("Entwürfe") == '"Entw&APw-rfe"'  # as measured against GMX (GMX-NOTES row 15)
    assert mailbox_argument('A "B" \\C') == '"A \\"B\\" \\\\C"'
