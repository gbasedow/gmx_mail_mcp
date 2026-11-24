"""Config flow. M0 spike: only the HA user whose tokens the endpoint admits (D9). Account fields follow in M1."""

from __future__ import annotations

from typing import Any

import probatio
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig, SelectSelectorMode

from .const import CONF_ALLOWED_USER, DOMAIN


class GmxMailMcpConfigFlow(ConfigFlow, domain=DOMAIN):
    """Set up one MCP endpoint."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the allowed user."""
        users = {
            user.id: user.name or user.id for user in await self.hass.auth.async_get_users() if user.is_active and not user.system_generated
        }
        if not users:
            return self.async_abort(reason="no_users")
        if user_input is not None and user_input[CONF_ALLOWED_USER] in users:
            user_id = user_input[CONF_ALLOWED_USER]
            return self.async_create_entry(title=f"GMX Mail MCP ({users[user_id]})", data={CONF_ALLOWED_USER: user_id})

        options = [SelectOptionDict(value=user_id, label=name) for user_id, name in sorted(users.items(), key=lambda item: item[1])]
        schema = probatio.Schema(
            {probatio.Required(CONF_ALLOWED_USER): SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))}
        )
        return self.async_show_form(step_id="user", data_schema=schema)
