"""Unit tests for sending (M6): recipient policy, message composition and the SMTP send step."""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Generator
from email import message_from_bytes, policy
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.gmx_mail_mcp.client.composer import (
    MAX_QUOTE_CHARS,
    Attachment,
    Original,
    forward_message,
    load_draft,
    load_original,
    new_message,
    reply_message,
)
from custom_components.gmx_mail_mcp.client.errors import (
    GmxConnectionError,
    GmxMailError,
    GmxOutcomeUnknownError,
    GmxSmtpAuthError,
    GmxSmtpRejectedError,
)
from custom_components.gmx_mail_mcp.client.imap import ImapSession, ServerInfo, parse_capabilities
from custom_components.gmx_mail_mcp.client.policy import RecipientPolicy, allowed_by_list
from custom_components.gmx_mail_mcp.client.reader import MailRef
from custom_components.gmx_mail_mcp.client.smtp import SmtpSettings, send_message
from custom_components.gmx_mail_mcp.const import Security

from .fakes import EMAIL, GMX_CAPABILITIES, PASSWORD, FakeImapServer, FakeSmtpServer, mixed_message, plain_message

GMX_INFO = ServerInfo(capabilities=parse_capabilities(GMX_CAPABILITIES), folders=())
SMTP = SmtpSettings(host="mail.gmx.net", port=587, security=Security.STARTTLS, username=EMAIL, password=PASSWORD)


# --------------------------------------------------------------------------- policy


def test_allowlist_matching() -> None:
    """Exact addresses and *@domain (no subdomains, case-insensitive); * allows all."""
    allow = ["anna@example.org", "*@firma.de"]
    assert allowed_by_list("Anna@Example.org", allow)
    assert allowed_by_list("x@firma.de", allow)
    assert not allowed_by_list("x@sub.firma.de", allow)
    assert not allowed_by_list("x@firma.de.evil.com", allow)
    assert not allowed_by_list("bob@example.org", allow)
    assert allowed_by_list("anyone@anywhere.test", ["*"])


def test_policy_modes_and_reply_exception() -> None:
    """Off/empty list blocks everything; the reply exception covers exactly the original From address."""
    assert RecipientPolicy("off", ["anna@example.org"], True).blocked(["anna@example.org"]) == ["anna@example.org"]
    assert RecipientPolicy("allowlist", [], False).blocked(["a@b.de"]) == ["a@b.de"]
    assert RecipientPolicy("any", ["*"], False).blocked(["a@b.de"]) == []
    # D46: "any" needs no "*" entry; in allowlist mode a stored "*" counts for nothing.
    assert RecipientPolicy("any", [], False).blocked(["a@b.de"]) == []
    assert RecipientPolicy("any", [], False).sending_enabled
    assert RecipientPolicy("allowlist", ["*"], False).blocked(["a@b.de"]) == ["a@b.de"]
    assert not RecipientPolicy("allowlist", ["*"], False).sending_enabled
    allow = RecipientPolicy("allowlist", ["anna@example.org"], True)
    assert allow.blocked(["shop@example.com"], original_sender="Shop@Example.com") == []
    # Reply-To differs from From (phishing pattern): the reply target is not covered by the exception.
    assert allow.blocked(["collect@evil.test"], original_sender="shop@example.com") == ["collect@evil.test"]
    assert RecipientPolicy("allowlist", ["anna@example.org"], False).blocked(["shop@example.com"], original_sender="shop@example.com") == [
        "shop@example.com"
    ]


# --------------------------------------------------------------------------- composer


def _parse(raw: bytes) -> Any:
    return message_from_bytes(raw, policy=policy.default)


