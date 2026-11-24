"""Entry life cycle: login at setup, error paths, reauth at runtime, idle timer, unload."""

from __future__ import annotations

import ssl
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser, async_fire_time_changed

from custom_components.gmx_mail_mcp.client.errors import GmxAuthError
from custom_components.gmx_mail_mcp.const import DOMAIN, IDLE_CHECK_INTERVAL_S, ISSUE_IMAP_POSSIBLY_DISABLED
from tests.client.fakes import EMAIL, FakeImapServer

from .conftest import entry_data


async def _setup(hass: HomeAssistant, user: MockUser) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data(user.id), unique_id=EMAIL, title=EMAIL)
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _issue(hass: HomeAssistant, entry: MockConfigEntry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_IMAP_POSSIBLY_DISABLED}_{entry.entry_id}")


def _reauth_flows(hass: HomeAssistant) -> list[object]:
    return [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == SOURCE_REAUTH]


async def test_setup_and_unload(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer) -> None:
    entry = await _setup(hass, mcp_user)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.worker.connected
    assert entry.runtime_data.allowed_user_id == mcp_user.id
    assert len(imap_server.open_clients) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert imap_server.open_clients == []


async def test_auth_failure_at_setup_starts_reauth(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer) -> None:
    imap_server.password = "changed-at-gmx"
    entry = await _setup(hass, mcp_user)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(_reauth_flows(hass)) == 1
    issue = _issue(hass, entry)
    assert issue is not None
    assert issue.translation_placeholders == {"account": EMAIL}
    assert imap_server.open_clients == []


async def test_connection_failures_retry(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer) -> None:
    imap_server.connect_error = OSError("unreachable")
    entry = await _setup(hass, mcp_user)
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _reauth_flows(hass) == []


async def test_tls_failure_retries(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer) -> None:
    imap_server.connect_error = ssl.SSLCertVerificationError("certificate verify failed")
    entry = await _setup(hass, mcp_user)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_successful_setup_removes_stale_issue(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer) -> None:
    imap_server.password = "changed-at-gmx"
    entry = await _setup(hass, mcp_user)
    assert _issue(hass, entry) is not None
    imap_server.password = entry.data["password"]
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert _issue(hass, entry) is None


async def test_auth_failure_at_runtime_starts_reauth(hass: HomeAssistant, mcp_user: MockUser, imap_server: FakeImapServer) -> None:
    """GMX switches IMAP off later (GMX-NOTES row 6): the next login fails -> issue and reauth."""
    entry = await _setup(hass, mcp_user)
    worker = entry.runtime_data.worker
    await worker._drop()  # connection lost, e.g. GMX idle timeout
    imap_server.password = "changed-at-gmx"
    with pytest.raises(GmxAuthError):
        await worker.async_connect()
    await hass.async_block_till_done()
    assert _issue(hass, entry) is not None
    assert len(_reauth_flows(hass)) == 1


async def test_idle_timer_closes_unused_connection(hass: HomeAssistant, mcp_user: MockUser) -> None:
    entry = await _setup(hass, mcp_user)
    with patch.object(entry.runtime_data.worker, "async_close_if_idle", AsyncMock()) as close_if_idle:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=IDLE_CHECK_INTERVAL_S + 1))
        await hass.async_block_till_done()
    close_if_idle.assert_awaited()


async def test_timer_stops_after_unload(hass: HomeAssistant, mcp_user: MockUser) -> None:
    entry = await _setup(hass, mcp_user)
    worker = entry.runtime_data.worker
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(worker, "async_close_if_idle", AsyncMock()) as close_if_idle:
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=IDLE_CHECK_INTERVAL_S + 1))
        await hass.async_block_till_done()
    close_if_idle.assert_not_awaited()
