"""Production code paths against a GMX-like Dovecot (TLS with a test CA, GMX capabilities and folder names).

Start: ``python3 tools/make_test_certs.py && docker compose up -d dovecot``, then
``pytest -p no:homeassistant -m integration tests/integration``. Skipped if the server is not reachable
(fails instead with ``REQUIRE_DOVECOT=1``, as in CI). Never a real mail account.

Unlike the GreenMail tests, these use the real connection code: implicit TLS with certificate verification,
``open_imap_connection``, the ImapWorker and the IDLE watcher with the real aioimaplib TLS client.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import ssl
import subprocess
import time
import uuid
from collections.abc import Callable, Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from imapclient import IMAPClient

from custom_components.gmx_mail_mcp.client.errors import GmxAuthError, GmxTlsError
from custom_components.gmx_mail_mcp.client.folders import map_special_folders
from custom_components.gmx_mail_mcp.client.idle import IdleWatcher
from custom_components.gmx_mail_mcp.client.imap import (
    ConnectionLimiter,
    ImapSettings,
    ImapWorker,
    ServerInfo,
    check_imap_account,
    open_imap_connection,
    tls_context_with_minimum,
)
from custom_components.gmx_mail_mcp.client.reader import MailRef, SearchQuery, get_message, list_folders, poll, search
from custom_components.gmx_mail_mcp.client.smtp import SmtpSettings, check_smtp_login
from custom_components.gmx_mail_mcp.const import FolderRole, PushState, Security

pytestmark = pytest.mark.integration

HOST = os.environ.get("DOVECOT_HOST", "127.0.0.1")
IMAPS_PORT = int(os.environ.get("DOVECOT_IMAPS_PORT", "31993"))
SUBMISSION_PORT = int(os.environ.get("DOVECOT_SUBMISSION_PORT", "31587"))
USER = "user@gmx.test"
PASSWORD = "secret"
CA_FILE = Path(__file__).parent / "certs" / "ca.crt"
SETTINGS = ImapSettings(host=HOST, port=IMAPS_PORT, security=Security.SSL, username=USER, password=PASSWORD)


def _reachable() -> bool:
    try:
        with socket.create_connection((HOST, IMAPS_PORT), timeout=2):
            return CA_FILE.exists()
    except OSError:
        return False


def _context() -> ssl.SSLContext:
    return tls_context_with_minimum(ssl.create_default_context(cafile=str(CA_FILE)))


@pytest.fixture(autouse=True)
def _require_server() -> None:
    if not _reachable():
        if os.environ.get("REQUIRE_DOVECOT"):
            pytest.fail(f"Dovecot not reachable on {HOST}:{IMAPS_PORT} or test CA missing (REQUIRE_DOVECOT is set)")
        pytest.skip(f"Dovecot not reachable on {HOST}:{IMAPS_PORT} (or tools/make_test_certs.py not run)")


@pytest.fixture
def mailer() -> Iterator[IMAPClient]:
    """A TLS connection used by the tests to deliver and inspect mail."""
    client = IMAPClient(HOST, port=IMAPS_PORT, ssl=True, ssl_context=_context(), timeout=15)
    client.login(USER, PASSWORD)
    client.normalise_times = False
    yield client
    client.logout()


@pytest.fixture
def folder(mailer: IMAPClient) -> Iterator[str]:
    """A fresh folder (umlaut name, modified UTF-7 on the wire), removed afterwards."""
    name = f"IT-Prüfung-{uuid.uuid4().hex[:8]}"
    mailer.create_folder(name)
    yield name
    mailer.delete_folder(name)


def _deliver(client: IMAPClient, folder: str, subject: str, flags: tuple[bytes, ...] = ()) -> None:
    message = EmailMessage()
    message["From"] = "Jörg Müller <joerg@example.org>"
    message["To"] = USER
    message["Subject"] = subject
    message.set_content("Hallo,\nText mit Umlauten: äöü\n")
    client.append(folder, message.as_bytes(), flags=flags)


def test_tls_capabilities_and_special_folders() -> None:
    """GMX capabilities and SPECIAL-USE folders (Gesendet, Entwürfe, Gelöscht, Spamverdacht) over verified TLS."""
    info = check_imap_account(SETTINGS, _context())
    assert info.missing_recommended == []
    assert info.append_limit == 29360128
    for absent in ("CONDSTORE", "QRESYNC", "QUOTA", "ESEARCH", "UTF8=ACCEPT"):
        assert absent not in info.capabilities
    folder_map = map_special_folders(info.folders)
    assert {role: folder_map.get(role) for role in FolderRole} == {
        FolderRole.INBOX: "INBOX",
        FolderRole.SENT: "Gesendet",
        FolderRole.DRAFTS: "Entwürfe",
        FolderRole.TRASH: "Gelöscht",
        FolderRole.JUNK: "Spamverdacht",
    }
    assert not folder_map.ambiguous


def test_tls_and_login_failures_are_classified() -> None:
    """Wrong password -> auth error; unknown CA -> TLS error (verification is never switched off)."""
    wrong = ImapSettings(host=HOST, port=IMAPS_PORT, security=Security.SSL, username=USER, password="wrong")
    with pytest.raises(GmxAuthError):
        check_imap_account(wrong, _context())
    with pytest.raises(GmxTlsError):
        check_imap_account(SETTINGS, tls_context_with_minimum(ssl.create_default_context()))


def test_smtp_login_with_starttls() -> None:
    """The config-flow SMTP check works with STARTTLS (submission port)."""
    smtp = SmtpSettings(host=HOST, port=SUBMISSION_PORT, security=Security.STARTTLS, username=USER, password=PASSWORD)
    check_smtp_login(smtp, _context())


async def _executor[T](target: Callable[..., T], *args: Any) -> T:
    return await asyncio.get_running_loop().run_in_executor(None, target, *args)


async def test_worker_and_reader_with_unselect(mailer: IMAPClient, folder: str) -> None:
    """ImapWorker (real TLS connect) runs the read path; UNSELECT is available here (not with GreenMail)."""
    await asyncio.to_thread(_deliver, mailer, folder, "Rechnung Größe")
    await asyncio.to_thread(_deliver, mailer, folder, "Gelesen", (b"\\Seen",))
    worker = ImapWorker(SETTINGS, executor=_executor, ssl_context_factory=_context, limiter=ConnectionLimiter())
    try:
        info = await worker.async_connect()
        folders = await worker.async_run(
            lambda s: list_folders(s, info, map_special_folders(info.folders), include_counts=True), readonly=True
        )
        entry = next(f for f in folders if f["path"] == folder)
        assert entry["messages"] == 2 and entry["unseen"] == 1
        result = await worker.async_run(
            lambda s: search(s, info, folder, SearchQuery(subject="Größe"), order="date_desc", limit=10, offset=0, snippet=True),
            readonly=True,
        )
        assert [m["subject"] for m in result.messages] == ["Rechnung Größe"]
        assert result.messages[0]["snippet"].startswith("Hallo, Text mit Umlauten: äöü")
        ref = MailRef(folder, result.messages[0]["ref"]["uid"], result.uidvalidity)
        content = await worker.async_run(lambda s: get_message(s, info, ref, body_format="text"), readonly=True)
        assert content.text == "Hallo,\nText mit Umlauten: äöü"
        # RFC 3501: "1000:*" also matches the last message when no UID reaches 1000 - the server does so ...
        mailer.select_folder(folder, readonly=True)
        assert mailer.search(["UID", "1000:*"]) != []
        # ... the integration drops it (found live against GMX, version 0.9.2)
        beyond = await worker.async_run(
            lambda s: search(s, info, folder, SearchQuery(uid_range="1000:*"), order="date_desc", limit=10, offset=0, snippet=False),
            readonly=True,
        )
        assert beyond.total == 0 and beyond.messages == []
    finally:
        await worker.async_shutdown()
    mailer.select_folder(folder, readonly=True)
    assert b"\\Seen" not in mailer.get_flags([ref.uid])[ref.uid]


async def test_idle_watcher_over_tls(mailer: IMAPClient, folder: str) -> None:
    """The IDLE watcher with the real aioimaplib TLS client is notified about new mail, also after a renewal."""
    changes: list[float] = []
    loop = asyncio.get_running_loop()

    async def _ssl() -> ssl.SSLContext:
        return await loop.run_in_executor(None, _context)

    watcher = IdleWatcher(
        SETTINGS,
        ssl_context=_ssl,
        limiter=ConnectionLimiter(),
        on_change=lambda: changes.append(time.monotonic()),
        on_auth_failed=lambda: None,
        renew_seconds=2,
        folder=folder,
    )
    task = asyncio.create_task(watcher.async_run())
    try:
        await _wait_for(lambda: watcher.state is PushState.CONNECTED and len(changes) == 1)
        await asyncio.to_thread(_deliver, mailer, folder, "eins")
        await _wait_for(lambda: len(changes) >= 2)
        await asyncio.sleep(3)  # at least one renewal
        before = len(changes)
        await asyncio.to_thread(_deliver, mailer, folder, "zwei")
        await _wait_for(lambda: len(changes) > before)
        assert watcher.reconnects == 0
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_poll_with_real_uidnext(mailer: IMAPClient, folder: str) -> None:
    """Baseline from UIDNEXT, then exactly the new message is reported."""
    info = check_imap_account(SETTINGS, _context())
    _deliver(mailer, folder, "alt")
    first = poll(
        mailer, info, inbox=folder, junk="Spamverdacht", last_uid=None, known_uidvalidity=None, max_events=20, include_headers=False
    )
    assert first.new_messages == [] and first.junk_messages is not None
    _deliver(mailer, folder, "neu")
    second = poll(
        mailer,
        info,
        inbox=folder,
        junk="Spamverdacht",
        last_uid=first.last_uid,
        known_uidvalidity=first.uidvalidity,
        max_events=20,
        include_headers=True,
    )
    assert [m["subject"] for m in second.new_messages] == ["neu"]


async def _wait_for(condition: Callable[[], bool], seconds: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.05)


# --------------------------------------------------------------------------- M4: organising against the real server


def test_move_copy_flags_and_fallback(mailer: IMAPClient, folder: str) -> None:
    """MOVE/COPY return new references from the real server; the no-MOVE fallback leaves foreign \\Deleted mails alone."""
    from custom_components.gmx_mail_mcp.client.writer import copy_or_move, create_folder, set_flags  # noqa: PLC0415

    info = check_imap_account(SETTINGS, _context())
    folder_map = map_special_folders(info.folders)
    session = open_imap_connection(SETTINGS, _context())
    try:
        target = create_folder(session, info, folder_map, "Ziel ä", folder)
        for subject in ("eins", "zwei", "drei", "fremd gelöscht"):
            _deliver(mailer, folder, subject)
        mailer.select_folder(folder, readonly=True)
        validity = mailer.folder_status(folder, ["UIDVALIDITY"])[b"UIDVALIDITY"]
        uids = sorted(mailer.search(["ALL"]))
        mailer.unselect_folder()
        refs = [MailRef(folder, uid, validity) for uid in uids]
        # Another client marked the 4th message \Deleted but did not expunge it.
        mailer.select_folder(folder)
        mailer.add_flags([uids[3]], [b"\\Deleted"])
        mailer.unselect_folder()

        assert set_flags(session, info, refs[:1], ["flagged", "$Steuer"], []).count == 1
        copied = copy_or_move(session, info, refs[:1], target, move=False)
        moved = copy_or_move(session, info, refs[1:2], target, move=True)
        no_move = ServerInfo(capabilities=info.capabilities - {"MOVE"}, folders=info.folders)
        fallback = copy_or_move(session, no_move, refs[2:3], target, move=True)
        for result in (copied, moved, fallback):
            assert result.count == 1 and result.warnings == [] and result.new_refs[0] is not None

        mailer.select_folder(folder, readonly=True)
        remaining = sorted(mailer.search(["ALL"]))
        flags = mailer.get_flags(remaining)
        mailer.unselect_folder()
        assert remaining == [uids[0], uids[3]]  # moved ones gone, the foreign \Deleted message still there
        assert b"\\Deleted" in flags[uids[3]]
        mailer.select_folder(target, readonly=True)
        subjects = {
            m[b"BODY[HEADER.FIELDS (SUBJECT)]"].decode().strip()
            for m in mailer.fetch(mailer.search(["ALL"]), ["BODY.PEEK[HEADER.FIELDS (SUBJECT)]"]).values()
        }
        mailer.unselect_folder()
        assert subjects == {"Subject: eins", "Subject: zwei", "Subject: drei"}
    finally:
        session.logout()
        with contextlib.suppress(Exception):
            mailer.delete_folder(f"{folder}/Ziel ä")


def _subjects(client: IMAPClient, folder: str) -> dict[int, str]:
    client.select_folder(folder, readonly=True)
    uids = client.search(["ALL"])
    data = client.fetch(uids, ["BODY.PEEK[HEADER.FIELDS (SUBJECT)]"]) if uids else {}
    client.unselect_folder()
    return {uid: item[b"BODY[HEADER.FIELDS (SUBJECT)]"].decode().strip().removeprefix("Subject: ") for uid, item in data.items()}


def test_trash_delete_permanently_and_delete_folder(mailer: IMAPClient, folder: str) -> None:
    """M5 against the GMX-like server: trash, permanent deletion of exactly these UIDs (a foreign \\Deleted message
    in the trash survives), and the folder rules."""
    from custom_components.gmx_mail_mcp.client.deleter import (  # noqa: PLC0415
        check_delete_folder,
        delete_folder,
        delete_permanently,
        preview_permanent,
        trash,
    )
    from custom_components.gmx_mail_mcp.client.errors import GmxDeleteRuleError, GmxProtectedFolderError  # noqa: PLC0415
    from custom_components.gmx_mail_mcp.client.writer import create_folder  # noqa: PLC0415

    marker = uuid.uuid4().hex[:8]
    info = check_imap_account(SETTINGS, _context())
    folder_map = map_special_folders(info.folders)
    session = open_imap_connection(SETTINGS, _context())
    try:
        for subject in (f"weg-1-{marker}", f"weg-2-{marker}"):
            _deliver(mailer, folder, subject)
        _deliver(mailer, "Gelöscht", f"fremd-{marker}", flags=(b"\\Deleted",))  # marked by another client
        mailer.select_folder(folder, readonly=True)
        validity = mailer.folder_status(folder, ["UIDVALIDITY"])[b"UIDVALIDITY"]
        refs = [MailRef(folder, uid, validity) for uid in sorted(mailer.search(["ALL"]))]
        mailer.unselect_folder()

        target, moved = trash(session, info, folder_map, refs)
        assert target == "Gelöscht" and moved.count == 2 and None not in moved.new_refs
        in_trash = [MailRef(**r) for r in moved.new_refs if r]
        with pytest.raises(GmxDeleteRuleError):
            trash(session, info, folder_map, in_trash)

        preview = preview_permanent(session, info, folder_map, in_trash)
        assert preview["count"] == 2 and {m["subject"] for m in preview["messages"]} == {f"weg-1-{marker}", f"weg-2-{marker}"}
        result = delete_permanently(session, info, folder_map, in_trash)
        assert result.count == 2 and result.warnings == []
        left = {subject for subject in _subjects(mailer, "Gelöscht").values() if marker in subject}
        assert left == {f"fremd-{marker}"}  # only our two are gone

        child = create_folder(session, info, folder_map, "Unter ü", folder)
        with pytest.raises(GmxDeleteRuleError, match="folder_has_children"):
            check_delete_folder(session, folder_map, folder)
        _deliver(mailer, child, "drin")
        with pytest.raises(GmxDeleteRuleError, match="folder_not_empty"):
            delete_folder(session, folder_map, child)
        mailer.select_folder(child)
        mailer.add_flags(mailer.search(["ALL"]), [b"\\Deleted"])
        mailer.uid_expunge(mailer.search(["DELETED"]))
        mailer.unselect_folder()
        assert delete_folder(session, folder_map, child) == child
        for protected in ("INBOX", "Gelöscht", "Spamverdacht"):
            with pytest.raises(GmxProtectedFolderError):
                check_delete_folder(session, folder_map, protected)
    finally:
        session.logout()
        own = [uid for uid, subject in _subjects(mailer, "Gelöscht").items() if marker in subject]
        if own:
            mailer.select_folder("Gelöscht")
            mailer.add_flags(own, [b"\\Deleted"])
            mailer.uid_expunge(own)
            mailer.unselect_folder()
        with contextlib.suppress(Exception):
            mailer.delete_folder(f"{folder}/Unter ü")


def test_send_over_submission_with_sent_copy(mailer: IMAPClient) -> None:
    """M6 against the GMX-like server: SMTP with STARTTLS and certificate check, Bcc only in the envelope,
    then the Sent copy via APPEND (GMX stores none itself). Delivery is checked in GreenMail (Dovecot relays there)."""
    import imaplib  # noqa: PLC0415

    from custom_components.gmx_mail_mcp.client.composer import new_message  # noqa: PLC0415
    from custom_components.gmx_mail_mcp.client.sent import store_sent_copy  # noqa: PLC0415
    from custom_components.gmx_mail_mcp.client.smtp import send_message  # noqa: PLC0415

    marker = uuid.uuid4().hex[:8]
    outgoing = new_message(
        sender=USER,
        sender_name="Haus Test",
        to=["test@example.test"],
        cc=[],
        bcc=["test@example.test"],
        subject=f"Versand {marker}",
        body_text="Grüße",
        body_html=None,
        attachments=[],
    )
    smtp = SmtpSettings(host=HOST, port=SUBMISSION_PORT, security=Security.STARTTLS, username=USER, password=PASSWORD)
    send_message(smtp, _context(), USER, outgoing.recipients, outgoing.wire_bytes)
    message_id = str(outgoing.message["Message-ID"])

    info = check_imap_account(SETTINGS, _context())
    session = open_imap_connection(SETTINGS, _context())
    try:
        assert store_sent_copy(
            session, info, map_special_folders(info.folders), outgoing.copy_bytes, message_id=message_id, mode="append"
        ) == ("appended")
    finally:
        session.logout()
    copies = [uid for uid, subject in _subjects(mailer, "Gesendet").items() if marker in subject]
    assert len(copies) == 1

    delivered = b""
    greenmail = imaplib.IMAP4(HOST, int(os.environ.get("GREENMAIL_IMAP_PORT", "3143")))
    try:
        greenmail.login("test@example.test", "secret")
        greenmail.select("INBOX")
        for _ in range(20):
            _typ, found = greenmail.search(None, "HEADER", "Message-ID", message_id.strip("<>"))
            if found and found[0]:
                _typ, data = greenmail.fetch(found[0].split()[-1], "(BODY.PEEK[HEADER])")
                delivered = bytes(data[0][1]) if data and isinstance(data[0], tuple) else b""
                for num in found[0].split():
                    greenmail.store(num, "+FLAGS", "\\Deleted")
                greenmail.expunge()  # GreenMail test mailbox only
                break
            time.sleep(0.5)
    finally:
        greenmail.logout()
    assert f"Versand {marker}".encode() in delivered and b"Bcc" not in delivered and b"Haus Test" in delivered
    mailer.select_folder("Gesendet")
    mailer.add_flags(copies, [b"\\Deleted"])
    mailer.uid_expunge(copies)
    mailer.unselect_folder()


def test_draft_lands_in_entwuerfe(mailer: IMAPClient) -> None:
    """save_draft appends to Entwürfe with \\Draft and \\Seen and returns the reference (APPENDUID)."""
    from custom_components.gmx_mail_mcp.client.writer import DraftInput, save_draft  # noqa: PLC0415

    info = check_imap_account(SETTINGS, _context())
    session = open_imap_connection(SETTINGS, _context())
    try:
        subject = f"Entwurf {uuid.uuid4().hex[:6]}"
        ref, _size = save_draft(
            session, info, map_special_folders(info.folders), DraftInput(sender=USER, to=["a@example.org"], subject=subject, body_text="x")
        )
    finally:
        session.logout()
    assert ref is not None and ref["folder"] == "Entwürfe"
    mailer.select_folder("Entwürfe")
    try:
        flags = mailer.get_flags([ref["uid"]])[ref["uid"]]
        assert b"\\Draft" in flags and b"\\Seen" in flags
        mailer.add_flags([ref["uid"]], [b"\\Deleted"])
        mailer.uid_expunge([ref["uid"]])  # clean up exactly this draft
    finally:
        mailer.unselect_folder()


def _kick() -> bool:
    """Disconnect all sessions of the test user server-side (like GMX's '* BYE timeout')."""
    root = Path(__file__).resolve().parent.parent.parent
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "dovecot", "doveadm", "kick", USER], cwd=root, capture_output=True, timeout=30, check=False
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _fresh() -> IMAPClient:
    client = IMAPClient(HOST, port=IMAPS_PORT, ssl=True, ssl_context=_context(), timeout=15)
    client.login(USER, PASSWORD)
    return client


