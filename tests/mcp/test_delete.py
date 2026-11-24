"""The M4 deleting tools (level D), the audit log and the delete counter against the in-memory IMAP server."""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from imapclient.exceptions import IMAPClientError

from custom_components.gmx_mail_mcp.audit import MAX_ENTRIES, AuditLog, DeleteCounter, fingerprint
from custom_components.gmx_mail_mcp.client.imap import ConnectionLimiter, ImapSettings, ImapWorker
from custom_components.gmx_mail_mcp.const import (
    CONF_ALLOW_PERMANENT_DELETE,
    CONF_MAX_DELETE_PER_DAY,
    CONF_TOOLS_WITHOUT_CONFIRMATION,
    MAX_DELETE_PER_CALL,
    Permission,
    Security,
)
from custom_components.gmx_mail_mcp.mcp.protocol import REQUEST_USER, McpServer, Tool, ToolResult
from custom_components.gmx_mail_mcp.mcp.registry import (
    CONFIRMABLE_TOOLS,
    TOOL_SPECS,
    ToolContext,
    build_tools,
    default_enabled_tools,
    skips_confirmation,
)
from tests.client.fakes import EMAIL, PASSWORD, FakeIMAPClient, FakeImapServer, plain_message

UIDVALIDITY = 1234
TRASH_VALIDITY = 900
D_TOOLS = ("gmx_trash", "gmx_delete_permanently", "gmx_delete_folder")
SECRET_SUBJECT = "Geheimer Betreff 4711"


async def _inline(func: Any, *args: Any) -> Any:
    return func(*args)


@pytest.fixture
def imap() -> Generator[FakeImapServer]:
    server = FakeImapServer()
    inbox = server.mailbox("INBOX", UIDVALIDITY)
    for uid in (1, 2, 3):
        inbox.messages[uid] = plain_message(subject=f"{SECRET_SUBJECT} {uid}", message_id=f"<m{uid}@example.org>")
    trash = server.mailbox("Gelöscht", TRASH_VALIDITY)
    for uid in (1, 2):
        trash.messages[uid] = plain_message(subject=f"{SECRET_SUBJECT} t{uid}", message_id=f"<t{uid}@example.org>")
    for name in ("Archiv", "Gesendet", "Entwürfe", "Spamverdacht"):
        server.mailbox(name, TRASH_VALIDITY)
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", server):
        yield server


@pytest.fixture
def options() -> dict[str, Any]:
    return {}


@pytest.fixture
def context(imap: FakeImapServer, options: dict[str, Any]) -> ToolContext:
    settings = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password=PASSWORD)
    worker = ImapWorker(settings, executor=_inline, ssl_context_factory=ssl.create_default_context, limiter=ConnectionLimiter())
    return ToolContext(worker=worker, options=lambda: options, executor=_inline, account="Konto A", email=EMAIL)


@pytest.fixture
def server(context: ToolContext) -> McpServer:
    tools = build_tools(context)
    return McpServer(name="t", version="1", tools=lambda: tools)


