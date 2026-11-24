"""Config flow: account, allowed user (D9), validation of IMAP and SMTP login, reauth, reconfigure."""

from __future__ import annotations

import ssl
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import (
    CONF_ALLOWED_USER,
    CONF_EMAIL,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_PASSWORD,
    CONF_SMTP_HOST,
    DOMAIN,
    SECTION_SERVER,
)
from tests.client.fakes import EMAIL, PASSWORD, FakeImapServer, FakeSmtpServer

from .conftest import entry_data

SERVER = {
    "imap_host": "IMAP.gmx.net ",
    "imap_port": 993,
    "imap_security": "ssl",
    "smtp_host": "mail.gmx.net",
    "smtp_port": 587,
    "smtp_security": "starttls",
}


def _user_input(user_id: str, **overrides: Any) -> dict[str, Any]:
    return {CONF_EMAIL: f" {EMAIL} ", CONF_PASSWORD: PASSWORD, CONF_ALLOWED_USER: user_id, SECTION_SERVER: dict(SERVER), **overrides}


async def _start(hass: HomeAssistant) -> dict[str, Any]:
    return dict(await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER}))


def _user_options(result: dict[str, Any]) -> list[str]:
    return [o["value"] for o in result["data_schema"].schema[CONF_ALLOWED_USER].config["options"]]


async def test_flow_creates_entry(
    hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer, smtp_server: FakeSmtpServer
) -> None:
    system_user = await hass.auth.async_create_system_user("system test")
    inactive = MockUser(name="Inactive", is_active=False).add_to_hass(hass)
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert _user_options(result) == [mcp_user.id]  # no system or inactive users
    assert system_user.id not in _user_options(result)
    assert inactive.id not in _user_options(result)

    result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id)))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == EMAIL
    assert result["data"] == {**entry_data(mcp_user.id), CONF_IMAP_HOST: "imap.gmx.net"}
    assert result["options"] == {}  # defaults: read-only tools
    assert result["result"].unique_id == EMAIL
    await hass.async_block_till_done()
    assert result["result"].state is ConfigEntryState.LOADED
    assert imap_server.logins >= 2  # flow check + setup
    assert smtp_server.sessions  # SMTP login was checked


async def test_flow_aborts_without_users(hass: HomeAssistant) -> None:
    result = await _start(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_users"


@pytest.mark.parametrize("email", ["no-at-sign", "a@b@c", "@gmx.net", "user@"])
async def test_invalid_email(hass: HomeAssistant, mcp_user: MockUser, email: str) -> None:
    result = await _start(hass)
    result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id, **{CONF_EMAIL: email})))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_EMAIL: "invalid_email"}


@pytest.fixture
def servers(imap_server: FakeImapServer, smtp_server: FakeSmtpServer) -> tuple[FakeImapServer, FakeSmtpServer]:
    return imap_server, smtp_server


def _imap_login_rejected(imap: FakeImapServer, smtp: FakeSmtpServer) -> None:
    imap.password = "other"


def _smtp_login_rejected(imap: FakeImapServer, smtp: FakeSmtpServer) -> None:
    smtp.password = "other"


def _tls_failure(imap: FakeImapServer, smtp: FakeSmtpServer) -> None:
    imap.connect_error = ssl.SSLCertVerificationError("certificate verify failed")


def _unreachable(imap: FakeImapServer, smtp: FakeSmtpServer) -> None:
    imap.connect_error = OSError("unreachable")


@pytest.mark.parametrize(
    ("breakage", "error"),
    [
        (_imap_login_rejected, "invalid_auth_or_imap_disabled"),
        (_smtp_login_rejected, "smtp_auth_failed"),
        (_tls_failure, "tls_error"),
        (_unreachable, "cannot_connect"),
    ],
)
async def test_validation_errors_then_recovery(
    hass: HomeAssistant,
    mcp_user: MockUser,
    servers: tuple[FakeImapServer, FakeSmtpServer],
    breakage: Callable[[FakeImapServer, FakeSmtpServer], None],
    error: str,
) -> None:
    imap_server, smtp_server = servers
    breakage(imap_server, smtp_server)
    result = await _start(hass)
    result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id)))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert result["data_schema"] is not None

    imap_server.password = smtp_server.password = PASSWORD
    imap_server.connect_error = None
    result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id)))
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_unexpected_error_is_unknown(hass: HomeAssistant, mcp_user: MockUser) -> None:
    result = await _start(hass)
    with patch("custom_components.gmx_mail_mcp.config_flow.check_imap_account", side_effect=RuntimeError("boom")):
        result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id)))
    assert result["errors"] == {"base": "unknown"}