async def test_idle_reconnects_after_server_disconnect() -> None:
    """A server-side disconnect (* BYE, like GMX after ~30 min) leads to a quick reconnect; push works again.

    ``doveadm kick`` disconnects *all* sessions of the user, so this test uses its own short-lived connections.
    """
    folder = f"IT-Kick-{uuid.uuid4().hex[:8]}"
    setup = await asyncio.to_thread(_fresh)
    setup.create_folder(folder)
    setup.logout()
    changes: list[float] = []
    loop = asyncio.get_running_loop()

    async def _ssl() -> ssl.SSLContext:
        return await loop.run_in_executor(None, _context)

    watcher = IdleWatcher(
        SETTINGS,
        ssl_context=_ssl,
        limiter=ConnectionLimiter(),
        on_change=lambda: changes.append(time.monotonic()),
        on_auth_failed=lambda: None,
        renew_seconds=60,
        folder=folder,
    )
    task = asyncio.create_task(watcher.async_run())
    try:
        await _wait_for(lambda: watcher.state is PushState.CONNECTED)
        started = time.monotonic()
        if not await asyncio.to_thread(_kick):
            pytest.skip("docker compose exec not available (needs the compose project)")
        await _wait_for(lambda: watcher.reconnects >= 1 and watcher.state is PushState.CONNECTED, 20)
        assert time.monotonic() - started < 9  # 5 s backoff + reconnect, no waiting for answers of a dead connection
        before = len(changes)

        def _deliver_fresh() -> None:
            client = _fresh()
            _deliver(client, folder, "nach dem Reconnect")
            client.logout()

        await asyncio.to_thread(_deliver_fresh)
        await _wait_for(lambda: len(changes) > before)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cleaner = await asyncio.to_thread(_fresh)
        cleaner.delete_folder(folder)
        cleaner.logout()


