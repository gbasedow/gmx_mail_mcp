"""Sending tools (level S, M5): new message, reply, forward and sending a draft over SMTP.

All are off by default (D12). Rules, checked in this order before anything goes out (D46-D54):

* send mode: ``allowlist`` (exact addresses or ``*@domain``; an empty list allows nobody) or ``any`` (every address);
  the option "allow replies to the sender" (off by default, D47) covers exactly the From address of the answered mail;
* recipients per mail (at least 1), messages per day and recipients per hour (0 = no limit, D48), total size of the
  message (option, D54) and APPENDLIMIT for the Sent copy;
* two steps (D45): without ``confirm_token`` nothing is sent and a preview plus a code is returned; the code is bound to
  exactly these arguments (attachments by their SHA-256), valid 5 minutes, usable once, lost on a restart. The person
  can switch the step off per tool (``tools_without_confirmation``).

A message is sent at most once. It is never repeated, not even when the outcome is unknown (connection lost during
DATA): that counts as sent. Once SMTP has started, a timeout of the tool call does not stop it; the sending and the
follow-up steps finish in the background and a second audit entry records how it ended (D58).
After sending (failures there are warnings, never a second send): copy into the Sent folder (``sent_copy_mode``, D49),
``\\Answered`` on the original of a reply, removal of the sent draft (D56).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from imapclient.exceptions import IMAPClientError

from ...client.composer import Outgoing, addr_specs, forward_message, load_draft, load_original, new_message, reply_message
from ...client.errors import (
    GmxConnectionError,
    GmxMailError,
    GmxMessageTooLargeError,
    GmxOutcomeUnknownError,
    GmxSendInputError,
    GmxSmtpAuthError,
    GmxSmtpRejectedError,
    GmxTlsError,
)
from ...client.imap import ImapSession
from ...client.policy import RecipientPolicy, domain_of
from ...client.reader import MailRef
from ...client.sanitize import clean_inline, frame_untrusted, new_nonce
from ...client.sent import mark_answered, remove_draft, store_sent_copy
from ...client.smtp import send_message
from ...client.upload import MAX_FILES, UploadError, decode_uploads
from ...confirm import InvalidTokenError
from ...const import (
    CONF_ALLOW_REPLY_TO_SENDER,
    CONF_ALLOWLIST,
    CONF_MAX_MESSAGE_SIZE_MB,
    CONF_MAX_RECIPIENTS,
    CONF_MAX_RECIPIENTS_PER_HOUR,
    CONF_MAX_SEND_PER_DAY,
    CONF_MAX_UPLOAD_MB,
    CONF_SEND_MODE,
    CONF_SENDER_NAME,
    CONF_SENT_COPY_MODE,
    DEFAULT_MAX_MESSAGE_SIZE_MB,
    DEFAULT_MAX_RECIPIENTS,
    DEFAULT_MAX_RECIPIENTS_PER_HOUR,
    DEFAULT_MAX_SEND_PER_DAY,
    DEFAULT_MAX_UPLOAD_MB,
    FolderRole,
    Permission,
    SendMode,
    SentCopyMode,
)
from ..protocol import Tool, ToolResult
from ..registry import ToolContext, ToolSpec, run_to_end, skips_confirmation
from .common import (
    REF_SCHEMA,
    ToolInputError,
    check_keys,
    envelope,
    error_result,
    get_bool,
    get_ref,
    get_str,
    json_result,
    tool_errors,
)
from .organize import MAX_ADDRESS, MAX_BODY, MAX_RECIPIENTS, MAX_SUBJECT, UPLOAD_MESSAGES, get_addresses, get_uploads
from .read import server_state

_LOGGER = logging.getLogger(__name__)

SEND_MESSAGE: Final = "gmx_send_message"
REPLY: Final = "gmx_reply"
FORWARD: Final = "gmx_forward"
SEND_DRAFT: Final = "gmx_send_draft"
MAX_TOKEN: Final = 512
MAX_HTML: Final = 200_000
PREVIEW_TEXT_CHARS: Final = 600
_MB: Final = 1024 * 1024

_TOKEN_SCHEMA: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_TOKEN,
    "description": "confirm_token from the preview of the first call (same arguments, within 5 minutes).",
}
_ADDRESS_LIST: Final[dict[str, Any]] = {
    "type": "array",
    "maxItems": MAX_RECIPIENTS,
    "items": {"type": "string", "minLength": 3, "maxLength": MAX_ADDRESS},
}
_TWO_STEPS: Final = (
    "Two steps: the first call (without confirm_token) sends nothing and returns a preview and a confirm_token; show the "
    "preview (recipients, subject, text, attachments) to the person and ask. Only after their consent call again with "
    "the same arguments and the confirm_token (valid 5 minutes, once). If the person switched confirmation off for this "
    "tool, the first call sends at once (the result says confirmation: skipped). A message is never sent twice: after "
    "an error with outcome_unknown do not send again. Recipients must be allowed by the integration options "
    "(allowlist or any address) and the limits there apply."
)
_SMTP_ERRORS: Final = {
    "message_too_large": ("message_too_large", "The mail server refused the message as too large; nothing was sent."),
    "sender_rejected": ("smtp_sender_rejected", "The mail server refused the sender address; nothing was sent."),
    "recipient_rejected": ("smtp_recipient_rejected", "The mail server refused a recipient; nothing was sent to anyone."),
    "message_rejected": ("smtp_message_rejected", "The mail server refused the message; nothing was sent."),
}


# --------------------------------------------------------------------------- options


def _int(options: Mapping[str, Any], key: str, default: int) -> int:
    try:
        return int(options.get(key, default))
    except TypeError, ValueError:
        return default


def _policy(options: Mapping[str, Any]) -> RecipientPolicy:
    allowlist = options.get(CONF_ALLOWLIST)
    return RecipientPolicy(
        str(options.get(CONF_SEND_MODE, SendMode.ALLOWLIST.value)),
        [str(e) for e in allowlist] if isinstance(allowlist, list) else [],
        bool(options.get(CONF_ALLOW_REPLY_TO_SENDER, False)),
    )


def _max_message_bytes(options: Mapping[str, Any]) -> int:
    return max(1, _int(options, CONF_MAX_MESSAGE_SIZE_MB, DEFAULT_MAX_MESSAGE_SIZE_MB)) * _MB


def _sent_copy_mode(options: Mapping[str, Any]) -> str:
    mode = str(options.get(CONF_SENT_COPY_MODE, SentCopyMode.APPEND.value))
    return mode if mode in {m.value for m in SentCopyMode} else SentCopyMode.APPEND.value


def _sender_name(options: Mapping[str, Any]) -> str:
    # Validated in the options flow; collapsed again here (no line breaks in a header).
    return " ".join(str(options.get(CONF_SENDER_NAME) or "").split())


# --------------------------------------------------------------------------- shared flow


@dataclass(slots=True)
class _Request:
    """One sending call: what the code is bound to and how the message is composed."""

    tool: str
    action: str
    params: dict[str, Any]
    # Composes the message; IMAP-based composers get a session (read-only), the others None.
    compose: Callable[[ImapSession | None], Outgoing]
    needs_imap: bool
    warnings: list[str] = field(default_factory=list)
    # Recipient rule the call was checked against (shown in the preview and the answer, D60).
    send_mode: str = ""


def _domains(addresses: list[str]) -> list[str]:
    return sorted({domain_of(a) for a in addresses if "@" in a})


def _with_domains(result: ToolResult, domains: list[str], count: int | None = None) -> ToolResult:
    """Add recipient domains (D55) and the recipient count to the audit facts of a result."""
    facts = dict(result.audit or {})
    if domains:
        facts["recipient_domains"] = domains
    if count is not None:
        facts["count"] = count
    return dataclasses.replace(result, audit=facts)


def _limit_error(context: ToolContext, outgoing: Outgoing, policy: RecipientPolicy, append_limit: int | None) -> ToolResult | None:
    """Recipients, allowlist, daily and hourly limit, size; None when the message may go out."""
    return _recipient_error(context, outgoing, policy) or _quota_error(context, outgoing, append_limit)


def _recipient_error(context: ToolContext, outgoing: Outgoing, policy: RecipientPolicy) -> ToolResult | None:
    options = context.options()
    recipients = outgoing.recipients
    if not recipients:
        return error_result("no_recipients", "The message has no recipients.")
    max_recipients = max(1, _int(options, CONF_MAX_RECIPIENTS, DEFAULT_MAX_RECIPIENTS))
    if len(recipients) > max_recipients:
        return error_result(
            "too_many_recipients",
            "More recipients than the integration options allow per mail.",
            limit=max_recipients,
            recipients=len(recipients),
        )
    blocked = policy.blocked(recipients, original_sender=outgoing.original_sender)
    if blocked:
        # Count and domains only: no addresses in logs or answers about refusals.
        return error_result(
            "recipient_not_allowed",
            "Recipients not allowed by the integration options (allowlist); nothing was sent. The person can add them "
            "in the options; do not try other addresses to get around it.",
            count=len(blocked),
            domains=_domains(blocked),
        )
    return None


def _quota_error(context: ToolContext, outgoing: Outgoing, append_limit: int | None) -> ToolResult | None:
    options = context.options()
    recipients = outgoing.recipients
    per_day = _int(options, CONF_MAX_SEND_PER_DAY, DEFAULT_MAX_SEND_PER_DAY)
    if per_day > 0 and context.sends.sent_today + 1 > per_day:
        return error_result("daily_send_limit", "The daily limit for sent mails set in the integration options is reached.", limit=per_day)
    per_hour = _int(options, CONF_MAX_RECIPIENTS_PER_HOUR, DEFAULT_MAX_RECIPIENTS_PER_HOUR)
    used = context.sends.recipients_last_hour
    if per_hour > 0 and used + len(recipients) > per_hour:
        return error_result(
            "hourly_recipient_limit",
            "The limit of recipients per hour set in the integration options would be exceeded; try again later.",
            limit=per_hour,
            remaining=max(0, per_hour - used),
        )
    limit = _max_message_bytes(options)
    if len(outgoing.wire_bytes) > limit:
        return error_result(
            "message_too_large",
            "The message is larger than the size limit in the integration options.",
            limit_mb=limit // _MB,
        )
    if _sent_copy_mode(options) != SentCopyMode.NONE and append_limit and len(outgoing.copy_bytes) > append_limit:
        return error_result(
            "message_too_large",
            "The copy for the Sent folder would exceed the mail server's APPENDLIMIT.",
            reason="sent_copy",
        )
    return None


def _preview(outgoing: Outgoing, options: Mapping[str, Any]) -> dict[str, Any]:
    """What the person must see before sending; text from originals or drafts is framed as untrusted."""
    raw = outgoing.preview(text_chars=PREVIEW_TEXT_CHARS)
    preview = {
        "from": clean_inline(raw["from"], 400),
        "to": raw["to"],
        "cc": raw["cc"],
        "bcc": raw["bcc"],
        "subject": clean_inline(raw["subject"], 998),
        "size": raw["size"],
        "attachments": [
            {"filename": clean_inline(str(a["filename"] or ""), 255), "mime": a["mime"], "size": a["size"]} for a in raw["attachments"]
        ],
        "text_start": frame_untrusted(raw["text_start"], new_nonce()),
        "text_chars": raw["text_chars"],
        "html": raw["html"],
        "html_chars": raw["html_chars"],
        "sent_copy_mode": _sent_copy_mode(options),
    }
    if outgoing.answered is not None:
        preview["answers"] = outgoing.answered.as_dict()
    if outgoing.draft is not None:
        preview["draft"] = outgoing.draft.as_dict()
    return preview


async def _send_flow(context: ToolContext, request: _Request, token: str | None, arguments: Mapping[str, Any]) -> ToolResult:
    """Compose, check, preview + code or send once; holds the account's send lock until the sending has ended."""
    options = context.options()
    policy = _policy(options)
    if (unavailable := _unavailable(context, policy)) is not None:
        return unavailable
    skip = skips_confirmation(options, request.tool)
    request.send_mode = policy.send_mode
    if policy.send_mode == SendMode.ANY:
        # Make an open recipient rule visible in every preview and answer (D60); the allowlist is not checked.
        request.warnings.append("recipients_unrestricted")
    lock = context.send_lock
    await lock.acquire()
    handed_over = False
    try:
        info, folder_map = await server_state(context)
        outgoing = await _compose(context, request)
        if isinstance(outgoing, ToolResult):
            return outgoing
        domains = _domains(outgoing.recipients)
        count = len(outgoing.recipients)
        if (refused := _limit_error(context, outgoing, policy, info.append_limit)) is not None:
            return _with_domains(refused, domains, count)
        if not skip and token is None:
            data: dict[str, Any] = {
                "preview": _preview(outgoing, options),
                "send_mode": policy.send_mode,
                "sent": False,
                "confirm_token": context.tokens.issue(request.tool, request.params),
                "expires_in": context.tokens.ttl,
            }
            env = envelope(context.account, request.action, data, warnings=request.warnings)
            return json_result(env, audit={"outcome": "preview", "count": count, "recipient_domains": domains})
        if not skip and not _consume(context, request, token):
            result = error_result(
                "confirm_token_invalid",
                "The confirm_token is invalid, expired, already used or for other arguments; nothing was sent. Call "
                "again without it for a new preview.",
            )
            return _with_domains(result, domains, count)
        confirmation = "skipped" if skip else "code"
        delivery = asyncio.ensure_future(
            _deliver(context, request, outgoing, folder_map=folder_map, confirmation=confirmation, domains=domains)
        )
        delivery.add_done_callback(lambda _task: lock.release())
        handed_over = True
        # Timeout or aborted request: SMTP may be running; it finishes, and its end is recorded separately (D58).
        return await run_to_end(context.audit, tool=request.tool, level=Permission.SEND, arguments=arguments, task=delivery)
    finally:
        if not handed_over:
            lock.release()


