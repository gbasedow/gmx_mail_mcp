"""Options flow: connection URL (D22) and tool switches (D12) that act without a reload."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.network import NoURLAvailableError
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.connect_info import TOKEN_PLACEHOLDER, URL_PLACEHOLDER, claude_code_command, connection_url
from custom_components.gmx_mail_mcp.const import (
    CONF_ALLOW_HTML_OUTPUT,
    CONF_ALLOW_PERMANENT_DELETE,
    CONF_AUDIT_RETENTION_DAYS,
    CONF_ENABLED_TOOLS,
    CONF_KNOWN_TOOLS,
    CONF_MAX_ATTACHMENT_MB,
    CONF_MAX_DELETE_PER_DAY,
    CONF_MAX_UPLOAD_MB,
    CONF_TOOLS_WITHOUT_CONFIRMATION,
    DOMAIN,
)
from custom_components.gmx_mail_mcp.http import endpoint_path
from custom_components.gmx_mail_mcp.mcp.registry import TOOL_NAMES, TOOL_SPECS, TOOLS_WITHOUT_RECORD, default_enabled_tools
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

    assert await listed() == default_enabled_tools()
    worker = entry.runtime_data.worker

    result = await _open(hass, entry)
    result = dict(await hass.config_entries.options.async_configure(result["flow_id"], {CONF_ENABLED_TOOLS: []}))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {
        "unrelated_option": 1,
        CONF_ENABLED_TOOLS: [],
        CONF_KNOWN_TOOLS: [spec.name for spec in TOOL_SPECS],
        CONF_MAX_ATTACHMENT_MB: 10,
        CONF_MAX_UPLOAD_MB: 10,
        CONF_ALLOW_HTML_OUTPUT: False,
        # M4 (D39, D42, D45): defaults - permanent deletion off, no daily limit, confirmation everywhere, 90 days audit.
        CONF_ALLOW_PERMANENT_DELETE: False,
        CONF_MAX_DELETE_PER_DAY: 0,
        CONF_TOOLS_WITHOUT_CONFIRMATION: [],
        # M5 (D46-D54): allowlist mode with an empty list (nothing can be sent), template limits, copy in Sent.
        "send_mode": "allowlist",
        "allowlist": [],
        "allow_reply_to_sender": False,
        "sender_name": "",
        "sent_copy_mode": "append",
        "max_recipients": 10,
        "max_send_per_day": 20,
        "max_recipients_per_hour": 10,
        "max_message_size_mb": 20,
        CONF_AUDIT_RETENTION_DAYS: 90,
    }
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


def _suggested(result: dict[str, Any], key: str) -> Any:
    marker = next(k for k in result["data_schema"].schema if k == key)
    return (marker.description or {}).get("suggested_value")


async def test_attachment_limit_and_html_switch(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user)
    result = await _open(hass, entry)
    assert _suggested(result, CONF_MAX_ATTACHMENT_MB) == 10  # D14 default
    assert _suggested(result, CONF_ALLOW_HTML_OUTPUT) is False  # D29 default
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_ENABLED_TOOLS: ["gmx_get_message"], CONF_MAX_ATTACHMENT_MB: 3, CONF_ALLOW_HTML_OUTPUT: True}
    )
    assert entry.options[CONF_MAX_ATTACHMENT_MB] == 3
    assert entry.options[CONF_ALLOW_HTML_OUTPUT] is True
    result = await _open(hass, entry)
    assert _suggested(result, CONF_MAX_ATTACHMENT_MB) == 3
    assert _suggested(result, CONF_ALLOW_HTML_OUTPUT) is True


@pytest.mark.parametrize("value", [0, 11, 50])
async def test_attachment_limit_out_of_range_is_rejected(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser, value: int) -> None:
    entry = await add_entry(mcp_user)
    result = await _open(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(result["flow_id"], {CONF_MAX_ATTACHMENT_MB: value})
    assert CONF_MAX_ATTACHMENT_MB not in entry.options


# --------------------------------------------------------------------------- M3: upload limit, new tools (D31)


async def test_upload_limit(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user)
    result = await _open(hass, entry)
    assert _suggested(result, CONF_MAX_UPLOAD_MB) == 10  # D17 default
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_MAX_UPLOAD_MB: 20})
    assert entry.options[CONF_MAX_UPLOAD_MB] == 20
    for value in (0, 21):
        result = await _open(hass, entry)
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(result["flow_id"], {CONF_MAX_UPLOAD_MB: value})
    assert entry.options[CONF_MAX_UPLOAD_MB] == 20


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"new_tools_available_{entry.entry_id}")


async def test_new_tools_after_update_are_shown_until_saved(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    """Options saved by 0.0.4 (no known_tools): the M3 tools stay off, a repair issue and the form name them (D31)."""
    saved_by_old_version = {CONF_ENABLED_TOOLS: sorted(TOOLS_WITHOUT_RECORD)}
    entry = await add_entry(mcp_user, saved_by_old_version)
    new = [spec.name for spec in TOOL_SPECS if spec.name not in TOOLS_WITHOUT_RECORD]
    assert new and all(name.startswith("gmx_") for name in new)
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_key == "new_tools_available"
    assert issue.translation_placeholders == {"account": entry.title, "tools": ", ".join(new)}
    result = await _open(hass, entry)
    assert result["description_placeholders"]["new_tools"] == ", ".join(f"`{name}`" for name in new)
    assert _suggested(result, CONF_ENABLED_TOOLS) == [spec.name for spec in TOOL_SPECS if spec.name in TOOLS_WITHOUT_RECORD]

    # Saving without switching anything on acknowledges the new tools: the issue goes, nothing new is listed.
    await hass.config_entries.options.async_configure(result["flow_id"], {CONF_ENABLED_TOOLS: sorted(TOOLS_WITHOUT_RECORD)})
    await hass.async_block_till_done()
    assert _issue(hass, entry) is None
    assert set(entry.options[CONF_KNOWN_TOOLS]) == set(TOOL_NAMES)
    assert not set(entry.options[CONF_ENABLED_TOOLS]) - TOOLS_WITHOUT_RECORD
    result = await _open(hass, entry)
    assert result["description_placeholders"]["new_tools"] == "-"


async def test_no_new_tools_without_saved_choice(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    """Never saved: defaults apply (read tools on), so nothing is announced as new."""
    entry = await add_entry(mcp_user)
    assert _issue(hass, entry) is None
    assert (await _open(hass, entry))["description_placeholders"]["new_tools"] == "-"


async def test_recorded_tools_decide_what_is_new(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    """With known_tools recorded, only tools missing from that record are new."""
    known = [spec.name for spec in TOOL_SPECS if spec.name != "gmx_save_draft"]
    entry = await add_entry(mcp_user, {CONF_ENABLED_TOOLS: [], CONF_KNOWN_TOOLS: known})
    issue = _issue(hass, entry)
    assert issue is not None and issue.translation_placeholders is not None
    assert issue.translation_placeholders["tools"] == "gmx_save_draft"
