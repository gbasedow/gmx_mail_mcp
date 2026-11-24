"""Home Assistant glue for the client layer: settings from entry data, TLS context, repair issues."""

from __future__ import annotations

import ssl
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.ssl import create_client_context

from .client.imap import ImapSettings, tls_context_with_minimum
from .client.smtp import SmtpSettings
from .const import (
    CONF_EMAIL,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SECURITY,
    CONF_PASSWORD,
    CONF_SEND_MODE,
    CONF_SMTP_HOST,
    CONF_SMTP_PORT,
    CONF_SMTP_SECURITY,
    DOMAIN,
    ISSUE_IMAP_POSSIBLY_DISABLED,
    ISSUE_NEW_TOOLS,
    ISSUE_SEND_MODE_ANY,
    ISSUE_SMTP_AUTH_FAILED,
    Security,
    SendMode,
)
from .mcp.registry import new_tool_names


def imap_settings_from_data(data: Mapping[str, Any]) -> ImapSettings:
    """Build IMAP settings from config entry data."""
    return ImapSettings(
        host=data[CONF_IMAP_HOST],
        port=int(data[CONF_IMAP_PORT]),
        security=Security(data[CONF_IMAP_SECURITY]),
        username=data[CONF_EMAIL],
        password=data[CONF_PASSWORD],
    )


def smtp_settings_from_data(data: Mapping[str, Any]) -> SmtpSettings:
    """Build SMTP settings from config entry data."""
    return SmtpSettings(
        host=data[CONF_SMTP_HOST],
        port=int(data[CONF_SMTP_PORT]),
        security=Security(data[CONF_SMTP_SECURITY]),
        username=data[CONF_EMAIL],
        password=data[CONF_PASSWORD],
    )


def create_tls_context() -> ssl.SSLContext:
    """Return a new verifying TLS context (certifi bundle, TLS >= 1.2). Blocking: loads certificates."""
    return tls_context_with_minimum(create_client_context())


def auth_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id for a rejected IMAP login."""
    return f"{ISSUE_IMAP_POSSIBLY_DISABLED}_{entry.entry_id}"


@callback
def async_create_auth_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Explain that GMX may have switched IMAP off (indistinguishable from a wrong password, GMX-NOTES row 6)."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        auth_issue_id(entry),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_IMAP_POSSIBLY_DISABLED,
        translation_placeholders={"account": entry.title},
        learn_more_url="https://hilfe.gmx.net/pop-imap/einschalten.html",
    )


@callback
def async_delete_auth_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the issue after a successful login."""
    ir.async_delete_issue(hass, DOMAIN, auth_issue_id(entry))


def smtp_auth_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id for a rejected SMTP login (M5)."""
    return f"{ISSUE_SMTP_AUTH_FAILED}_{entry.entry_id}"


@callback
def async_create_smtp_auth_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """GMX rejected the SMTP login while sending: app password revoked or changed."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        smtp_auth_issue_id(entry),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key=ISSUE_SMTP_AUTH_FAILED,
        translation_placeholders={"account": entry.title},
    )


@callback
def async_delete_smtp_auth_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the issue when the entry is set up again (after re-authentication)."""
    ir.async_delete_issue(hass, DOMAIN, smtp_auth_issue_id(entry))


def new_tools_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id for tools added by an update (D31)."""
    return f"{ISSUE_NEW_TOOLS}_{entry.entry_id}"


@callback
def async_update_new_tools_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Show which tools an update added (they stay off, D23) until the options are saved again."""
    names = new_tool_names(entry.options)
    if not names:
        ir.async_delete_issue(hass, DOMAIN, new_tools_issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        new_tools_issue_id(entry),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_NEW_TOOLS,
        translation_placeholders={"account": entry.title, "tools": ", ".join(names)},
    )


def send_mode_issue_id(entry: ConfigEntry) -> str:
    """Return the repair issue id for the open recipient rule (D60)."""
    return f"{ISSUE_SEND_MODE_ANY}_{entry.entry_id}"


@callback
def async_update_send_mode_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Show a warning while sending may go to any address (D60); it goes away when the mode is set back."""
    if entry.options.get(CONF_SEND_MODE) != SendMode.ANY:
        ir.async_delete_issue(hass, DOMAIN, send_mode_issue_id(entry))
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        send_mode_issue_id(entry),
        is_fixable=False,
        is_persistent=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_SEND_MODE_ANY,
        translation_placeholders={"account": entry.title},
    )


@callback
def async_delete_entry_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove every repair issue of a removed account."""
    for issue_id in (auth_issue_id(entry), smtp_auth_issue_id(entry), new_tools_issue_id(entry), send_mode_issue_id(entry)):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
