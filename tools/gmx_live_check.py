#!/usr/bin/env python3
"""Live check of the integration's own IMAP code against a real GMX test account (manual, never in CI).

Answers the open M4 questions (SPEC section 7) with the code that Home Assistant will run:
  * verified TLS connect, capabilities, special folders;
  * effect of a read-write SELECT (used by the IDLE connection) on ``\\Recent``;
  * partial FETCH (``BODY.PEEK[1]<0.n>``): parsed directly or only via the fallback (GreenMail breaks it);
  * MOVE/COPY: new references from COPYUID or only via the Message-ID fallback;
  * draft APPEND to Entwürfe with APPENDUID;
  * deleting (M5): one own test message to the trash, preview, permanent deletion there, then verify it is gone;
  * sending (M6, only with ``--send-self-test``): ONE message to the own address, Sent copy, arrival in the INBOX;
  * IDLE: notification for a new message and - with ``--idle-minutes 35`` - GMX's ``* BYE timeout`` and the reconnect.

Safety rules (same as tools/gmx_probe.py):
  * credentials only from the environment (GMX_USER, GMX_PASS); prefer an app password of a test account;
  * works only in its own new test folder (aborts if it exists) and deletes exactly the messages it created
    (found by its own Message-ID and draft subject, over a fresh connection); ``--cleanup-only`` removes leftovers of earlier runs;
  * prints metadata only: no addresses, subjects or content.

Usage:
    export GMX_USER='testkonto@gmx.de' GMX_PASS='<app password>'
    python3 tools/gmx_live_check.py --idle-minutes 2 --output live_check.json
    python3 tools/gmx_live_check.py --idle-minutes 35 --output live_check_idle.json      # observe BYE + reconnect

Needs: Python 3.14 (same as Home Assistant 2026.11; the integration code uses 3.14 syntax) and
``pip install imapclient aioimaplib certifi`` (no Home Assistant installation required).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import email
import importlib.util
import json
import os
import ssl
import sys
import time
import types
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "custom_components" / "gmx_mail_mcp"
DRAFT_SUBJECT = "gmx_mail live check draft"
MESSAGE_ID_DOMAIN = "@gmx-mail-live-check"
CLEANUP_SCAN = 200  # newest messages per folder inspected by the clean-up


def _load_client() -> types.SimpleNamespace:
    """Import the integration's client modules without importing Home Assistant (the package __init__ needs HA)."""
    for name, path in (("custom_components", ROOT / "custom_components"), ("custom_components.gmx_mail_mcp", PACKAGE)):
        if name not in sys.modules:
            module = types.ModuleType(name)
            module.__path__ = [str(path)]
            sys.modules[name] = module
    names = ("const", "client", "client.errors", "client.folders", "client.sanitize", "client.structure", "client.imap")
    names += ("client.reader", "client.writer", "client.deleter", "client.policy", "client.composer", "client.sent", "client.smtp")
    names += ("client.idle",)
    loaded = {}
    for short in names:
        full = f"custom_components.gmx_mail_mcp.{short}"
        file = PACKAGE / (short.replace(".", "/") + ("/__init__.py" if short == "client" else ".py"))
        spec = importlib.util.spec_from_file_location(
            full, file, submodule_search_locations=[str(file.parent)] if short == "client" else None
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[full] = module
        spec.loader.exec_module(module)
        loaded[short.replace("client.", "")] = module
    return types.SimpleNamespace(**loaded)


def _context(cafile: str | None, imap: Any) -> ssl.SSLContext:
    if cafile:
        return imap.tls_context_with_minimum(ssl.create_default_context(cafile=cafile))
    try:
        import certifi

        return imap.tls_context_with_minimum(ssl.create_default_context(cafile=certifi.where()))
    except ImportError:
        return imap.tls_context_with_minimum(ssl.create_default_context())


def _message(user: str, marker: str, index: int) -> bytes:
    return (
        f"From: {user}\r\nTo: {user}\r\nSubject: gmx_mail live check {index}\r\n"
        f"Message-ID: <{marker}-{index}@gmx-mail-live-check>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
        + "Testnachricht der Integration gmx_mail (Live-Check). " * 20
        + "\r\n"
    ).encode()


def run_checks(args: argparse.Namespace, user: str, password: str) -> dict[str, Any]:
    """Run all checks and return a JSON-serialisable report (metadata only).

    The work connection is closed before the long IDLE wait (GMX drops connections idle for ~30 min), and the clean-up
    uses a fresh connection, so a dropped connection can never leave test data behind.
    """
    c = _load_client()
    settings = c.imap.ImapSettings(host=args.host, port=args.port, security=c.const.Security.SSL, username=user, password=password)
    report: dict[str, Any] = {"tool": "gmx_live_check", "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    context = _context(args.cafile, c.imap)
    marker = uuid.uuid4().hex[:10]
    test_folder = f"{args.folder_prefix}-{marker}"
    try:
        session = c.imap.open_imap_connection(settings, context)
        try:
            _work_checks(c, session, settings, context, user, marker, test_folder, report)
        finally:
            with contextlib.suppress(Exception):
                session.logout()
        if args.send_self_test:
            report["send"] = _send_check(c, settings, context, user, password, marker, args)
        report["idle"] = asyncio.run(_idle_check(c, settings, context, user, marker, args.idle_minutes))
    except Exception as err:  # noqa: BLE001 - recorded in the report; clean-up still runs
        report["aborted"] = type(err).__name__
    finally:
        report["cleanup"] = _cleanup(c, settings, context, args.folder_prefix, marker)
    report["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return report


def _work_checks(
    c: Any, session: Any, settings: Any, context: ssl.SSLContext, user: str, marker: str, folder: str, report: dict[str, Any]
) -> None:
    """Checks on the work connection (the code paths of the integration's actions)."""
    info = c.imap.probe_server(session)
    folder_map = c.folders.map_special_folders(info.folders)
    report["capabilities"] = sorted(info.capabilities)
    report["special_folders"] = folder_map.as_dict()
    if any(f.name == folder for f in info.folders):
        raise SystemExit("test folder exists - aborting")
    session.create_folder(folder)
    target = f"{folder}{next((f.delimiter for f in info.folders if f.delimiter), '/')}Ziel"
    session.create_folder(target)
    for index in range(3):
        session.append(folder, _message(user, marker, index))
    report["recent_after_select"] = _recent_check(c, settings, context, session, folder)
    session.select_folder(folder, readonly=True)
    uids = sorted(session.search(["ALL"]))
    validity = session.folder_status(folder, ["UIDVALIDITY"])[b"UIDVALIDITY"]
    # Partial FETCH exactly as the reader does it (GreenMail answers it in a form imapclient cannot parse).
    partial = c.reader._fetch_partial(session, uids[:1], "1", 64)
    report["partial_fetch"] = "parsed" if partial is not None else "fallback_needed"
    session.unselect_folder()
    refs = [c.reader.MailRef(folder, uid, int(validity)) for uid in uids]

    copied = c.writer.copy_or_move(session, info, refs[:1], target, move=False)
    report["copy"] = {"count": copied.count, "new_refs_known": copied.new_refs[0] is not None, "warnings": copied.warnings}
    store = c.writer._response_store(session)
    moved = c.writer.copy_or_move(session, info, refs[1:2], target, move=True)
    report["move"] = {
        "count": moved.count,
        "new_refs_known": moved.new_refs[0] is not None,
        "via_copyuid": not moved.warnings and bool(store is not None and store.get("COPYUID")),
        "warnings": moved.warnings,
    }
    flags = c.writer.set_flags(session, info, refs[2:3], ["flagged", "$GmxMailLiveCheck"], [])
    report["set_flags"] = {"count": flags.count}
    report["delete"] = _delete_check(c, session, info, folder_map, refs[2])

    draft = c.writer.DraftInput(sender=user, to=[user], subject=f"{DRAFT_SUBJECT} {marker}", body_text="Entwurf (Live-Check)")
    ref, size = c.writer.save_draft(session, info, folder_map, draft)
    report["draft"] = {
        "ref_known": ref is not None,
        "size": size,
        "folder_role_ok": bool(ref and ref["folder"] == folder_map.get(c.const.FolderRole.DRAFTS)),
    }


def _delete_check(c: Any, session: Any, info: Any, folder_map: Any, ref: Any) -> dict[str, Any]:
    """M5 path with one own test message: trash, preview, permanent deletion in the trash, then check it is gone."""
    trash_folder, moved = c.deleter.trash(session, info, folder_map, [ref])
    result: dict[str, Any] = {"trashed": moved.count, "trash_ref_known": bool(moved.new_refs and moved.new_refs[0])}
    if not result["trash_ref_known"]:
        return result  # the clean-up finds the message in the trash by its Message-ID
    in_trash = c.reader.MailRef(**moved.new_refs[0])
    preview = c.deleter.preview_permanent(session, info, folder_map, [in_trash])
    deleted = c.deleter.delete_permanently(session, info, folder_map, [in_trash])
    session.select_folder(trash_folder, readonly=True)
    still_there = in_trash.uid in session.fetch([in_trash.uid], ["FLAGS"])
    session.unselect_folder()
    result |= {"preview_count": preview["count"], "deleted": deleted.count, "warnings": deleted.warnings, "gone": not still_there}
    return result


def _send_check(c: Any, settings: Any, context: ssl.SSLContext, user: str, password: str, marker: str, args: Any) -> dict[str, Any]:
    """M6 (only with --send-self-test): ONE message to the own address over SMTP, Sent copy (mode auto: is there a
    server copy?), then wait for it in the INBOX. Never another recipient; the clean-up removes both copies."""
    outgoing = c.composer.new_message(
        sender=user,
        sender_name="",
        to=[user],
        cc=[],
        bcc=[],
        subject=f"gmx_mail live check send {marker}",
        body_text="Selbsttest der Integration gmx_mail (Live-Check). Diese Nachricht wird automatisch entfernt.",
        body_html=None,
        attachments=[],
    )
    message_id = f"<{marker}-send{MESSAGE_ID_DOMAIN}>"  # found again by the clean-up
    del outgoing.message["Message-ID"]
    outgoing.message["Message-ID"] = message_id
    smtp = c.smtp.SmtpSettings(
        host=args.smtp_host, port=args.smtp_port, security=c.const.Security.STARTTLS, username=user, password=password
    )
    started = time.monotonic()
    c.smtp.send_message(smtp, context, user, [user], outgoing.wire_bytes)
    result: dict[str, Any] = {"sent": True}
    session = c.imap.open_imap_connection(settings, context)
    try:
        info = c.imap.probe_server(session)
        roles = c.folders.map_special_folders(info.folders)
        result["sent_copy"] = c.sent.store_sent_copy(session, info, roles, outgoing.copy_bytes, message_id=message_id, mode="auto")
        arrived = None
        while time.monotonic() - started < args.arrival_timeout and arrived is None:
            if _own_messages(session, "INBOX", lambda mid, _s, _f: mid == message_id):
                arrived = round(time.monotonic() - started, 1)
            else:
                time.sleep(2)
        result["arrived_after_s"] = arrived
    finally:
        with contextlib.suppress(Exception):
            session.logout()
    return result


def _recent_check(c: Any, settings: Any, context: ssl.SSLContext, session: Any, folder: str) -> dict[str, Any]:
    """Effect of a read-write SELECT (as the IDLE connection does it) on ``\\Recent``, seen from a second connection."""
    observer = c.imap.open_imap_connection(settings, context)
    try:
        before = observer.folder_status(folder, [b"RECENT"]).get(b"RECENT")
        selected = session.select_folder(folder, readonly=False).get(b"RECENT")
        session.unselect_folder()  # never CLOSE after a read-write SELECT
        after = observer.folder_status(folder, [b"RECENT"]).get(b"RECENT")
    finally:
        observer.logout()
    return {"status_before": before, "on_select": selected, "status_after": after, "cleared_by_select": bool(before) and not after}


async def _idle_check(c: Any, settings: Any, context: ssl.SSLContext, user: str, marker: str, minutes: float) -> dict[str, Any]:
    """IDLE on the INBOX: notification for an appended message, then wait ``minutes`` and record BYE/reconnects."""
    changes: list[float] = []
    states: list[tuple[float, str]] = []
    start = time.monotonic()

    async def _ssl() -> ssl.SSLContext:
        return context

    watcher = c.idle.IdleWatcher(
        settings,
        ssl_context=_ssl,
        limiter=c.imap.ConnectionLimiter(),
        on_change=lambda: changes.append(time.monotonic() - start),
        on_auth_failed=lambda: None,
        renew_seconds=24 * 60,
        on_state_change=lambda: states.append((round(time.monotonic() - start, 1), watcher.state.value)),
    )
    task = asyncio.create_task(watcher.async_run())
    result: dict[str, Any] = {}
    try:
        for _ in range(200):
            if watcher.state.value == "connected":
                break
            await asyncio.sleep(0.1)
        result["connected"] = watcher.state.value == "connected"
        before = len(changes)
        appended_at = time.monotonic() - start

        def _append() -> None:
            helper = c.imap.open_imap_connection(settings, context)
            helper.append("INBOX", _message(user, marker, 99))
            helper.logout()

        await asyncio.to_thread(_append)
        for _ in range(300):
            if len(changes) > before:
                break
            await asyncio.sleep(0.1)
        result["push_delay_s"] = round(changes[before] - appended_at, 1) if len(changes) > before else None
        await asyncio.sleep(max(0.0, minutes * 60 - (time.monotonic() - start)))
        result["reconnects"] = watcher.reconnects
        result["state_changes"] = states
        result["minutes"] = minutes
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return result


def _own_messages(session: Any, folder: str, wanted: Any) -> list[int]:
    """UIDs among the newest ``CLEANUP_SCAN`` messages whose headers satisfy ``wanted`` (FETCH, never SEARCH).

    SEARCH is not used: GMX found nothing with ``SEARCH HEADER Message-ID`` (live check of 21.11.2026).
    """
    session.select_folder(folder)
    uids = sorted(session.search(["ALL"]))[-CLEANUP_SCAN:]
    found: list[int] = []
    if uids:
        for uid, item in session.fetch(uids, ["FLAGS", "BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT)]"]).items():
            headers = email.message_from_bytes(next((v for k, v in item.items() if k.startswith(b"BODY")), b"") or b"")
            flags = {f.decode() if isinstance(f, bytes) else str(f) for f in item.get(b"FLAGS", ())}
            if wanted(str(headers.get("Message-ID", "")), str(headers.get("Subject", "")), flags):
                found.append(uid)
    return found


def _delete_own(session: Any, folder: str, uids: list[int]) -> None:
    """Delete exactly these UIDs (UIDPLUS UID EXPUNGE), never a mailbox-wide EXPUNGE; leave with UNSELECT."""
    try:
        if uids:
            session.add_flags(uids, [b"\\Deleted"], silent=True)
            session.uid_expunge(uids)
    finally:
        session.unselect_folder()


def _cleanup(c: Any, settings: Any, context: ssl.SSLContext, prefix: str, marker: str | None) -> dict[str, Any]:
    """Remove the test data of this run (``marker``) or, with ``marker=None``, of all earlier runs - over a fresh connection.

    Only data the tool itself creates is matched: folders ``<prefix>-…``, INBOX messages with the Message-ID
    ``<…@gmx-mail-live-check>`` and drafts flagged ``\\Draft`` whose subject starts with the draft subject.
    """
    result: dict[str, Any] = {
        "ok": False,
        "errors": [],
        "folders_deleted": 0,
        "inbox_deleted": 0,
        "trash_deleted": 0,
        "sent_deleted": 0,
        "drafts_deleted": 0,
    }
    tag = f"{prefix}-{marker}" if marker else f"{prefix}-"
    try:
        session = c.imap.open_imap_connection(settings, context)
    except Exception as err:  # noqa: BLE001 - report, never raise during cleanup
        result["errors"].append(f"connect: {type(err).__name__}")
        return result
    try:
        info = c.imap.probe_server(session)
        roles = c.folders.map_special_folders(info.folders)
        drafts_folder = roles.get(c.const.FolderRole.DRAFTS)

        def own_message_id(mid: str, _subject: str, _flags: set[str]) -> bool:
            return mid.endswith(MESSAGE_ID_DOMAIN + ">") and (marker is None or f"<{marker}-" in mid)

        steps = (
            ("folders", None, None),
            ("inbox", "INBOX", own_message_id),
            ("trash", roles.get(c.const.FolderRole.TRASH), own_message_id),  # left there if the delete check was cut short
            ("sent", roles.get(c.const.FolderRole.SENT), own_message_id),  # copy of the send self-test
            (
                "drafts",
                drafts_folder,
                lambda _mid, subject, flags: (
                    "\\Draft" in flags and (subject == f"{DRAFT_SUBJECT} {marker}" if marker else subject.startswith(DRAFT_SUBJECT))
                ),
            ),
        )
        for name, folder, wanted in steps:
            try:
                if name == "folders":
                    own = sorted({f.name for f in info.folders if f.name.startswith(tag)}, reverse=True)  # children first
                    for path in own:
                        session.delete_folder(path)
                    result["folders_deleted"] = len(own)
                elif folder:
                    uids = _own_messages(session, folder, wanted)
                    _delete_own(session, folder, uids)
                    result[f"{name}_deleted"] = len(uids)
            except Exception as err:  # noqa: BLE001 - report, never raise during cleanup
                result["errors"].append(f"{name}: {type(err).__name__}")
    finally:
        with contextlib.suppress(Exception):
            session.logout()
    expected = {"folders_deleted": 2, "inbox_deleted": 1, "drafts_deleted": 1} if marker else {}
    result["ok"] = not result["errors"] and all(result[k] >= v for k, v in expected.items())
    return result


def main() -> int:
    """Entry point."""
    if sys.version_info < (3, 14):  # noqa: UP036 - the tool runs outside HA, often on an older Python
        print(f"Python 3.14 or newer is required (found {sys.version.split()[0]}); the integration code uses 3.14 syntax", file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--idle-minutes", type=float, default=2.0, help="how long to keep IDLE open (35 to observe GMX's BYE)")
    parser.add_argument("--host", default="imap.gmx.net")
    parser.add_argument("--port", type=int, default=993)
    parser.add_argument("--cafile", help="CA file (only for local test servers)")
    parser.add_argument("--folder-prefix", default="HA-Live")
    parser.add_argument("--output", help="write the JSON report to this file (UTF-8) instead of standard output")
    parser.add_argument("--cleanup-only", action="store_true", help="only remove test data left behind by earlier runs, then exit")
    parser.add_argument("--send-self-test", action="store_true", help="also send ONE message to the own address (M6) and remove it")
    parser.add_argument("--smtp-host", default="mail.gmx.net")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--arrival-timeout", type=float, default=90.0, help="seconds to wait for the self-test message in the INBOX")
    args = parser.parse_args()
    user, password = os.environ.get("GMX_USER"), os.environ.get("GMX_PASS")
    if not user or not password:
        print("GMX_USER and GMX_PASS must be set (use an app password of a test account)", file=sys.stderr)
        return 2
    try:
        if args.cleanup_only:
            c = _load_client()
            settings = c.imap.ImapSettings(host=args.host, port=args.port, security=c.const.Security.SSL, username=user, password=password)
            report = {
                "tool": "gmx_live_check",
                "cleanup_only": _cleanup(c, settings, _context(args.cafile, c.imap), args.folder_prefix, None),
            }
        else:
            report = run_checks(args, user, password)
    except Exception as err:
        hints = {
            "GmxAuthError": "login rejected: check the address, the (app) password and that POP3/IMAP is enabled in the GMX webmailer",
            "GmxTlsError": "TLS handshake failed (certificate or TLS version)",
            "GmxConnectionError": "server not reachable (host, port, firewall or network)",
        }
        if type(err).__name__ not in hints:
            raise  # unexpected: keep the traceback (it contains no credentials)
        print(f"{type(err).__name__}: {hints[type(err).__name__]}", file=sys.stderr)
        return 1
    text = json.dumps(report, indent=2, default=str)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")  # Windows PowerShell's ">" would write UTF-16
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
