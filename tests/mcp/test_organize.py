"""The M3 organising tools (level O) against the in-memory IMAP server (no Home Assistant)."""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
from collections.abc import Generator
from email import message_from_bytes, policy
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.gmx_mail_mcp.client.imap import ConnectionLimiter, ImapSettings, ImapWorker
from custom_components.gmx_mail_mcp.const import CONF_MAX_UPLOAD_MB, Permission, Security
from custom_components.gmx_mail_mcp.mcp.protocol import McpServer
from custom_components.gmx_mail_mcp.mcp.registry import TOOL_SPECS, ToolContext, build_tools, default_enabled_tools
from tests.client.fakes import EMAIL, PASSWORD, FakeImapServer, plain_message

from .documents import PNG_1X1

UIDVALIDITY = 1234
O_TOOLS = (
    "gmx_set_flags",
    "gmx_move",
    "gmx_copy",
    "gmx_mark_spam",
    "gmx_mark_not_spam",
    "gmx_create_folder",
    "gmx_rename_folder",
    "gmx_subscribe_folder",
    "gmx_save_draft",
)


async def _inline(func: Any, *args: Any) -> Any:
    return func(*args)


@pytest.fixture
def imap() -> Generator[FakeImapServer]:
    server = FakeImapServer()
    inbox = server.mailbox("INBOX", UIDVALIDITY)
    for uid in (1, 2, 3):
        inbox.messages[uid] = plain_message(subject=f"m{uid}", message_id=f"<m{uid}@example.org>")
    for name in ("Archiv", "Gesendet", "Entwürfe", "Gelöscht", "Spamverdacht"):
        server.mailbox(name, 900)
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", server):
        yield server


@pytest.fixture
def options() -> dict[str, Any]:
    return {}


@pytest.fixture
def server(imap: FakeImapServer, options: dict[str, Any]) -> McpServer:
    settings = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password=PASSWORD)
    worker = ImapWorker(settings, executor=_inline, ssl_context_factory=ssl.create_default_context, limiter=ConnectionLimiter())
    tools = build_tools(ToolContext(worker=worker, options=lambda: options, executor=_inline, account="Konto A", email=EMAIL))
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


def _file(content: bytes, name: str, mime: str | None = None) -> dict[str, Any]:
    item = {"filename": name, "content_base64": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest()}
    return {**item, "mime_type": mime} if mime else item


def _writes(imap: FakeImapServer) -> list[tuple[Any, ...]]:
    return [
        c
        for c in imap.commands
        if c[0]
        in (
            "add_flags",
            "remove_flags",
            "move",
            "copy",
            "uid_expunge",
            "append",
            "create",
            "rename",
            "delete_folder",
            "subscribe",
            "unsubscribe",
        )
    ]


# --------------------------------------------------------------------------- catalogue


def test_organize_tools_are_level_o_and_off_by_default() -> None:
    """All nine tools exist with level O, are not in the defaults and are announced as writing but not destructive."""
    levels = {spec.name: spec.level for spec in TOOL_SPECS}
    assert all(levels[name] is Permission.ORGANIZE for name in O_TOOLS)
    assert not set(O_TOOLS) & set(default_enabled_tools())
    tools = {t.name: t.as_dict() for t in build_tools(ToolContext(worker=None))}  # type: ignore[arg-type]
    for name in O_TOOLS:
        assert tools[name]["annotations"]["readOnlyHint"] is False
        assert tools[name]["annotations"]["destructiveHint"] is False


# --------------------------------------------------------------------------- flags


async def test_set_flags(server: McpServer, imap: FakeImapServer) -> None:
    result = await call(server, "gmx_set_flags", {"refs": [ref(1), ref(99)], "add": ["seen", "$Steuer"]})
    assert result["is_error"] is False
    assert result["data"]["data"] == {"count": 1, "folders": ["INBOX"], "missing": [ref(99)]}
    assert imap.mailbox("INBOX").messages[1].flags == (b"\\Seen", b"$Steuer")


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        ({"refs": [ref(1)]}, "at least one flag"),
        ({"refs": [ref(1)], "add": ["\\Deleted"]}, "not allowed"),
        ({"refs": [ref(1)], "add": ["draft"]}, "not allowed"),
        ({"refs": [], "add": ["seen"]}, "list of 1 to 100"),
        ({"refs": [ref(1), ref(1)], "add": ["seen"]}, "listed twice"),
        ({"refs": [ref(1)] * 101, "add": ["seen"]}, "list of 1 to 100"),
        ({"refs": [ref(1)], "add": ["seen"], "extra": 1}, "unknown argument"),
    ],
)
async def test_set_flags_input_errors(server: McpServer, imap: FakeImapServer, arguments: dict[str, Any], fragment: str) -> None:
    result = await call(server, "gmx_set_flags", arguments)
    assert result["is_error"] is True
    assert result["data"]["error"] == "invalid_arguments"
    assert fragment in result["data"]["message"]
    assert not _writes(imap)