def _live_check(*extra: str, kick_after: float | None = None) -> dict[str, Any]:
    """Run tools/gmx_live_check.py against Dovecot; optionally disconnect all sessions server-side after ``kick_after`` s."""
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415

    root = Path(__file__).parent.parent.parent
    env = {**os.environ, "GMX_USER": USER, "GMX_PASS": PASSWORD}
    args = ["--host", HOST, "--port", str(IMAPS_PORT), "--cafile", str(CA_FILE), *extra]
    process = subprocess.Popen(
        [sys.executable, str(root / "tools" / "gmx_live_check.py"), *args], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if kick_after is not None:
        time.sleep(kick_after)
        assert _kick(), "doveadm kick failed"
    stdout, stderr = process.communicate(timeout=120)
    assert process.returncode == 0, stderr.decode()[-2000:]
    assert USER not in stdout.decode() and PASSWORD not in stdout.decode()  # metadata only
    report: dict[str, Any] = json.loads(stdout)
    return report


def _no_live_check_leftovers() -> None:
    client = _fresh()
    try:
        assert not [f for f in client.list_folders() if f[2].startswith("HA-Live")]
        for folder in ("INBOX", "Entwürfe"):
            client.select_folder(folder, readonly=True)
            uids = client.search(["ALL"])
            headers = client.fetch(uids, ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)]"]) if uids else {}
            text = b"".join(v for item in headers.values() for k, v in item.items() if k.startswith(b"BODY"))
            assert b"gmx-mail-live-check" not in text and b"gmx_mail live check draft" not in text
            client.unselect_folder()
    finally:
        client.logout()


