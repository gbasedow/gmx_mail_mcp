"""Tests for the IMAP connection handling (no Home Assistant instance needed)."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Callable, Generator
from typing import Any
from unittest.mock import patch

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from custom_components.gmx_mail_mcp.client.errors import (
    GmxAuthError,
    GmxBackoffError,
    GmxConnectionError,
    GmxConnectionLimitError,
    GmxOutcomeUnknownError,
    GmxTlsError,
)
from custom_components.gmx_mail_mcp.client.imap import (
    Backoff,
    ConnectionLimiter,
    ImapSession,
    ImapSettings,
    ImapWorker,
    ServerInfo,
    check_imap_account,
    open_imap_connection,
    parse_capabilities,
    tls_context_with_minimum,
)
from custom_components.gmx_mail_mcp.const import Security

from .fakes import EMAIL, PASSWORD, FakeIMAPClient, FakeImapServer

SETTINGS = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password=PASSWORD)


class Clock:
    """Manually advanced monotonic clock."""

    def __init__(self) -> None:
        """Start at an arbitrary point."""
        self.now = 1000.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move forward."""
        self.now += seconds


async def run_inline[T](target: Callable[..., T], *args: Any) -> T:
    """Executor replacement that runs the job directly."""
    return target(*args)


@pytest.fixture
def server() -> Generator[FakeImapServer]:
    """Patch IMAPClient with the fake server."""
    fake = FakeImapServer()
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", fake):
        yield fake


@pytest.fixture
def clock() -> Clock:
    """Return a manual clock."""
    return Clock()


def make_worker(clock: Clock, limiter: ConnectionLimiter | None = None, **kwargs: Any) -> ImapWorker:
    """Create a worker with test doubles."""
    return ImapWorker(
        SETTINGS,
        executor=run_inline,
        ssl_context_factory=ssl.create_default_context,
        limiter=limiter or ConnectionLimiter(),
        clock=clock,
        backoff=Backoff(rng=lambda: 0.5, clock=clock),
        **kwargs,
    )


def _list(client: ImapSession) -> int:
    return len(client.list_folders())


# --------------------------------------------------------------------------- helpers


def test_settings_repr_hides_password() -> None:
    """The password never appears in a repr (logs, diagnostics)."""
    assert PASSWORD not in repr(SETTINGS)


def test_server_info_properties() -> None:
    """Capabilities are normalised; APPENDLIMIT is parsed."""
    info = ServerInfo(capabilities=parse_capabilities([b"imap4rev1", "Move", b"APPENDLIMIT=29360128"]), folders=())
    assert info.has("move") and info.has("IMAP4REV1")
    assert info.append_limit == 29360128
    assert info.missing_recommended == ["UIDPLUS", "SPECIAL-USE", "UNSELECT"]
    assert ServerInfo(capabilities=frozenset({"APPENDLIMIT"}), folders=()).append_limit is None


@pytest.mark.filterwarnings("ignore:ssl.TLSVersion.TLSv1 is deprecated:DeprecationWarning")
def test_tls_context_minimum_version() -> None:
    """TLS >= 1.2 is enforced; contexts without verification are refused."""
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1
    assert tls_context_with_minimum(context).minimum_version == ssl.TLSVersion.TLSv1_2

    unverified = ssl.create_default_context()
    unverified.check_hostname = False
    unverified.verify_mode = ssl.CERT_NONE
    with pytest.raises(GmxTlsError):
        tls_context_with_minimum(unverified)


def test_backoff_grows_exponentially_and_is_capped() -> None:
    """5 s, 10 s, 20 s ... up to 15 min; jitter stays within +/- 20 %."""
    clock = Clock()
    backoff = Backoff(rng=lambda: 0.5, clock=clock)
    delays = [backoff.record_failure() for _ in range(12)]
    assert delays[:4] == [5.0, 10.0, 20.0, 40.0]
    assert delays[-1] == 900.0
    assert backoff.remaining() == 900.0
    clock.advance(900)
    assert backoff.remaining() == 0.0
    backoff.reset()
    assert backoff.failures == 0

    low = Backoff(rng=lambda: 0.0, clock=clock)
    low.record_failure()
    low.record_failure()
    assert low.remaining() == pytest.approx(8.0)  # 10 s - 20 %
    high = Backoff(rng=lambda: 0.999999, clock=clock)
    assert high.record_failure() == pytest.approx(6.0, rel=1e-4)  # 5 s + 20 %
    assert Backoff(rng=lambda: 0.0, clock=clock).record_failure() == 5.0  # never below the minimum