# --------------------------------------------------------------------------- move / copy


async def test_move_and_copy_return_new_refs(server: McpServer, imap: FakeImapServer) -> None:
    moved = await call(server, "gmx_move", {"refs": [ref(2)], "target": "Archiv"})
    assert moved["data"]["data"]["new_refs"] == [ref(1, "Archiv", 900)]
    copied = await call(server, "gmx_copy", {"refs": [ref(3)], "target": "Archiv"})
    assert copied["data"]["data"]["new_refs"] == [ref(2, "Archiv", 900)]
    assert sorted(imap.mailbox("INBOX").messages) == [1, 3]


@pytest.mark.parametrize("tool", ["gmx_move", "gmx_copy"])
async def test_trash_is_not_a_target(server: McpServer, imap: FakeImapServer, tool: str) -> None:
    """D32: deleting stays level D; the trash is refused before anything is touched."""
    result = await call(server, tool, {"refs": [ref(1)], "target": "Gelöscht"})
    assert result["is_error"] is True
    assert result["data"]["error"] == "protected_target"
    assert not _writes(imap)
    assert 1 in imap.mailbox("INBOX").messages


async def test_move_errors(server: McpServer, imap: FakeImapServer) -> None:
    unknown = await call(server, "gmx_move", {"refs": [ref(1)], "target": "Gibt es nicht"})
    assert unknown["data"]["error"] == "folder_not_found"
    same = await call(server, "gmx_move", {"refs": [ref(1)], "target": "INBOX"})
    assert (same["data"]["error"], same["data"]["reason"]) == ("invalid_folder_name", "same_folder")
    stale = await call(server, "gmx_move", {"refs": [ref(1, validity=1)], "target": "Archiv"})
    assert stale["data"]["error"] == "uidvalidity_changed"
    assert not [c for c in imap.commands if c[0] == "move"]


async def test_move_into_spam_folder_is_allowed(server: McpServer, imap: FakeImapServer) -> None:
    """Decision of the user (M3 question 3): gmx_move may target the spam folder."""
    result = await call(server, "gmx_move", {"refs": [ref(1)], "target": "Spamverdacht"})
    assert result["is_error"] is False


# --------------------------------------------------------------------------- spam


async def test_mark_spam_and_back(server: McpServer, imap: FakeImapServer) -> None:
    spam = await call(server, "gmx_mark_spam", {"refs": [ref(2)]})
    assert spam["data"]["data"]["new_refs"] == [ref(1, "Spamverdacht", 900)]
    assert imap.mailbox("Spamverdacht").messages[1].flags == (b"$Junk",)
    wrong = await call(server, "gmx_mark_not_spam", {"refs": [ref(1)]})
    assert (wrong["data"]["error"], wrong["data"]["reason"]) == ("wrong_folder", "not_in_junk")
    back = await call(server, "gmx_mark_not_spam", {"refs": [ref(1, "Spamverdacht", 900)]})
    assert back["data"]["data"]["new_refs"] == [ref(4)]
    assert imap.mailbox("INBOX").messages[4].flags == (b"$NotJunk",)


# --------------------------------------------------------------------------- folders


async def test_folder_tools(server: McpServer, imap: FakeImapServer) -> None:
    created = await call(server, "gmx_create_folder", {"name": "Rechnungen", "parent": "Archiv"})
    assert created["data"]["data"] == {"path": "Archiv/Rechnungen"}
    renamed = await call(server, "gmx_rename_folder", {"folder": "Archiv/Rechnungen", "new_name": "Belege"})
    assert renamed["data"]["data"] == {"old_path": "Archiv/Rechnungen", "path": "Archiv/Belege"}
    unsubscribed = await call(server, "gmx_subscribe_folder", {"folder": "Archiv/Belege", "subscribed": False})
    assert unsubscribed["data"]["data"] == {"path": "Archiv/Belege", "subscribed": False}
    listing = await call(server, "gmx_list_folders", {})
    by_path = {f["path"]: f for f in listing["data"]["data"]["folders"]}
    assert by_path["Archiv/Belege"]["subscribed"] is False
    assert by_path["INBOX"]["subscribed"] is True


