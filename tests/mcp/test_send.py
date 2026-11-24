"""The M5 sending tools (level S) against the in-memory IMAP server and a fake SMTP server."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import ssl
from collections.abc import Generator
from email import message_from_bytes, policy
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.gmx_mail_mcp.audit import AuditLog, SendCounter
from custom_components.gmx_mail_mcp.client.imap import ConnectionLimiter, ImapSettings, ImapWorker
from custom_components.gmx_mail_mcp.client.smtp import SmtpSettings
from custom_components.gmx_mail_mcp.const import Permission, Security
from custom_components.gmx_mail_mcp.mcp.protocol import REQUEST_USER, McpServer
from custom_components.gmx_mail_mcp.mcp.registry import (
    CONFIRMABLE_TOOLS,
    TOOL_SPECS,
    ToolContext,
    build_tools,
    default_enabled_tools,
)
from tests.client.fakes import EMAIL, PASSWORD, FakeImapServer, FakeSmtpServer, mixed_message, plain_message

S_TOOLS = ("gmx_send_message", "gmx_reply", "gmx_forward", "gmx_send_draft")
INBOX_VALIDITY = 777
DRAFTS_VALIDITY = 900
SECRET_SUBJECT = "Geheimer Betreff 4711"
SECRET_TEXT = "Vertraulicher Inhalt 0815"
ALLOW = {"send_mode": "allowlist", "allowlist": ["anna@example.org", "*@firma.de"]}
SMTP = SmtpSettings(host="mail.gmx.net", port=587, security=Security.STARTTLS, username=EMAIL, password=PASSWORD)


async def _inline(func: Any, *args: Any) -> Any:
    return func(*args)


@pytest.fixture
def imap() -> Generator[FakeImapServer]:
    server = FakeImapServer()
    inbox = server.mailbox("INBOX", INBOX_VALIDITY)
    inbox.messages[1] = plain_message(
        subject=SECRET_SUBJECT, body=SECRET_TEXT, sender="Fremder <fremd@other.example>", message_id="<orig1@example.org>"
    )
    inbox.messages[2] = plain_message(
        subject="Runde",
        sender="Anna <anna@example.org>",
        to="user@gmx.test, kollege@firma.de",
        extra_headers="Cc: fremd@other.example\r\n",
        message_id="<orig2@example.org>",
    )
    inbox.messages[5] = mixed_message(html="<p>Rechnung</p>")  # From shop@example.com, Reply-To other@evil.example, PDF
    server.mailbox("Gesendet", 901)
    server.mailbox("Gelöscht", 902)
    drafts = server.mailbox("Entwürfe", DRAFTS_VALIDITY)
    draft = (
        b"From: alt@example.org\r\nTo: Anna <anna@example.org>\r\nBcc: chef@firma.de\r\nSubject: Entwurf\r\n"
        b"Message-ID: <d1@gmx.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHallo Anna\r\n"
    )
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", server):
        client = server("imap.gmx.net", 993)
        client.login(EMAIL, PASSWORD)
        client.append("Entwürfe", draft, flags=("\\Draft", "\\Seen"))
        client.append("Entwürfe", draft, flags=("\\Seen",))  # not a draft
        assert set(drafts.messages) == {1, 2}
        server.commands.clear()
        yield server


@pytest.fixture
def smtp() -> Generator[FakeSmtpServer]:
    fake = FakeSmtpServer()
    with (
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP", fake.factory(implicit_tls=False)),
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP_SSL", fake.factory(implicit_tls=True)),
    ):
        yield fake


@pytest.fixture
def options() -> dict[str, Any]:
    return dict(ALLOW)


@pytest.fixture
def auth_failures() -> list[int]:
    return []


@pytest.fixture
def context(imap: FakeImapServer, smtp: FakeSmtpServer, options: dict[str, Any], auth_failures: list[int]) -> ToolContext:
    settings = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password=PASSWORD)
    worker = ImapWorker(settings, executor=_inline, ssl_context_factory=ssl.create_default_context, limiter=ConnectionLimiter())
    return ToolContext(
        worker=worker,
        options=lambda: options,
        executor=_inline,
        account="Konto A",
        email=EMAIL,
        audit=AuditLog(),
        sends=SendCounter(),
        smtp=SMTP,
        on_smtp_auth_failed=lambda: auth_failures.append(1),
    )


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
    text = result["content"][0]["text"]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = {"text": text}
    return {"is_error": result["isError"], "data": data, "raw": text}


async def send_confirmed(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    preview = await call(server, name, arguments)
    assert not preview["is_error"], preview
    token = preview["data"]["data"]["confirm_token"]
    return await call(server, name, {**arguments, "confirm_token": token})


def ref(uid: int, folder: str = "INBOX", validity: int = INBOX_VALIDITY) -> dict[str, Any]:
    return {"folder": folder, "uid": uid, "uidvalidity": validity}


def dref(uid: int) -> dict[str, Any]:
    return ref(uid, "Entwürfe", DRAFTS_VALIDITY)


def _file(content: bytes, name: str = "notiz.txt") -> dict[str, Any]:
    return {"filename": name, "content_base64": base64.b64encode(content).decode(), "sha256": hashlib.sha256(content).hexdigest()}


def _sent_copies(imap: FakeImapServer) -> list[Any]:
    return list(imap.mailboxes["Gesendet"].messages.values())


def _parse(raw: bytes) -> Any:
    return message_from_bytes(raw, policy=policy.default)


# --------------------------------------------------------------------------- catalogue


def test_send_tools_are_level_s_off_by_default_and_confirmable() -> None:
    specs = {spec.name: spec for spec in TOOL_SPECS}
    for name in S_TOOLS:
        assert specs[name].level is Permission.SEND
        assert specs[name].confirmable
        assert name not in default_enabled_tools()
    assert ("gmx_delete_permanently", "gmx_delete_folder", *S_TOOLS) == CONFIRMABLE_TOOLS


def test_send_tools_are_marked_destructive(context: ToolContext) -> None:
    tools = {t.name: t.as_dict() for t in build_tools(context)}
    for name in S_TOOLS:
        assert tools[name]["annotations"]["destructiveHint"] is True
        assert tools[name]["annotations"]["readOnlyHint"] is False
        assert "confirm_token" in tools[name]["inputSchema"]["properties"]


def test_switched_off_send_tool_is_neither_listed_nor_callable(context: ToolContext, smtp: FakeSmtpServer) -> None:
    tools = build_tools(context)
    off = McpServer(name="t", version="1", tools=lambda: [t for t in tools if t.name not in S_TOOLS])

    async def run() -> None:
        listed = await off.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert listed.body is not None
        assert not {t["name"] for t in listed.body["result"]["tools"]} & set(S_TOOLS)
        for name in S_TOOLS:
            outcome = await off.handle_message(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": {"to": ["anna@example.org"]}}}
            )
            assert outcome.body is not None and outcome.body["error"]["code"] == -32602

    asyncio.run(run())
    assert smtp.sessions == []


# --------------------------------------------------------------------------- mode and allowlist (D46, D47)


@pytest.mark.parametrize(
    "options", [{}, {"send_mode": "allowlist", "allowlist": []}, {"send_mode": "off", "allowlist": ["anna@example.org"]}]
)
async def test_nothing_can_be_sent_with_an_empty_allowlist(server: McpServer, smtp: FakeSmtpServer) -> None:
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "subject": "x"})
    assert result["is_error"] and result["data"]["error"] == "sending_disabled"
    assert smtp.sessions == []


async def test_recipient_outside_the_allowlist_is_refused_with_domains_only(
    server: McpServer, context: ToolContext, smtp: FakeSmtpServer
) -> None:
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org", "mallory@evil.example", "x@sub.firma.de"]})
    assert result["is_error"] and result["data"]["error"] == "recipient_not_allowed"
    assert result["data"]["count"] == 2 and result["data"]["domains"] == ["evil.example", "sub.firma.de"]
    assert "mallory" not in result["raw"]
    assert smtp.sessions == []
    entry = context.audit.latest(1)[0]
    assert entry["outcome"] == "error" and entry["error"] == "recipient_not_allowed"
    assert entry["recipient_domains"] == ["evil.example", "example.org", "sub.firma.de"]
    assert "@" not in json.dumps(entry)


@pytest.mark.parametrize("options", [{"send_mode": "any", "allowlist": []}])
async def test_mode_any_allows_every_address_without_a_wildcard(server: McpServer, smtp: FakeSmtpServer) -> None:
    result = await send_confirmed(server, "gmx_send_message", {"to": ["wer@irgendwo.example"], "subject": "x", "body_text": "y"})
    assert not result["is_error"], result
    assert [d[1] for d in smtp.delivered] == [["wer@irgendwo.example"]]


@pytest.mark.parametrize("options", [{"send_mode": "allowlist", "allowlist": ["*"]}])
async def test_a_stored_wildcard_does_not_open_the_allowlist(server: McpServer, smtp: FakeSmtpServer) -> None:
    result = await call(server, "gmx_send_message", {"to": ["wer@irgendwo.example"]})
    assert result["data"]["error"] == "sending_disabled"


# --------------------------------------------------------------------------- two steps (D45)


async def test_preview_sends_nothing_and_the_code_sends_exactly_once(
    server: McpServer, context: ToolContext, smtp: FakeSmtpServer, imap: FakeImapServer
) -> None:
    args = {"to": ["Anna <anna@example.org>"], "bcc": ["chef@firma.de"], "subject": "Hallo", "body_text": "Text"}
    preview = await call(server, "gmx_send_message", args)
    assert not preview["is_error"]
    data = preview["data"]["data"]
    assert data["sent"] is False and data["expires_in"] == 300
    assert data["preview"]["to"] == ["anna@example.org"] and data["preview"]["bcc"] == ["chef@firma.de"]
    assert data["preview"]["subject"] == "Hallo" and "Text" in data["preview"]["text_start"]
    assert data["preview"]["html"] is False and data["preview"]["sent_copy_mode"] == "append"
    assert preview["data"]["content_trust"] == "untrusted_external_email"
    assert smtp.sessions == [] and context.sends.sent_today == 0
    assert _sent_copies(imap) == []

    sent = await call(server, "gmx_send_message", {**args, "confirm_token": data["confirm_token"]})
    assert not sent["is_error"], sent
    body = sent["data"]["data"]
    assert body["sent"] is True and body["recipients"] == 2 and body["sent_copy"] == "appended" and body["confirmation"] == "code"
    assert len(smtp.delivered) == 1
    sender, recipients, wire = smtp.delivered[0]
    assert sender == EMAIL and recipients == ["anna@example.org", "chef@firma.de"]
    assert _parse(wire)["Bcc"] is None  # Bcc only in the SMTP envelope
    copies = _sent_copies(imap)
    assert len(copies) == 1 and b"\\Seen" in copies[0].flags and b"Bcc: chef@firma.de" in copies[0].header
    assert context.sends.sent_today == 1 and context.sends.recipients_last_hour == 2

    again = await call(server, "gmx_send_message", {**args, "confirm_token": data["confirm_token"]})
    assert again["is_error"] and again["data"]["error"] == "confirm_token_invalid"
    assert len(smtp.delivered) == 1


@pytest.mark.parametrize(
    "change",
    [
        {"to": ["anna@example.org", "x@firma.de"]},
        {"subject": "Anderer Betreff"},
        {"body_text": "Anderer Text"},
        {"bcc": ["chef@firma.de"]},
        {"body_html": "<p>neu</p>"},
    ],
)
async def test_code_is_bound_to_the_arguments(server: McpServer, smtp: FakeSmtpServer, change: dict[str, Any]) -> None:
    args = {"to": ["anna@example.org"], "subject": "Hallo", "body_text": "Text"}
    preview = await call(server, "gmx_send_message", args)
    result = await call(server, "gmx_send_message", {**args, **change, "confirm_token": preview["data"]["data"]["confirm_token"]})
    assert result["data"]["error"] == "confirm_token_invalid"
    assert smtp.delivered == []


async def test_code_of_another_tool_is_refused(server: McpServer, smtp: FakeSmtpServer) -> None:
    preview = await call(server, "gmx_reply", {"ref": ref(2), "body_text": "Danke"})
    token = preview["data"]["data"]["confirm_token"]
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "confirm_token": token})
    assert result["data"]["error"] == "confirm_token_invalid"
    assert smtp.delivered == []


async def test_forged_code_is_refused(server: McpServer, smtp: FakeSmtpServer) -> None:
    args = {"to": ["anna@example.org"]}
    token = (await call(server, "gmx_send_message", args))["data"]["data"]["confirm_token"]
    forged = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    assert (await call(server, "gmx_send_message", {**args, "confirm_token": forged}))["data"]["error"] == "confirm_token_invalid"
    assert smtp.delivered == []


@pytest.mark.parametrize("options", [{**ALLOW, "tools_without_confirmation": ["gmx_send_message"]}])
async def test_confirmation_can_be_switched_off_per_tool(server: McpServer, context: ToolContext, smtp: FakeSmtpServer) -> None:
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "subject": "x"})
    assert not result["is_error"] and result["data"]["data"]["confirmation"] == "skipped"
    assert len(smtp.delivered) == 1
    assert context.audit.latest(1)[0]["confirmation"] == "skipped"
    # Only the chosen tool: a reply still needs the code.
    reply = await call(server, "gmx_reply", {"ref": ref(2), "body_text": "Danke"})
    assert reply["data"]["data"]["sent"] is False and len(smtp.delivered) == 1


# --------------------------------------------------------------------------- content


async def test_attachments_are_checked_and_bound_by_content(server: McpServer, smtp: FakeSmtpServer) -> None:
    good = _file(b"%PDF-1.4 Inhalt", "rechnung.pdf")
    bad = {**good, "sha256": "0" * 64}
    refused = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "attachments": [bad]})
    assert refused["data"]["error"] == "sha256_mismatch"
    args = {"to": ["anna@example.org"], "attachments": [good]}
    preview = await call(server, "gmx_send_message", args)
    files = preview["data"]["data"]["preview"]["attachments"]
    assert files == [{"filename": "rechnung.pdf", "mime": "application/pdf", "size": 15}]
    other = _file(b"%PDF-1.4 Anders!", "rechnung.pdf")
    token = preview["data"]["data"]["confirm_token"]
    swapped = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "attachments": [other], "confirm_token": token})
    assert swapped["data"]["error"] == "confirm_token_invalid"
    sent = await call(server, "gmx_send_message", {**args, "confirm_token": token})
    assert not sent["is_error"]
    message = _parse(smtp.delivered[0][2])
    assert [p.get_filename() for p in message.iter_attachments()] == ["rechnung.pdf"]


async def test_html_is_sent_alongside_and_shown_in_the_preview(server: McpServer, smtp: FakeSmtpServer) -> None:
    args = {"to": ["anna@example.org"], "body_text": "Text", "body_html": "<p>Text</p>"}
    preview = await call(server, "gmx_send_message", args)
    assert preview["data"]["data"]["preview"]["html"] is True and preview["data"]["data"]["preview"]["html_chars"] > 0
    await call(server, "gmx_send_message", {**args, "confirm_token": preview["data"]["data"]["confirm_token"]})
    message = _parse(smtp.delivered[0][2])
    assert message.get_body(preferencelist=("html",)) is not None and message.get_body(preferencelist=("plain",)) is not None


@pytest.mark.parametrize("options", [{**ALLOW, "sender_name": "Haus Assistent"}])
async def test_sender_is_the_account_with_the_configured_name(server: McpServer, smtp: FakeSmtpServer) -> None:
    await send_confirmed(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert str(_parse(smtp.delivered[0][2])["From"]) == f"Haus Assistent <{EMAIL}>"


@pytest.mark.parametrize(
    ("args", "error"),
    [
        ({"to": ["kein-at"]}, "invalid_arguments"),
        ({"to": ["anna@example.org"], "subject": "a\nBcc: x@evil.example"}, "invalid_arguments"),
        ({"to": ["anna@example.org"], "unknown": 1}, "invalid_arguments"),
        ({}, "no_recipients"),
    ],
)
async def test_invalid_input_sends_nothing(server: McpServer, smtp: FakeSmtpServer, args: dict[str, Any], error: str) -> None:
    result = await call(server, "gmx_send_message", args)
    assert result["is_error"] and result["data"]["error"] == error
    assert smtp.sessions == []


# --------------------------------------------------------------------------- limits (D48, D54)


@pytest.mark.parametrize("options", [{**ALLOW, "max_recipients": 2}])
async def test_too_many_recipients(server: McpServer) -> None:
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "cc": ["a@firma.de", "b@firma.de"]})
    assert result["data"]["error"] == "too_many_recipients" and result["data"]["limit"] == 2


@pytest.mark.parametrize("options", [{**ALLOW, "max_send_per_day": 1, "tools_without_confirmation": ["gmx_send_message"]}])
async def test_daily_limit(server: McpServer, smtp: FakeSmtpServer) -> None:
    assert not (await call(server, "gmx_send_message", {"to": ["anna@example.org"]}))["is_error"]
    second = await call(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert second["data"]["error"] == "daily_send_limit" and second["data"]["limit"] == 1
    assert len(smtp.delivered) == 1


@pytest.mark.parametrize(
    "options",
    [{**ALLOW, "max_send_per_day": 0, "max_recipients_per_hour": 0, "tools_without_confirmation": ["gmx_send_message"]}],
)
async def test_zero_means_no_daily_or_hourly_limit(server: McpServer, context: ToolContext, smtp: FakeSmtpServer) -> None:
    for counter in range(25):
        context.sends.add(10)
        assert counter >= 0
    assert not (await call(server, "gmx_send_message", {"to": ["anna@example.org"]}))["is_error"]
    assert len(smtp.delivered) == 1


@pytest.mark.parametrize("options", [{**ALLOW, "max_recipients_per_hour": 3}])
async def test_hourly_recipient_limit(server: McpServer, context: ToolContext) -> None:
    context.sends.add(2)
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org", "x@firma.de"]})
    assert result["data"]["error"] == "hourly_recipient_limit" and result["data"]["remaining"] == 1
    assert not (await call(server, "gmx_send_message", {"to": ["anna@example.org"]}))["is_error"]


@pytest.mark.parametrize("options", [{**ALLOW, "max_message_size_mb": 1, "max_upload_mb": 5}])
async def test_message_size_limit(server: McpServer, smtp: FakeSmtpServer) -> None:
    big = _file(b"x" * (1024 * 1024 + 10), "gross.bin")
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "attachments": [big]})
    assert result["data"]["error"] == "message_too_large" and result["data"]["limit_mb"] == 1
    assert smtp.sessions == []


async def test_appendlimit_guards_the_sent_copy(server: McpServer, imap: FakeImapServer, smtp: FakeSmtpServer) -> None:
    imap.capabilities = tuple(c if not c.startswith(b"APPENDLIMIT") else b"APPENDLIMIT=500" for c in imap.capabilities)
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org"], "body_text": "y" * 600})
    assert result["data"]["error"] == "message_too_large" and result["data"]["reason"] == "sent_copy"
    assert smtp.sessions == []


# --------------------------------------------------------------------------- SMTP outcomes


async def test_unknown_outcome_counts_and_is_not_repeated(server: McpServer, context: ToolContext, smtp: FakeSmtpServer) -> None:
    smtp.data_error = TimeoutError()
    result = await send_confirmed(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert result["is_error"] and result["data"]["error"] == "outcome_unknown"
    assert len(smtp.sessions) == 1  # one attempt only
    assert context.sends.sent_today == 1
    assert context.audit.latest(1)[0]["outcome"] == "unknown"


async def test_smtp_login_rejected_reports_and_counts_nothing(
    server: McpServer, context: ToolContext, smtp: FakeSmtpServer, auth_failures: list[int], imap: FakeImapServer
) -> None:
    smtp.password = "other"
    result = await send_confirmed(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert result["data"]["error"] == "smtp_auth_failed" and auth_failures == [1]
    assert context.sends.sent_today == 0 and _sent_copies(imap) == []


@pytest.mark.parametrize(
    ("setup", "error"),
    [
        ({"refused_recipients": {"anna@example.org"}}, "smtp_recipient_rejected"),
        ({"sender_code": 553}, "smtp_sender_rejected"),
        ({"data_code": 554}, "smtp_message_rejected"),
        ({"size": "10"}, "message_too_large"),
        ({"connect_error": ConnectionRefusedError()}, "cannot_connect"),
    ],
)
async def test_refusals_send_nothing_and_count_nothing(
    server: McpServer, context: ToolContext, smtp: FakeSmtpServer, imap: FakeImapServer, *, setup: dict[str, Any], error: str
) -> None:
    for key, value in setup.items():
        setattr(smtp, key, value)
    result = await send_confirmed(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert result["is_error"] and result["data"]["error"] == error
    assert smtp.delivered == [] and context.sends.sent_today == 0 and _sent_copies(imap) == []


@pytest.mark.parametrize("options", [{**ALLOW, "sent_copy_mode": "none"}])
async def test_no_sent_copy_when_switched_off(server: McpServer, imap: FakeImapServer) -> None:
    result = await send_confirmed(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert result["data"]["data"]["sent_copy"] == "none" and _sent_copies(imap) == []


async def test_failed_sent_copy_is_a_warning_not_an_error(server: McpServer, imap: FakeImapServer, smtp: FakeSmtpServer) -> None:
    args = {"to": ["anna@example.org"]}
    token = (await call(server, "gmx_send_message", args))["data"]["data"]["confirm_token"]
    del imap.mailboxes["Gesendet"]  # still listed, but APPEND fails (NO [TRYCREATE])
    result = await call(server, "gmx_send_message", {**args, "confirm_token": token})
    assert not result["is_error"] and result["data"]["data"]["sent"] is True
    assert "sent_copy_failed" in result["data"]["warnings"] and len(smtp.delivered) == 1


# --------------------------------------------------------------------------- reply (D47, D51)


async def test_reply_goes_to_the_sender_marks_answered_and_quotes(server: McpServer, smtp: FakeSmtpServer, imap: FakeImapServer) -> None:
    preview = await call(server, "gmx_reply", {"ref": ref(2), "body_text": "Danke!"})
    data = preview["data"]["data"]["preview"]
    assert data["to"] == ["anna@example.org"] and data["cc"] == [] and data["subject"] == "Re: Runde"
    assert data["answers"] == ref(2) and "UNTRUSTED_EMAIL_CONTENT" in data["text_start"]
    assert imap.commands and not [c for c in imap.commands if c[0] in ("add_flags", "append")]
    token = preview["data"]["data"]["confirm_token"]
    sent = await call(server, "gmx_reply", {"ref": ref(2), "body_text": "Danke!", "confirm_token": token})
    assert not sent["is_error"], sent
    message = _parse(smtp.delivered[0][2])
    assert str(message["In-Reply-To"]) == "<orig2@example.org>" and "> Hallo Welt" in message.get_body().get_content()
    assert b"\\Answered" in imap.mailboxes["INBOX"].messages[2].flags


async def test_reply_all_needs_every_recipient_allowed(server: McpServer, smtp: FakeSmtpServer) -> None:
    result = await call(server, "gmx_reply", {"ref": ref(2), "body_text": "x", "reply_all": True})
    assert result["data"]["error"] == "recipient_not_allowed" and result["data"]["domains"] == ["other.example"]
    assert smtp.sessions == []


@pytest.mark.parametrize("options", [{**ALLOW, "allowlist": ["anna@example.org", "*@firma.de", "fremd@other.example"]}])
async def test_reply_all_adds_to_and_cc_without_the_own_address(server: McpServer) -> None:
    preview = (await call(server, "gmx_reply", {"ref": ref(2), "body_text": "x", "reply_all": True}))["data"]["data"]["preview"]
    assert preview["to"] == ["anna@example.org"] and preview["cc"] == ["kollege@firma.de", "fremd@other.example"]


async def test_reply_to_a_sender_outside_the_allowlist_is_refused_by_default(server: McpServer) -> None:
    result = await call(server, "gmx_reply", {"ref": ref(1), "body_text": "x"})
    assert result["data"]["error"] == "recipient_not_allowed"


@pytest.mark.parametrize("options", [{"send_mode": "allowlist", "allowlist": ["*@firma.de"], "allow_reply_to_sender": True}])
async def test_reply_to_sender_option_covers_only_the_from_address(server: McpServer) -> None:
    # fremd@other.example wrote mail 1 (From): allowed by the option.
    assert not (await call(server, "gmx_reply", {"ref": ref(1), "body_text": "x"}))["is_error"]
    # Mail 5 has a different Reply-To (other@evil.example): the option does not cover it.
    result = await call(server, "gmx_reply", {"ref": ref(5), "body_text": "x"})
    assert result["data"]["error"] == "recipient_not_allowed" and result["data"]["domains"] == ["evil.example"]


@pytest.mark.parametrize("options", [{"send_mode": "allowlist", "allowlist": [], "allow_reply_to_sender": True}])
async def test_reply_option_does_not_help_with_an_empty_allowlist(server: McpServer) -> None:
    assert (await call(server, "gmx_reply", {"ref": ref(1), "body_text": "x"}))["data"]["error"] == "sending_disabled"


async def test_reply_to_a_missing_mail(server: McpServer) -> None:
    result = await call(server, "gmx_reply", {"ref": ref(99), "body_text": "x"})
    assert result["data"]["error"] == "message_not_found"


# --------------------------------------------------------------------------- forward (D50)


@pytest.mark.parametrize("include", [True, False])
async def test_forward_with_or_without_attachments(server: McpServer, smtp: FakeSmtpServer, include: bool) -> None:
    args = {"ref": ref(5), "to": ["anna@example.org"], "comment": "Zur Info", "include_attachments": include}
    preview = (await call(server, "gmx_forward", args))["data"]["data"]
    names = [a["filename"] for a in preview["preview"]["attachments"]]
    assert names == (["Rechnung März.pdf"] if include else [])
    assert preview["preview"]["subject"].startswith("Fwd:")
    await call(server, "gmx_forward", {**args, "confirm_token": preview["confirm_token"]})
    message = _parse(smtp.delivered[0][2])
    assert len(list(message.iter_attachments())) == (1 if include else 0)
    assert "versteckt" not in message.get_body().get_content()


async def test_forward_defaults_to_attachments_and_needs_recipients(server: McpServer) -> None:
    preview = (await call(server, "gmx_forward", {"ref": ref(5), "to": ["anna@example.org"]}))["data"]["data"]["preview"]
    assert len(preview["attachments"]) == 1
    assert (await call(server, "gmx_forward", {"ref": ref(5), "to": []}))["data"]["error"] == "invalid_arguments"


@pytest.mark.parametrize("options", [{**ALLOW, "max_message_size_mb": 1}])
async def test_forward_attachments_count_against_the_size_limit(server: McpServer, imap: FakeImapServer) -> None:
    imap.mailboxes["INBOX"].messages[6] = mixed_message(html="<p>x</p>", pdf=b"%PDF-1.4 " + b"x" * (1024 * 1024 + 10))
    result = await call(server, "gmx_forward", {"ref": ref(6), "to": ["anna@example.org"]})
    assert result["data"]["error"] in ("message_too_large", "too_large")


# --------------------------------------------------------------------------- send draft (D56)


async def test_send_draft_sends_it_unchanged_and_removes_it(server: McpServer, smtp: FakeSmtpServer, imap: FakeImapServer) -> None:
    preview = (await call(server, "gmx_send_draft", {"ref": dref(1)}))["data"]["data"]
    assert preview["preview"]["to"] == ["anna@example.org"] and preview["preview"]["bcc"] == ["chef@firma.de"]
    assert preview["preview"]["draft"] == dref(1)
    assert 1 in imap.mailboxes["Entwürfe"].messages
    sent = await call(server, "gmx_send_draft", {"ref": dref(1), "confirm_token": preview["confirm_token"]})
    assert not sent["is_error"], sent
    _sender, recipients, wire = smtp.delivered[0]
    assert recipients == ["anna@example.org", "chef@firma.de"] and str(_parse(wire)["From"]) == EMAIL
    assert 1 not in imap.mailboxes["Entwürfe"].messages and 2 in imap.mailboxes["Entwürfe"].messages
    assert [c for c in imap.commands if c[0] == "uid_expunge"]
    assert len(_sent_copies(imap)) == 1


async def test_send_draft_only_for_drafts(server: McpServer, smtp: FakeSmtpServer) -> None:
    assert (await call(server, "gmx_send_draft", {"ref": ref(2)}))["data"]["error"] == "not_a_draft"  # not the Drafts folder
    assert (await call(server, "gmx_send_draft", {"ref": dref(2)}))["data"]["error"] == "not_a_draft"  # no \\Draft flag
    assert smtp.sessions == []


async def test_send_draft_refuses_a_draft_outside_the_drafts_folder(server: McpServer, imap: FakeImapServer, smtp: FakeSmtpServer) -> None:
    """A \\Draft-flagged copy in another folder is not sent (and so never removed there)."""
    archive = imap.mailbox("Archiv", 950)
    archive.messages[1] = plain_message(to="anna@example.org", flags=(b"\\Draft",))
    result = await call(server, "gmx_send_draft", {"ref": ref(1, "Archiv", 950)})
    assert result["data"]["error"] == "not_a_draft"
    assert smtp.sessions == [] and 1 in archive.messages


async def test_send_draft_recipients_must_be_allowed(server: McpServer, imap: FakeImapServer, options: dict[str, Any]) -> None:
    options["allowlist"] = ["anna@example.org"]
    result = await call(server, "gmx_send_draft", {"ref": dref(1)})
    assert result["data"]["error"] == "recipient_not_allowed" and result["data"]["domains"] == ["firma.de"]
    assert 1 in imap.mailboxes["Entwürfe"].messages


# --------------------------------------------------------------------------- audit (D41, D55)


async def test_audit_has_domains_but_no_addresses_subjects_or_content(server: McpServer, context: ToolContext) -> None:
    token = REQUEST_USER.set("user-1")
    try:
        await send_confirmed(server, "gmx_send_message", {"to": ["anna@example.org"], "subject": SECRET_SUBJECT, "body_text": SECRET_TEXT})
        await send_confirmed(server, "gmx_forward", {"ref": ref(1), "to": ["x@firma.de"]})
    finally:
        REQUEST_USER.reset(token)
    entries = context.audit.latest(10)
    assert [(e["tool"], e["outcome"]) for e in entries] == [
        ("gmx_forward", "ok"),
        ("gmx_forward", "preview"),
        ("gmx_send_message", "ok"),
        ("gmx_send_message", "preview"),
    ]
    assert entries[0]["recipient_domains"] == ["firma.de"] and entries[0]["folders"] == ["INBOX"]
    assert entries[2]["count"] == 1 and entries[2]["confirmation"] == "code" and entries[2]["user"] == "user-1"
    dump = json.dumps(entries, ensure_ascii=False)
    for secret in ("anna", "@", SECRET_SUBJECT, SECRET_TEXT, "Geheimer", "x@firma"):
        assert secret not in dump


# --------------------------------------------------------------------------- timeout and concurrency (D58)


async def test_timeout_during_smtp_lets_the_sending_finish_and_records_it(
    context: ToolContext, smtp: FakeSmtpServer, imap: FakeImapServer, options: dict[str, Any]
) -> None:
    options["tools_without_confirmation"] = ["gmx_send_message"]

    async def slow(func: Any, *args: Any) -> Any:
        await asyncio.sleep(0.2)
        return func(*args)

    slow_context = ToolContext(
        worker=context.worker,
        options=context.options,
        executor=slow,
        account="Konto A",
        email=EMAIL,
        audit=context.audit,
        sends=context.sends,
        smtp=SMTP,
    )
    tools = build_tools(slow_context)
    server = McpServer(name="t", version="1", tools=lambda: tools, call_timeout=0.3)
    result = await call(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert result["is_error"] and "timed out" in result["raw"]
    for _ in range(50):
        if any(e["outcome"].startswith("late_") for e in context.audit.latest(5)):
            break
        await asyncio.sleep(0.05)
    assert len(smtp.delivered) == 1 and context.sends.sent_today == 1
    outcomes = [e["outcome"] for e in context.audit.latest(5)]
    assert outcomes[:2] == ["late_ok", "cancelled"]
    assert len(_sent_copies(imap)) == 1
    assert not slow_context.send_lock.locked()


@pytest.mark.parametrize("options", [{**ALLOW, "max_send_per_day": 1, "tools_without_confirmation": ["gmx_send_message"]}])
async def test_concurrent_calls_cannot_overrun_the_daily_limit(server: McpServer, smtp: FakeSmtpServer) -> None:
    results = await asyncio.gather(*(call(server, "gmx_send_message", {"to": ["anna@example.org"]}) for _ in range(3)))
    assert sorted(r["is_error"] for r in results) == [False, True, True]
    assert len(smtp.delivered) == 1


async def test_lock_is_released_after_errors(server: McpServer, context: ToolContext, smtp: FakeSmtpServer) -> None:
    await call(server, "gmx_send_message", {"to": ["mallory@evil.example"]})
    await call(server, "gmx_reply", {"ref": ref(99), "body_text": "x"})
    smtp.data_error = TimeoutError()
    await send_confirmed(server, "gmx_send_message", {"to": ["anna@example.org"]})
    assert not context.send_lock.locked()


# --------------------------------------------------------------------------- counter


def test_audit_keeps_only_the_domain_even_if_given_an_address() -> None:
    """Defence in depth (D55): whatever reaches record(), only the part after the last @ is stored."""
    entry = AuditLog().record(
        tool="gmx_send_message",
        level="send",
        outcome="ok",
        arguments={},
        recipient_domains=["Anna@Example.org", "x@y@firma.de", "gmx.de", "", "a@\x00evil.example"],
    )
    assert entry["recipient_domains"] == ["evil.example", "example.org", "firma.de", "gmx.de"]


def test_send_counter_day_hour_and_snapshot() -> None:
    now = [1_000_000.0]
    day = ["2026-11-24"]
    counter = SendCounter(today=lambda: day[0], clock=lambda: now[0])
    counter.add(3)
    now[0] += 1800
    counter.add(2)
    assert counter.sent_today == 2 and counter.recipients_last_hour == 5
    now[0] += 1801
    assert counter.recipients_last_hour == 2  # the first message left the window
    snap = counter.snapshot()
    restored = SendCounter(today=lambda: day[0], clock=lambda: now[0], stored=snap)
    assert restored.sent_today == 2 and restored.recipients_last_hour == 2
    day[0] = "2026-11-25"
    assert restored.sent_today == 0
    assert SendCounter(today=lambda: "2026-11-24", stored={"date": "x", "sent": "y", "recipients": [["a"], None, [1, -3]]}).sent_today == 0
