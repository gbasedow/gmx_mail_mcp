"""GMX Mail MCP: MCP server for a GMX mailbox, hosted inside Home Assistant.

One config entry per GMX account (D11), each with its own endpoint ``/api/gmx_mail_mcp/<entry_id>/mcp``. Home Assistant
checks the access token; the view admits only the user chosen for the entry (D9); tools are switched in the options (D12).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .client.errors import GmxAuthError, GmxMailError, GmxTlsError
from .client.imap import ConnectionLimiter, ImapWorker
from .connection import (
    async_create_auth_issue,
    async_delete_auth_issue,
    create_tls_context,
    imap_settings_from_data,
    smtp_settings_from_data,
)
from .const import CONF_ALLOWED_USER, DOMAIN, IDLE_CHECK_INTERVAL_S
from .http import McpView
from .mcp.protocol import McpServer
from .mcp.registry import ToolContext, build_tools, enabled_tools
from .runtime import GmxMailMcpConfigEntry, RuntimeData

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the endpoint once; it serves every entry of this domain."""
    hass.http.register_view(McpView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GmxMailMcpConfigEntry) -> bool:
    """Log in once to verify access, then serve the MCP endpoint of this account."""
    limiter = ConnectionLimiter()
    worker = ImapWorker(
        imap_settings_from_data(entry.data),
        executor=hass.async_add_executor_job,
        ssl_context_factory=create_tls_context,
        limiter=limiter,
    )
    try:
        await worker.async_connect()
    except GmxAuthError as err:
        await worker.async_shutdown()
        async_create_auth_issue(hass, entry)
        raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="invalid_auth_or_imap_disabled") from err
    except GmxTlsError as err:
        await worker.async_shutdown()
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="tls_error") from err
    except GmxMailError as err:
        await worker.async_shutdown()
        raise ConfigEntryNotReady(translation_domain=DOMAIN, translation_key="cannot_connect") from err
    async_delete_auth_issue(hass, entry)

    @callback
    def _auth_failed() -> None:
        # Later logins are rejected: password revoked or IMAP switched off by GMX (GMX-NOTES row 6).
        _LOGGER.warning("GMX rejected the login; IMAP may have been switched off or the app password revoked")
        async_create_auth_issue(hass, entry)
        entry.async_start_reauth(hass)

    worker.on_auth_failed = _auth_failed

    async def _close_if_idle(_now: object) -> None:
        await worker.async_close_if_idle()

    entry.async_on_unload(async_track_time_interval(hass, _close_if_idle, timedelta(seconds=IDLE_CHECK_INTERVAL_S)))

    integration = await async_get_integration(hass, DOMAIN)
    tools = build_tools(
        ToolContext(worker=worker, options=lambda: entry.options, executor=hass.async_add_executor_job, account=entry.title)
    )
    server = McpServer(
        name=DOMAIN,
        version=str(integration.version),
        # Read per message: switching a tool in the options takes effect without a reload.
        tools=lambda: enabled_tools(tools, entry.options),
    )
    entry.runtime_data = RuntimeData(
        server=server,
        allowed_user_id=entry.data[CONF_ALLOWED_USER],
        worker=worker,
        limiter=limiter,
        smtp_settings=smtp_settings_from_data(entry.data),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GmxMailMcpConfigEntry) -> bool:
    """Log out; the endpoint answers 404 from now on (the view checks the entry state)."""
    await entry.runtime_data.worker.async_shutdown()
    return True
