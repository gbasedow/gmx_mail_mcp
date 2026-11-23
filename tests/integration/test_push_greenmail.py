"""IDLE watcher and polling against a real IMAP server (GreenMail, see docker-compose.yml).

The watcher runs with the real aioimaplib client but over the plain test port (TLS is covered by unit tests);
GreenMail announces IDLE. Never a real mail account.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import ssl
import uuid
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from typing import Any

import pytest
from aioimaplib import IMAP4
from imapclient import IMAPClient
from imapclient.exceptions import IMAPClientAbortError

from custom_components.gmx_mail_mcp.client.idle import IdleWatcher
from custom_components.gmx_mail_mcp.client.imap import ConnectionLimiter, ImapSettings, probe_server
from custom_components.gmx_mail_mcp.client.reader import poll
from custom_components.gmx_mail_mcp.const import PushState, Security

pytestmark = pytest.mark.integration

HOST = os.environ.get("GREENMAIL_HOST", "127.0.0.1")
PORT = int(os.environ.get("GREENMAIL_IMAP_PORT", "3143"))
USER = "test@example.test"
PASSWORD = "secret"


def _reachable() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=2):
            return True
    except OSError:
        return False


@pytest.fixture
def session() -> Iterator[IMAPClient]:
    """Logged-in imapclient connection (used to deliver mail and for polling)."""
    if not _reachable():
        if os.environ.get("REQUIRE_GREENMAIL"):
            pytest.fail(f"GreenMail not reachable on {HOST}:{PORT} (REQUIRE_GREENMAIL is set)")
        pytest.skip(f"GreenMail not reachable on {HOST}:{PORT}")
    client = IMAPClient(HOST, port=PORT, ssl=False, timeout=15)
    client.login(USER, PASSWORD)
    client.normalise_times = False
    yield client
    client.logout()


@pytest.fixture
def folder(session: IMAPClient) -> Iterator[str]:
    """A fresh folder, removed afterwards over a separate connection.

    GreenMail 2.1.3 throws a NullPointerException (and drops the deleting connection) when a folder is deleted that
    a since-closed session had selected - here the IDLE connection. That is a server bug, not an integration bug.
    """
    name = f"IT-Push-{uuid.uuid4().hex[:8]}"
    session.create_folder(name)
    yield name
    cleaner = IMAPClient(HOST, port=PORT, ssl=False, timeout=15)
    try:
        cleaner.login(USER, PASSWORD)
        with contextlib.suppress(IMAPClientAbortError):
            cleaner.delete_folder(name)
    finally:
        with contextlib.suppress(IMAPClientAbortError, OSError):
            cleaner.logout()


def _deliver(session: IMAPClient, folder: str, subject: str) -> None:
    message = EmailMessage()
    message["From"] = "anna@example.org"
    message["To"] = USER
    message["Subject"] = subject
    message.set_content("Text")
    session.append(folder, message.as_bytes())


def _plain_factory(settings: ImapSettings, context: ssl.SSLContext, on_lost: Callable[[Exception | None], None]) -> Any:
    """Like create_idle_client, but without TLS for the GreenMail test port."""
    return IMAP4(host=settings.host, port=settings.port, timeout=10, conn_lost_cb=on_lost)


async def _wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


async def test_idle_reports_new_mail_also_after_renewal(session: IMAPClient, folder: str) -> None:
    """A delivered message wakes the watcher; after an IDLE renewal it still works."""
    changes: list[int] = []
    auth: list[int] = []

    async def _context() -> ssl.SSLContext:
        return ssl.create_default_context()

    limiter = ConnectionLimiter()
    watcher = IdleWatcher(
        ImapSettings(host=HOST, port=PORT, security=Security.SSL, username=USER, password=PASSWORD),
        ssl_context=_context,
        limiter=limiter,
        on_change=lambda: changes.append(1),
        on_auth_failed=lambda: auth.append(1),
        renew_seconds=2,  # renew quickly to exercise DONE + new IDLE
        folder=folder,
        client_factory=_plain_factory,
    )
    task = asyncio.create_task(watcher.async_run())
    try:
        await _wait_for(lambda: watcher.state is PushState.CONNECTED and len(changes) == 1)
        await asyncio.to_thread(_deliver, session, folder, "eins")
        await _wait_for(lambda: len(changes) >= 2)
        await asyncio.sleep(3)  # at least one renewal
        before = len(changes)
        await asyncio.to_thread(_deliver, session, folder, "zwei")
        await _wait_for(lambda: len(changes) > before)
        assert watcher.state is PushState.CONNECTED
        assert watcher.reconnects == 0
        assert limiter.in_use == 1
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert limiter.in_use == 0
    assert auth == []


async def test_idle_rejected_login(session: IMAPClient) -> None:
    """A wrong password ends the watcher with 'auth_failed' instead of a retry loop."""
    failures: list[int] = []

    async def _context() -> ssl.SSLContext:
        return ssl.create_default_context()

    watcher = IdleWatcher(
        ImapSettings(host=HOST, port=PORT, security=Security.SSL, username=USER, password="wrong"),
        ssl_context=_context,
        limiter=ConnectionLimiter(),
        on_change=lambda: None,
        on_auth_failed=lambda: failures.append(1),
        renew_seconds=60,
        client_factory=_plain_factory,
    )
    await asyncio.wait_for(watcher.async_run(), 15)
    assert watcher.state is PushState.AUTH_FAILED
    assert failures == [1]


def test_poll_baseline_and_new_messages(session: IMAPClient, folder: str) -> None:
    """UIDNEXT gives the baseline; a later message is reported with its reference."""
    info = probe_server(session)
    _deliver(session, folder, "alt")
    first = poll(session, info, inbox=folder, junk=None, last_uid=None, known_uidvalidity=None, max_events=20, include_headers=True)
    assert first.new_messages == [] and first.inbox_messages == 1 and first.last_uid >= 1
    _deliver(session, folder, "Neu ✓")
    second = poll(
        session,
        info,
        inbox=folder,
        junk=None,
        last_uid=first.last_uid,
        known_uidvalidity=first.uidvalidity,
        max_events=20,
        include_headers=True,
    )
    assert [m["subject"] for m in second.new_messages] == ["Neu ✓"]
    assert second.new_messages[0]["ref"]["uid"] > first.last_uid
    third = poll(
        session,
        info,
        inbox=folder,
        junk=None,
        last_uid=second.last_uid,
        known_uidvalidity=second.uidvalidity,
        max_events=20,
        include_headers=False,
    )
    assert third.new_messages == []
    assert third.unseen_inbox == 2
