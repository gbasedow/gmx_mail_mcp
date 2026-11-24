"""Tool registry and switches (D12), without Home Assistant."""

from __future__ import annotations

import json
import ssl
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.gmx_mail_mcp.client.imap import ConnectionLimiter, ImapSettings, ImapWorker
from custom_components.gmx_mail_mcp.const import CONF_ENABLED_TOOLS, Permission, Security
from custom_components.gmx_mail_mcp.mcp.protocol import INVALID_PARAMS, McpServer
from custom_components.gmx_mail_mcp.mcp.registry import (
    TOOL_NAMES,
    TOOL_SPECS,
    ToolContext,
    build_tools,
    default_enabled_tools,
    enabled_tool_names,
    enabled_tools,
)
from tests.client.fakes import EMAIL, PASSWORD, FakeImapServer

TRANSLATIONS = Path(__file__).parents[2] / "custom_components" / "gmx_mail_mcp" / "translations"


@pytest.fixture
def imap_server() -> Generator[FakeImapServer]:
    server = FakeImapServer()
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", server):
        yield server


async def _executor(func: Any, *args: Any) -> Any:
    return func(*args)


def _worker(password: str = PASSWORD) -> ImapWorker:
    settings = ImapSettings(host="imap.gmx.net", port=993, security=Security.SSL, username=EMAIL, password=password)
    return ImapWorker(settings, executor=_executor, ssl_context_factory=ssl.create_default_context, limiter=ConnectionLimiter())


def _server(options: dict[str, Any], worker: ImapWorker | None = None) -> McpServer:
    tools = build_tools(ToolContext(worker=worker or _worker()))
    return McpServer(name="srv", version="1", tools=lambda: enabled_tools(tools, options))


def _req(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}


async def _listed(server: McpServer) -> list[str]:
    outcome = await server.handle_message(_req("tools/list"))
    assert outcome.body is not None
    return [tool["name"] for tool in outcome.body["result"]["tools"]]


# --------------------------------------------------------------------------- catalogue


def test_catalogue_is_consistent() -> None:
    assert len(TOOL_NAMES) == len(TOOL_SPECS)  # unique names
    for spec in TOOL_SPECS:
        assert spec.name.startswith("gmx_")
    tools = build_tools(ToolContext(worker=_worker()))
    assert [(t.name, t.level) for t in tools] == [(s.name, s.level) for s in TOOL_SPECS]
    for tool in tools:
        assert tool.input_schema["type"] == "object"
        assert tool.description


@pytest.mark.parametrize("language", ["en", "de"])
def test_every_tool_has_a_switch_label(language: str) -> None:
    labels = json.loads((TRANSLATIONS / f"{language}.json").read_text(encoding="utf-8"))["selector"][CONF_ENABLED_TOOLS]["options"]
    assert set(labels) == set(TOOL_NAMES)


def test_defaults_are_read_only_tools() -> None:
    assert default_enabled_tools() == [s.name for s in TOOL_SPECS if s.level is Permission.READ]
    assert enabled_tool_names({}) == frozenset(default_enabled_tools())


def test_saved_choice_is_explicit_and_unknown_names_are_dropped() -> None:
    assert enabled_tool_names({CONF_ENABLED_TOOLS: []}) == frozenset()
    assert enabled_tool_names({CONF_ENABLED_TOOLS: None}) == frozenset()
    assert enabled_tool_names({CONF_ENABLED_TOOLS: ["gmx_get_status", "gmx_removed_tool"]}) == {"gmx_get_status"}


# --------------------------------------------------------------------------- SPEC section 7: every tool, switched off


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
async def test_disabled_tool_is_neither_listed_nor_callable(spec: Any) -> None:
    others = [name for name in TOOL_NAMES if name != spec.name]
    server = _server({CONF_ENABLED_TOOLS: others})
    assert spec.name not in await _listed(server)
    outcome = await server.handle_message(_req("tools/call", {"name": spec.name, "arguments": {}}))
    assert outcome.body is not None
    assert outcome.body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
async def test_enabled_tool_is_listed(spec: Any) -> None:
    assert await _listed(_server({CONF_ENABLED_TOOLS: [spec.name]})) == [spec.name]


async def test_switch_change_applies_to_the_next_message() -> None:
    options: dict[str, Any] = {}
    server = _server(options)
    assert await _listed(server) == default_enabled_tools()
    options[CONF_ENABLED_TOOLS] = []
    assert await _listed(server) == []


# --------------------------------------------------------------------------- gmx_get_status


async def _status(worker: ImapWorker) -> dict[str, Any]:
    outcome = await _server({}, worker).handle_message(_req("tools/call", {"name": "gmx_get_status"}))
    assert outcome.body is not None
    result = outcome.body["result"]
    return {"is_error": result["isError"], **json.loads(result["content"][0]["text"])}


async def test_status_ok(imap_server: FakeImapServer) -> None:
    status = await _status(_worker())
    assert status["is_error"] is False
    assert status["connection"] == "ok"
    server = status["server"]
    assert server["special_folders"] == ["drafts", "inbox", "junk", "sent", "trash"]
    assert server["missing_recommended_capabilities"] == []
    assert server["folders"] >= 5
    text = json.dumps(status)
    assert EMAIL not in text  # no address in the output
    assert PASSWORD not in text


async def test_status_auth_failure(imap_server: FakeImapServer) -> None:
    status = await _status(_worker(password="wrong"))
    assert status == {"is_error": True, "connection": "error", "error": "auth_failed_or_imap_disabled"}


async def test_status_connection_failure(imap_server: FakeImapServer) -> None:
    imap_server.connect_error = OSError("unreachable")
    status = await _status(_worker())
    assert status == {"is_error": True, "connection": "error", "error": "cannot_connect"}


async def test_status_rejects_arguments(imap_server: FakeImapServer) -> None:
    outcome = await _server({}).handle_message(_req("tools/call", {"name": "gmx_get_status", "arguments": {"x": 1}}))
    assert outcome.body is not None
    assert outcome.body["result"]["isError"] is True