def _unavailable(context: ToolContext, policy: RecipientPolicy) -> ToolResult | None:
    """Sending is impossible for this account as configured."""
    if context.smtp is None:
        return error_result("sending_not_configured", "No SMTP settings for this account; reconfigure the integration.")
    if not policy.sending_enabled:
        return error_result(
            "sending_disabled",
            "Sending is not possible: the allowlist in the integration options is empty (or sending is off). The person "
            "can add addresses or switch to any address.",
        )
    return None


async def _compose(context: ToolContext, request: _Request) -> Outgoing | ToolResult:
    """Build the message (IMAP read-only for originals and drafts); input problems become tool errors."""
    try:
        if request.needs_imap:
            return await context.worker.async_run(request.compose, readonly=True)
        return await context.executor(request.compose, None)
    except ValueError:
        # An address the email library rejects (also in originals and drafts), or a line break in a header.
        return error_result("invalid_address", "An address or header is not valid; use name@example.org or Name <name@example.org>.")
    except GmxMessageTooLargeError:
        return error_result("message_too_large", "The original's attachments exceed the size limit in the integration options.")
    except GmxSendInputError as err:
        return _input_error(err.reason)


def _consume(context: ToolContext, request: _Request, token: str | None) -> bool:
    try:
        context.tokens.consume(token or "", request.tool, request.params)
    except InvalidTokenError:
        return False
    return True