def test_new_message_bcc_only_in_envelope_and_copy() -> None:
    """Bcc recipients get the mail (envelope) but are not visible to others; the Sent copy keeps them."""
    out = new_message(
        sender=EMAIL,
        sender_name="Haus Assistent",
        to=["Anna <anna@example.org>"],
        cc=["b@example.org"],
        bcc=["geheim@example.org"],
        subject="Hallo",
        body_text="Text äöü",
        body_html="<p>Text</p>",
        attachments=[Attachment("plan.pdf", b"%PDF-1.4")],
    )
    assert out.recipients == ["anna@example.org", "b@example.org", "geheim@example.org"]
    wire, stored = _parse(out.wire_bytes), _parse(out.copy_bytes)
    assert wire["Bcc"] is None and "geheim@example.org" in str(stored["Bcc"])
    assert str(wire["From"]) == f"Haus Assistent <{EMAIL}>"
    assert [p.get_filename() for p in wire.iter_attachments()] == ["plan.pdf"]
    preview = out.preview()
    assert preview["bcc"] == ["geheim@example.org"] and preview["attachments"][0]["filename"] == "plan.pdf"
    assert preview["text_start"].startswith("Text äöü") and preview["size"] == len(out.wire_bytes)


def test_new_message_rejects_header_injection() -> None:
    """Line breaks in addresses are refused (header injection)."""
    with pytest.raises(ValueError):
        new_message(
            sender=EMAIL,
            sender_name="",
            to=["a@b.de\r\nBcc: x@evil.test"],
            cc=[],
            bcc=[],
            subject="",
            body_text="",
            body_html=None,
            attachments=[],
        )


def _original(**changes: Any) -> Original:
    values: dict[str, Any] = {
        "ref": MailRef("INBOX", 5, 777),
        "from_addr": "shop@example.com",
        "from_display": "Shop <shop@example.com>",
        "reply_to": [],
        "to": [EMAIL, "kollege@example.org"],
        "cc": ["chef@example.org"],
        "subject": "Rechnung",
        "message_id": "<m5@example.com>",
        "references": "<m1@example.com>",
        "date": "2026-11-20T10:00:00+00:00",
        "text": "Zeile 1\n\nZeile 3",
    }
    values.update(changes)
    return Original(**values)


def test_reply_targets_quote_and_threading() -> None:
    """Reply goes to Reply-To if present; reply-all adds To/Cc minus the own address; quote and headers are set."""
    out = reply_message(_original(), sender=EMAIL, sender_name="", body_text="Danke!", reply_all=False, quote=True)
    assert out.recipients == ["shop@example.com"] and out.original_sender == "shop@example.com" and out.answered == MailRef("INBOX", 5, 777)
    msg = _parse(out.wire_bytes)
    assert str(msg["Subject"]) == "Re: Rechnung"
    assert str(msg["In-Reply-To"]) == "<m5@example.com>" and str(msg["References"]) == "<m1@example.com> <m5@example.com>"
    body = msg.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n")
    assert body.startswith("Danke!") and "> Zeile 1\n>\n> Zeile 3" in body
    everyone = reply_message(
        _original(reply_to=["antwort@example.com"]), sender=EMAIL, sender_name="", body_text="x", reply_all=True, quote=False
    )
    assert everyone.recipients == ["antwort@example.com", "kollege@example.org", "chef@example.org"]
    assert str(_parse(everyone.wire_bytes)["Subject"]) == "Re: Rechnung"


def test_reply_quote_is_capped() -> None:
    """At most MAX_QUOTE_CHARS of the original are quoted."""
    out = reply_message(
        _original(text="#" * (MAX_QUOTE_CHARS + 500)), sender=EMAIL, sender_name="", body_text="", reply_all=False, quote=True
    )
    body = _parse(out.wire_bytes).get_body(preferencelist=("plain",)).get_content()
    assert body.count("#") == MAX_QUOTE_CHARS


def test_reply_without_any_sender_is_refused() -> None:
    """A message without From or Reply-To cannot be answered."""
    with pytest.raises(GmxMailError):
        reply_message(_original(from_addr=None), sender=EMAIL, sender_name="", body_text="x", reply_all=False, quote=False)


def test_forward_inline_with_attachments() -> None:
    """Forward: Fwd: prefix, header block, original text and attachments."""
    original = _original(attachments=[Attachment("Rechnung.pdf", b"%PDF", "application/pdf")])
    out = forward_message(original, sender=EMAIL, sender_name="", to=["anna@example.org"], cc=[], comment="Zur Info")
    msg = _parse(out.wire_bytes)
    assert str(msg["Subject"]) == "Fwd: Rechnung" and out.recipients == ["anna@example.org"]
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert body.startswith("Zur Info") and "Weitergeleitete Nachricht" in body and "Zeile 3" in body
    assert [p.get_filename() for p in msg.iter_attachments()] == ["Rechnung.pdf"]