async def call(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    outcome = await server.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    assert outcome.body is not None
    assert "result" in outcome.body, outcome.body
    result = outcome.body["result"]
    return {"is_error": result["isError"], "data": json.loads(result["content"][0]["text"])}


def ref(uid: int, folder: str = "INBOX", validity: int = UIDVALIDITY) -> dict[str, Any]:
    return {"folder": folder, "uid": uid, "uidvalidity": validity}


def tref(uid: int) -> dict[str, Any]:
    return ref(uid, "Gelöscht", TRASH_VALIDITY)


def _writes(imap: FakeImapServer) -> list[tuple[Any, ...]]:
    kinds = ("add_flags", "remove_flags", "move", "copy", "uid_expunge", "append", "create", "rename", "delete_folder")
    return [c for c in imap.commands if c[0] in kinds]


# --------------------------------------------------------------------------- catalogue


def test_delete_tools_are_level_d_and_off_by_default() -> None:
    specs = {spec.name: spec for spec in TOOL_SPECS}
    for name in D_TOOLS:
        assert specs[name].level is Permission.DELETE
        assert name not in default_enabled_tools()
    assert specs["gmx_get_audit_log"].level is Permission.READ
    assert CONFIRMABLE_TOOLS[:2] == ("gmx_delete_permanently", "gmx_delete_folder")


def test_delete_tools_are_marked_destructive(context: ToolContext) -> None:
    tools = {t.name: t.as_dict() for t in build_tools(context)}
    for name in D_TOOLS:
        assert tools[name]["annotations"]["destructiveHint"] is True
        assert tools[name]["annotations"]["readOnlyHint"] is False


def test_only_confirmable_tools_can_skip_confirmation() -> None:
    chosen = {CONF_TOOLS_WITHOUT_CONFIRMATION: ["gmx_delete_folder", "gmx_trash", "gmx_move"]}
    assert skips_confirmation(chosen, "gmx_delete_folder") is True
    assert skips_confirmation(chosen, "gmx_delete_permanently") is False
    assert skips_confirmation(chosen, "gmx_move") is False
    assert skips_confirmation({CONF_TOOLS_WITHOUT_CONFIRMATION: "gmx_delete_folder"}, "gmx_delete_folder") is False


# --------------------------------------------------------------------------- gmx_trash


async def test_trash_moves_and_counts(server: McpServer, imap: FakeImapServer, context: ToolContext) -> None:
    result = await call(server, "gmx_trash", {"refs": [ref(1), ref(2)]})
    assert result["is_error"] is False
    data = result["data"]["data"]
    assert (data["count"], data["trash_folder"]) == (2, "Gelöscht")
    assert [r["folder"] for r in data["new_refs"]] == ["Gelöscht", "Gelöscht"]
    assert sorted(imap.mailbox("INBOX").messages) == [3]
    assert context.deletes.used == 2
    entry = context.audit.latest(1)[0]
    assert (entry["tool"], entry["level"], entry["outcome"], entry["count"]) == ("gmx_trash", "delete", "ok", 2)
    assert entry["folders"] == ["Gelöscht", "INBOX"]


async def test_trash_refuses_mails_in_the_trash(server: McpServer, imap: FakeImapServer, context: ToolContext) -> None:
    result = await call(server, "gmx_trash", {"refs": [tref(1)]})
    assert result["is_error"] is True
    assert (result["data"]["error"], result["data"]["reason"]) == ("delete_not_allowed", "already_in_trash")
    assert not _writes(imap)
    assert context.deletes.used == 0
    entry = context.audit.latest(1)[0]
    assert (entry["outcome"], entry["error"]) == ("error", "delete_not_allowed")


async def test_trash_takes_at_most_50(server: McpServer, imap: FakeImapServer) -> None:
    refs = [ref(uid) for uid in range(1, MAX_DELETE_PER_CALL + 2)]
    result = await call(server, "gmx_trash", {"refs": refs})
    assert result["data"]["error"] == "invalid_arguments"
    assert not _writes(imap)


@pytest.mark.parametrize("options", [{CONF_MAX_DELETE_PER_DAY: 2}])
async def test_daily_limit(server: McpServer, imap: FakeImapServer, context: ToolContext) -> None:
    refused = await call(server, "gmx_trash", {"refs": [ref(1), ref(2), ref(3)]})
    assert refused["data"]["error"] == "daily_delete_limit"
    assert (refused["data"]["limit"], refused["data"]["remaining"]) == (2, 2)
    assert not _writes(imap)
    assert (await call(server, "gmx_trash", {"refs": [ref(1), ref(2)]}))["is_error"] is False
    again = await call(server, "gmx_trash", {"refs": [ref(3)]})
    assert (again["data"]["error"], again["data"]["remaining"]) == ("daily_delete_limit", 0)


async def test_no_daily_limit_by_default(server: McpServer, context: ToolContext) -> None:
    context.deletes.add(10_000)
    assert (await call(server, "gmx_trash", {"refs": [ref(1)]}))["is_error"] is False


# --------------------------------------------------------------------------- gmx_delete_permanently


async def test_permanent_delete_is_off_without_the_option(server: McpServer, imap: FakeImapServer) -> None:
    result = await call(server, "gmx_delete_permanently", {"refs": [tref(1)]})
    assert result["data"]["error"] == "permanent_delete_disabled"
    assert not _writes(imap)


@pytest.mark.parametrize("options", [{CONF_ALLOW_PERMANENT_DELETE: True}])
async def test_permanent_delete_two_steps(server: McpServer, imap: FakeImapServer, context: ToolContext) -> None:
    preview = await call(server, "gmx_delete_permanently", {"refs": [tref(2), tref(1)]})
    assert preview["is_error"] is False
    body = preview["data"]["data"]
    assert (body["deleted"], body["preview"]["count"], body["preview"]["folders"]) == (0, 2, ["Gelöscht"])
    assert not _writes(imap)  # the preview deletes nothing
    token = body["confirm_token"]

    # Code for other messages, or a forged code: refused, nothing deleted.
    other = await call(server, "gmx_delete_permanently", {"refs": [tref(1)], "confirm_token": token})
    assert other["data"]["error"] == "confirm_token_invalid"
    forged = await call(server, "gmx_delete_permanently", {"refs": [tref(1), tref(2)], "confirm_token": token[:-2] + "xx"})
    assert forged["data"]["error"] == "confirm_token_invalid"
    assert not _writes(imap)

    # Same set in another order: accepted; only UID EXPUNGE of exactly these UIDs.
    done = await call(server, "gmx_delete_permanently", {"refs": [tref(1), tref(2)], "confirm_token": token})
    assert done["is_error"] is False
    assert (done["data"]["data"]["count"], done["data"]["data"]["confirmation"]) == (2, "code")
    assert imap.mailbox("Gelöscht").messages == {}
    assert [c for c in imap.commands if c[0] == "uid_expunge"] == [("uid_expunge", [1, 2])]
    assert context.deletes.used == 2

    reused = await call(server, "gmx_delete_permanently", {"refs": [tref(1), tref(2)], "confirm_token": token})
    assert reused["data"]["error"] == "confirm_token_invalid"

    outcomes = [(e["outcome"], e.get("confirmation"), e.get("error")) for e in reversed(context.audit.latest(10))]
    assert outcomes == [
        ("preview", None, None),
        ("error", None, "confirm_token_invalid"),
        ("error", None, "confirm_token_invalid"),
        ("ok", "code", None),
        ("error", None, "confirm_token_invalid"),
    ]
    # The fingerprint covers the arguments without the code, so a preview and its execution can be matched.
    by_outcome = {e["outcome"]: e["hash"] for e in context.audit.latest(10) if e["outcome"] in ("preview", "ok")}
    assert by_outcome["preview"] == fingerprint("gmx_delete_permanently", {"refs": [tref(2), tref(1)]})
    assert by_outcome["ok"] == fingerprint("gmx_delete_permanently", {"refs": [tref(1), tref(2)]})


@pytest.mark.parametrize("options", [{CONF_ALLOW_PERMANENT_DELETE: True}])
async def test_permanent_delete_only_in_trash_or_spam(server: McpServer, imap: FakeImapServer) -> None:
    result = await call(server, "gmx_delete_permanently", {"refs": [ref(1)]})
    assert (result["data"]["error"], result["data"]["reason"]) == ("delete_not_allowed", "not_in_trash_or_junk")
    assert not _writes(imap)


@pytest.mark.parametrize("options", [{CONF_ALLOW_PERMANENT_DELETE: True}])
async def test_preview_of_missing_mails_gives_no_code(server: McpServer, imap: FakeImapServer) -> None:
    result = await call(server, "gmx_delete_permanently", {"refs": [tref(9)]})
    body = result["data"]["data"]
    assert body["preview"]["count"] == 0
    assert "confirm_token" not in body


@pytest.mark.parametrize("options", [{CONF_ALLOW_PERMANENT_DELETE: True, CONF_TOOLS_WITHOUT_CONFIRMATION: ["gmx_delete_permanently"]}])
async def test_permanent_delete_without_confirmation(server: McpServer, imap: FakeImapServer, context: ToolContext) -> None:
    result = await call(server, "gmx_delete_permanently", {"refs": [tref(1)]})
    assert result["is_error"] is False
    assert (result["data"]["data"]["count"], result["data"]["data"]["confirmation"]) == (1, "skipped")
    assert sorted(imap.mailbox("Gelöscht").messages) == [2]
    assert context.audit.latest(1)[0]["confirmation"] == "skipped"


@pytest.mark.parametrize("options", [{CONF_TOOLS_WITHOUT_CONFIRMATION: ["gmx_delete_permanently"]}])
async def test_skipping_confirmation_does_not_lift_the_option(server: McpServer, imap: FakeImapServer) -> None:
    result = await call(server, "gmx_delete_permanently", {"refs": [tref(1)]})
    assert result["data"]["error"] == "permanent_delete_disabled"
    assert not _writes(imap)


@pytest.mark.parametrize("options", [{CONF_ALLOW_PERMANENT_DELETE: True}])
async def test_expired_code_is_refused(server: McpServer, imap: FakeImapServer, context: ToolContext) -> None:
    preview = await call(server, "gmx_delete_permanently", {"refs": [tref(1)]})
    token = preview["data"]["data"]["confirm_token"]
    context.tokens._clock = lambda: 10**12  # far in the future
    result = await call(server, "gmx_delete_permanently", {"refs": [tref(1)], "confirm_token": token})
    assert result["data"]["error"] == "confirm_token_invalid"
    assert not _writes(imap)


# --------------------------------------------------------------------------- gmx_delete_folder


async def test_delete_folder_two_steps(server: McpServer, imap: FakeImapServer, context: ToolContext) -> None:
    preview = await call(server, "gmx_delete_folder", {"folder": "Archiv"})
    body = preview["data"]["data"]
    assert (body["deleted"], body["preview"]) == (False, {"path": "Archiv", "messages": 0})
    assert not _writes(imap)
    wrong = await call(server, "gmx_delete_folder", {"folder": "Gesendet", "confirm_token": body["confirm_token"]})
    assert wrong["data"]["error"] == "confirm_token_invalid"  # code bound to "Archiv"
    done = await call(server, "gmx_delete_folder", {"folder": "Archiv", "confirm_token": body["confirm_token"]})
    assert done["data"]["data"] == {"deleted_folder": "Archiv", "count": 1, "confirmation": "code"}
    assert ("delete_folder", "Archiv") in imap.commands
    assert context.deletes.used == 0  # folders do not count towards the mail limit


@pytest.mark.parametrize(
    ("setup", "folder", "error", "reason"),
    [
        ("none", "INBOX", "protected_folder", None),
        ("none", "Gelöscht", "protected_folder", None),
        ("none", "Fehlt", "folder_not_found", None),
        ("mail", "Archiv", "delete_not_allowed", "folder_not_empty"),
        ("child", "Archiv", "delete_not_allowed", "folder_has_children"),
    ],
)
async def test_delete_folder_rules(
    *, server: McpServer, imap: FakeImapServer, setup: str, folder: str, error: str, reason: str | None
) -> None:
    if setup == "mail":
        imap.mailbox("Archiv").messages[1] = plain_message(subject="x", message_id="<a1@example.org>")
    if setup == "child":
        imap.folders.append(((b"\\HasNoChildren",), b"/", "Archiv/2026"))
        imap.mailbox("Archiv/2026")
    result = await call(server, "gmx_delete_folder", {"folder": folder})
    assert result["data"]["error"] == error
    assert result["data"].get("reason") == reason
    assert "confirm_token" not in json.dumps(result["data"])
    assert not _writes(imap)


async def test_delete_folder_checks_again_at_execution(server: McpServer, imap: FakeImapServer) -> None:
    preview = await call(server, "gmx_delete_folder", {"folder": "Archiv"})
    imap.mailbox("Archiv").messages[1] = plain_message(subject="x", message_id="<a1@example.org>")  # mail arrived meanwhile
    result = await call(server, "gmx_delete_folder", {"folder": "Archiv", "confirm_token": preview["data"]["data"]["confirm_token"]})
    assert result["data"]["reason"] == "folder_not_empty"
    assert not _writes(imap)


@pytest.mark.parametrize("options", [{CONF_TOOLS_WITHOUT_CONFIRMATION: ["gmx_delete_folder"]}])
async def test_delete_folder_without_confirmation(server: McpServer, imap: FakeImapServer) -> None:
    result = await call(server, "gmx_delete_folder", {"folder": "Archiv"})
    assert result["data"]["data"]["confirmation"] == "skipped"
    assert ("delete_folder", "Archiv") in imap.commands


# --------------------------------------------------------------------------- audit log


async def test_reading_is_not_audited_writing_is(server: McpServer, context: ToolContext) -> None:
    await call(server, "gmx_search", {})
    await call(server, "gmx_get_message", {"ref": ref(1)})
    assert len(context.audit) == 0
    token = REQUEST_USER.set("user-123")
    try:
        await call(server, "gmx_set_flags", {"refs": [ref(1)], "add": ["flagged"]})
    finally:
        REQUEST_USER.reset(token)
    entry = context.audit.latest(1)[0]
    assert (entry["tool"], entry["level"], entry["outcome"], entry["count"], entry["user"]) == (
        "gmx_set_flags",
        "organize",
        "ok",
        1,
        "user-123",
    )


@pytest.mark.parametrize("options", [{CONF_ALLOW_PERMANENT_DELETE: True}])
async def test_audit_holds_no_content(server: McpServer, context: ToolContext) -> None:
    await call(server, "gmx_delete_permanently", {"refs": [tref(1)]})  # the preview lists the subject
    await call(server, "gmx_save_draft", {"to": ["someone@example.org"], "subject": SECRET_SUBJECT, "body_text": "Inhalt 0815"})
    dump = json.dumps(context.audit.snapshot(), ensure_ascii=False)
    for secret in (SECRET_SUBJECT, "someone@example.org", "example.org", "Inhalt 0815"):
        assert secret not in dump
    assert {e["tool"] for e in context.audit.latest(10)} == {"gmx_delete_permanently", "gmx_save_draft"}


async def test_audit_tool(server: McpServer, context: ToolContext) -> None:
    for uid in (1, 2, 3):
        await call(server, "gmx_set_flags", {"refs": [ref(uid)], "add": ["seen"]})
    result = await call(server, "gmx_get_audit_log", {"limit": 2})
    data = result["data"]["data"]
    assert [e["tool"] for e in data["entries"]] == ["gmx_set_flags", "gmx_set_flags"]
    assert data["entries"][0]["time"] >= data["entries"][1]["time"]
    assert (data["stored"], data["deleted_today"], data["max_delete_per_day"]) == (3, 0, None)
    assert len(context.audit) == 3  # reading the log is not audited


async def test_cancelled_and_failing_calls_are_audited(context: ToolContext) -> None:
    async def slow(_args: dict[str, Any]) -> ToolResult:
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    async def broken(_args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("boom")

    schema = {"type": "object"}
    with patch(
        "custom_components.gmx_mail_mcp.mcp.registry.TOOL_SPECS",
        (
            type(TOOL_SPECS[0])(
                name="slow", level=Permission.DELETE, build=lambda _c: Tool("slow", "s", "s", schema, Permission.DELETE, slow)
            ),
            type(TOOL_SPECS[0])(
                name="broken", level=Permission.ORGANIZE, build=lambda _c: Tool("broken", "b", "b", schema, Permission.ORGANIZE, broken)
            ),
        ),
    ):
        tools = build_tools(context)
    server = McpServer(name="t", version="1", tools=lambda: tools, call_timeout=0.05)
    timed_out = await server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "slow"}})
    failed = await server.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "broken"}})
    assert timed_out.body is not None and failed.body is not None
    assert timed_out.body["result"]["isError"] is True
    assert failed.body["result"]["isError"] is True
    assert [(e["tool"], e["outcome"]) for e in reversed(context.audit.latest(5))] == [("slow", "cancelled"), ("broken", "internal_error")]


