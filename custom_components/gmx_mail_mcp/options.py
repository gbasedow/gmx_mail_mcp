"""Option handling: defaults, section layout and validation (no Home Assistant imports)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from .const import (
    ALLOWLIST_WILDCARD,
    ATTACHMENT_RETENTION_MAX_DAYS,
    AUDIT_RETENTION_MAX_DAYS,
    AUDIT_RETENTION_MIN_DAYS,
    CONF_ALLOW_HTML_OUTPUT,
    CONF_ALLOW_PERMANENT_DELETE,
    CONF_ALLOW_REPLY_TO_SENDER,
    CONF_ALLOWLIST,
    CONF_ATTACHMENT_RETENTION_DAYS,
    CONF_AUDIT_RETENTION_DAYS,
    CONF_EVENT_INCLUDE_HEADERS,
    CONF_FOLDER_DRAFTS,
    CONF_FOLDER_JUNK,
    CONF_FOLDER_SENT,
    CONF_FOLDER_TRASH,
    CONF_IDLE_ENABLED,
    CONF_IDLE_RENEW_MINUTES,
    CONF_MAX_DELETE_PER_DAY,
    CONF_MAX_MESSAGE_SIZE_MB,
    CONF_MAX_RECIPIENTS,
    CONF_MAX_RECIPIENTS_PER_HOUR,
    CONF_MAX_SEND_PER_DAY,
    CONF_PERMISSIONS,
    CONF_POLL_INTERVAL,
    CONF_SEND_MODE,
    CONF_SENDER_NAME,
    CONF_SENT_COPY_MODE,
    DEFAULT_OPTIONS,
    FOLDER_OVERRIDE_KEYS,
    IDLE_RENEW_MAX_MINUTES,
    IDLE_RENEW_MIN_MINUTES,
    MAX_MESSAGE_SIZE_MB,
    MAX_SENDER_NAME,
    POLL_INTERVAL_MAX_S,
    POLL_INTERVAL_MIN_S,
    SECTION_CONNECTION,
    SECTION_FOLDERS,
    SECTION_LIMITS,
    SECTION_PERMISSIONS,
    SECTION_PRIVACY,
    SECTION_SENDING,
    FolderRole,
    Permission,
    SendMode,
    SentCopyMode,
)

OPTION_SECTIONS: Final[dict[str, tuple[str, ...]]] = {
    SECTION_PERMISSIONS: (CONF_PERMISSIONS, CONF_ALLOW_PERMANENT_DELETE, CONF_ALLOW_HTML_OUTPUT),
    SECTION_SENDING: (CONF_SEND_MODE, CONF_ALLOWLIST, CONF_ALLOW_REPLY_TO_SENDER, CONF_SENDER_NAME, CONF_SENT_COPY_MODE),
    SECTION_LIMITS: (
        CONF_MAX_RECIPIENTS,
        CONF_MAX_RECIPIENTS_PER_HOUR,
        CONF_MAX_SEND_PER_DAY,
        CONF_MAX_MESSAGE_SIZE_MB,
        CONF_MAX_DELETE_PER_DAY,
    ),
    SECTION_CONNECTION: (CONF_POLL_INTERVAL, CONF_IDLE_ENABLED, CONF_IDLE_RENEW_MINUTES),
    SECTION_FOLDERS: (CONF_FOLDER_SENT, CONF_FOLDER_DRAFTS, CONF_FOLDER_TRASH, CONF_FOLDER_JUNK),
    SECTION_PRIVACY: (CONF_EVENT_INCLUDE_HEADERS, CONF_AUDIT_RETENTION_DAYS, CONF_ATTACHMENT_RETENTION_DAYS),
}

# (minimum, maximum); maximum None = no upper bound.
INT_RANGES: Final[dict[str, tuple[int, int | None]]] = {
    CONF_MAX_RECIPIENTS: (1, None),
    CONF_MAX_SEND_PER_DAY: (0, None),  # 0 = no daily limit (D48)
    CONF_MAX_DELETE_PER_DAY: (0, None),  # 0 = no daily limit (D39)
    CONF_MAX_RECIPIENTS_PER_HOUR: (0, None),  # 0 = no hourly limit (D48)
    CONF_MAX_MESSAGE_SIZE_MB: (1, MAX_MESSAGE_SIZE_MB),
    CONF_POLL_INTERVAL: (POLL_INTERVAL_MIN_S, POLL_INTERVAL_MAX_S),
    CONF_IDLE_RENEW_MINUTES: (IDLE_RENEW_MIN_MINUTES, IDLE_RENEW_MAX_MINUTES),
    CONF_AUDIT_RETENTION_DAYS: (AUDIT_RETENTION_MIN_DAYS, AUDIT_RETENTION_MAX_DAYS),
    CONF_ATTACHMENT_RETENTION_DAYS: (0, ATTACHMENT_RETENTION_MAX_DAYS),
}

# Deliberately simple: one "@", no whitespace, no "*" and a dot in the domain. Exact matching happens later.
_ADDRESS_RE: Final = re.compile(r"^[^@\s*]+@[^@\s*]+\.[^@\s*.]+$")
_DOMAIN_WILDCARD_RE: Final = re.compile(r"^\*@[^@\s*]+\.[^@\s*.]+$")


def nest(flat: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Split flat options into the form sections."""
    return {section: {key: flat[key] for key in keys if key in flat} for section, keys in OPTION_SECTIONS.items()}


