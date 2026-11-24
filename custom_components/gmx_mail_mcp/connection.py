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
    CONF_SMTP_HOST,
    CONF_SMTP_PORT,
    CONF_SMTP_SECURITY,
    DOMAIN,
    ISSUE_IMAP_POSSIBLY_DISABLED,
    Security,
)


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
