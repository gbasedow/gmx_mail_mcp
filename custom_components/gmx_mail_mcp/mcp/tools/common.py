"""Shared helpers of the MCP tools: argument checks, error results, response envelope.

Arguments come from an LLM and are checked here field by field (no JSON-Schema library): unknown keys, wrong types and
out-of-range values give a tool error (``isError``) the model can correct, as MCP 2025-11-25 recommends for input
validation. Client-layer errors are mapped to stable codes; their messages never contain credentials or mail content.
"""

from __future__ import annotations

import functools
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import date
from typing import Any, Final

from imapclient.exceptions import IMAPClientError

from ...client.errors import (
    GmxAttachmentTooLargeError,
    GmxAuthError,
    GmxBackoffError,
    GmxConnectionError,
    GmxConnectionLimitError,
    GmxDeleteRuleError,
    GmxFolderExistsError,
    GmxFolderNotFoundError,
    GmxInvalidFolderNameError,
    GmxMailError,
    GmxMessageNotFoundError,
    GmxMessageTooLargeError,
    GmxPartNotFoundError,
    GmxProtectedFolderDeleteError,
    GmxProtectedFolderError,
    GmxProtectedTargetError,
    GmxSearchRejectedError,
    GmxTlsError,
    GmxUidValidityError,
    GmxUnsupportedError,
    GmxWrongFolderError,
)
from ...client.reader import MailRef
from ...const import MAX_REFS_PER_CALL
from ...envelope import build_envelope
from ..protocol import ToolResult

MAX_FOLDER: Final = 500
PART_ID_PATTERN: Final = r"^[1-9]\d{0,3}(?:\.[1-9]\d{0,3}){0,9}$"

REF_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "Message reference exactly as returned by gmx_search (folder, uid, uidvalidity).",
    "properties": {
        "folder": {"type": "string", "minLength": 1, "maxLength": MAX_FOLDER},
        "uid": {"type": "integer", "minimum": 1},
        "uidvalidity": {"type": "integer", "minimum": 0},
    },
    "required": ["folder", "uid", "uidvalidity"],
    "additionalProperties": False,
}


REFS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "minItems": 1,
    "maxItems": MAX_REFS_PER_CALL,
    "items": REF_SCHEMA,
    "description": f"Message references exactly as returned by gmx_search (1 to {MAX_REFS_PER_CALL}).",
}


class ToolInputError(Exception):
    """An argument is missing, of the wrong type or out of range."""


def check_keys(args: Mapping[str, Any], allowed: Sequence[str]) -> None:
    """Reject unknown arguments (additionalProperties: false)."""
    unknown = sorted(set(args) - set(allowed))
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(unknown)}; allowed: {', '.join(allowed)}")


def get_str(
    args: Mapping[str, Any], key: str, *, max_len: int, required: bool = False, default: str | None = None, pattern: str | None = None
) -> str | None:
    """Optional or required non-empty string."""
    value = args.get(key)
    if value is None:
        if required:
            raise ToolInputError(f"{key} is required")
        return default
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise ToolInputError(f"{key} must be a non-empty string of at most {max_len} characters")
    if pattern is not None and not re.fullmatch(pattern, value):
        raise ToolInputError(f"{key} has an invalid format")
    return value


def get_int(args: Mapping[str, Any], key: str, *, minimum: int, maximum: int, default: int | None = None) -> int | None:
    """Optional integer within ``[minimum, maximum]`` (booleans are not integers here)."""
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ToolInputError(f"{key} must be an integer from {minimum} to {maximum}")
    return value


def get_bool(args: Mapping[str, Any], key: str, default: bool | None = None) -> bool | None:
    """Optional boolean."""
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolInputError(f"{key} must be true or false")
    return value


def get_enum(args: Mapping[str, Any], key: str, values: Sequence[str], default: str) -> str:
    """One of ``values``."""
    value = args.get(key, default)
    if value not in values:
        raise ToolInputError(f"{key} must be one of: {', '.join(values)}")
    return str(value)