def test_connection_limiter() -> None:
    """At most two connections per account."""
    limiter = ConnectionLimiter()
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(GmxConnectionLimitError):
        limiter.acquire()
    limiter.release()
    limiter.release()
    limiter.release()  # never negative
    assert limiter.in_use == 0


# --------------------------------------------------------------------------- open_imap_connection


def test_open_implicit_tls(server: FakeImapServer) -> None:
    """Port 993 with implicit TLS and the given context."""
    context = ssl.create_default_context()
    client = open_imap_connection(SETTINGS, context)
    assert isinstance(client, FakeIMAPClient)
    assert client.ssl and client.ssl_context is context and client.logged_in


def test_open_starttls(server: FakeImapServer) -> None:
    """Port 143 upgrades with STARTTLS before the login."""
    settings = ImapSettings(host="imap.gmx.net", port=143, security=Security.STARTTLS, username=EMAIL, password=PASSWORD)
    client = open_imap_connection(settings, ssl.create_default_context())
    assert isinstance(client, FakeIMAPClient)
    assert not client.ssl and client.tls and client.logged_in


def test_starttls_failure_never_logs_in(server: FakeImapServer) -> None:
    """Without STARTTLS the credentials are not sent."""
    server.starttls_error = IMAPClientError("STARTTLS extension not supported by server.")
    settings = ImapSettings(host="imap.gmx.net", port=143, security=Security.STARTTLS, username=EMAIL, password=PASSWORD)
    with pytest.raises(GmxTlsError):
        open_imap_connection(settings, ssl.create_default_context())
    assert server.logins == 0
    assert server.clients[0].closed


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ssl.SSLCertVerificationError("certificate verify failed"), GmxTlsError),
        (ConnectionRefusedError(), GmxConnectionError),
        (TimeoutError(), GmxConnectionError),
        (IMAPClientError("weird greeting"), GmxConnectionError),
    ],
)
def test_connect_errors(server: FakeImapServer, error: Exception, expected: type[Exception]) -> None:
    """Socket, TLS and protocol errors are mapped."""
    server.connect_error = error
    with pytest.raises(expected):
        open_imap_connection(SETTINGS, ssl.create_default_context())


def test_starttls_tls_failure(server: FakeImapServer) -> None:
    """A certificate error during STARTTLS closes the plain connection."""
    server.starttls_error = ssl.SSLCertVerificationError("certificate verify failed")
    settings = ImapSettings(host="imap.gmx.net", port=143, security=Security.STARTTLS, username=EMAIL, password=PASSWORD)
    with pytest.raises(GmxTlsError):
        open_imap_connection(settings, ssl.create_default_context())
    assert server.clients[0].closed
    assert server.logins == 0


def test_connection_lost_during_login(server: FakeImapServer) -> None:
    """EOF during LOGIN is a connection error, not an auth error."""
    server.login_error = IMAPClientAbortError("socket error: EOF")
    with pytest.raises(GmxConnectionError):
        open_imap_connection(SETTINGS, ssl.create_default_context())
    assert server.clients[0].closed


def test_login_rejected(server: FakeImapServer) -> None:
    """A rejected login is an auth error (wrong password or IMAP disabled)."""
    settings = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password="wrong")
    with pytest.raises(GmxAuthError) as err:
        open_imap_connection(settings, ssl.create_default_context())
    assert "wrong" not in str(err.value)
    assert server.clients[0].closed


def test_check_imap_account_logs_out(server: FakeImapServer) -> None:
    """The config-flow check reads capabilities and folders and logs out."""
    info = check_imap_account(SETTINGS, ssl.create_default_context())
    assert info.has("UIDPLUS")
    assert info.append_limit == 29360128
    assert len(info.folders) == 6
    assert server.clients[0].logged_out


def test_check_imap_account_list_failure(server: FakeImapServer) -> None:
    """A failing LIST is a connection problem and closes the connection."""
    server.list_error = IMAPClientAbortError("socket error: EOF")
    with pytest.raises(GmxConnectionError):
        check_imap_account(SETTINGS, ssl.create_default_context())
    assert server.clients[0].closed


# --------------------------------------------------------------------------- worker


