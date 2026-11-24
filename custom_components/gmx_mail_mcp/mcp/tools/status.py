"""``gmx_get_status``: connection and server state of the account. Contains no mail data and no addresses."""

from __future__ import annotations

import json
from typing import Any

from ...client.errors import GmxAuthError, GmxBackoffError, GmxConnectionLimitError, GmxMailError, GmxTlsError
from ...client.folders import map_special_folders
from ...const import Permission
from ..protocol import Tool, ToolResult
from ..registry import ToolContext, ToolSpec

NAME = "gmx_get_status"

_ERRORS: tuple[tuple[type[GmxMailError], str], ...] = (
    (GmxAuthError, "auth_failed_or_imap_disabled"),
    (GmxTlsError, "tls_error"),
    (GmxBackoffError, "reconnect_backoff"),
    (GmxConnectionLimitError, "connection_limit"),
    (GmxMailError, "cannot_connect"),
)


def build(context: ToolContext) -> Tool:
    """Create the tool for one entry."""
    worker = context.worker

    async def _handler(arguments: dict[str, Any]) -> ToolResult:
        if arguments:
            return ToolResult.text("This tool takes no arguments", is_error=True)
        try:
            info = await worker.async_connect()
        except GmxMailError as err:
            code = next(code for kind, code in _ERRORS if isinstance(err, kind))
            return ToolResult.text(json.dumps({"connection": "error", "error": code}), is_error=True)
        folder_map = map_special_folders(info.folders)
        status = {
            "connection": "ok",
            "reconnects": worker.reconnects,
            "server": {
                "folders": sum(1 for folder in info.folders if folder.selectable),
                "special_folders": sorted(role.value for role in folder_map.roles),
                "append_limit_bytes": info.append_limit,
                "missing_recommended_capabilities": info.missing_recommended,
                "capabilities": sorted(info.capabilities),
            },
        }
        return ToolResult.text(json.dumps(status, ensure_ascii=False))

    return Tool(
        name=NAME,
        title="GMX mailbox status",
        description=(
            "Checks the connection to the GMX mailbox and returns server facts (number of folders, recognised special "
            "folders, upload limit, IMAP capabilities). Takes no arguments; reads no mail."
        ),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        level=Permission.READ,
        handler=_handler,
    )


SPEC = ToolSpec(name=NAME, level=Permission.READ, build=build)
