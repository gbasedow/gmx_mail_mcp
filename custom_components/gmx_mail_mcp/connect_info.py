"""Connection URL and client snippets shown in the options flow (D22). Never contains a token."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .http import endpoint_path

URL_PLACEHOLDER = "https://<your-home-assistant-url>"
TOKEN_PLACEHOLDER = "<LONG_LIVED_ACCESS_TOKEN>"  # noqa: S105 - placeholder text, not a secret


def connection_url(hass: HomeAssistant, entry_id: str) -> str:
    """URL a client should use (external first, so that claude.ai can reach it)."""
    try:
        # Order: configured external URL, else Nabu Casa, else the internal URL.
        base = get_url(hass, allow_internal=True, allow_ip=True, prefer_external=True)
    except NoURLAvailableError:
        base = URL_PLACEHOLDER
    return f"{base.rstrip('/')}{endpoint_path(entry_id)}"


def claude_code_command(url: str, server_name: str = "gmx-mail") -> str:
    """``claude mcp add`` command with a token placeholder."""
    return f'claude mcp add --transport http {server_name} {url} --header "Authorization: Bearer {TOKEN_PLACEHOLDER}"'