async def test_lazy_connect_and_reuse(server: FakeImapServer, clock: Clock) -> None:
    """Nothing happens before the first command; the connection is reused afterwards."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    assert not server.clients and not worker.connected

    assert await worker.async_run(_list, readonly=True) == 6
    assert await worker.async_run(_list, readonly=True) == 6
    assert len(server.clients) == 1
    assert limiter.in_use == 1
    assert worker.server_info is not None and worker.server_info.has("MOVE")


async def test_noop_after_idle_time(server: FakeImapServer, clock: Clock) -> None:
    """After more than 5 min without use a NOOP checks the connection first."""
    worker = make_worker(clock)
    await worker.async_connect()
    client = server.clients[0]
    clock.advance(299)
    await worker.async_run(_list, readonly=True)
    assert client.noops == 0
    clock.advance(301)
    await worker.async_run(_list, readonly=True)
    assert client.noops == 1
    assert len(server.clients) == 1


async def test_failed_noop_reconnects_before_the_command(server: FakeImapServer, clock: Clock) -> None:
    """A dead connection (GMX timeout) is replaced before a writing command is sent."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    await worker.async_connect()
    server.clients[0].closed = True  # server dropped it (* BYE timeout)
    clock.advance(31 * 60)
    calls: list[int] = []

    def write(client: ImapSession) -> str:
        calls.append(1)
        return "stored"

    assert await worker.async_run(write, readonly=False) == "stored"
    assert calls == [1]
    assert len(server.clients) == 2
    assert limiter.in_use == 1
    assert worker.reconnects == 1


async def test_read_is_repeated_once_after_connection_loss(server: FakeImapServer, clock: Clock) -> None:
    """Reading commands are retried exactly once on a fresh connection."""
    worker = make_worker(clock)
    attempts: list[int] = []

    def flaky_read(client: ImapSession) -> str:
        attempts.append(1)
        if len(attempts) == 1:
            raise IMAPClientAbortError("socket error: EOF")
        return "ok"

    assert await worker.async_run(flaky_read, readonly=True) == "ok"
    assert len(attempts) == 2
    assert len(server.clients) == 2
    assert server.clients[0].closed


async def test_read_fails_after_second_connection_loss(server: FakeImapServer, clock: Clock) -> None:
    """No endless retries."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)

    def always_broken(client: ImapSession) -> None:
        raise ConnectionResetError

    with pytest.raises(GmxConnectionError):
        await worker.async_run(always_broken, readonly=True)
    assert len(server.clients) == 2
    assert limiter.in_use == 0
    assert not worker.connected


async def test_write_is_never_repeated(server: FakeImapServer, clock: Clock) -> None:
    """A connection loss during a writing command yields 'outcome unknown'."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    attempts: list[int] = []

    def write(client: ImapSession) -> None:
        attempts.append(1)
        raise IMAPClientAbortError("command: UID MOVE => socket error: EOF")

    with pytest.raises(GmxOutcomeUnknownError):
        await worker.async_run(write, readonly=False)
    assert attempts == [1]
    assert limiter.in_use == 0


async def test_other_errors_pass_through_and_keep_the_connection(server: FakeImapServer, clock: Clock) -> None:
    """IMAP NO/BAD answers are not connection problems."""
    worker = make_worker(clock)

    def bad(client: ImapSession) -> None:
        raise IMAPClientError("SELECT command error: BAD")

    with pytest.raises(IMAPClientError):
        await worker.async_run(bad, readonly=True)
    assert worker.connected
    assert len(server.clients) == 1


async def test_auth_failure_stops_reconnects(server: FakeImapServer, clock: Clock) -> None:
    """A rejected login calls back once and blocks further attempts until reset."""
    server.password = "changed"
    called: list[int] = []
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter, on_auth_failed=lambda: called.append(1))

    with pytest.raises(GmxAuthError):
        await worker.async_connect()
    with pytest.raises(GmxAuthError):
        await worker.async_run(_list, readonly=True)
    assert called == [1]
    assert len(server.clients) == 1
    assert worker.auth_failed
    assert limiter.in_use == 0

    server.password = PASSWORD
    worker.reset_auth_failure()
    assert await worker.async_run(_list, readonly=True) == 6


