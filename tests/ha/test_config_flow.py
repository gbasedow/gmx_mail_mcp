"""Config flow of the M0 spike: choice of the allowed user (D9)."""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockUser

from custom_components.gmx_mail_mcp.const import CONF_ALLOWED_USER, DOMAIN


async def test_flow_creates_entry_for_chosen_user(hass: HomeAssistant, mcp_user: MockUser) -> None:
    system_user = await hass.auth.async_create_system_user("system test")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    options = result["data_schema"].schema[CONF_ALLOWED_USER].config["options"]
    values = [o["value"] for o in options]
    assert mcp_user.id in values
    assert system_user.id not in values  # system users are never offered

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ALLOWED_USER: mcp_user.id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_ALLOWED_USER: mcp_user.id}
    assert result["title"] == "GMX Mail MCP (MCP client)"
    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED


async def test_flow_aborts_without_users(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_users"


async def test_inactive_user_not_offered(hass: HomeAssistant, mcp_user: MockUser) -> None:
    inactive = MockUser(name="Inactive", is_active=False).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    values = [o["value"] for o in result["data_schema"].schema[CONF_ALLOWED_USER].config["options"]]
    assert values == [mcp_user.id]
    assert inactive.id not in values