async def test_audit_failure_does_not_fail_the_call(server: McpServer, context: ToolContext, imap: FakeImapServer) -> None:
    def explode() -> None:
        raise OSError("disk full")

    context.audit.on_change = explode
    result = await call(server, "gmx_set_flags", {"refs": [ref(1)], "add": ["seen"]})
    assert result["is_error"] is False
    assert b"\\Seen" in imap.mailbox("INBOX").messages[1].flags


# --------------------------------------------------------------------------- AuditLog / DeleteCounter


def test_audit_retention_and_size() -> None:
    now = [1_800_000_000.0]
    log = AuditLog(retention_days=lambda: 2, clock=lambda: now[0])
    log.record(tool="t", level="delete", outcome="ok", arguments={})
    now[0] += 3 * 86_400
    log.record(tool="u", level="delete", outcome="ok", arguments={})
    assert [e["tool"] for e in log.latest(10)] == ["u"]
    for _ in range(MAX_ENTRIES + 5):
        log.record(tool="v", level="organize", outcome="ok", arguments={})
    assert len(log) == MAX_ENTRIES


def test_audit_loads_stored_entries_and_drops_malformed() -> None:
    now = 1_800_000_000.0
    recent = {"time": "2027-01-15T08:00:00+00:00", "tool": "t"}
    log = AuditLog(retention_days=lambda: 3650, clock=lambda: now, entries=[recent, {"tool": "no time"}, "junk"])  # type: ignore[list-item]
    assert log.latest(5) == [recent]


