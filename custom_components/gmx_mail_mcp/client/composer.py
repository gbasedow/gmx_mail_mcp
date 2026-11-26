"""Building outgoing messages (M6): new mail, reply, forward and sending a draft - blocking IMAP reads, no SMTP.

* From is always the account address (GMX only accepts own addresses, GMX-NOTES row 21), optionally with a display name;
* Bcc stays in the message for the Sent copy and is removed from the copy that goes over SMTP (``wire_bytes``);
* quotes and forwarded text use the *sanitized* text of the original (no HTML, hidden text or tracking links),
  at most ``MAX_QUOTE_CHARS`` characters (DECISIONS Nr. 61);
* the original is only read (EXAMINE, ``BODY.PEEK``); nothing here changes the mailbox.
"""

from __future__ import annotations

import copy
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import SMTP, default
from email.utils import formataddr, getaddresses, make_msgid
from typing import Any

from .errors import GmxMessageNotFoundError, GmxMessageTooLargeError, GmxSendInputError
from .imap import ImapSession, ServerInfo
from .reader import MailRef, _check_uidvalidity, _find_key, _header_str, examine, get_attachment, get_message, parse_headers
from .structure import attachments as list_attachments
from .structure import parse_bodystructure
from .upload import Attachment
from .writer import parse_addresses

MAX_QUOTE_CHARS = 20_000
FORWARD_SEPARATOR = "---------- Weitergeleitete Nachricht ----------"


@dataclass(slots=True)
class Original:
    """What a reply or forward needs from the original message (addresses as lower-case addr-specs)."""

    ref: MailRef
    from_addr: str | None
    from_display: str
    reply_to: list[str]
    to: list[str]
    cc: list[str]
    subject: str
    message_id: str | None
    references: str
    date: str | None
    text: str
    attachments: list[Attachment] = field(default_factory=list)


@dataclass(slots=True)
class Outgoing:
    """A composed message plus what the action needs to check, preview and finish it."""

    message: EmailMessage
    recipients: list[str]
    original_sender: str | None = None
    answered: MailRef | None = None
    draft: MailRef | None = None

    @property
    def wire_bytes(self) -> bytes:
        """The bytes sent over SMTP: without Bcc (Bcc recipients are only in the SMTP envelope)."""
        wire = copy.deepcopy(self.message)
        del wire["Bcc"]
        return wire.as_bytes(policy=SMTP)

    @property
    def copy_bytes(self) -> bytes:
        """The bytes stored in the Sent folder (Bcc kept, like mail clients do)."""
        return self.message.as_bytes(policy=SMTP)

    def preview(self, text_chars: int = 300) -> dict[str, Any]:
        """Recipients, subject, size, attachments and the start of the text (for the confirmation step)."""
        text = _content(self.message.get_body(preferencelist=("plain",)))
        files = [
            {"filename": part.get_filename(), "mime": part.get_content_type(), "size": len(part.get_payload(decode=True) or b"")}
            for part in self.message.iter_attachments()
        ]
        html = self.message.get_body(preferencelist=("html",))
        html_text = _content(html)
        return {
            "from": str(self.message["From"]),
            "to": _header_addresses(self.message, "To"),
            "cc": _header_addresses(self.message, "Cc"),
            "bcc": _header_addresses(self.message, "Bcc"),
            "subject": str(self.message["Subject"] or ""),
            "size": len(self.wire_bytes),
            "attachments": files,
            "text_start": text[:text_chars],
            "text_chars": len(text),
            # An HTML part is sent as well (D53); its size tells the person that there is more than the text.
            "html": html is not None,
            "html_chars": len(html_text),
        }


def _content(part: Any) -> str:
    """Text of a body part; empty if missing or undecodable (unknown charset in a draft from another program)."""
    if part is None:
        return ""
    try:
        content = part.get_content()
    except LookupError, ValueError:
        return ""
    # Drafts from the server keep CRLF line ends; the preview shows plain line breaks.
    return content.replace("\r\n", "\n").replace("\r", "\n") if isinstance(content, str) else ""


def _header_addresses(message: EmailMessage, name: str) -> list[str]:
    return [addr for _name, addr in getaddresses(message.get_all(name, []))] if message.get(name) else []


