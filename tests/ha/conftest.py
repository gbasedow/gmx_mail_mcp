"""Fixtures for tests with Home Assistant: real HA authentication, test users and long-lived tokens."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Generator
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, User
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import (
    CONF_ALLOWED_USER,
    CONF_EMAIL,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SECURITY,
    CONF_PASSWORD,
    CONF_SMTP_HOST,
    CONF_SMTP_PORT,
    CONF_SMTP_SECURITY,
    DOMAIN,
)
from tests.client.fakes import EMAIL, PASSWORD, FakeImapServer, FakeSmtpServer

MCP_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load the integration from ``custom_components``."""


@pytest.fixture(autouse=True)
def imap_server() -> Generator[FakeImapServer]:
    """In-memory IMAP server for every test: no test can reach a real mail server."""
    server = FakeImapServer()
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", server):
        yield server


@pytest.fixture(autouse=True)
def smtp_server() -> Generator[FakeSmtpServer]:
    """In-memory SMTP server for every test."""
    server = FakeSmtpServer()
    with (
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP", server.factory(implicit_tls=False)),
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP_SSL", server.factory(implicit_tls=True)),
    ):
        yield server


def entry_data(user_id: str, email: str = EMAIL) -> dict[str, Any]:
    """Config entry data as the config flow stores it (GMX defaults)."""
    return {
        CONF_EMAIL: email,
        CONF_PASSWORD: PASSWORD,
        CONF_ALLOWED_USER: user_id,
        CONF_IMAP_HOST: "imap.gmx.net",
        CONF_IMAP_PORT: 993,
        CONF_IMAP_SECURITY: "ssl",
        CONF_SMTP_HOST: "mail.gmx.net",
        CONF_SMTP_PORT: 587,
        CONF_SMTP_SECURITY: "starttls",
    }


@pytest.fixture
async def mcp_user(hass: HomeAssistant) -> MockUser:
    """Dedicated non-admin user for MCP access (D9)."""
    return MockUser(name="MCP client").add_to_hass(hass)


@pytest.fixture
async def other_admin(hass: HomeAssistant) -> MockUser:
    """Another, even more privileged user: must not be admitted either."""
    admin_group = await hass.auth.async_get_group(GROUP_ID_ADMIN)
    return MockUser(name="Admin", groups=[admin_group]).add_to_hass(hass)


@pytest.fixture
def make_token(hass: HomeAssistant) -> Callable[[User], Awaitable[str]]:
    """Create a long-lived access token for a user, as the HA profile page does."""

    async def _make(user: User) -> str:
        refresh_token = await hass.auth.async_create_refresh_token(
            user,
            client_name=f"test token {user.id}",
            token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
            access_token_expiration=timedelta(days=30),
        )
        return hass.auth.async_create_access_token(refresh_token)

    return _make


@pytest.fixture
def add_entry(hass: HomeAssistant) -> Callable[..., Awaitable[MockConfigEntry]]:
    """Add and set up an entry that admits the given user."""

    async def _add(user: User, options: dict[str, Any] | None = None) -> MockConfigEntry:
        email = f"user-{user.id[:8]}@gmx.test"
        entry = MockConfigEntry(domain=DOMAIN, data=entry_data(user.id, email), options=options or {}, title=email, unique_id=email)
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return entry

    return _add


@pytest.fixture
async def client(hass: HomeAssistant, hass_client_no_auth: Callable[[], Awaitable[TestClient]]) -> TestClient:
    """HTTP client without any credentials of its own; each test sets the Authorization header."""
    # The test client freezes the aiohttp router; the integration must register its view first.
    # (A running HA keeps its router unfrozen so integrations set up later work; checked end to end, see docs/M0-SPIKE.md.)
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "auth", {})  # .well-known OAuth documents, as in a real HA
    assert await async_setup_component(hass, DOMAIN, {})
    return await hass_client_no_auth()


def rpc(method: str, params: dict[str, Any] | None = None, request_id: int | str = 1) -> dict[str, Any]:
    """Build a JSON-RPC request."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def bearer(token: str) -> dict[str, str]:
    """MCP headers with an Authorization header."""
    return {**MCP_HEADERS, "Authorization": f"Bearer {token}"}
