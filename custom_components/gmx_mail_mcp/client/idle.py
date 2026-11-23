"""IDLE watcher: the second IMAP connection, waiting for changes in the inbox (SPEC section 2).

* aioimaplib (same pin as the core integration ``imap``); only implicit TLS - aioimaplib has no STARTTLS,
  so with STARTTLS the integration polls only;
* IDLE is renewed after ``renew_seconds`` (GMX drops idle connections after ~30 min, M0);
* ``* BYE`` (e.g. ``* BYE timeout``) or a lost connection is a normal reason to reconnect, not an error state;
* reconnects use the shared exponential backoff; a rejected login stops the watcher and reports it;
* the folder is opened with SELECT: aioimaplib 2.0.3 only enters the SELECTED state (required for IDLE) after
  ``select()``, not after ``examine()`` (found by the GreenMail test). The watcher still never changes anything:
  it only sends IDLE, DONE and LOGOUT - never CLOSE, STORE or EXPUNGE (LOGOUT does not expunge, RFC 3501).
  The only side effect of a read-write SELECT is that the server may clear ``\\Recent`` flags.

Every change notification (EXISTS, EXPUNGE, FETCH) only triggers ``on_change``; counting and new-mail detection
happen in the coordinator on the work connection.

This module does not import Home Assistant.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from aioimaplib import IMAP4, STOP_WAIT_SERVER_PUSH, AioImapException
from imapclient import imap_utf7

from ..const import CONNECT_TIMEOUT_S, IDLE_ACK_TIMEOUT_S, IDLE_PUSH_GRACE_S, PushState
from .errors import GmxAuthError, GmxConnectionError, GmxConnectionLimitError
from .imap import Backoff, ConnectionLimiter, ImapSettings

_LOGGER = logging.getLogger(__name__)

_CHANGE_WORDS = (b"EXISTS", b"EXPUNGE", b"FETCH", b"RECENT")


class IdleClient(Protocol):
    """The subset of :class:`aioimaplib.IMAP4` used here."""

    async def wait_hello_from_server(self) -> None:
        """Wait for the greeting."""

    async def login(self, user: str, password: str) -> Any:
        """LOGIN."""

    async def select(self, mailbox: str = "INBOX") -> Any:
        """SELECT (enters the SELECTED state needed for IDLE)."""

    async def idle_start(self, timeout: float = ...) -> asyncio.Future[Any]:
        """Start IDLE; after ``timeout`` the wait for pushes is stopped."""

    async def wait_server_push(self, timeout: float = ...) -> Any:
        """Return the next list of untagged lines (or the stop marker)."""

    def idle_done(self) -> None:
        """Send DONE."""

    def has_pending_idle(self) -> bool:
        """Return whether an IDLE command is pending."""

    async def logout(self) -> Any:
        """LOGOUT."""

    def has_capability(self, capability: str) -> bool:
        """Return whether the server announced a capability."""


type IdleClientFactory = Callable[[ImapSettings, ssl.SSLContext, Callable[[Exception | None], None]], IdleClient]


def create_idle_client(settings: ImapSettings, context: ssl.SSLContext, on_lost: Callable[[Exception | None], None]) -> IdleClient:
    """Create an implicit-TLS aioimaplib client that reports a lost connection.

    ``IMAP4_SSL`` does not pass ``conn_lost_cb`` on, so ``IMAP4`` is used with an SSL context instead.
    """
    client: IdleClient = IMAP4(host=settings.host, port=settings.port, timeout=CONNECT_TIMEOUT_S, conn_lost_cb=on_lost, ssl_context=context)
    return client


def mailbox_argument(name: str) -> str:
    """Return ``name`` as IMAP command argument: modified UTF-7 (RFC 3501 5.1.3), quoted.

    aioimaplib puts the mailbox into the command line unchanged; without this, names with umlauts
    (e.g. ``Entwürfe``) or spaces would be rejected by the server.
    """
    encoded: str = bytes(imap_utf7.encode(name)).decode("ascii")
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _said_bye(push_task: asyncio.Future[Any]) -> bool:
    """Return whether the push contained ``* BYE`` (the server closes the connection right after)."""
    if not push_task.done() or push_task.cancelled() or push_task.exception() is not None:
        return False
    push = push_task.result()
    return isinstance(push, list) and any(isinstance(line, bytes) and b"BYE" in line for line in push)


class _IdleUnsupportedError(Exception):
    """The server does not announce IDLE after login."""


def _result(response: Any) -> str:
    return str(getattr(response, "result", ""))


class IdleWatcher:
    """Keeps an IDLE connection on one folder and calls ``on_change`` when the server reports changes."""

    def __init__(
        self,
        settings: ImapSettings,
        *,
        ssl_context: Callable[[], Awaitable[ssl.SSLContext]],
        limiter: ConnectionLimiter,
        on_change: Callable[[], None],
        on_auth_failed: Callable[[], None],
        renew_seconds: float,
        folder: str = "INBOX",
        client_factory: IdleClientFactory | None = None,
        backoff: Backoff | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        on_state_change: Callable[[], None] | None = None,
    ) -> None:
        """Initialise; :meth:`async_run` does the work."""
        self._settings = settings
        self._ssl_context = ssl_context
        self._limiter = limiter
        self._on_change = on_change
        self._on_auth_failed = on_auth_failed
        self._renew_seconds = renew_seconds
        self._folder = folder
        self._client_factory = client_factory or create_idle_client
        self._backoff = backoff or Backoff()
        self._sleep = sleep
        self._client: IdleClient | None = None
        self._lost = asyncio.Event()
        self._holds_slot = False
        self._on_state_change = on_state_change
        self._state = PushState.CONNECTING
        self.reconnects = 0
        self.last_error: str | None = None

    @property
    def state(self) -> PushState:
        """Return the current push state."""
        return self._state

    @state.setter
    def state(self, value: PushState) -> None:
        changed = value is not self._state
        self._state = value
        if changed and self._on_state_change is not None:
            self._on_state_change()

    @property
    def connected(self) -> bool:
        """Return whether IDLE is active."""
        return self.state is PushState.CONNECTED

    async def async_run(self) -> None:
        """Run until cancelled or the login is rejected."""
        try:
            while True:
                remaining = self._backoff.remaining()
                if remaining > 0:
                    await self._sleep(remaining)
                try:
                    await self._connect()
                    self._backoff.reset()
                    self.state = PushState.CONNECTED
                    self.last_error = None
                    self._on_change()  # catch up with anything that arrived while disconnected
                    await self._idle_loop()
                except _IdleUnsupportedError:
                    self.state = PushState.UNSUPPORTED  # polling continues; no point in reconnecting
                    await self._cleanup()
                    return
                except GmxAuthError:
                    self.state = PushState.AUTH_FAILED
                    self.last_error = "auth_failed"
                    await self._cleanup()
                    self._on_auth_failed()
                    return
                except (GmxConnectionError, GmxConnectionLimitError, AioImapException, OSError, TimeoutError) as err:
                    self.last_error = type(err).__name__
                    self.state = PushState.RECONNECTING
                    self.reconnects += 1
                    delay = self._backoff.record_failure()
                    _LOGGER.debug("IDLE connection ended (%s), reconnecting in %.0f s", type(err).__name__, delay)
                    await self._cleanup()
        finally:
            await self._cleanup()
            if self.state not in (PushState.AUTH_FAILED, PushState.UNSUPPORTED):
                self.state = PushState.STOPPED

    async def _connect(self) -> None:
        self.state = PushState.CONNECTING if self.reconnects == 0 else PushState.RECONNECTING
        context = await self._ssl_context()
        self._limiter.acquire()
        self._holds_slot = True
        lost = asyncio.Event()
        self._lost = lost

        def _on_lost(_exc: Exception | None) -> None:
            # Called by the protocol in the event loop; wakes the IDLE wait instead of waiting for the renewal timer.
            lost.set()

        client = self._client_factory(self._settings, context, _on_lost)
        self._client = client
        await client.wait_hello_from_server()
        response = await client.login(self._settings.username, self._settings.password)
        if _result(response) != "OK":
            raise GmxAuthError("IDLE login rejected")
        if not client.has_capability("IDLE"):
            raise _IdleUnsupportedError("server does not offer IDLE")
        response = await client.select(mailbox_argument(self._folder))
        if _result(response) != "OK":
            raise GmxConnectionError("folder cannot be selected")

    async def _idle_loop(self) -> None:
        client = self._client
        assert client is not None
        while True:
            idle = await client.idle_start(timeout=self._renew_seconds)
            push_task = asyncio.ensure_future(client.wait_server_push(timeout=self._renew_seconds + IDLE_PUSH_GRACE_S))
            lost_task = asyncio.ensure_future(self._lost.wait())
            try:
                await asyncio.wait({push_task, lost_task}, return_when=asyncio.FIRST_COMPLETED)
                if not push_task.done():
                    raise GmxConnectionError("connection lost")
                push = push_task.result()  # raises TimeoutError if the server stayed silent beyond the grace time
            finally:
                for task in (push_task, lost_task):
                    if not task.done():
                        task.cancel()
                if self._lost.is_set() or _said_bye(push_task):
                    # The socket is gone or closing (* BYE): no DONE, no waiting for an acknowledgement that cannot come.
                    self._lost.set()
                    idle.cancel()
                else:
                    if client.has_pending_idle():
                        client.idle_done()
                    with contextlib.suppress(AioImapException, TimeoutError, asyncio.CancelledError):
                        async with asyncio.timeout(IDLE_ACK_TIMEOUT_S):
                            await idle
            if push == STOP_WAIT_SERVER_PUSH:
                continue  # renewal: DONE was sent, start a new IDLE
            lines = [line for line in push if isinstance(line, bytes)]
            if any(b"BYE" in line for line in lines):
                raise GmxConnectionError("server sent BYE")  # e.g. "* BYE timeout" after ~30 min at GMX
            if any(word in line for line in lines for word in _CHANGE_WORDS):
                self._on_change()

    async def _cleanup(self) -> None:
        client, self._client = self._client, None
        if client is not None and not self._lost.is_set():  # LOGOUT only on a living connection
            with contextlib.suppress(AioImapException, OSError, TimeoutError, asyncio.CancelledError):
                async with asyncio.timeout(IDLE_ACK_TIMEOUT_S):
                    await client.logout()
        if self._holds_slot:
            self._holds_slot = False
            self._limiter.release()