async def test_password_never_in_form_errors_or_logs(
    hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer, caplog: pytest.LogCaptureFixture
) -> None:
    imap_server.password = "other"
    result = await _start(hass)
    result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id)))
    assert PASSWORD not in str(result)
    assert PASSWORD not in caplog.text


async def test_duplicate_account_aborts(hass: HomeAssistant, mcp_user: MockUser) -> None:
    MockConfigEntry(domain=DOMAIN, data=entry_data(mcp_user.id), unique_id=EMAIL).add_to_hass(hass)
    result = await _start(hass)
    result = dict(
        await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id, **{CONF_EMAIL: EMAIL.upper()}))
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_several_accounts(hass: HomeAssistant, mcp_user: MockUser) -> None:
    """D11: a second address becomes a second entry with its own endpoint."""
    for email in (EMAIL, "second@gmx.test"):
        result = await _start(hass)
        result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], _user_input(mcp_user.id, **{CONF_EMAIL: email})))
        assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


# --------------------------------------------------------------------------- reauth


async def test_reauth(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer, smtp_server: FakeSmtpServer) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={**entry_data(mcp_user.id), CONF_PASSWORD: "old"}, unique_id=EMAIL, title=EMAIL)
    entry.add_to_hass(hass)
    result = dict(await entry.start_reauth_flow(hass))
    assert result["step_id"] == "reauth_confirm"
    assert result["description_placeholders"]["account"] == EMAIL

    result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: "still-wrong"}))
    assert result["errors"] == {"base": "invalid_auth_or_imap_disabled"}

    result = dict(await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_PASSWORD: PASSWORD}))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == PASSWORD
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


# --------------------------------------------------------------------------- reconfigure


async def test_reconfigure_user_and_server(hass: HomeAssistant, mcp_user: MockUser, other_admin: MockUser) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(mcp_user.id), unique_id=EMAIL, title=EMAIL)
    entry.add_to_hass(hass)
    result = dict(await entry.start_reconfigure_flow(hass))
    assert result["step_id"] == "reconfigure"
    assert set(_user_options(result)) == {mcp_user.id, other_admin.id}

    new_server = {**SERVER, "smtp_host": " SMTP.example.test"}
    result = dict(
        await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_ALLOWED_USER: other_admin.id, SECTION_SERVER: new_server})
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ALLOWED_USER] == other_admin.id
    assert entry.data[CONF_SMTP_HOST] == "smtp.example.test"
    assert entry.data[CONF_PASSWORD] == PASSWORD  # unchanged
    assert entry.data[CONF_EMAIL] == EMAIL


async def test_reconfigure_validation_error(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(mcp_user.id), unique_id=EMAIL, title=EMAIL)
    entry.add_to_hass(hass)
    result = dict(await entry.start_reconfigure_flow(hass))
    imap_server.connect_error = OSError("unreachable")
    result = dict(
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ALLOWED_USER: mcp_user.id, SECTION_SERVER: {**SERVER, CONF_IMAP_PORT: 1993}}
        )
    )
    assert result["errors"] == {"base": "cannot_connect"}
    assert entry.data[CONF_IMAP_PORT] == 993  # unchanged


async def test_reconfigure_does_not_preselect_removed_user(hass: HomeAssistant, mcp_user: MockUser) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data("deleted-user-id"), unique_id=EMAIL, title=EMAIL)
    entry.add_to_hass(hass)
    result = dict(await entry.start_reconfigure_flow(hass))
    key = next(k for k in result["data_schema"].schema if k == CONF_ALLOWED_USER)
    suggested = (key.description or {}).get("suggested_value")
    assert suggested is None
