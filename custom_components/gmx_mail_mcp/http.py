"""MCP endpoint (Streamable HTTP, stateless, POST only): ``/api/gmx_mail_mcp/<entry_id>/mcp``.

Home Assistant authenticates the request before this view runs (``requires_auth``): only a valid access token
(Bearer, long-lived tokens included) gets through, otherwise 401. POST is never authenticated by signed paths
(HA accepts those for GET/HEAD only). This view then admits only the HA user chosen for the entry (D9); every
other user gets 403. GET (server-sent events) and DELETE (sessions) are not offered: 405.
"""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from aiohttp import web
from homeassistant.components.http.const import KEY_HASS_USER
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.http import KEY_HASS, HomeAssistantView

from .const import DOMAIN
from .mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS, MessageKind, parse_error

if TYPE_CHECKING:
    from homeassistant.auth.models import User

    from .runtime import GmxMailMcpConfigEntry

_LOGGER = logging.getLogger(__name__)

MCP_URL: Final = "/api/gmx_mail_mcp/{entry_id}/mcp"
HEADER_PROTOCOL_VERSION: Final = "MCP-Protocol-Version"
CONTENT_TYPE_JSON: Final = "application/json"


def endpoint_path(entry_id: str) -> str:
    """Path of the endpoint of one config entry."""
    return MCP_URL.format(entry_id=entry_id)


class McpView(HomeAssistantView):
    """One view for all entries; the entry is part of the path."""

    url = MCP_URL
    name = "api:gmx_mail_mcp:mcp"
    requires_auth = True

    async def post(self, request: web.Request, entry_id: str) -> web.Response:
        """Handle one JSON-RPC message."""
        hass = request.app[KEY_HASS]
        entry: GmxMailMcpConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
        # runtime_data survives an unload, so the state decides whether the endpoint is live.
        if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
            return _plain(HTTPStatus.NOT_FOUND, "Unknown endpoint")

        user: User = request[KEY_HASS_USER]
        if not _user_allowed(user, entry.runtime_data.allowed_user_id):
            # Same answer for every reason; the log names no token.
            _LOGGER.warning("Rejected MCP request of a user who is not allowed for entry %s", entry.entry_id)
            return _plain(HTTPStatus.FORBIDDEN, "User not allowed")
        if (rejection := _check_transport(request)) is not None:
            return rejection

        try:
            message = json.loads(await request.read())
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            outcome = parse_error()
        else:
            outcome = await entry.runtime_data.server.handle_message(message)

        match outcome.kind:
            case MessageKind.ACCEPTED:
                return web.Response(status=HTTPStatus.ACCEPTED)
            case MessageKind.INVALID:
                return self.json(outcome.body, HTTPStatus.BAD_REQUEST)
            case MessageKind.REQUEST:
                return self.json(outcome.body)


def _check_transport(request: web.Request) -> web.Response | None:
    """Header rules of the Streamable HTTP transport; None if the request may be processed."""
    if not _origin_allowed(request):
        return _plain(HTTPStatus.FORBIDDEN, "Origin not allowed")
    version = request.headers.get(HEADER_PROTOCOL_VERSION)
    if version is not None and version not in SUPPORTED_PROTOCOL_VERSIONS:
        return _plain(HTTPStatus.BAD_REQUEST, "Unsupported MCP-Protocol-Version")
    if request.content_type != CONTENT_TYPE_JSON:
        return _plain(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
    if not _accepts_json(request.headers.get("Accept")):
        return _plain(HTTPStatus.NOT_ACCEPTABLE, "Client must accept application/json")
    return None


def _user_allowed(user: User, allowed_user_id: str) -> bool:
    return user.is_active and not user.system_generated and user.id == allowed_user_id


def _origin_allowed(request: web.Request) -> bool:
    """DNS-rebinding protection required by the MCP transport spec.

    MCP clients (Claude Code, the claude.ai backend) send no Origin. Browsers do; then only the same origin
    as the request itself is accepted.
    """
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    parts = urlsplit(origin)
    return parts.scheme == request.scheme and parts.netloc == request.host


def _accepts_json(accept: str | None) -> bool:
    if accept is None:
        return True
    media = {part.split(";", 1)[0].strip().lower() for part in accept.split(",")}
    return bool(media & {CONTENT_TYPE_JSON, "application/*", "*/*"})


def _plain(status: HTTPStatus, text: str) -> web.Response:
    return web.Response(status=status, text=text)