@pytest.mark.parametrize(
    ("tool", "arguments", "error"),
    [
        ("gmx_create_folder", {"name": "a/b"}, "invalid_folder_name"),
        ("gmx_create_folder", {"name": "Archiv"}, "folder_exists"),
        ("gmx_create_folder", {"name": "x", "parent": "Gesendet"}, "protected_folder"),
        ("gmx_rename_folder", {"folder": "Gelöscht", "new_name": "Papierkorb"}, "protected_folder"),
        ("gmx_rename_folder", {"folder": "INBOX", "new_name": "Eingang"}, "protected_folder"),
        ("gmx_rename_folder", {"folder": "Fehlt", "new_name": "x"}, "folder_not_found"),
        ("gmx_subscribe_folder", {"folder": "Fehlt"}, "folder_not_found"),
    ],
)
async def test_folder_errors(server: McpServer, imap: FakeImapServer, tool: str, arguments: dict[str, Any], error: str) -> None:
    result = await call(server, tool, arguments)
    assert result["is_error"] is True
    assert result["data"]["error"] == error
    assert not _writes(imap)


# --------------------------------------------------------------------------- drafts


async def test_save_draft_with_attachments(server: McpServer, imap: FakeImapServer) -> None:
    pdf = b"%PDF-1.4\n" + b"x" * 50
    result = await call(
        server,
        "gmx_save_draft",
        {
            "to": ["Anna <anna@example.org>"],
            "subject": "Unterlagen",
            "body_text": "Hallo Anna",
            "in_reply_to": ref(2),
            "attachments": [_file(pdf, "../rechnung.pdf"), _file(PNG_1X1, "bild.jpg", "image/jpeg")],
        },
    )
    assert result["is_error"] is False, result
    data = result["data"]
    assert data["data"]["ref"] == ref(1, "Entwürfe", 900)
    assert data["data"]["attachments"] == [
        {"filename": "rechnung.pdf", "mime": "application/pdf", "size": len(pdf)},
        {"filename": "bild.jpg", "mime": "image/png", "size": len(PNG_1X1)},
    ]
    assert data["warnings"] == ["type_mismatch"]
    stored = message_from_bytes(imap.appended[0], policy=policy.default)
    assert stored["From"] == EMAIL
    assert stored["In-Reply-To"] == "<m2@example.org>"
    assert stored["Subject"] == "Unterlagen"
    assert [p.get_content_type() for p in stored.iter_attachments()] == ["application/pdf", "image/png"]
    assert next(c for c in imap.commands if c[0] == "append")[1:3] == ("Entwürfe", ("\\Draft", "\\Seen"))


@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        ({"to": ["kein-adresse"]}, "invalid_arguments"),
        ({"to": ["a@b.de\r\nBcc: x@y.de"]}, "invalid_arguments"),
        ({"subject": "Zeile 1\r\nBcc: x@y.de"}, "invalid_arguments"),
        ({"body_text": 5}, "invalid_arguments"),
        ({"attachments": [{"filename": "a", "content_base64": "eA==", "sha256": "0" * 64, "extra": 1}]}, "invalid_arguments"),
        ({"attachments": [{"filename": "a", "content_base64": "eA==", "sha256": "0" * 64}]}, "sha256_mismatch"),
        ({"attachments": [{"filename": "a", "content_base64": "%%%", "sha256": "0" * 64}]}, "invalid_base64"),
    ],
)
async def test_save_draft_refusals(server: McpServer, imap: FakeImapServer, arguments: dict[str, Any], error: str) -> None:
    result = await call(server, "gmx_save_draft", {"body_text": "x", **arguments} if "body_text" not in arguments else arguments)
    assert result["is_error"] is True
    assert result["data"]["error"] == error
    assert not imap.appended


@pytest.mark.parametrize("options", [{CONF_MAX_UPLOAD_MB: 1}])
async def test_save_draft_upload_limit(server: McpServer, imap: FakeImapServer, options: dict[str, Any]) -> None:
    big = b"\x00" * (1024 * 1024 + 1)
    result = await call(server, "gmx_save_draft", {"body_text": "x", "attachments": [_file(big, "gross.bin")]})
    assert (result["data"]["error"], result["data"]["index"]) == ("attachment_too_large", 0)
    assert not imap.appended
