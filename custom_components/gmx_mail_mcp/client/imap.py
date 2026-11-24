"""IMAP work connection (imapclient in the executor, serialised by an asyncio.Lock).

Rules implemented here (SPEC section 2, "Verbindungsmanagement"):

* lazy connect on first use, close after ``CLOSE_AFTER_IDLE_S`` without use;
* ``NOOP`` before a command if the last use is older than ``NOOP_AFTER_IDLE_S``
  (GMX drops idle connections after ~30 min);
* socket error / EOF / ``* BYE`` during a command: drop the connection; reading commands are
  repeated exactly once on a fresh connection, writing commands are never repeated and raise
  :class:`GmxOutcomeUnknownError`;
* exponential backoff (5 s .. 15 min, with jitter) after failed connection attempts;
* an authentication error stops all reconnects until reauthentication;
* at most ``MAX_IMAP_CONNECTIONS`` connections per account (shared with the IDLE watcher, M3).

This module does not import Home Assistant so that it can be unit-tested and type-checked strictly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import ssl
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from imapclient import IMAPClient, SocketTimeout
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError

from ..const import (
    BACKOFF_JITTER,
    BACKOFF_MAX_S,
    BACKOFF_MIN_S,
    CLOSE_AFTER_IDLE_S,
    COMMAND_TIMEOUT_S,
    CONNECT_TIMEOUT_S,
    MAX_IMAP_CONNECTIONS,
    NOOP_AFTER_IDLE_S,
    Security,
)
from .errors import (
    GmxAuthError,
    GmxBackoffError,
    GmxConnectionError,
    GmxConnectionLimitError,
    GmxMailError,
    GmxOutcomeUnknownError,
    GmxTlsError,
)
from .folders import FolderInfo, RawFolder, parse_folder_list

_LOGGER = logging.getLogger(__name__)

# Errors that mean "the connection is gone or unusable". IMAPClientAbortError is imaplib's ``abort``
# (raised on EOF and on an unexpected ``* BYE``); OSError covers socket errors, timeouts and TLS errors.
CONNECTION_ERRORS: tuple[type[BaseException], ...] = (IMAPClientAbortError, OSError, EOFError)


class ImapSession(Protocol):
    """The subset of :class:`imapclient.IMAPClient` used by this module."""

    def capabilities(self) -> tuple[bytes, ...]:
        """Return the server capabilities."""

    def list_folders(self, directory: str = "", pattern: str = "*") -> list[RawFolder]:
        """Return the folder list."""

    def noop(self) -> Any:
        """Send NOOP."""

    def logout(self) -> Any:
        """Log out and close the connection."""

    def shutdown(self) -> None:
        """Close the connection without logging out."""

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        """SELECT (or EXAMINE with ``readonly=True``) a folder."""

    def unselect_folder(self) -> Any:
        """UNSELECT the current folder (never expunges)."""

    def folder_status(self, folder: str, what: Sequence[str] | None = None) -> dict[bytes, Any]:
        """STATUS of a folder."""

    def search(self, criteria: Any = "ALL", charset: str | None = None) -> list[int]:
        """UID SEARCH."""

    def sort(self, sort_criteria: Any, criteria: Any = "ALL", charset: str = "UTF-8") -> list[int]:
        """UID SORT (RFC 5256)."""

    def fetch(self, messages: Sequence[int], data: Sequence[str], modifiers: Any = None) -> dict[int, dict[bytes, Any]]:
        """UID FETCH."""

    def add_flags(self, messages: Sequence[int], flags: Sequence[str], silent: bool = False) -> Any:
        """UID STORE +FLAGS."""

    def remove_flags(self, messages: Sequence[int], flags: Sequence[str], silent: bool = False) -> Any:
        """UID STORE -FLAGS."""

    def move(self, messages: Sequence[int], folder: str) -> Any:
        """UID MOVE (RFC 6851)."""

    def copy(self, messages: Sequence[int], folder: str) -> Any:
        """UID COPY."""

    def uid_expunge(self, messages: Sequence[int]) -> Any:
        """UID EXPUNGE of exactly these UIDs (UIDPLUS)."""

    def create_folder(self, folder: str) -> Any:
        """CREATE."""

    def rename_folder(self, old_name: str, new_name: str) -> Any:
        """RENAME."""

    def delete_folder(self, folder: str) -> Any:
        """DELETE (a folder)."""

    def append(self, folder: str, msg: bytes, flags: Sequence[str] = (), msg_time: Any = None) -> Any:
        """APPEND."""

    def thread(self, algorithm: str = "REFERENCES", criteria: Any = "ALL", charset: str = "UTF-8") -> Any:
        """UID THREAD (RFC 5256): nested tuples of UIDs."""

    def list_sub_folders(self, directory: str = "", pattern: str = "*") -> list[RawFolder]:
        """LSUB: the subscribed folders."""

    def subscribe_folder(self, folder: str) -> Any:
        """SUBSCRIBE."""

    def unsubscribe_folder(self, folder: str) -> Any:
        """UNSUBSCRIBE."""


class Executor(Protocol):
    """Runs a blocking callable outside the event loop (``hass.async_add_executor_job``)."""

    def __call__[T](self, target: Callable[..., T], *args: Any) -> Awaitable[T]:
        """Schedule ``target(*args)`` and return an awaitable for the result."""


type Connector = Callable[[ImapSettings, ssl.SSLContext], ImapSession]
type SslContextFactory = Callable[[], ssl.SSLContext]


@dataclass(frozen=True, slots=True)
class ImapSettings:
    """Connection parameters. The password is excluded from ``repr``."""

    host: str
    port: int
    security: Security
    username: str
    password: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """What the server told us after login."""

    capabilities: frozenset[str]
    folders: tuple[FolderInfo, ...]

    def has(self, capability: str) -> bool:
        """Return whether the server announced ``capability`` (case-insensitive)."""
        return capability.upper() in self.capabilities

    @property
    def append_limit(self) -> int | None:
        """Return the APPENDLIMIT in bytes (RFC 7889), if announced with a value."""
        for cap in self.capabilities:
            if cap.startswith("APPENDLIMIT="):
                value = cap.partition("=")[2]
                if value.isdigit():
                    return int(value)
        return None

    @property
    def missing_recommended(self) -> list[str]:
        """Return recommended capabilities that are missing (fallback paths are used instead)."""
        return [cap for cap in ("MOVE", "UIDPLUS", "SPECIAL-USE", "UNSELECT") if not self.has(cap)]


def parse_capabilities(raw: Sequence[bytes | str]) -> frozenset[str]:
    """Normalise a capability response to upper-case strings."""
    return frozenset((c.decode("ascii", "replace") if isinstance(c, bytes) else c).upper() for c in raw)


def tls_context_with_minimum(context: ssl.SSLContext) -> ssl.SSLContext:
    """Enforce certificate verification and TLS >= 1.2 on ``context`` (GMX-NOTES row 3).

    Must be called on a context owned by the caller (never on a shared, cached one).
    """
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise GmxTlsError("refusing an SSL context without certificate verification")
    context.minimum_version = max(context.minimum_version, ssl.TLSVersion.TLSv1_2)
    return context


def _safe_close(client: ImapSession, graceful: bool) -> None:
    """Close a connection, ignoring all errors (blocking)."""
    if graceful:
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - best effort, fall through to shutdown
            _LOGGER.debug("IMAP logout failed, shutting the socket down")
        else:
            return
    try:
        client.shutdown()
    except Exception:  # noqa: BLE001 - best effort
        _LOGGER.debug("IMAP shutdown failed")


def open_imap_connection(settings: ImapSettings, context: ssl.SSLContext) -> ImapSession:
    """Connect, secure (implicit TLS or STARTTLS) and log in (blocking).

    Never sends credentials over an unencrypted connection: if STARTTLS fails, the login is not attempted.
    """
    timeout = SocketTimeout(connect=CONNECT_TIMEOUT_S, read=COMMAND_TIMEOUT_S)
    client: IMAPClient | None = None
    try:
        if settings.security is Security.SSL:
            client = IMAPClient(settings.host, port=settings.port, ssl=True, ssl_context=context, timeout=timeout)
        else:
            client = IMAPClient(settings.host, port=settings.port, ssl=False, timeout=timeout)
            client.starttls(context)
    except ssl.SSLError as err:
        if client is not None:
            _safe_close(client, graceful=False)
        raise GmxTlsError(f"TLS failed: {type(err).__name__}") from err
    except IMAPClientError as err:
        # STARTTLS not offered or refused -> never fall back to plain text.
        if client is not None:
            _safe_close(client, graceful=False)
        if settings.security is Security.STARTTLS:
            raise GmxTlsError("STARTTLS not available") from err
        raise GmxConnectionError(f"connection failed: {type(err).__name__}") from err
    except OSError as err:
        if client is not None:
            _safe_close(client, graceful=False)
        raise GmxConnectionError(f"connection failed: {type(err).__name__}") from err

    assert client is not None
    # Keep server time zones (aware datetimes) instead of converting to naive local time.
    client.normalise_times = False
    session: ImapSession = client
    try:
        client.login(settings.username, settings.password)
    except LoginError as err:
        _safe_close(client, graceful=False)
        raise GmxAuthError("login rejected") from err
    except (*CONNECTION_ERRORS, IMAPClientError) as err:
        _safe_close(client, graceful=False)
        raise GmxConnectionError(f"login failed: {type(err).__name__}") from err
    return session


def probe_server(client: ImapSession) -> ServerInfo:
    """Read capabilities (after login) and the folder list (blocking)."""
    try:
        capabilities = parse_capabilities(client.capabilities())
        folders = tuple(parse_folder_list(client.list_folders()))
    except (*CONNECTION_ERRORS, IMAPClientError) as err:
        raise GmxConnectionError(f"probing the server failed: {type(err).__name__}") from err
    return ServerInfo(capabilities=capabilities, folders=folders)


def check_imap_account(settings: ImapSettings, context: ssl.SSLContext, connector: Connector = open_imap_connection) -> ServerInfo:
    """Log in, read CAPABILITY and LIST, log out (blocking; used by the config flow)."""
    client = connector(settings, context)
    try:
        return probe_server(client)
    finally:
        _safe_close(client, graceful=True)


class Backoff:
    """Exponential backoff with jitter for reconnect attempts."""

    def __init__(
        self,
        *,
        minimum: float = BACKOFF_MIN_S,
        maximum: float = BACKOFF_MAX_S,
        jitter: float = BACKOFF_JITTER,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Initialise; ``rng`` returns a float in [0, 1)."""
        self._minimum = minimum
        self._maximum = maximum
        self._jitter = jitter
        self._rng = rng
        self._clock = clock
        self.failures = 0
        self._not_before = 0.0

    def remaining(self) -> float:
        """Return seconds until the next attempt is allowed (0 if allowed now)."""
        remaining: float = max(0.0, self._not_before - self._clock())
        return remaining

    def record_failure(self) -> float:
        """Register a failed attempt and return the delay until the next one."""
        base = min(self._maximum, self._minimum * (2.0 ** min(self.failures, 30)))
        factor = 1.0 + self._jitter * (2.0 * self._rng() - 1.0)
        delay = min(self._maximum, max(self._minimum, base * factor))
        self.failures += 1
        self._not_before = self._clock() + delay
        return delay

    def reset(self) -> None:
        """Forget all failures."""
        self.failures = 0
        self._not_before = 0.0


