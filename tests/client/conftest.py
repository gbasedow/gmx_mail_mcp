"""Fixtures for the client-layer tests (in-memory fakes, no Home Assistant)."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.gmx_mail_mcp.const import (
    CONF_EMAIL,
    CONF_IMAP_HOST,
    CONF_IMAP_PORT,
    CONF_IMAP_SECURITY,
    CONF_PASSWORD,
    CONF_SMTP_HOST,
    CONF_SMTP_PORT,
    CONF_SMTP_SECURITY,
)

from .fakes import EMAIL, PASSWORD, FakeIdleServer, FakeImapServer, FakeSmtpServer

ENTRY_DATA: dict[str, Any] = {
    CONF_EMAIL: EMAIL,
    CONF_PASSWORD: PASSWORD,
    CONF_IMAP_HOST: "imap.gmx.net",
    CONF_IMAP_PORT: 993,
    CONF_IMAP_SECURITY: "ssl",
    CONF_SMTP_HOST: "mail.gmx.net",
    CONF_SMTP_PORT: 587,
    CONF_SMTP_SECURITY: "starttls",
}


@pytest.fixture(autouse=True)
def idle_server() -> Generator[FakeIdleServer]:
    """Replace the aioimaplib client of the IDLE watcher in every test (never a real connection)."""
    server = FakeIdleServer()
    with patch("custom_components.gmx_mail_mcp.client.idle.create_idle_client", server):
        yield server


@pytest.fixture
def imap_server() -> Generator[FakeImapServer]:
    """Replace imapclient.IMAPClient by an in-memory fake."""
    server = FakeImapServer()
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", server):
        yield server


@pytest.fixture
def smtp_server() -> Generator[FakeSmtpServer]:
    """Replace smtplib.SMTP / SMTP_SSL (as used by the integration) by in-memory fakes."""
    server = FakeSmtpServer()
    with (
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP", server.factory(implicit_tls=False)),
        patch("custom_components.gmx_mail_mcp.client.smtp.smtplib.SMTP_SSL", server.factory(implicit_tls=True)),
    ):
        yield server