def get_date(args: Mapping[str, Any], key: str) -> date | None:
    """Optional ``YYYY-MM-DD``."""
    value = get_str(args, key, max_len=10, pattern=r"\d{4}-\d{2}-\d{2}")
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as err:
        raise ToolInputError(f"{key} is not a valid date") from err


def get_str_list(args: Mapping[str, Any], key: str, *, max_items: int, max_len: int, pattern: str | None = None) -> list[str]:
    """Optional list of strings."""
    value = args.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise ToolInputError(f"{key} must be a list of at most {max_items} strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or len(item) > max_len or (pattern is not None and not re.fullmatch(pattern, item)):
            raise ToolInputError(f"{key} contains an invalid entry")
        items.append(item)
    return items


def get_ref(args: Mapping[str, Any], key: str = "ref") -> MailRef:
    """Mail reference ``{folder, uid, uidvalidity}``."""
    value = args.get(key)
    if not isinstance(value, dict):
        raise ToolInputError(f"{key} must be an object with folder, uid and uidvalidity (as returned by gmx_search)")
    check_keys(value, ("folder", "uid", "uidvalidity"))
    folder = get_str(value, "folder", max_len=MAX_FOLDER, required=True)
    uid = get_int(value, "uid", minimum=1, maximum=2**32 - 1)
    uidvalidity = get_int(value, "uidvalidity", minimum=0, maximum=2**32 - 1)
    if folder is None or uid is None or uidvalidity is None:
        raise ToolInputError(f"{key} must contain folder, uid and uidvalidity")
    return MailRef(folder=folder, uid=uid, uidvalidity=uidvalidity)


def get_refs(args: Mapping[str, Any], key: str = "refs") -> list[MailRef]:
    """1 to ``MAX_REFS_PER_CALL`` distinct mail references."""
    value = args.get(key)
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_REFS_PER_CALL:
        raise ToolInputError(f"{key} must be a list of 1 to {MAX_REFS_PER_CALL} references (as returned by gmx_search)")
    refs: list[MailRef] = []
    for index, item in enumerate(value):
        ref = get_ref({"ref": item}, "ref")
        if ref in refs:
            raise ToolInputError(f"{key}[{index}] is listed twice")
        refs.append(ref)
    return refs


# --------------------------------------------------------------------------- errors

# Client error -> (code, message). Order matters for subclasses (GmxBackoffError before GmxConnectionError).
_ERRORS: tuple[tuple[type[Exception], str, str], ...] = (
    (GmxAuthError, "auth_failed_or_imap_disabled", "GMX rejected the login: app password revoked or IMAP access switched off."),
    (GmxTlsError, "tls_error", "Secure connection to the mail server failed."),
    (GmxBackoffError, "reconnect_backoff", "The mail server was unreachable recently; retry in a few minutes."),
    (GmxConnectionLimitError, "connection_limit", "All IMAP connections of this account are busy; retry shortly."),
    (GmxConnectionError, "cannot_connect", "The mail server cannot be reached."),
    (GmxFolderNotFoundError, "folder_not_found", "The folder does not exist; use gmx_list_folders for valid paths."),
    (GmxUidValidityError, "uidvalidity_changed", "The folder was rebuilt (UIDVALIDITY changed); search again for fresh references."),
    (GmxMessageNotFoundError, "message_not_found", "No message with this reference (deleted or moved?)."),
    (GmxPartNotFoundError, "attachment_not_found", "No such part; use part_id from gmx_get_message."),
    (GmxAttachmentTooLargeError, "too_large", "Larger than the limit set in the integration options."),
    (GmxSearchRejectedError, "search_rejected", "The server rejected the search; simplify the criteria."),
    (GmxUnsupportedError, "unsupported", "The mail server does not support this."),
    (
        GmxProtectedTargetError,
        "protected_target",
        "Moving or copying into the trash is not allowed here; use gmx_trash (if switched on).",
    ),
    (GmxProtectedFolderDeleteError, "protected_folder", "INBOX and special folders cannot be deleted."),
    (
        GmxDeleteRuleError,
        "delete_not_allowed",
        "Not allowed for these messages or this folder (see reason): permanent deletion only in trash or spam, "
        "trash only for mails outside the trash, folders only when empty and without subfolders.",
    ),
    (
        GmxProtectedFolderError,
        "protected_folder",
        "INBOX and special folders cannot be renamed; the trash and folders the server marks as without subfolders get none.",
    ),
    (GmxFolderExistsError, "folder_exists", "A folder with this name already exists."),
    (
        GmxInvalidFolderNameError,
        "invalid_folder_name",
        "Folder name not allowed (empty, too long, / % * or control characters), or source and target are the same (see reason).",
    ),
    (GmxWrongFolderError, "wrong_folder", "The messages are not in the folder this tool works on."),
    (GmxMessageTooLargeError, "message_too_large", "The message is larger than the mail server accepts."),
)
# Errors whose ``reason`` is more precise than the code above (e.g. same_folder, not_in_junk, keywords_not_supported).
_REASON_ERRORS: Final = (GmxInvalidFolderNameError, GmxWrongFolderError, GmxUnsupportedError, GmxDeleteRuleError)


def error_result(code: str, message: str, **details: Any) -> ToolResult:
    """Tool error the model can read (``isError``)."""
    text = json.dumps({"error": code, "message": message, **details}, ensure_ascii=False)
    return ToolResult(content=[{"type": "text", "text": text}], is_error=True, audit={"outcome": "error", "error": code})


def map_client_error(err: Exception) -> ToolResult:
    """Translate a client-layer or imapclient error."""
    for kind, code, message in _ERRORS:
        if isinstance(err, kind):
            reason = getattr(err, "reason", None) if isinstance(err, _REASON_ERRORS) else None
            return error_result(code, message, reason=reason) if isinstance(reason, str) else error_result(code, message)
    if isinstance(err, GmxMailError | IMAPClientError):
        return error_result("imap_error", "The mail server returned an error for this request.")
    raise err


type Handler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


def tool_errors(handler: Handler) -> Handler:
    """Turn input errors and client errors into tool errors; anything else stays an internal error."""

    @functools.wraps(handler)
    async def _wrapped(arguments: dict[str, Any]) -> ToolResult:
        try:
            return await handler(arguments)
        except ToolInputError as err:
            return error_result("invalid_arguments", str(err))
        except (GmxMailError, IMAPClientError) as err:
            return map_client_error(err)

    return _wrapped


def envelope(account: str, action: str, data: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Common response envelope (budget, trust label, request id); see ``envelope.py``."""
    return build_envelope(account=account, action=action, data=data, **kwargs)


def audit_facts(data: Mapping[str, Any]) -> dict[str, Any]:
    """Count and folder names of a result's ``data`` for the audit log (never content)."""
    facts: dict[str, Any] = {}
    if isinstance(data.get("count"), int) and not isinstance(data.get("count"), bool):
        facts["count"] = data["count"]
    folders = [f for f in data.get("folders") or () if isinstance(f, str)]
    folders += [data[key] for key in ("path", "old_path", "trash_folder", "deleted_folder") if isinstance(data.get(key), str)]
    folders += [r["folder"] for r in data.get("new_refs") or () if isinstance(r, Mapping) and isinstance(r.get("folder"), str)]
    if folders:
        facts["folders"] = folders
    return facts


def json_result(payload: Mapping[str, Any], *, audit: Mapping[str, Any] | None = None) -> ToolResult:
    """Serialise an envelope as the text block of a tool result; ``audit`` adds to the facts taken from ``data``."""
    data = payload.get("data")
    facts = {"outcome": "ok", **(audit_facts(data) if isinstance(data, Mapping) else {}), **(audit or {})}
    return ToolResult(content=[{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}], audit=facts)
