"""Config, reauth, reconfigure and options flows.

Config flow: address, app password, allowed HA user (D9), server settings (GMX defaults, collapsed). IMAP and SMTP
logins are checked before the entry is created. Options flow: connection URL (D22) and the tool switches (D12).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override

import probatio
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .client.errors import GmxAuthError, GmxConnectionError, GmxConnectionLimitError, GmxSmtpAuthError, GmxTlsError
from .client.imap import ServerInfo, check_imap_account
from .client.smtp import check_smtp_login
from .connect_info import claude_code_command, connection_url
from .connection import create_tls_context, imap_settings_from_data, smtp_settings_from_data
from .const import (
    AUDIT_RETENTION_MAX_DAYS,
    AUDIT_RETENTION_MIN_DAYS,
    CONF_ALLOW_HTML_OUTPUT,
    CONF_ALLOW_PERMANENT_DELETE,
    CONF_ALLOWED_USER,
    CONF_AUDIT_RETENTION_DAYS,
    CONF_EMAIL,
    CONF_ENABLED_TOOLS,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SECURITY,
    CONF_KNOWN_TOOLS,
    CONF_MAX_ATTACHMENT_MB,
    CONF_MAX_DELETE_PER_DAY,
    CONF_MAX_UPLOAD_MB,
    CONF_PASSWORD,
    CONF_SMTP_HOST,
    CONF_SMTP_PORT,
    CONF_SMTP_SECURITY,
    CONF_TOOLS_WITHOUT_CONFIRMATION,
    DEFAULT_AUDIT_RETENTION_DAYS,
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_IMAP_SECURITY,
    DEFAULT_MAX_ATTACHMENT_MB,
    DEFAULT_MAX_UPLOAD_MB,
    DEFAULT_SMTP_HOST,
    DEFAULT_SMTP_PORT,
    DEFAULT_SMTP_SECURITY,
    DOMAIN,
    MAX_ATTACHMENT_MB,
    MAX_UPLOAD_MB,
    SECTION_SERVER,
    Security,
)
from .mcp.registry import CONFIRMABLE_TOOLS, TOOL_SPECS, enabled_tool_names, new_tool_names

_LOGGER = logging.getLogger(__name__)

SERVER_KEYS = (CONF_IMAP_HOST, CONF_IMAP_PORT, CONF_IMAP_SECURITY, CONF_SMTP_HOST, CONF_SMTP_PORT, CONF_SMTP_SECURITY)
SERVER_DEFAULTS: dict[str, Any] = {
    CONF_IMAP_HOST: DEFAULT_IMAP_HOST,
    CONF_IMAP_PORT: DEFAULT_IMAP_PORT,
    CONF_IMAP_SECURITY: DEFAULT_IMAP_SECURITY.value,
    CONF_SMTP_HOST: DEFAULT_SMTP_HOST,
    CONF_SMTP_PORT: DEFAULT_SMTP_PORT,
    CONF_SMTP_SECURITY: DEFAULT_SMTP_SECURITY.value,
}

_SECURITY_SELECTOR = SelectSelector(
    SelectSelectorConfig(options=[s.value for s in Security], mode=SelectSelectorMode.DROPDOWN, translation_key="security")
)
_PORT = probatio.All(NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)), probatio.Coerce(int))
_HOST = TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT))
_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password"))

SERVER_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_IMAP_HOST): _HOST,
        probatio.Required(CONF_IMAP_PORT): _PORT,
        probatio.Required(CONF_IMAP_SECURITY): _SECURITY_SELECTOR,
        probatio.Required(CONF_SMTP_HOST): _HOST,
        probatio.Required(CONF_SMTP_PORT): _PORT,
        probatio.Required(CONF_SMTP_SECURITY): _SECURITY_SELECTOR,
    }
)
REAUTH_SCHEMA = probatio.Schema({probatio.Required(CONF_PASSWORD): _PASSWORD})


def _normalize_server(values: Mapping[str, Any]) -> dict[str, Any]:
    server = {**SERVER_DEFAULTS, **{k: v for k, v in values.items() if k in SERVER_KEYS}}
    server[CONF_IMAP_HOST] = str(server[CONF_IMAP_HOST]).strip().lower()
    server[CONF_SMTP_HOST] = str(server[CONF_SMTP_HOST]).strip().lower()
    server[CONF_IMAP_PORT] = int(server[CONF_IMAP_PORT])
    server[CONF_SMTP_PORT] = int(server[CONF_SMTP_PORT])
    return server


async def _selectable_users(hass: HomeAssistant) -> dict[str, str]:
    """Active, non-system users by id; these are the only ones that can be chosen (D9)."""
    return {user.id: user.name or user.id for user in await hass.auth.async_get_users() if user.is_active and not user.system_generated}


def _user_selector(users: Mapping[str, str]) -> SelectSelector:
    options = [SelectOptionDict(value=user_id, label=name) for user_id, name in sorted(users.items(), key=lambda item: item[1])]
    return SelectSelector(SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN))


def _validate_account(data: Mapping[str, Any]) -> ServerInfo:
    """Check IMAP login, CAPABILITY, LIST and the SMTP login (blocking)."""
    context = create_tls_context()
    info = check_imap_account(imap_settings_from_data(data), context)
    check_smtp_login(smtp_settings_from_data(data), context)
    return info


async def async_validate_account(hass: HomeAssistant, data: Mapping[str, Any]) -> dict[str, str]:
    """Validate credentials and servers; return form errors (empty on success)."""
    try:
        info = await hass.async_add_executor_job(_validate_account, data)
    except GmxAuthError:
        return {"base": "invalid_auth_or_imap_disabled"}
    except GmxSmtpAuthError:
        return {"base": "smtp_auth_failed"}
    except GmxTlsError:
        return {"base": "tls_error"}
    except GmxConnectionError, GmxConnectionLimitError:
        return {"base": "cannot_connect"}
    except Exception:  # shown as "unknown" in the form
        _LOGGER.exception("Unexpected error while validating the GMX account")
        return {"base": "unknown"}
    if missing := info.missing_recommended:
        _LOGGER.warning("IMAP server lacks %s; fallback paths will be used", ", ".join(missing))
    return {}


class GmxMailMcpConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry (and one MCP endpoint) per GMX account (D11)."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    @override
    def async_get_options_flow(config_entry: ConfigEntry) -> GmxMailMcpOptionsFlow:
        """Return the options flow."""
        return GmxMailMcpOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for address, app password and the allowed user; server settings are pre-filled."""
        users = await _selectable_users(self.hass)
        if not users:
            return self.async_abort(reason="no_users")
        errors: dict[str, str] = {}
        if user_input is not None:
            email = str(user_input[CONF_EMAIL]).strip()
            if email.count("@") != 1 or not all(email.split("@")):
                errors[CONF_EMAIL] = "invalid_email"
            elif user_input[CONF_ALLOWED_USER] not in users:
                errors[CONF_ALLOWED_USER] = "invalid_user"
            else:
                await self.async_set_unique_id(email.lower())
                self._abort_if_unique_id_configured()
                data = {
                    CONF_EMAIL: email,
                    CONF_PASSWORD: user_input[CONF_PASSWORD],
                    CONF_ALLOWED_USER: user_input[CONF_ALLOWED_USER],
                    **_normalize_server(user_input.get(SECTION_SERVER) or {}),
                }
                errors = await async_validate_account(self.hass, data)
                if not errors:
                    return self.async_create_entry(title=email, data=data)

        schema = probatio.Schema(
            {
                probatio.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")),
                probatio.Required(CONF_PASSWORD): _PASSWORD,
                probatio.Required(CONF_ALLOWED_USER): _user_selector(users),
                probatio.Required(SECTION_SERVER): section(SERVER_SCHEMA, {"collapsed": True}),
            }
        )
        suggested: dict[str, Any] = {SECTION_SERVER: dict(SERVER_DEFAULTS)}
        if user_input is not None:
            suggested = {
                CONF_EMAIL: user_input.get(CONF_EMAIL),
                CONF_ALLOWED_USER: user_input.get(CONF_ALLOWED_USER),
                SECTION_SERVER: _normalize_server(user_input.get(SECTION_SERVER) or {}),
            }
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(schema, suggested), errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauthentication after a rejected login."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new app password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = await async_validate_account(self.hass, {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]})
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=REAUTH_SCHEMA, errors=errors, description_placeholders={"account": entry.title}
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Change the allowed user, hosts, ports and TLS mode."""
        entry = self._get_reconfigure_entry()
        users = await _selectable_users(self.hass)
        if not users:
            return self.async_abort(reason="no_users")
        errors: dict[str, str] = {}
        if user_input is not None:
            server = _normalize_server(user_input.get(SECTION_SERVER) or {})
            if user_input[CONF_ALLOWED_USER] not in users:
                errors[CONF_ALLOWED_USER] = "invalid_user"
            else:
                errors = await async_validate_account(self.hass, {**entry.data, **server})
            if not errors:
                return self.async_update_reload_and_abort(entry, data_updates={**server, CONF_ALLOWED_USER: user_input[CONF_ALLOWED_USER]})

        schema = probatio.Schema(
            {
                probatio.Required(CONF_ALLOWED_USER): _user_selector(users),
                probatio.Required(SECTION_SERVER): section(SERVER_SCHEMA, {"collapsed": False}),
            }
        )
        source = user_input if user_input is not None else {**entry.data, SECTION_SERVER: entry.data}
        suggested = {
            SECTION_SERVER: _normalize_server(source.get(SECTION_SERVER) or {}),
            # A user who no longer exists is not pre-selected, so the form forces a new choice.
            **({CONF_ALLOWED_USER: source[CONF_ALLOWED_USER]} if source.get(CONF_ALLOWED_USER) in users else {}),
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"account": entry.title},
        )


def _int_option(value: Any, default: int, low: int, high: int | None) -> int:
    try:
        number = int(value)
    except TypeError, ValueError:
        number = default
    number = max(low, number)
    return number if high is None else min(high, number)


class GmxMailMcpOptionsFlow(OptionsFlow):
    """Connection URL (D22), tool switches (D12), new tools (D31), size limits (D14, D17), HTML output (D29), deleting
    (permanent deletion, daily limit D39, tools without confirmation D45) and audit retention (D42).

    All take effect at once. Saving records the tools of this version (``known_tools``), so tools of a later update can
    be shown as new.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the URL and the settings."""
        options = self.config_entry.options
        if user_input is not None:
            chosen = set(user_input.get(CONF_ENABLED_TOOLS) or ())
            # Catalogue order; names the selector does not offer cannot get in.
            enabled = [spec.name for spec in TOOL_SPECS if spec.name in chosen]
            limit = min(MAX_ATTACHMENT_MB, max(1, int(user_input.get(CONF_MAX_ATTACHMENT_MB, DEFAULT_MAX_ATTACHMENT_MB))))
            upload = min(MAX_UPLOAD_MB, max(1, int(user_input.get(CONF_MAX_UPLOAD_MB, DEFAULT_MAX_UPLOAD_MB))))
            skipped = set(user_input.get(CONF_TOOLS_WITHOUT_CONFIRMATION) or ())
            return self.async_create_entry(
                data={
                    **options,
                    CONF_ENABLED_TOOLS: enabled,
                    CONF_KNOWN_TOOLS: [spec.name for spec in TOOL_SPECS],
                    CONF_MAX_ATTACHMENT_MB: limit,
                    CONF_MAX_UPLOAD_MB: upload,
                    CONF_ALLOW_HTML_OUTPUT: bool(user_input.get(CONF_ALLOW_HTML_OUTPUT, False)),
                    CONF_ALLOW_PERMANENT_DELETE: bool(user_input.get(CONF_ALLOW_PERMANENT_DELETE, False)),
                    CONF_MAX_DELETE_PER_DAY: _int_option(user_input.get(CONF_MAX_DELETE_PER_DAY), 0, 0, None),
                    # Only tools with a confirmation step can be chosen (D45).
                    CONF_TOOLS_WITHOUT_CONFIRMATION: [name for name in CONFIRMABLE_TOOLS if name in skipped],
                    CONF_AUDIT_RETENTION_DAYS: _int_option(
                        user_input.get(CONF_AUDIT_RETENTION_DAYS),
                        DEFAULT_AUDIT_RETENTION_DAYS,
                        AUDIT_RETENTION_MIN_DAYS,
                        AUDIT_RETENTION_MAX_DAYS,
                    ),
                }
            )

        url = connection_url(self.hass, self.config_entry.entry_id)
        schema = probatio.Schema(
            {
                probatio.Optional(CONF_ENABLED_TOOLS): SelectSelector(
                    SelectSelectorConfig(
                        options=[spec.name for spec in TOOL_SPECS],
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_ENABLED_TOOLS,
                    )
                ),
                probatio.Required(CONF_MAX_ATTACHMENT_MB, default=DEFAULT_MAX_ATTACHMENT_MB): probatio.All(
                    NumberSelector(
                        NumberSelectorConfig(min=1, max=MAX_ATTACHMENT_MB, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="MB")
                    ),
                    probatio.Coerce(int),
                ),
                probatio.Required(CONF_MAX_UPLOAD_MB, default=DEFAULT_MAX_UPLOAD_MB): probatio.All(
                    NumberSelector(
                        NumberSelectorConfig(min=1, max=MAX_UPLOAD_MB, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="MB")
                    ),
                    probatio.Coerce(int),
                ),
                probatio.Required(CONF_ALLOW_HTML_OUTPUT, default=False): BooleanSelector(),
                probatio.Required(CONF_ALLOW_PERMANENT_DELETE, default=False): BooleanSelector(),
                probatio.Required(CONF_MAX_DELETE_PER_DAY, default=0): probatio.All(
                    NumberSelector(NumberSelectorConfig(min=0, step=1, mode=NumberSelectorMode.BOX)),
                    probatio.Coerce(int),
                ),
                probatio.Optional(CONF_TOOLS_WITHOUT_CONFIRMATION): SelectSelector(
                    SelectSelectorConfig(
                        options=list(CONFIRMABLE_TOOLS),
                        multiple=True,
                        mode=SelectSelectorMode.LIST,
                        translation_key=CONF_TOOLS_WITHOUT_CONFIRMATION,
                    )
                ),
                probatio.Required(CONF_AUDIT_RETENTION_DAYS, default=DEFAULT_AUDIT_RETENTION_DAYS): probatio.All(
                    NumberSelector(
                        NumberSelectorConfig(
                            min=AUDIT_RETENTION_MIN_DAYS,
                            max=AUDIT_RETENTION_MAX_DAYS,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="d",
                        )
                    ),
                    probatio.Coerce(int),
                ),
            }
        )
        suggested = {
            CONF_ENABLED_TOOLS: [spec.name for spec in TOOL_SPECS if spec.name in enabled_tool_names(options)],
            CONF_MAX_ATTACHMENT_MB: int(options.get(CONF_MAX_ATTACHMENT_MB, DEFAULT_MAX_ATTACHMENT_MB)),
            CONF_MAX_UPLOAD_MB: int(options.get(CONF_MAX_UPLOAD_MB, DEFAULT_MAX_UPLOAD_MB)),
            CONF_ALLOW_HTML_OUTPUT: bool(options.get(CONF_ALLOW_HTML_OUTPUT, False)),
            CONF_ALLOW_PERMANENT_DELETE: bool(options.get(CONF_ALLOW_PERMANENT_DELETE, False)),
            CONF_MAX_DELETE_PER_DAY: _int_option(options.get(CONF_MAX_DELETE_PER_DAY), 0, 0, None),
            CONF_TOOLS_WITHOUT_CONFIRMATION: [n for n in CONFIRMABLE_TOOLS if n in (options.get(CONF_TOOLS_WITHOUT_CONFIRMATION) or ())],
            CONF_AUDIT_RETENTION_DAYS: _int_option(
                options.get(CONF_AUDIT_RETENTION_DAYS), DEFAULT_AUDIT_RETENTION_DAYS, AUDIT_RETENTION_MIN_DAYS, AUDIT_RETENTION_MAX_DAYS
            ),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            description_placeholders={
                "url": url,
                "claude_code": claude_code_command(url),
                # Names only (language-neutral); "-" when nothing is new.
                "new_tools": ", ".join(f"`{name}`" for name in new_tool_names(options)) or "-",
            },
        )