@pytest.fixture
def server() -> Generator[FakeImapServer]:
    """INBOX with a mail with Reply-To and a PDF; Entwürfe with a draft and a non-draft."""
    fake = FakeImapServer()
    inbox = fake.mailbox("INBOX", uidvalidity=777)
    inbox.messages[5] = mixed_message(html="<p>Hallo <span style='display:none'>versteckt</span>Welt</p>")
    drafts = fake.mailbox("Entwürfe", uidvalidity=900)
    draft = (
        b"From: alt@example.org\r\nTo: Anna <anna@example.org>\r\nBcc: b@example.org\r\nSubject: Entwurf\r\n"
        b"Message-ID: <d1@gmx.test>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHallo Anna\r\n"
    )
    session = _session(fake)
    session.append("Entwürfe", draft, flags=("\\Draft", "\\Seen"))
    session.append("Entwürfe", draft, flags=("\\Seen",))
    assert set(drafts.messages) == {1, 2}
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", fake):
        yield fake


def _session(server: FakeImapServer) -> ImapSession:
    client = server("imap.gmx.net", 993)
    client.login(EMAIL, server.password)
    return client


def test_load_original_reads_sanitized_text_and_attachments(server: FakeImapServer) -> None:
    """The original is read with EXAMINE/BODY.PEEK (plain part preferred); attachments only when asked for."""
    session = _session(server)
    server.commands.clear()  # the fixture appended the drafts
    original = load_original(session, GMX_INFO, MailRef("INBOX", 5, 777), with_attachments=True, max_bytes=10_000_000)
    assert "Plain body" in original.text and original.subject and original.from_addr
    assert [a.filename for a in original.attachments] == ["Rechnung März.pdf"]
    assert not [c for c in server.commands if c[0] in ("add_flags", "append")]
    with pytest.raises(GmxMailError):
        load_original(session, GMX_INFO, MailRef("INBOX", 5, 777), with_attachments=True, max_bytes=5)


def test_load_draft_sets_account_sender(server: FakeImapServer) -> None:
    """From is replaced by the account; recipients include Bcc; only messages flagged \\Draft qualify."""
    session = _session(server)
    out = load_draft(session, GMX_INFO, MailRef("Entwürfe", 1, 900), sender=EMAIL, sender_name="", max_bytes=1_000_000)
    assert out.recipients == ["anna@example.org", "b@example.org"] and out.draft == MailRef("Entwürfe", 1, 900)
    msg = _parse(out.wire_bytes)
    assert str(msg["From"]) == EMAIL and msg["Bcc"] is None and str(msg["Message-ID"]) == "<d1@gmx.test>"
    with pytest.raises(GmxMailError):
        load_draft(session, GMX_INFO, MailRef("Entwürfe", 2, 900), sender=EMAIL, sender_name="", max_bytes=1_000_000)


# --------------------------------------------------------------------------- SMTP


@pytest.fixture
def smtp() -> Generator[FakeSmtpServer]:
    """Fake SMTP server."""
    fake = FakeSmtpServer()
    with (
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP", fake.factory(implicit_tls=False)),
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP_SSL", fake.factory(implicit_tls=True)),
    ):
        yield fake


def test_send_delivers_to_all_recipients(smtp: FakeSmtpServer) -> None:
    """MAIL, RCPT for every recipient, DATA; QUIT afterwards."""
    send_message(SMTP, ssl.create_default_context(), EMAIL, ["a@example.org", "b@example.org"], b"Subject: x\r\n\r\nHi")
    assert smtp.delivered == [(EMAIL, ["a@example.org", "b@example.org"], b"Subject: x\r\n\r\nHi")]
    assert smtp.sessions[0].quit_called


def test_send_refused_recipient_sends_nothing(smtp: FakeSmtpServer) -> None:
    """If one recipient is refused, nothing is sent to the others either."""
    smtp.refused_recipients = {"b@example.org"}
    with pytest.raises(GmxSmtpRejectedError) as err:
        send_message(SMTP, ssl.create_default_context(), EMAIL, ["a@example.org", "b@example.org"], b"x")
    assert err.value.reason == "recipient_rejected" and smtp.delivered == []