def _input_error(reason: str) -> ToolResult:
    messages = {
        "not_a_draft": "This mail is not a draft (no \\Draft flag or not in the Drafts folder).",
        "no_reply_address": "The original has no sender or Reply-To address to answer.",
        "invalid_address": "An address is not valid.",
    }
    return error_result(reason, messages.get(reason, "The message cannot be sent as given."))


async def _deliver(
    context: ToolContext,
    request: _Request,
    outgoing: Outgoing,
    *,
    folder_map: Any,
    confirmation: str,
    domains: list[str],
) -> ToolResult:
    """Send once, count, then the IMAP follow-up steps (their failures are warnings)."""
    smtp = context.smtp
    assert smtp is not None  # checked by the caller
    count = len(outgoing.recipients)
    facts: dict[str, Any] = {"count": count, "recipient_domains": domains, "confirmation": confirmation}

    def smtp_job() -> None:
        send_message(smtp, context.tls_context(), smtp.username, outgoing.recipients, outgoing.wire_bytes)

    try:
        await context.executor(smtp_job)
    except GmxOutcomeUnknownError:
        context.sends.add(count)  # may have gone out: count it, never resend
        result = error_result(
            "outcome_unknown",
            "The connection broke while the message was being handed over; it may or may not have been sent. Do NOT "
            "send it again; check the recipient's inbox or ask the person.",
        )
        return dataclasses.replace(result, audit={**(result.audit or {}), **facts, "outcome": "unknown"})
    except GmxSmtpAuthError:
        context.on_smtp_auth_failed()
        result = error_result("smtp_auth_failed", "GMX rejected the SMTP login (app password revoked?); nothing was sent.")
        return dataclasses.replace(result, audit={**(result.audit or {}), **facts})
    except GmxSmtpRejectedError as err:
        code, message = _SMTP_ERRORS.get(err.reason, _SMTP_ERRORS["message_rejected"])
        result = error_result(code, message)
        return dataclasses.replace(result, audit={**(result.audit or {}), **facts})
    except GmxTlsError, GmxConnectionError:
        result = error_result("cannot_connect", "The mail server for sending cannot be reached securely; nothing was sent.")
        return dataclasses.replace(result, audit={**(result.audit or {}), **facts})
    context.sends.add(count)
    warnings = list(request.warnings)
    sent_copy = await _follow_up(context, outgoing, folder_map, warnings)
    sent_folder = folder_map.get(FolderRole.SENT) if sent_copy == "appended" else None
    if sent_folder:
        facts["folders"] = [sent_folder]  # the copy is a write into this folder (audit, D41)
    data = {
        "sent": True,
        "send_mode": request.send_mode,
        "message_id": str(outgoing.message["Message-ID"]),
        "recipients": count,
        "sent_copy": sent_copy,
        "confirmation": confirmation,
    }
    return json_result(envelope(context.account, request.action, data, warnings=warnings), audit=facts)


