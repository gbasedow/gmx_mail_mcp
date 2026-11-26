"""Option helpers of the options form: allowlist, sending mode, sender name, folder overrides (no Home Assistant imports).

The options form itself (``config_flow.py``) validates and stores the options; the template's layout with sections and
its own validation of all options were removed in M6 (D64).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Final

from .const import (
    ALLOWLIST_WILDCARD,
    CONF_ALLOWLIST,
    CONF_SEND_MODE,
    CONF_SENDER_NAME,
    FOLDER_OVERRIDE_KEYS,
    MAX_SENDER_NAME,
    FolderRole,
    SendMode,
)

_ADDRESS_RE: Final = re.compile(r"^[^@\s*]+@[^@\s*]+\.[^@\s*.]+$")
_DOMAIN_WILDCARD_RE: Final = re.compile(r"^\*@[^@\s*]+\.[^@\s*.]+$")

# Send modes offered in the options flow (D46); a stored ``off`` (template) blocks sending and is shown as allowlist.
FORM_SEND_MODES: Final = (SendMode.ALLOWLIST.value, SendMode.ANY.value)


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


def _allowlist_error(allowlist: list[str]) -> str | None:
    if ALLOWLIST_WILDCARD in allowlist:
        return "wildcard_not_allowed"
    if any(not is_valid_allowlist_entry(e) for e in allowlist):
        return "invalid_allowlist_entry"
    return None


def check_send_options(send_mode: Any, allowlist: Any, sender_name: Any) -> tuple[dict[str, Any], dict[str, str]]:
    """Normalise the sending fields of the options form; errors map the field to a translation key (D46, D52)."""
    errors: dict[str, str] = {}
    mode = str(send_mode or SendMode.ALLOWLIST.value)
    if mode not in FORM_SEND_MODES:
        errors[CONF_SEND_MODE] = "invalid_send_mode"
    entries = normalize_allowlist(allowlist)
    if error := _allowlist_error(entries):
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


def folder_overrides(options: Mapping[str, Any]) -> dict[FolderRole, str]:
    """Return the non-empty folder overrides by role (not offered in the form; kept for stored options of the template)."""
    return {role: str(options.get(key) or "").strip() for role, key in FOLDER_OVERRIDE_KEYS.items() if options.get(key)}