def addr_specs(values: Sequence[str]) -> list[str]:
    """Validate and reduce addresses to lower-case addr-specs (ValueError for invalid ones)."""
    return [a.addr_spec.lower() for a in parse_addresses(values)]


def _unique(addresses: Sequence[str], *, exclude: Sequence[str] = ()) -> list[str]:
    seen = {a.lower() for a in exclude}
    result: list[str] = []
    for address in addresses:
        lowered = address.lower()
        if lowered not in seen:
            seen.add(lowered)
            result.append(lowered)
    return result


def _formatted(values: Sequence[str]) -> str:
    return ", ".join(formataddr((a.display_name, a.addr_spec)) for a in parse_addresses(values))


def build_message(
    *,
    sender: str,
    sender_name: str = "",
    to: Sequence[str] = (),
    cc: Sequence[str] = (),
    bcc: Sequence[str] = (),
    subject: str = "",
    body_text: str = "",
    body_html: str | None = None,
    attachments: Sequence[Attachment] = (),
    in_reply_to: str | None = None,
    references: str = "",
) -> EmailMessage:
    """Build a UTF-8 message (RFC 5322); headers are validated by the email policy."""
    message = EmailMessage(policy=SMTP)
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    for header, values in (("To", to), ("Cc", cc), ("Bcc", bcc)):
        if values:
            message[header] = _formatted(values)
    message["Subject"] = subject
    message["Date"] = datetime.now(UTC)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if in_reply_to or references:
        message["References"] = " ".join([*references.split()[-10:], *([in_reply_to] if in_reply_to else [])])
    message.set_content(body_text)
    if body_html:
        message.add_alternative(body_html, subtype="html")
    for item in attachments:
        maintype, _, subtype = item.mime.partition("/")
        message.add_attachment(item.content, maintype=maintype, subtype=subtype or "octet-stream", filename=item.filename)
    return message


def new_message(
    *,
    sender: str,
    sender_name: str,
    to: Sequence[str],
    cc: Sequence[str],
    bcc: Sequence[str],
    subject: str,
    body_text: str,
    body_html: str | None,
    attachments: Sequence[Attachment],
) -> Outgoing:
    """A new message to the given recipients."""
    message = build_message(
        sender=sender,
        sender_name=sender_name,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        attachments=attachments,
    )
    return Outgoing(message=message, recipients=_unique([*addr_specs(to), *addr_specs(cc), *addr_specs(bcc)]))


# --------------------------------------------------------------------------- originals


def load_original(session: ImapSession, info: ServerInfo, ref: MailRef, *, with_attachments: bool, max_bytes: int) -> Original:
    """Read headers, sanitized text and (for forwarding) the attachments of the original message (read-only)."""
    content = get_message(session, info, ref, body_format="text")
    with examine(session, info, ref.folder) as uidvalidity:
        _check_uidvalidity(ref, uidvalidity)
        data = session.fetch([ref.uid], ["BODY.PEEK[HEADER]", "BODYSTRUCTURE"]).get(ref.uid)
        if not data:
            raise GmxMessageNotFoundError("no such message")
        headers = parse_headers(_find_key(data, b"BODY[HEADER"))
        parts = [p for p in list_attachments(parse_bodystructure(data.get(b"BODYSTRUCTURE"))) if not p.embedded]

    def specs(name: str) -> list[str]:
        return [addr.lower() for _n, addr in getaddresses([str(v) for v in headers.get_all(name, [])]) if "@" in addr]

    from_specs = specs("from")
    from_list = content.headers.get("from") or []
    original = Original(
        ref=ref,
        from_addr=from_specs[0] if from_specs else None,
        from_display=from_list[0] if from_list else (from_specs[0] if from_specs else ""),
        reply_to=specs("reply-to"),
        to=specs("to"),
        cc=specs("cc"),
        subject=_header_str(headers, "subject", 998) or "",
        message_id=_header_str(headers, "message-id", 998),
        references=_header_str(headers, "references", 4000) or "",
        date=content.headers.get("date"),
        text=content.text,
    )
    if with_attachments:
        total = 0
        for part in parts:
            info_part, data_bytes = get_attachment(session, info, ref, part.part_id, max_bytes=max_bytes)
            total += len(data_bytes)
            if total > max_bytes:
                raise GmxMessageTooLargeError("message_too_large")
            original.attachments.append(Attachment(info_part.filename or f"anhang-{part.part_id}", data_bytes, info_part.mime))
    return original