async def _follow_up(context: ToolContext, outgoing: Outgoing, folder_map: Any, warnings: list[str]) -> str:
    """Sent copy, \\Answered and draft removal; the message is out, so nothing here may fail the call."""
    info = await _server_info(context)
    message_id = str(outgoing.message["Message-ID"])
    mode = _sent_copy_mode(context.options())
    sent_copy = "failed"
    if info is not None:
        try:
            sent_copy = await context.worker.async_run(
                lambda s: store_sent_copy(s, info, folder_map, outgoing.copy_bytes, message_id=message_id, mode=mode),
                readonly=False,
            )
        except GmxMailError, IMAPClientError:
            sent_copy = "failed"
    if sent_copy in ("failed", "no_sent_folder"):
        warnings.append("sent_copy_failed")
    if outgoing.answered is not None:
        answered = outgoing.answered
        try:
            if info is None or not await context.worker.async_run(lambda s: mark_answered(s, info, answered), readonly=False):
                warnings.append("original_not_found" if info is not None else "answered_flag_failed")
        except GmxMailError, IMAPClientError:
            warnings.append("answered_flag_failed")
    if outgoing.draft is not None:
        draft = outgoing.draft
        try:
            if info is None or not await context.worker.async_run(lambda s: remove_draft(s, info, draft), readonly=False):
                warnings.append("draft_not_removed")
        except GmxMailError, IMAPClientError:
            warnings.append("draft_not_removed")
    return sent_copy