@pytest.mark.parametrize(
    ("setup", "error", "reason"),
    [
        ({"password": "other"}, GmxSmtpAuthError, None),
        ({"size": "10"}, GmxSmtpRejectedError, "message_too_large"),
        ({"sender_code": 553}, GmxSmtpRejectedError, "sender_rejected"),
        ({"data_code": 554}, GmxSmtpRejectedError, "message_rejected"),
        ({"data_error": smtplib.SMTPDataError(552, b"too big")}, GmxSmtpRejectedError, "message_rejected"),
        ({"data_error": smtplib.SMTPServerDisconnected("gone")}, GmxOutcomeUnknownError, None),
        ({"data_error": TimeoutError()}, GmxOutcomeUnknownError, None),
        ({"connect_error": ConnectionRefusedError()}, GmxConnectionError, None),
    ],
)
def test_send_failures_are_classified(smtp: FakeSmtpServer, setup: dict[str, Any], error: type[Exception], reason: str | None) -> None:
    """Before DATA nothing was sent; a lost connection during DATA leaves the outcome unknown (never retried)."""
    for key, value in setup.items():
        setattr(smtp, key, value)
    with pytest.raises(error) as err:
        send_message(SMTP, ssl.create_default_context(), EMAIL, ["a@example.org"], b"Subject: x\r\n\r\n" + b"y" * 20)
    if reason is not None:
        assert getattr(err.value, "reason", None) == reason
    assert smtp.delivered == []


def test_plain_message_helper_is_compatible() -> None:
    """Guard: the shared test helper still builds parsable messages (used by the M6 service tests)."""
    message = plain_message(subject="x")
    assert message.header.startswith(b"From:") or b"From:" in message.header


def test_sent_copy_modes_and_follow_up_steps(server: FakeImapServer) -> None:
    """auto finds a server copy by Message-ID; no Sent folder is reported; draft removal needs UIDPLUS."""
    from custom_components.gmx_mail_mcp.client.folders import map_special_folders, parse_folder_list  # noqa: PLC0415
    from custom_components.gmx_mail_mcp.client.sent import mark_answered, remove_draft, store_sent_copy  # noqa: PLC0415

    server.mailbox("Gesendet", uidvalidity=901).messages[1] = plain_message(message_id="<already@gmx.test>")
    session = _session(server)
    roles = map_special_folders(parse_folder_list(server.folders))
    assert store_sent_copy(session, GMX_INFO, roles, b"x", message_id="<already@gmx.test>", mode="auto") == "server"
    raw = b"Message-ID: <new@gmx.test>\r\nSubject: s\r\n\r\nbody\r\n"
    assert store_sent_copy(session, GMX_INFO, roles, raw, message_id="<new@gmx.test>", mode="auto") == "appended"
    assert store_sent_copy(session, GMX_INFO, roles, raw, message_id="<new@gmx.test>", mode="none") == "none"
    no_sent = map_special_folders(parse_folder_list([f for f in server.folders if f[2] != "Gesendet"]))
    assert store_sent_copy(session, GMX_INFO, no_sent, raw, message_id="<x@gmx.test>", mode="append") == "no_sent_folder"
    assert mark_answered(session, GMX_INFO, MailRef("INBOX", 5, 777)) is True
    assert mark_answered(session, GMX_INFO, MailRef("INBOX", 99, 777)) is False
    no_uidplus = ServerInfo(capabilities=parse_capabilities(tuple(c for c in GMX_CAPABILITIES if c != b"UIDPLUS")), folders=())
    assert remove_draft(session, no_uidplus, MailRef("Entwürfe", 1, 900)) is False
    assert remove_draft(session, GMX_INFO, MailRef("Entwürfe", 99, 900)) is False
    assert remove_draft(session, GMX_INFO, MailRef("Entwürfe", 1, 900)) is True
    assert 1 not in server.mailboxes["Entwürfe"].messages and 2 in server.mailboxes["Entwürfe"].messages
