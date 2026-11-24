"""GMX Mail MCP: MCP server for a GMX mailbox, hosted inside Home Assistant.

M0 spike: the MCP endpoint with Home Assistant authentication and the per-entry user check (D9), serving only a
connection test tool. The client layer (``client/``, ``const.py``, ``options.py``, ``envelope.py``, ``confirm.py``) is
carried over from ``gbasedow/gmx_mail`` and not wired up yet, see ``SPEC.md`` (M1).
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import CONF_ALLOWED_USER, DOMAIN
from .http import McpView
from .mcp.protocol import McpServer
from .mcp.spike import SPIKE_STATUS
from .runtime import GmxMailMcpConfigEntry, RuntimeData

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the endpoint once; it serves every entry of this domain."""
    hass.http.register_view(McpView())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: GmxMailMcpConfigEntry) -> bool:
    """Create the MCP server of one entry."""
    integration = await async_get_integration(hass, DOMAIN)
    server = McpServer(name=DOMAIN, version=str(integration.version), tools=lambda: (SPIKE_STATUS,))
    entry.runtime_data = RuntimeData(server=server, allowed_user_id=entry.data[CONF_ALLOWED_USER])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: GmxMailMcpConfigEntry) -> bool:
    """Nothing to tear down yet (no connections in M0)."""
    return True