def flatten(nested: Mapping[str, Any]) -> dict[str, Any]:
    """Merge form sections into flat options (unknown keys are dropped)."""
    flat: dict[str, Any] = {}
    for section, keys in OPTION_SECTIONS.items():
        values = nested.get(section) or {}
        for key in keys:
            if key in values:
                flat[key] = values[key]
    return flat


def with_defaults(options: Mapping[str, Any]) -> dict[str, Any]:
    """Return options completed with defaults."""
    merged = dict(DEFAULT_OPTIONS)
    merged.update({k: v for k, v in options.items() if k in DEFAULT_OPTIONS})
    return merged


def normalize_allowlist(entries: Any) -> list[str]:
    """Split, trim, lower-case and de-duplicate allowlist entries (order preserved)."""
    if isinstance(entries, str):
        entries = [entries]
    result: list[str] = []
    for entry in entries or []:
        for part in re.split(r"[,;\s]+", str(entry)):
            item = part.strip().lower()
            if item and item not in result:
                result.append(item)
    return result


def is_valid_allowlist_entry(entry: str) -> bool:
    """Return whether ``entry`` is an exact address or ``*@domain``; ``*`` is not an entry ("any address" is a mode, D46)."""
    return bool(_ADDRESS_RE.match(entry) or _DOMAIN_WILDCARD_RE.match(entry))


def _send_mode_error(send_mode: str, allowlist: list[str]) -> str | None:
    if send_mode not in {m.value for m in SendMode}:
        return "invalid_send_mode"
    if ALLOWLIST_WILDCARD in allowlist:
        return "wildcard_not_allowed"
    if any(not is_valid_allowlist_entry(e) for e in allowlist):
        return "invalid_allowlist_entry"
    return None


# Send modes offered in the options flow (D46); ``off`` stays readable for old data but is not offered.
FORM_SEND_MODES: Final = (SendMode.ALLOWLIST.value, SendMode.ANY.value)


def check_send_options(send_mode: Any, allowlist: Any, sender_name: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalise the sending fields of the options form; errors map the field to a translation key (D46, D52)."""
    errors: dict[str, str] = {}
    mode = str(send_mode or SendMode.ALLOWLIST.value)
    if mode not in FORM_SEND_MODES:
        errors[CONF_SEND_MODE] = "invalid_send_mode"
    entries = normalize_allowlist(allowlist)
    if error := _send_mode_error(mode if mode in FORM_SEND_MODES else SendMode.ALLOWLIST.value, entries):
        errors[CONF_ALLOWLIST] = error
    name, valid = normalize_sender_name(sender_name)
    if not valid:
        errors[CONF_SENDER_NAME] = "invalid_sender_name"
    return {CONF_SEND_MODE: mode, CONF_ALLOWLIST: entries, CONF_SENDER_NAME: name}, errors


def normalize_sender_name(value: Any) -> tuple[str, bool]:
    """Collapse whitespace (no line breaks: header injection) and check length and characters."""
    name = " ".join(str(value or "").split())
    valid = len(name) <= MAX_SENDER_NAME and name.isprintable() and not any(ch in name for ch in '<>"')
    return name, valid


def validate_options(raw: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalise flat options and return ``(options, errors)``; errors map field or ``base`` to a key."""
    options = with_defaults(raw)
    errors: dict[str, str] = {}

    permissions = [str(p) for p in options[CONF_PERMISSIONS] or []]
    known = {p.value for p in Permission}
    if any(p not in known for p in permissions):
        errors["base"] = "invalid_permission"
    elif Permission.READ.value not in permissions:
        errors["base"] = "read_required"
    options[CONF_PERMISSIONS] = [p.value for p in Permission if p.value in permissions]

    allowlist = normalize_allowlist(options[CONF_ALLOWLIST])
    options[CONF_ALLOWLIST] = allowlist
    if send_error := _send_mode_error(str(options[CONF_SEND_MODE]), allowlist):
        errors.setdefault("base", send_error)

    if str(options[CONF_SENT_COPY_MODE]) not in {m.value for m in SentCopyMode}:
        errors.setdefault("base", "invalid_sent_copy_mode")

    for key, (low, high) in INT_RANGES.items():
        try:
            value = int(options[key])
        except TypeError, ValueError:
            errors.setdefault("base", "out_of_range")
            continue
        if value < low or (high is not None and value > high):
            errors.setdefault("base", "out_of_range")
        options[key] = value

    for key in (
        CONF_ALLOW_PERMANENT_DELETE,
        CONF_ALLOW_HTML_OUTPUT,
        CONF_ALLOW_REPLY_TO_SENDER,
        CONF_IDLE_ENABLED,
        CONF_EVENT_INCLUDE_HEADERS,
    ):
        options[key] = bool(options[key])

    options[CONF_SENDER_NAME], valid_name = normalize_sender_name(options[CONF_SENDER_NAME])
    if not valid_name:
        errors.setdefault("base", "invalid_sender_name")

    for key in FOLDER_OVERRIDE_KEYS.values():
        options[key] = str(options[key] or "").strip()

    return options, errors


def folder_overrides(options: Mapping[str, Any]) -> dict[FolderRole, str]:
    """Return the non-empty folder overrides by role."""
    return {role: str(options.get(key) or "").strip() for role, key in FOLDER_OVERRIDE_KEYS.items() if options.get(key)}