async def _server_info(context: ToolContext) -> Any:
    try:
        info, _folder_map = await server_state(context)
    except GmxMailError, IMAPClientError:
        return None
    return info


def _token(args: Mapping[str, Any]) -> str | None:
    return get_str(args, "confirm_token", max_len=MAX_TOKEN)


def _text(args: Mapping[str, Any], key: str, *, max_len: int = MAX_BODY, required: bool = False) -> str:
    value = args.get(key)
    if value is None:
        if required:
            raise ToolInputError(f"{key} is required")
        return ""
    if not isinstance(value, str) or len(value) > max_len:
        raise ToolInputError(f"{key} must be a string of at most {max_len} characters")
    return value


def _ref_params(ref: MailRef) -> dict[str, Any]:
    return ref.as_dict()


# --------------------------------------------------------------------------- gmx_send_message

_SEND_ARGS: Final = ("to", "cc", "bcc", "subject", "body_text", "body_html", "attachments", "confirm_token")


def _build_send_message(context: ToolContext) -> Tool:
    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, _SEND_ARGS)
        to, cc, bcc = get_addresses(args, "to"), get_addresses(args, "cc"), get_addresses(args, "bcc")
        subject = _text(args, "subject", max_len=MAX_SUBJECT)
        if any(ch in subject for ch in "\r\n\x00"):
            raise ToolInputError("subject must be a single line")
        body = _text(args, "body_text")
        html = _text(args, "body_html", max_len=MAX_HTML) or None
        uploads = get_uploads(args)
        token = _token(args)
        limit = _int(context.options(), CONF_MAX_UPLOAD_MB, DEFAULT_MAX_UPLOAD_MB) * _MB
        try:
            files, warnings = await context.executor(lambda: decode_uploads(uploads, max_bytes=limit)) if uploads else ([], [])
        except UploadError as err:
            return error_result(err.code, UPLOAD_MESSAGES.get(err.code, "The file was refused."), index=err.index)
        options = context.options()
        sender, name = context.email, _sender_name(options)
        params = {
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": subject,
            "body_text": body,
            "body_html": html,
            # The code binds the files by content (the SHA-256 was checked against the decoded bytes).
            "attachments": [{"filename": u.filename, "sha256": u.sha256.lower(), "mime_type": u.mime} for u in uploads],
        }

        def compose(_session: ImapSession | None) -> Outgoing:
            return new_message(
                sender=sender,
                sender_name=name,
                to=to,
                cc=cc,
                bcc=bcc,
                subject=subject,
                body_text=body,
                body_html=html,
                attachments=files,
            )

        request = _Request(SEND_MESSAGE, "send_message", params, compose, needs_imap=False, warnings=warnings)
        return await _send_flow(context, request, token, args)

    return Tool(
        name=SEND_MESSAGE,
        title="Send a new mail from GMX",
        description=(
            "Sends a new mail from this account (sender: the account address). body_text is the plain text; body_html is "
            "an optional HTML version sent alongside (the preview only shows its size). Attachments: base64 content plus "
            f"the SHA-256 of the file (at most {MAX_FILES}); the type is taken from the content; size limits per the "
            f"integration options. Bcc recipients are not visible to the others. {_TWO_STEPS}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": _ADDRESS_LIST,
                "cc": _ADDRESS_LIST,
                "bcc": _ADDRESS_LIST,
                "subject": {"type": "string", "maxLength": MAX_SUBJECT},
                "body_text": {"type": "string", "maxLength": MAX_BODY},
                "body_html": {"type": "string", "maxLength": MAX_HTML, "description": "Optional HTML version of the text."},
                "attachments": {
                    "type": "array",
                    "maxItems": MAX_FILES,
                    "items": {
                        "type": "object",
                        "properties": {
                            "filename": {"type": "string", "minLength": 1, "maxLength": 255},
                            "content_base64": {"type": "string", "minLength": 1},
                            "sha256": {"type": "string", "pattern": "^[0-9a-fA-F]{64}$", "description": "SHA-256 of the file (hex)."},
                            "mime_type": {"type": "string", "maxLength": 200, "description": "Used only if the content is not recognised."},
                        },
                        "required": ["filename", "content_base64", "sha256"],
                        "additionalProperties": False,
                    },
                },
                "confirm_token": _TOKEN_SCHEMA,
            },
            "additionalProperties": False,
        },
        level=Permission.SEND,
        handler=handler,
    )


