"""M4 in Home Assistant: audit log with the caller's user id, persistence in .storage, removal, options of the delete level."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import (
    CONF_ALLOW_PERMANENT_DELETE,
    CONF_AUDIT_RETENTION_DAYS,
    CONF_ENABLED_TOOLS,
    CONF_MAX_DELETE_PER_DAY,
    CONF_TOOLS_WITHOUT_CONFIRMATION,
    DOMAIN,
)
from custom_components.gmx_mail_mcp.http import endpoint_path
from tests.client.fakes import FakeImapServer

from .conftest import bearer, rpc

AddEntry = Callable[..., Awaitable[MockConfigEntry]]
TOOLS = ["gmx_create_folder", "gmx_get_audit_log"]


async def _call(client: TestClient, entry: MockConfigEntry, headers: dict[str, str], name: str, arguments: dict[str, Any]) -> Any:
    resp = await client.post(endpoint_path(entry.entry_id), json=rpc("tools/call", {"name": name, "arguments": arguments}), headers=headers)
    return (await resp.json())["result"]


async def test_audit_records_user_and_survives_reload(
    *,
    hass: HomeAssistant,
    client: TestClient,
    add_entry: AddEntry,
    mcp_user: MockUser,
    make_token: Callable[..., Awaitable[str]],
    hass_storage: dict[str, Any],
    imap_server: FakeImapServer,
) -> None:
    entry = await add_entry(mcp_user, {CONF_ENABLED_TOOLS: TOOLS})
    headers = bearer(await make_token(mcp_user))
    result = await _call(client, entry, headers, "gmx_create_folder", {"name": "Belege"})
    assert result["isError"] is False

    entries = entry.runtime_data.ledgers.audit.latest(5)
    assert [(e["tool"], e["outcome"], e["user"], e["folders"]) for e in entries] == [("gmx_create_folder", "ok", mcp_user.id, ["Belege"])]

    assert await hass.config_entries.async_unload(entry.entry_id)  # writes the log at once
    key = f"{DOMAIN}.{entry.entry_id}.audit"
    assert hass_storage[key]["data"]["entries"][0]["tool"] == "gmx_create_folder"
    assert f"{DOMAIN}.{entry.entry_id}.deletes" in hass_storage

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    log = await _call(client, entry, headers, "gmx_get_audit_log", {})
    assert log["isError"] is False
    assert '"gmx_create_folder"' in log["content"][0]["text"]


async def test_removing_the_entry_deletes_the_log(
    *,
    hass: HomeAssistant,
    client: TestClient,
    add_entry: AddEntry,
    mcp_user: MockUser,
    make_token: Callable[..., Awaitable[str]],
    hass_storage: dict[str, Any],
) -> None:
    entry = await add_entry(mcp_user, {CONF_ENABLED_TOOLS: TOOLS})
    await _call(client, entry, bearer(await make_token(mcp_user)), "gmx_create_folder", {"name": "Belege"})
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert f"{DOMAIN}.{entry.entry_id}.audit" in hass_storage
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}.audit" not in hass_storage
    assert f"{DOMAIN}.{entry.entry_id}.deletes" not in hass_storage


async def test_delete_options_are_saved(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user)
    result = dict(await hass.config_entries.options.async_init(entry.entry_id))
    keys = {str(k) for k in result["data_schema"].schema}
    assert {CONF_ALLOW_PERMANENT_DELETE, CONF_MAX_DELETE_PER_DAY, CONF_TOOLS_WITHOUT_CONFIRMATION, CONF_AUDIT_RETENTION_DAYS} <= keys
    result = dict(
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_ENABLED_TOOLS: ["gmx_trash"],
                CONF_ALLOW_PERMANENT_DELETE: True,
                CONF_MAX_DELETE_PER_DAY: 25,
                CONF_TOOLS_WITHOUT_CONFIRMATION: ["gmx_delete_folder"],
                CONF_AUDIT_RETENTION_DAYS: 30,
            },
        )
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_ALLOW_PERMANENT_DELETE] is True
    assert entry.options[CONF_MAX_DELETE_PER_DAY] == 25
    assert entry.options[CONF_TOOLS_WITHOUT_CONFIRMATION] == ["gmx_delete_folder"]
    assert entry.options[CONF_AUDIT_RETENTION_DAYS] == 30


async def test_only_confirmable_tools_are_offered(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user)
    result = dict(await hass.config_entries.options.async_init(entry.entry_id))
    key = next(k for k in result["data_schema"].schema if k == CONF_TOOLS_WITHOUT_CONFIRMATION)
    selector = result["data_schema"].schema[key]
    assert selector.config["options"] == [
        "gmx_delete_permanently",
        "gmx_delete_folder",
        # M5 (D45): the sending tools can be chosen as well.
        "gmx_send_message",
        "gmx_reply",
        "gmx_forward",
        "gmx_send_draft",
    ]
    assert key.description["suggested_value"] == []