def test_audit_folder_names_and_fingerprint() -> None:
    log = AuditLog()
    arguments = {"refs": [ref(1), ref(2, "Archiv")], "target": "Ziel\u202e", "confirm_token": "abc"}
    entry = log.record(tool="t", level="organize", outcome="ok", arguments=arguments, folders=["Spamverdacht"])
    assert entry["folders"] == ["Archiv", "INBOX", "Spamverdacht", "Ziel"]  # cleaned, sorted, unique
    assert entry["hash"] == fingerprint("t", {k: v for k, v in arguments.items() if k != "confirm_token"})
    assert len(entry["hash"]) == 16


def test_fingerprint_depends_on_the_tool() -> None:
    log = AuditLog()
    arguments = {"refs": [ref(1)]}
    trashed = log.record(tool="gmx_trash", level="delete", outcome="ok", arguments=arguments)
    deleted = log.record(tool="gmx_delete_permanently", level="delete", outcome="ok", arguments=arguments)
    assert trashed["hash"] != deleted["hash"]


def test_delete_counter_rolls_over_and_restores() -> None:
    day = ["2026-11-24"]
    counter = DeleteCounter(today=lambda: day[0], stored={"date": "2026-11-24", "deleted": 7})
    assert counter.used == 7
    assert counter.remaining(10) == 3
    assert counter.remaining(0) is None
    counter.add(5)
    assert counter.remaining(10) == 0
    day[0] = "2026-11-25"
    assert counter.used == 0
    assert DeleteCounter(today=lambda: "2026-11-25", stored={"date": "2026-11-24", "deleted": 7}).used == 0
    assert DeleteCounter(today=lambda: "2026-11-25", stored={"date": "2026-11-25", "deleted": "x"}).used == 0


