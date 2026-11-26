"""Diagnostics ("Download diagnostics") without credentials, addresses or mail content (D63).

Contains: version, server settings (host, port, security), connection state and server facts already known (no new
login is made), the options with the allowlist reduced to a count and domains and the sender name to set or not, the
counters and the newest audit entries without the HA user id. Never: password, account address, HA user id, subjects,
mail content, file names, the names of the user's own folders (except where an audit entry names one).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .client.folders import map_special_folders
from .client.policy import domain_of
from .const import (
    CONF_ALLOWED_USER,
    CONF_ALLOWLIST,
    CONF_EMAIL,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SECURITY,
    CONF_SENDER_NAME,
    CONF_SMTP_HOST,
    CONF_SMTP_PORT,
    CONF_SMTP_SECURITY,
    DOMAIN,
)
from .mcp.registry import enabled_tool_names, new_tool_names
from .runtime import GmxMailMcpConfigEntry

REDACTED: Final = "**REDACTED**"
AUDIT_ENTRIES: Final = 20
_SERVER_KEYS: Final = (CONF_IMAP_HOST, CONF_IMAP_PORT, CONF_IMAP_SECURITY, CONF_SMTP_HOST, CONF_SMTP_PORT, CONF_SMTP_SECURITY)


def _options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Options as stored, with the allowlist and the sender name reduced (no addresses, no names)."""
    result = {key: value for key, value in options.items() if key not in (CONF_ALLOWLIST, CONF_SENDER_NAME)}
    allowlist = options.get(CONF_ALLOWLIST)
    entries = [str(item) for item in allowlist] if isinstance(allowlist, list) else []
    result[CONF_ALLOWLIST] = {"count": len(entries), "domains": sorted({domain_of(item) for item in entries if "@" in item})}
    result[CONF_SENDER_NAME] = {"set": bool(str(options.get(CONF_SENDER_NAME) or "").strip())}
    return result


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GmxMailMcpConfigEntry) -> dict[str, Any]:
    """Return the diagnostics of one account."""
    integration = await async_get_integration(hass, DOMAIN)
    runtime = entry.runtime_data
    worker = runtime.worker
    info = worker.server_info
    connection: dict[str, Any] = {"connected": worker.connected, "reconnects": worker.reconnects}
    if info is not None:
        folder_map = map_special_folders(info.folders)
        connection["server"] = {
            "folders": sum(1 for folder in info.folders if folder.selectable),
            # Names of the special folders are system names (Gesendet, Entwürfe, ...), not chosen by the user.
            "special_folders": {role.value: folder_map.get(role) for role in folder_map.roles},
            "append_limit_bytes": info.append_limit,
            "capabilities": sorted(info.capabilities),
        }
    ledgers = runtime.ledgers
    audit = [{**item, "user": REDACTED if item.get("user") else None} for item in ledgers.audit.latest(AUDIT_ENTRIES)]
    return {
        "version": str(integration.version),
        "entry": {
            "title": REDACTED,
            CONF_EMAIL: REDACTED,
            CONF_ALLOWED_USER: REDACTED,
            **{key: entry.data.get(key) for key in _SERVER_KEYS},
        },
        "options": _options(entry.options),
        "tools": {
            "enabled": sorted(enabled_tool_names(entry.options)),
            "new_since_last_save": new_tool_names(entry.options),
        },
        "connection": connection,
        "counters": {
            "deleted_today": ledgers.deletes.used,
            "sent_today": ledgers.sends.sent_today,
            "recipients_last_hour": ledgers.sends.recipients_last_hour,
        },
        "audit_stored": len(ledgers.audit),
        "audit_latest": audit,
    }
