"""M0 spike tool: proves the transport end to end without touching any mailbox. Removed with M2."""

from __future__ import annotations

from typing import Any

from .protocol import Tool, ToolResult


async def _spike_status(arguments: dict[str, Any]) -> ToolResult:
    if arguments:
        return ToolResult.text("This tool takes no arguments", is_error=True)
    return ToolResult.text("gmx_mail_mcp spike: transport and authentication OK. No mailbox is connected.")


SPIKE_STATUS = Tool(
    name="gmx_spike_status",
    title="GMX MCP spike status",
    description="Connection test of the GMX Mail MCP endpoint. Takes no arguments and reads no mail.",
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    handler=_spike_status,
)