# --------------------------------------------------------------------------- folder subscriptions (D44)


async def test_rename_keeps_unsubscribed_state(server: McpServer, imap: FakeImapServer) -> None:
    imap.subscribed = {"INBOX"}
    result = await call(server, "gmx_rename_folder", {"folder": "Archiv", "new_name": "Ablage"})
    assert result["data"]["warnings"] == []
    assert imap.subscribed == {"INBOX"}


async def test_rename_without_lsub_warns(server: McpServer, imap: FakeImapServer) -> None:
    imap.lsub_error = IMAPClientError("LSUB failed")
    result = await call(server, "gmx_rename_folder", {"folder": "Archiv", "new_name": "Ablage"})
    assert result["data"]["data"]["path"] == "Ablage"
    assert result["data"]["warnings"] == ["subscription_unknown"]


async def test_create_folder_warns_when_subscribe_fails(server: McpServer, imap: FakeImapServer) -> None:
    with patch.object(FakeIMAPClient, "subscribe_folder", side_effect=IMAPClientError("SUBSCRIBE failed")):
        result = await call(server, "gmx_create_folder", {"name": "Neu"})
    assert result["is_error"] is False
    assert result["data"]["data"] == {"path": "Neu", "subscribed": False}
    assert result["data"]["warnings"] == ["subscribe_failed"]
    assert ("create", "Neu") in imap.commands