def test_live_check_tool_survives_a_dropped_connection() -> None:
    """The manual GMX check works end to end, and a server-side disconnect during the IDLE wait (GMX drops idle
    connections after ~30 min) neither crashes it nor leaves test data behind (found in the 35-minute run on 21.11.2026)."""
    report = _live_check("--idle-minutes", "0.4", kick_after=12)
    assert "aborted" not in report
    assert report["partial_fetch"] == "parsed"
    assert report["recent_after_select"]["cleared_by_select"]  # Dovecot: SELECT takes \Recent away from others
    assert report["copy"]["new_refs_known"] and report["move"]["via_copyuid"]
    assert report["draft"] == {"ref_known": True, "size": report["draft"]["size"], "folder_role_ok": True}
    assert report["idle"]["connected"] and report["idle"]["push_delay_s"] is not None
    assert report["delete"] == {"trashed": 1, "trash_ref_known": True, "preview_count": 1, "deleted": 1, "warnings": [], "gone": True}
    assert report["idle"]["reconnects"] >= 1  # the kick was noticed and the watcher reconnected
    assert report["cleanup"] == {
        "ok": True,
        "errors": [],
        "folders_deleted": 2,
        "inbox_deleted": 1,
        "trash_deleted": 0,
        "sent_deleted": 0,
        "drafts_deleted": 1,
    }
    _no_live_check_leftovers()