async def test_backoff_after_connection_failure(server: FakeImapServer, clock: Clock) -> None:
    """After a failed connect, further attempts are refused until the backoff has passed."""
    server.connect_error = ConnectionRefusedError()
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    with pytest.raises(GmxConnectionError):
        await worker.async_connect()
    with pytest.raises(GmxBackoffError) as err:
        await worker.async_connect()
    assert err.value.retry_in == 5.0
    assert limiter.in_use == 0

    clock.advance(5)
    with pytest.raises(GmxConnectionError):
        await worker.async_connect()
    with pytest.raises(GmxBackoffError) as err:
        await worker.async_connect()
    assert err.value.retry_in == 10.0

    server.connect_error = None
    clock.advance(10)
    await worker.async_connect()
    clock.advance(1)
    server.clients[-1].closed = True
    # Backoff was reset by the successful connect: a later failure starts at 5 s again.
    server.connect_error = TimeoutError()
    clock.advance(6 * 60)
    with pytest.raises(GmxConnectionError):
        await worker.async_run(_list, readonly=True)
    with pytest.raises(GmxBackoffError) as err:
        await worker.async_connect()
    assert err.value.retry_in == 5.0


async def test_connection_limit_shared_with_other_users(server: FakeImapServer, clock: Clock) -> None:
    """The worker respects connections held by others (e.g. the IDLE watcher)."""
    limiter = ConnectionLimiter(limit=2)
    limiter.acquire()
    limiter.acquire()
    worker = make_worker(clock, limiter)
    with pytest.raises(GmxConnectionLimitError):
        await worker.async_connect()
    assert not server.clients


async def test_close_if_idle(server: FakeImapServer, clock: Clock) -> None:
    """The connection is logged out after 10 min without use and reopened on demand."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    await worker.async_close_if_idle()  # nothing open: no-op
    await worker.async_connect()
    clock.advance(9 * 60)
    await worker.async_close_if_idle()
    assert worker.connected
    clock.advance(61)
    await worker.async_close_if_idle()
    assert not worker.connected
    assert server.clients[0].logged_out
    assert limiter.in_use == 0
    await worker.async_run(_list, readonly=True)
    assert len(server.clients) == 2


async def test_failing_logout_still_closes_the_socket(server: FakeImapServer, clock: Clock) -> None:
    """If LOGOUT fails (connection already dead), the socket is shut down anyway."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    await worker.async_connect()
    server.logout_error = IMAPClientAbortError("socket error: EOF")
    await worker.async_shutdown()
    assert server.clients[0].closed
    assert not server.clients[0].logged_out
    assert limiter.in_use == 0


async def test_missing_capabilities_are_logged_once(server: FakeImapServer, clock: Clock, caplog: pytest.LogCaptureFixture) -> None:
    """Missing MOVE/UIDPLUS do not fail the connection but are reported once."""
    server.capabilities = (b"IMAP4REV1", b"IDLE")
    worker = make_worker(clock)
    info = await worker.async_connect()
    assert info.missing_recommended == ["MOVE", "UIDPLUS", "SPECIAL-USE", "UNSELECT"]
    server.clients[0].closed = True
    clock.advance(6 * 60)
    await worker.async_run(_list, readonly=True)
    assert caplog.text.count("IMAP server lacks MOVE, UIDPLUS, SPECIAL-USE, UNSELECT") == 1


async def test_shutdown(server: FakeImapServer, clock: Clock) -> None:
    """After shutdown the worker refuses commands and holds no connection."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    await worker.async_connect()
    await worker.async_shutdown()
    assert server.clients[0].logged_out
    assert limiter.in_use == 0
    with pytest.raises(GmxConnectionError):
        await worker.async_run(_list, readonly=True)


async def test_commands_are_serialised(server: FakeImapServer, clock: Clock) -> None:
    """The lock prevents interleaved commands on the single connection."""
    worker = make_worker(clock)
    active = 0
    peak = 0

    async def executor[T](target: Callable[..., T], *args: Any) -> T:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        try:
            return target(*args)
        finally:
            active -= 1

    worker._executor = executor
    await asyncio.gather(*(worker.async_run(_list, readonly=True) for _ in range(5)))
    assert peak == 1
    assert len(server.clients) == 1


async def test_cancelled_connect_closes_the_late_connection(server: FakeImapServer, clock: Clock) -> None:
    """If the caller is cancelled while the blocking connect runs, the connection is closed afterwards."""
    limiter = ConnectionLimiter()
    worker = make_worker(clock, limiter)
    release = asyncio.Event()

    async def slow_executor[T](target: Callable[..., T], *args: Any) -> T:
        await release.wait()
        return target(*args)

    worker._executor = slow_executor
    task = asyncio.create_task(worker.async_connect())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert limiter.in_use == 0
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(server.clients) == 1
    assert server.clients[0].closed
    assert not worker.connected