# --------------------------------------------------------------------------- uniform preview count, timeout (D61)


async def test_folder_preview_counts_the_folder(server: McpServer, context: ToolContext, imap: FakeImapServer) -> None:
    await call(server, "gmx_delete_folder", {"folder": "Archiv"})
    entry = context.audit.latest(1)[0]
    assert (entry["outcome"], entry["count"]) == ("preview", 1)


def _slow_worker(context: ToolContext, delay: float) -> tuple[ToolContext, list[bool]]:
    """A context whose IMAP work runs in a thread and, once switched on, sleeps first (event loop stays free)."""
    switch = [False]

    async def threaded(func: Any, *args: Any) -> Any:
        def job() -> Any:
            if switch[0]:
                time.sleep(delay)
            return func(*args)

        return await asyncio.to_thread(job)

    settings = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password=PASSWORD)
    worker = ImapWorker(settings, executor=threaded, ssl_context_factory=ssl.create_default_context, limiter=ConnectionLimiter())
    slow = ToolContext(worker=worker, options=context.options, executor=_inline, account="Konto A", email=EMAIL, audit=context.audit)
    return slow, switch


@pytest.mark.parametrize(
    ("tool", "arguments", "expected_count"),
    [
        ("gmx_trash", {"refs": [ref(1), ref(2)]}, 2),
        ("gmx_delete_permanently", {"refs": [tref(1)]}, 1),
        ("gmx_delete_folder", {"folder": "Archiv"}, 1),
    ],
)
@pytest.mark.parametrize(
    "options",
    [{CONF_ALLOW_PERMANENT_DELETE: True, CONF_TOOLS_WITHOUT_CONFIRMATION: ["gmx_delete_permanently", "gmx_delete_folder"]}],
)
async def test_timeout_during_deleting_lets_it_finish_and_records_it(
    context: ToolContext, imap: FakeImapServer, tool: str, arguments: dict[str, Any], expected_count: int
) -> None:
    slow, switch = _slow_worker(context, 0.4)
    tools = build_tools(slow)
    mcp = McpServer(name="t", version="1", tools=lambda: tools, call_timeout=0.2)
    await call(mcp, "gmx_list_folders", {})  # connect and learn the folders at normal speed
    switch[0] = True
    outcome = await mcp.handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    )
    assert outcome.body is not None and outcome.body["result"]["isError"]
    for _ in range(60):
        if any(e["outcome"].startswith("late_") for e in context.audit.latest(5)):
            break
        await asyncio.sleep(0.05)
    entries = context.audit.latest(5)
    assert [e["outcome"] for e in entries[:2]] == ["late_ok", "cancelled"]
    assert entries[0]["count"] == expected_count and entries[0]["hash"] == entries[1]["hash"]
    if tool == "gmx_delete_folder":
        assert "Archiv" not in imap.mailboxes
    else:
        assert slow.deletes.used == expected_count