# --------------------------------------------------------------------------- gmx_reply


def _build_reply(context: ToolContext) -> Tool:
    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, ("ref", "body_text", "reply_all", "quote", "confirm_token"))
        ref = get_ref(args, "ref")
        body = _text(args, "body_text", required=True)
        reply_all = bool(get_bool(args, "reply_all", False))
        quote = bool(get_bool(args, "quote", True))
        token = _token(args)
        options = context.options()
        sender, name, limit = context.email, _sender_name(options), _max_message_bytes(options)
        info, _folder_map = await server_state(context)
        params = {"ref": _ref_params(ref), "body_text": body, "reply_all": reply_all, "quote": quote}

        def compose(session: ImapSession | None) -> Outgoing:
            assert session is not None
            original = load_original(session, info, ref, with_attachments=False, max_bytes=limit)
            return reply_message(original, sender=sender, sender_name=name, body_text=body, reply_all=reply_all, quote=quote)

        return await _send_flow(context, _Request(REPLY, "reply", params, compose, needs_imap=True), token, args)

    return Tool(
        name=REPLY,
        title="Reply to a GMX mail",
        description=(
            'Replies to a mail (subject "Re: ..." unless it already starts with Re:/AW:; In-Reply-To/References set): to '
            "its Reply-To address if present, else to its sender. reply_all (default false) also writes to the original "
            "To/Cc recipients except this account. quote (default true) appends the cleaned original text as a quote. "
            f"The original is marked as answered after sending. {_TWO_STEPS}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": REF_SCHEMA,
                "body_text": {"type": "string", "maxLength": MAX_BODY},
                "reply_all": {"type": "boolean", "default": False},
                "quote": {"type": "boolean", "default": True},
                "confirm_token": _TOKEN_SCHEMA,
            },
            "required": ["ref", "body_text"],
            "additionalProperties": False,
        },
        level=Permission.SEND,
        handler=handler,
    )


