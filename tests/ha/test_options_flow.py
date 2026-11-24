"""Options flow: connection URL (D22) and tool switches (D12) that act without a reload."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.connect_info import TOKEN_PLACEHOLDER, URL_PLACEHOLDER, claude_code_command, connection_url
from custom_components.gmx_mail_mcp.const import CONF_ENABLED_TOOLS
from custom_components.gmx_mail_mcp.http import endpoint_path
from custom_components.gmx_mail_mcp.mcp.registry import default_enabled_tools
from tests.client.fakes import PASSWORD

from .conftest import bearer, rpc

type AddEntry = Callable[..., Awaitable[MockConfigEntry]]


async def _open(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return dict(await hass.config_entries.options.async_init(entry.entry_id))


async def test_form_shows_external_url_and_claude_code_command(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    await async_process_ha_core_config(hass, {"external_url": "https://ha.example.com", "internal_url": "http://192.168.1.2:8123"})
    entry = await add_entry(mcp_user)
    result = await _open(hass, entry)
    assert result["type"] is FlowResultType.FORM
    url = f"https://ha.example.com/api/gmx_mail_mcp/{entry.entry_id}/mcp"
    placeholders = result["description_placeholders"]
    assert placeholders["url"] == url
    assert url in placeholders["claude_code"]
    assert TOKEN_PLACEHOLDER in placeholders["claude_code"]
    assert PASSWORD not in str(result)
    key = next(k for k in result["data_schema"].schema if k == CONF_ENABLED_TOOLS)
    assert key.description["suggested_value"] == default_enabled_tools()


async def test_internal_url_when_no_external(hass: HomeAssistant) -> None:
    await async_process_ha_core_config(hass, {"internal_url": "http://192.168.1.2:8123"})
    assert connection_url(hass, "abc") == "http://192.168.1.2:8123/api/gmx_mail_mcp/abc/mcp"


async def test_placeholder_when_no_url(hass: HomeAssistant) -> None:
    with patch("custom_components.gmx_mail_mcp.connect_info.get_url", side_effect=NoURLAvailableError):
        assert connection_url(hass, "abc") == f"{URL_PLACEHOLDER}/api/gmx_mail_mcp/abc/mcp"


def test_claude_code_command_quotes_header() -> None:
    command = claude_code_command("https://h/x")
    assert command == f'claude mcp add --transport http gmx-mail https://h/x --header "Authorization: Bearer {TOKEN_PLACEHOLDER}"'


async def test_switches_apply_without_reload(
    hass: HomeAssistant, client: TestClient, add_entry: AddEntry, mcp_user: MockUser, make_token: Callable[..., Awaitable[str]]
) -> None:
    entry = await add_entry(mcp_user, {"unrelated_option": 1})
    headers = bearer(await make_token(mcp_user))

    async def listed() -> list[str]:
        resp = await client.post(endpoint_path(entry.entry_id), json=rpc("tools/list"), headers=headers)
        return [t["name"] for t in (await resp.json())["result"]["tools"]]

    assert await listed() == ["gmx_get_status"]
    worker = entry.runtime_data.worker

    result = await _open(hass, entry)
    result = dict(await hass.config_entries.options.async_configure(result["flow_id"], {CONF_ENABLED_TOOLS: []}))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"unrelated_option": 1, CONF_ENABLED_TOOLS: []}
    assert entry.runtime_data.worker is worker  # no reload, connection kept
    assert await listed() == []
    resp = await client.post(endpoint_path(entry.entry_id), json=rpc("tools/call", {"name": "gmx_get_status"}), headers=headers)
    assert (await resp.json())["error"]["code"] == -32602

    result = await _open(hass, entry)
    result = dict(await hass.config_entries.options.async_configure(result["flow_id"], {CONF_ENABLED_TOOLS: ["gmx_get_status"]}))
    assert entry.options[CONF_ENABLED_TOOLS] == ["gmx_get_status"]
    assert await listed() == ["gmx_get_status"]


async def test_missing_field_means_all_off(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    """The frontend may omit an empty multi-select; that must switch everything off, not restore defaults."""
    entry = await add_entry(mcp_user)
    result = await _open(hass, entry)
    await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.options[CONF_ENABLED_TOOLS] == []
