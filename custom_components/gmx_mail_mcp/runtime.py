"""Runtime data of a config entry."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry

from .client.imap import ConnectionLimiter, ImapWorker
from .client.smtp import SmtpSettings
from .mcp.protocol import McpServer


@dataclass(slots=True)
class RuntimeData:
    """Objects that live as long as the entry is loaded."""

    server: McpServer
    allowed_user_id: str  # the only HA user the endpoint admits (D9)
    worker: ImapWorker
    limiter: ConnectionLimiter
    smtp_settings: SmtpSettings  # used from M5 (sending)


type GmxMailMcpConfigEntry = ConfigEntry[RuntimeData]
