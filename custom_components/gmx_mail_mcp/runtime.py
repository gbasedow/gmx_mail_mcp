"""Runtime data of a config entry."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .mcp.protocol import McpServer


@dataclass(slots=True)
class RuntimeData:
    """Per-entry state: the MCP server and the only HA user it admits (D9)."""

    server: McpServer
    allowed_user_id: str


type GmxMailMcpConfigEntry = ConfigEntry[RuntimeData]