def test_live_check_send_self_test() -> None:
    """--send-self-test: one message to the own address over the TLS-verified submission port, Sent copy, clean-up.
    (Dovecot relays to GreenMail, so the message does not come back into this INBOX - arrival stays None here.)"""
    report = _live_check(
        "--idle-minutes", "0.1", "--send-self-test", "--smtp-host", HOST, "--smtp-port", str(SUBMISSION_PORT), "--arrival-timeout", "2"
    )
    assert "aborted" not in report
    assert report["send"] == {"sent": True, "sent_copy": "appended", "arrived_after_s": None}
    assert report["cleanup"]["ok"] and report["cleanup"]["sent_deleted"] == 1
    _no_live_check_leftovers()


def test_live_check_cleanup_only_removes_just_its_own_leftovers(mailer: IMAPClient) -> None:
    """--cleanup-only removes leftovers of earlier runs and nothing else (matching by headers, not by SEARCH)."""

    def message(message_id: str, subject: str) -> bytes:
        return f"From: {USER}\r\nTo: {USER}\r\nSubject: {subject}\r\nMessage-ID: {message_id}\r\n\r\nText\r\n".encode()

    mailer.create_folder("HA-Live-0123456789")
    mailer.create_folder("HA-Live-0123456789/Ziel")
    mailer.append("INBOX", message("<0123456789-99@gmx-mail-live-check>", "gmx_mail live check 99"))
    mailer.append("INBOX", message("<keep-inbox@example.org>", "gmx_mail live check 99"))  # same subject, foreign Message-ID
    mailer.append("Entwürfe", message("<d1@gmx.test>", "gmx_mail live check draft"), flags=[b"\\Draft"])
    mailer.append("Entwürfe", message("<keep-draft@gmx.test>", "gmx_mail live check draft"))  # not flagged \Draft
    try:
        report = _live_check("--cleanup-only")["cleanup_only"]
        assert report == {
            "ok": True,
            "errors": [],
            "folders_deleted": 2,
            "inbox_deleted": 1,
            "trash_deleted": 0,
            "sent_deleted": 0,
            "drafts_deleted": 1,
        }
        for folder, kept in (("INBOX", b"keep-inbox@example.org"), ("Entwürfe", b"keep-draft@gmx.test")):
            mailer.select_folder(folder, readonly=True)
            found = mailer.fetch(mailer.search(["ALL"]), ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
            ids = b"".join(v for item in found.values() for k, v in item.items() if k.startswith(b"BODY"))
            assert kept in ids and b"gmx-mail-live-check" not in ids and b"<d1@gmx.test>" not in ids
            mailer.unselect_folder()
    finally:
        for folder, kept in (("INBOX", "keep-inbox@example.org"), ("Entwürfe", "keep-draft@gmx.test")):
            mailer.select_folder(folder)
            own = mailer.search(["HEADER", "Message-ID", kept])
            if own:
                mailer.add_flags(own, [b"\\Deleted"])
                mailer.uid_expunge(own)
            mailer.unselect_folder()
        for path in ("HA-Live-0123456789/Ziel", "HA-Live-0123456789"):
            with contextlib.suppress(Exception):
                mailer.delete_folder(path)