# --------------------------------------------------------------------------- gmx_forward


def _build_forward(context: ToolContext) -> Tool:
    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, ("ref", "to", "cc", "comment", "include_attachments", "confirm_token"))
        ref = get_ref(args, "ref")
        to, cc = get_addresses(args, "to"), get_addresses(args, "cc")
        if not to:
            raise ToolInputError("to must name at least one recipient")
        comment = _text(args, "comment")
        include = bool(get_bool(args, "include_attachments", True))
        token = _token(args)
        options = context.options()
        sender, name, limit = context.email, _sender_name(options), _max_message_bytes(options)
        try:
            to_specs, cc_specs = addr_specs(to), addr_specs(cc)
        except ValueError:
            return error_result("invalid_address", "An address is not valid; use name@example.org or Name <name@example.org>.")
        info, _folder_map = await server_state(context)
        params = {"ref": _ref_params(ref), "to": to, "cc": cc, "comment": comment, "include_attachments": include}

        def compose(session: ImapSession | None) -> Outgoing:
            assert session is not None
            original = load_original(session, info, ref, with_attachments=include, max_bytes=limit)
            return forward_message(original, sender=sender, sender_name=name, to=to_specs, cc=cc_specs, comment=comment)

        return await _send_flow(context, _Request(FORWARD, "forward", params, compose, needs_imap=True), token, args)

    return Tool(
        name=FORWARD,
        title="Forward a GMX mail",
        description=(
            'Forwards a mail inline (subject "Fwd: ..." unless it already starts with Fwd:/WG:/Fw:): your comment, then the '
            "original's sender, date, subject and recipients and its cleaned text (no HTML). include_attachments (default "
            f"true) adds the original's attachments, counted against the size limit in the integration options. {_TWO_STEPS}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": REF_SCHEMA,
                "to": {**_ADDRESS_LIST, "minItems": 1},
                "cc": _ADDRESS_LIST,
                "comment": {"type": "string", "maxLength": MAX_BODY},
                "include_attachments": {"type": "boolean", "default": True},
                "confirm_token": _TOKEN_SCHEMA,
            },
            "required": ["ref", "to"],
            "additionalProperties": False,
        },
        level=Permission.SEND,
        handler=handler,
    )


# --------------------------------------------------------------------------- gmx_send_draft


def _build_send_draft(context: ToolContext) -> Tool:
    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, ("ref", "confirm_token"))
        ref = get_ref(args, "ref")
        token = _token(args)
        options = context.options()
        sender, name, limit = context.email, _sender_name(options), _max_message_bytes(options)
        info, folder_map = await server_state(context)
        if ref.folder != folder_map.get(FolderRole.DRAFTS):
            return _input_error("not_a_draft")

        def compose(session: ImapSession | None) -> Outgoing:
            assert session is not None
            return load_draft(session, info, ref, sender=sender, sender_name=name, max_bytes=limit)

        request = _Request(SEND_DRAFT, "send_draft", {"ref": _ref_params(ref)}, compose, needs_imap=True)
        return await _send_flow(context, request, token, args)

    return Tool(
        name=SEND_DRAFT,
        title="Send a GMX draft",
        description=(
            "Sends a draft from the Drafts folder unchanged (also its HTML part and attachments, if any); sender is set to "
            "this account, recipients are its To/Cc/Bcc. After sending the draft is removed from the Drafts folder (the "
            f"copy in Sent keeps it). {_TWO_STEPS}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": {**REF_SCHEMA, "description": "Reference of the draft (folder Drafts) from gmx_search."},
                "confirm_token": _TOKEN_SCHEMA,
            },
            "required": ["ref"],
            "additionalProperties": False,
        },
        level=Permission.SEND,
        handler=handler,
    )


SEND_TOOLS: Final = (SEND_MESSAGE, REPLY, FORWARD, SEND_DRAFT)
SPECS: Final = (
    ToolSpec(name=SEND_MESSAGE, level=Permission.SEND, build=_build_send_message, confirmable=True),
    ToolSpec(name=REPLY, level=Permission.SEND, build=_build_reply, confirmable=True),
    ToolSpec(name=FORWARD, level=Permission.SEND, build=_build_forward, confirmable=True),
    ToolSpec(name=SEND_DRAFT, level=Permission.SEND, build=_build_send_draft, confirmable=True),
)
