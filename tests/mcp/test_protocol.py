"""JSON-RPC/MCP message handling without Home Assistant."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from custom_components.gmx_mail_mcp.const import Permission
from custom_components.gmx_mail_mcp.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    SUPPORTED_PROTOCOL_VERSIONS,
    McpServer,
    MessageKind,
    Outcome,
    Tool,
    ToolResult,
    parse_error,
)


async def _echo(arguments: dict[str, Any]) -> ToolResult:
    return ToolResult.text(f"echo:{sorted(arguments)}")


async def _boom(arguments: dict[str, Any]) -> ToolResult:
    raise RuntimeError("secret internal detail")


async def _noargs(arguments: dict[str, Any]) -> ToolResult:
    return ToolResult.text("plain")


async def _slow(arguments: dict[str, Any]) -> ToolResult:
    await asyncio.sleep(10)
    return ToolResult.text("never")


def _tool(name: str, handler: Any, level: Permission = Permission.READ) -> Tool:
    return Tool(name=name, title=name.title(), description="d", input_schema={"type": "object"}, level=level, handler=handler)


ECHO = _tool("t_echo", _echo)
BOOM = _tool("t_boom", _boom)
PLAIN = _tool("t_plain", _noargs)


def _server(*tools: Tool) -> McpServer:
    return McpServer(name="srv", version="1.2.3", tools=lambda: tools)


def _req(method: str, params: Any = None, request_id: Any = 1) -> dict[str, Any]:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def _error_code(outcome: Outcome) -> int:
    assert outcome.body is not None
    return int(outcome.body["error"]["code"])


@pytest.mark.parametrize("version", SUPPORTED_PROTOCOL_VERSIONS)
async def test_initialize_echoes_supported_version(version: str) -> None:
    outcome = await _server().handle_message(_req("initialize", {"protocolVersion": version, "capabilities": {}}))
    assert outcome.kind is MessageKind.REQUEST
    assert outcome.body is not None
    result = outcome.body["result"]
    assert outcome.body["id"] == 1
    assert result["protocolVersion"] == version
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "srv", "version": "1.2.3"}
    assert "untrusted" in result["instructions"]


@pytest.mark.parametrize("version", ["1999-01-01", None, 5])
async def test_initialize_offers_latest_for_unknown_version(version: Any) -> None:
    outcome = await _server().handle_message(_req("initialize", {"protocolVersion": version}))
    assert outcome.body is not None
    assert outcome.body["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


async def test_ping() -> None:
    outcome = await _server().handle_message(_req("ping", request_id="abc"))
    assert outcome == Outcome(MessageKind.REQUEST, {"jsonrpc": "2.0", "id": "abc", "result": {}})


async def test_tools_list_and_call() -> None:
    server = _server(ECHO, PLAIN)
    listed = await server.handle_message(_req("tools/list"))
    assert listed.body is not None
    tools = listed.body["result"]["tools"]
    assert [t["name"] for t in tools] == ["t_echo", "t_plain"]
    assert tools[0]["inputSchema"] == {"type": "object"}
    assert tools[0]["title"] == "T_Echo"

    called = await server.handle_message(_req("tools/call", {"name": "t_echo", "arguments": {"b": 1, "a": 2}}))
    assert called.body is not None
    assert called.body["result"] == {"content": [{"type": "text", "text": "echo:['a', 'b']"}], "isError": False}


@pytest.mark.parametrize(
    ("level", "read_only", "destructive"),
    [
        (Permission.READ, True, False),
        (Permission.ORGANIZE, False, False),
        (Permission.DELETE, False, True),
        (Permission.SEND, False, True),  # sending cannot be undone (M5)
    ],
)
def test_annotations_follow_level(level: Permission, read_only: bool, destructive: bool) -> None:
    annotations = _tool("x", _noargs, level).as_dict()["annotations"]
    assert annotations["readOnlyHint"] is read_only
    assert annotations["destructiveHint"] is destructive
    assert annotations["openWorldHint"] is True


@pytest.mark.parametrize("arguments", [None, {}])
async def test_tools_call_without_arguments(arguments: Any) -> None:
    params: dict[str, Any] = {"name": "t_plain"}
    if arguments is not None:
        params["arguments"] = arguments
    outcome = await _server(PLAIN).handle_message(_req("tools/call", params))
    assert outcome.body is not None
    assert outcome.body["result"] == {"content": [{"type": "text", "text": "plain"}], "isError": False}


async def test_tool_timeout(caplog: pytest.LogCaptureFixture) -> None:
    server = McpServer(name="srv", version="1", tools=lambda: (_tool("t_slow", _slow),), call_timeout=0.05)
    outcome = await server.handle_message(_req("tools/call", {"name": "t_slow"}))
    assert outcome.body is not None
    assert outcome.body["result"] == {"content": [{"type": "text", "text": "Tool call timed out"}], "isError": True}
    assert "timed out" in caplog.text


async def test_tool_list_is_evaluated_per_message() -> None:
    """A tool switched off between two calls is neither listed nor callable any more."""
    enabled = [ECHO]
    server = McpServer(name="srv", version="1", tools=lambda: tuple(enabled))
    assert (await server.handle_message(_req("tools/call", {"name": "t_echo"}))).body["result"]["isError"] is False  # type: ignore[index]
    enabled.clear()
    listed = await server.handle_message(_req("tools/list"))
    assert listed.body is not None
    assert listed.body["result"]["tools"] == []
    assert _error_code(await server.handle_message(_req("tools/call", {"name": "t_echo"}))) == INVALID_PARAMS


async def test_unknown_tool() -> None:
    outcome = await _server(ECHO).handle_message(_req("tools/call", {"name": "nope"}))
    assert outcome.kind is MessageKind.REQUEST
    assert _error_code(outcome) == INVALID_PARAMS


@pytest.mark.parametrize("params", [{"name": 1}, {}, {"name": "t_echo", "arguments": []}, {"name": "t_echo", "arguments": "x"}])
async def test_tools_call_invalid_params(params: dict[str, Any]) -> None:
    assert _error_code(await _server(ECHO).handle_message(_req("tools/call", params))) == INVALID_PARAMS


async def test_tool_exception_is_hidden(caplog: pytest.LogCaptureFixture) -> None:
    outcome = await _server(BOOM).handle_message(_req("tools/call", {"name": "t_boom"}))
    assert outcome.body is not None
    result = outcome.body["result"]
    assert result["isError"] is True
    assert "secret internal detail" not in str(result)
    assert "secret internal detail" in caplog.text  # traceback only in the server log


async def test_unknown_method() -> None:
    outcome = await _server().handle_message(_req("resources/list"))
    assert outcome.kind is MessageKind.REQUEST
    assert _error_code(outcome) == METHOD_NOT_FOUND


async def test_params_must_be_object() -> None:
    assert _error_code(await _server().handle_message(_req("ping", [1, 2]))) == INVALID_PARAMS


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
        {"jsonrpc": "2.0", "id": 7, "result": {}},
        {"jsonrpc": "2.0", "id": 7, "error": {"code": 1, "message": "x"}},
    ],
)
async def test_notifications_and_responses_are_accepted(message: dict[str, Any]) -> None:
    assert await _server().handle_message(message) == Outcome(MessageKind.ACCEPTED)


@pytest.mark.parametrize(
    "message",
    [
        [_req("ping")],  # batch
        [],
        "ping",
        42,
        None,
        {"id": 1, "method": "ping"},  # jsonrpc missing
        {"jsonrpc": "1.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "id": 1},  # neither method nor result
        {"jsonrpc": "2.0", "id": 1, "method": 5},
        _req("ping", request_id=None),
        _req("ping", request_id=True),
        _req("ping", request_id=1.5),
        _req("ping", request_id={"a": 1}),
    ],
)
async def test_invalid_messages(message: Any) -> None:
    outcome = await _server().handle_message(message)
    assert outcome.kind is MessageKind.INVALID
    assert outcome.body is not None
    assert outcome.body["id"] is None
    assert _error_code(outcome) == INVALID_REQUEST


def test_parse_error() -> None:
    outcome = parse_error()
    assert outcome.kind is MessageKind.INVALID
    assert _error_code(outcome) == PARSE_ERROR