class ConnectionLimiter:
    """Counts IMAP connections of one account. Only used from the event loop."""

    def __init__(self, limit: int = MAX_IMAP_CONNECTIONS) -> None:
        """Initialise with the maximum number of simultaneous connections."""
        self._limit = limit
        self._in_use = 0

    @property
    def in_use(self) -> int:
        """Return the number of connections currently held."""
        return self._in_use

    def acquire(self) -> None:
        """Reserve one connection or raise :class:`GmxConnectionLimitError`."""
        if self._in_use >= self._limit:
            raise GmxConnectionLimitError(f"at most {self._limit} IMAP connections per account")
        self._in_use += 1

    def release(self) -> None:
        """Give one connection back."""
        self._in_use = max(0, self._in_use - 1)


class ImapWorker:
    """The single work connection of an account."""

    def __init__(
        self,
        settings: ImapSettings,
        *,
        executor: Executor,
        ssl_context_factory: SslContextFactory,
        limiter: ConnectionLimiter,
        connector: Connector = open_imap_connection,
        backoff: Backoff | None = None,
        clock: Callable[[], float] = time.monotonic,
        on_auth_failed: Callable[[], None] | None = None,
    ) -> None:
        """Initialise; nothing is connected until the first command."""
        self._settings = settings
        self._executor = executor
        self._ssl_context_factory = ssl_context_factory
        self._ssl_context: ssl.SSLContext | None = None
        self._limiter = limiter
        self._connector = connector
        self._clock = clock
        self._backoff = backoff or Backoff(clock=clock)
        self.on_auth_failed = on_auth_failed
        self._lock = asyncio.Lock()
        self._client: ImapSession | None = None
        self._last_used = 0.0
        self._auth_failed = False
        self._closed = False
        self._background: set[asyncio.Future[None]] = set()
        self._warned_missing = False
        self.server_info: ServerInfo | None = None
        self.reconnects = 0

    @property
    def connected(self) -> bool:
        """Return whether a connection is currently open (it may still be dead on the server side)."""
        return self._client is not None

    @property
    def auth_failed(self) -> bool:
        """Return whether the last login was rejected."""
        return self._auth_failed

    async def async_connect(self) -> ServerInfo:
        """Make sure a connection exists and return the server info."""
        async with self._lock:
            self._check_usable()
            await self._ensure_connected()
            assert self.server_info is not None
            return self.server_info

    async def async_run[T](self, func: Callable[[ImapSession], T], *, readonly: bool) -> T:
        """Run ``func(client)`` in the executor on the work connection.

        ``readonly`` must only be ``True`` for commands without side effects on the server; only those
        are repeated after a connection loss.
        """
        async with self._lock:
            self._check_usable()
            client = await self._ensure_connected()
            try:
                result = await self._executor(func, client)
            except CONNECTION_ERRORS as err:
                _LOGGER.debug("IMAP connection lost during a command: %s", type(err).__name__)
                await self._drop()
                if not readonly:
                    raise GmxOutcomeUnknownError("connection lost during a writing command") from err
                self.reconnects += 1
                client = await self._ensure_connected()
                try:
                    result = await self._executor(func, client)
                except CONNECTION_ERRORS as err2:
                    await self._drop()
                    raise GmxConnectionError(f"connection lost again: {type(err2).__name__}") from err2
            self._last_used = self._clock()
            return result

    async def async_close_if_idle(self) -> None:
        """Close the connection after ``CLOSE_AFTER_IDLE_S`` without use (called periodically)."""
        if self._client is None or self._lock.locked():
            return
        if self._clock() - self._last_used < CLOSE_AFTER_IDLE_S:
            return
        async with self._lock:
            if self._client is not None and self._clock() - self._last_used >= CLOSE_AFTER_IDLE_S:
                _LOGGER.debug("Closing idle IMAP work connection")
                await self._drop(graceful=True)

    async def async_shutdown(self) -> None:
        """Log out and refuse all further commands."""
        async with self._lock:
            self._closed = True
            await self._drop(graceful=True)

    def reset_auth_failure(self) -> None:
        """Allow connections again (after reauthentication)."""
        self._auth_failed = False
        self._backoff.reset()

    # ------------------------------------------------------------------ internals (lock held)

    def _check_usable(self) -> None:
        if self._closed:
            raise GmxConnectionError("worker is shut down")
        if self._auth_failed:
            raise GmxAuthError("login was rejected, reauthentication required")

    async def _ensure_connected(self) -> ImapSession:
        if self._client is not None and self._clock() - self._last_used >= NOOP_AFTER_IDLE_S:
            try:
                await self._executor(self._client.noop)
            except (*CONNECTION_ERRORS, IMAPClientError) as err:
                _LOGGER.debug("NOOP after idle time failed (%s), reconnecting", type(err).__name__)
                await self._drop()
                self.reconnects += 1
            else:
                self._last_used = self._clock()
        if self._client is None:
            await self._connect()
        assert self._client is not None
        return self._client

    def _open_and_probe(self) -> tuple[ImapSession, ServerInfo]:
        """Blocking part of the connection set-up."""
        if self._ssl_context is None:
            self._ssl_context = tls_context_with_minimum(self._ssl_context_factory())
        client = self._connector(self._settings, self._ssl_context)
        try:
            info = probe_server(client)
        except GmxMailError:
            _safe_close(client, graceful=False)
            raise
        return client, info

    async def _connect(self) -> None:
        remaining = self._backoff.remaining()
        if remaining > 0:
            raise GmxBackoffError(remaining)
        self._limiter.acquire()
        job = asyncio.ensure_future(self._executor(self._open_and_probe))
        try:
            client, info = await asyncio.shield(job)
        except asyncio.CancelledError:
            # The blocking job keeps running; close whatever it opens so no connection is orphaned.
            job.add_done_callback(self._close_orphan)
            self._limiter.release()
            raise
        except GmxAuthError:
            self._limiter.release()
            self._auth_failed = True
            if self.on_auth_failed is not None:
                self.on_auth_failed()
            raise
        except Exception as err:
            self._limiter.release()
            delay = self._backoff.record_failure()
            _LOGGER.debug("IMAP connect failed (%s), next attempt in %.0f s", type(err).__name__, delay)
            raise
        self._backoff.reset()
        self._client = client
        self.server_info = info
        self._last_used = self._clock()
        missing = info.missing_recommended
        if missing and not self._warned_missing:
            self._warned_missing = True
            _LOGGER.warning("IMAP server lacks %s; fallback paths will be used", ", ".join(missing))

    def _close_orphan(self, job: asyncio.Future[tuple[ImapSession, ServerInfo]]) -> None:
        if job.cancelled() or job.exception() is not None:
            return
        client, _info = job.result()
        # Runs in the event loop: hand the blocking close to the executor without awaiting it.
        task = asyncio.ensure_future(self._executor(_safe_close, client, False))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _drop(self, *, graceful: bool = False) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        self._limiter.release()
        await self._executor(_safe_close, client, graceful)
