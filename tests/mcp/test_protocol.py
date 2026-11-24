"""JSON-RPC/MCP message handling without Home Assistant."""

from __future__ import annotations

from typing import Any

import pytest

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
from custom_components.gmx_mail_mcp.mcp.spike import SPIKE_STATUS


async def _echo(arguments: dict[str, Any]) -> ToolResult:
    return ToolResult.text(f"echo:{sorted(arguments)}")


async def _boom(arguments: dict[str, Any]) -> ToolResult:
    raise RuntimeError("secret internal detail")


ECHO = Tool(name="t_echo", title="Echo", description="d", input_schema={"type": "object"}, handler=_echo)
BOOM = Tool(name="t_boom", title="Boom", description="d", input_schema={"type": "object"}, handler=_boom)


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
    server = _server(ECHO, SPIKE_STATUS)
    listed = await server.handle_message(_req("tools/list"))
    assert listed.body is not None
    tools = listed.body["result"]["tools"]
    assert [t["name"] for t in tools] == ["t_echo", "gmx_spike_status"]
    spike = tools[1]
    assert spike["inputSchema"] == {"type": "object", "properties": {}, "additionalProperties": False}
    assert spike["annotations"]["readOnlyHint"] is True
    assert spike["annotations"]["destructiveHint"] is False

    called = await server.handle_message(_req("tools/call", {"name": "t_echo", "arguments": {"b": 1, "a": 2}}))
    assert called.body is not None
    assert called.body["result"] == {"content": [{"type": "text", "text": "echo:['a', 'b']"}], "isError": False}


@pytest.mark.parametrize("arguments", [None, {}])
async def test_tools_call_without_arguments(arguments: Any) -> None:
    params: dict[str, Any] = {"name": "gmx_spike_status"}
    if arguments is not None:
        params["arguments"] = arguments
    outcome = await _server(SPIKE_STATUS).handle_message(_req("tools/call", params))
    assert outcome.body is not None
    result = outcome.body["result"]
    assert result["isError"] is False
    assert "transport and authentication OK" in result["content"][0]["text"]


async def test_spike_rejects_arguments() -> None:
    outcome = await _server(SPIKE_STATUS).handle_message(_req("tools/call", {"name": "gmx_spike_status", "arguments": {"x": 1}}))
    assert outcome.body is not None
    assert outcome.body["result"]["isError"] is True


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