def _prefixed(subject: str, prefix: str, known: Sequence[str]) -> str:
    stripped = subject.strip()
    return stripped if stripped.lower().startswith(tuple(k.lower() for k in known)) else f"{prefix} {stripped}".strip()


def reply_message(
    original: Original,
    *,
    sender: str,
    sender_name: str,
    body_text: str,
    reply_all: bool,
    quote: bool,
) -> Outgoing:
    """Reply to the Reply-To address if present, else to From; with ``reply_all`` also to To/Cc (minus own address)."""
    targets = original.reply_to or ([original.from_addr] if original.from_addr else [])
    if not targets:
        raise GmxSendInputError("no_reply_address")
    cc = _unique([*original.to, *original.cc], exclude=[sender, *targets]) if reply_all else []
    text = body_text
    if quote and original.text:
        quoted = "\n".join(f"> {line}" if line else ">" for line in original.text[:MAX_QUOTE_CHARS].splitlines())
        text = f"{body_text}\n\nAm {original.date or '?'} schrieb {original.from_display}:\n{quoted}\n"
    message = build_message(
        sender=sender,
        sender_name=sender_name,
        to=targets,
        cc=cc,
        subject=_prefixed(original.subject, "Re:", ("re:", "aw:")),
        body_text=text,
        in_reply_to=original.message_id,
        references=original.references,
    )
    return Outgoing(message=message, recipients=_unique([*targets, *cc]), original_sender=original.from_addr, answered=original.ref)


def forward_message(original: Original, *, sender: str, sender_name: str, to: Sequence[str], cc: Sequence[str], comment: str) -> Outgoing:
    """Forward inline (sanitized text) with the original attachments that were loaded."""
    header_lines = [
        FORWARD_SEPARATOR,
        f"Von: {original.from_display}",
        f"Datum: {original.date or '?'}",
        f"Betreff: {original.subject}",
        f"An: {', '.join(original.to)}",
    ]
    text = f"{comment}\n\n" + "\n".join(header_lines) + "\n\n" + original.text[: MAX_QUOTE_CHARS * 5]
    message = build_message(
        sender=sender,
        sender_name=sender_name,
        to=to,
        cc=cc,
        subject=_prefixed(original.subject, "Fwd:", ("fwd:", "wg:", "fw:")),
        body_text=text,
        attachments=original.attachments,
        references=" ".join(filter(None, [original.references, original.message_id or ""])),
    )
    return Outgoing(message=message, recipients=_unique([*addr_specs(to), *addr_specs(cc)]))


def load_draft(session: ImapSession, info: ServerInfo, ref: MailRef, *, sender: str, sender_name: str, max_bytes: int) -> Outgoing:
    """Load a draft for sending: From is set to the account, Date renewed, a missing Message-ID added."""
    with examine(session, info, ref.folder) as uidvalidity:
        _check_uidvalidity(ref, uidvalidity)
        meta = session.fetch([ref.uid], ["FLAGS", "RFC822.SIZE"]).get(ref.uid)
        if not meta:
            raise GmxMessageNotFoundError("no such message")
        flags = {f.decode() if isinstance(f, bytes) else str(f) for f in meta.get(b"FLAGS", ())}
        if "\\Draft" not in flags:
            raise GmxSendInputError("not_a_draft")
        if int(meta.get(b"RFC822.SIZE", 0)) > max_bytes:
            raise GmxMessageTooLargeError("message_too_large")
        raw = _find_key(session.fetch([ref.uid], ["BODY.PEEK[]"]).get(ref.uid) or {}, b"BODY[")
    if not raw:
        raise GmxMessageNotFoundError("draft has no content")
    message = BytesParser(policy=default).parsebytes(bytes(raw))
    assert isinstance(message, EmailMessage)
    message = copy.deepcopy(message)
    message.policy = SMTP
    del message["From"]
    message["From"] = formataddr((sender_name, sender)) if sender_name else sender
    del message["Date"]
    message["Date"] = datetime.now(UTC)
    if not message.get("Message-ID"):
        message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    recipients = [str(v) for name in ("To", "Cc", "Bcc") for v in message.get_all(name, [])]
    specs = [addr for _n, addr in getaddresses(recipients)]
    return Outgoing(message=message, recipients=_unique(addr_specs(specs)), draft=ref)
