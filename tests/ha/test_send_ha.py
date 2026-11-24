"""M5 in Home Assistant: sending over the endpoint, the send counter in .storage, the sending options, SMTP login failures."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import CONF_ENABLED_TOOLS, DOMAIN
from custom_components.gmx_mail_mcp.http import endpoint_path
from tests.client.fakes import FakeImapServer, FakeSmtpServer

from .conftest import bearer, rpc

AddEntry = Callable[..., Awaitable[MockConfigEntry]]
SEND_OPTIONS = {CONF_ENABLED_TOOLS: ["gmx_send_message", "gmx_get_audit_log"], "send_mode": "allowlist", "allowlist": ["anna@example.org"]}


async def _call(client: TestClient, entry: MockConfigEntry, headers: dict[str, str], name: str, arguments: dict[str, Any]) -> Any:
    resp = await client.post(endpoint_path(entry.entry_id), json=rpc("tools/call", {"name": name, "arguments": arguments}), headers=headers)
    return (await resp.json())["result"]


async def test_send_over_the_endpoint_counts_and_persists(
    *,
    hass: HomeAssistant,
    client: TestClient,
    add_entry: AddEntry,
    mcp_user: MockUser,
    make_token: Callable[..., Awaitable[str]],
    hass_storage: dict[str, Any],
    imap_server: FakeImapServer,
    smtp_server: FakeSmtpServer,
) -> None:
    imap_server.mailbox("Gesendet", 901)
    entry = await add_entry(mcp_user, SEND_OPTIONS)
    headers = bearer(await make_token(mcp_user))
    args = {"to": ["anna@example.org"], "subject": "Test", "body_text": "Hallo"}
    preview = await _call(client, entry, headers, "gmx_send_message", args)
    assert preview["isError"] is False and smtp_server.delivered == []
    token = json.loads(preview["content"][0]["text"])["data"]["confirm_token"]
    sent = await _call(client, entry, headers, "gmx_send_message", {**args, "confirm_token": token})
    assert sent["isError"] is False, sent
    assert len(smtp_server.delivered) == 1
    assert len(imap_server.mailboxes["Gesendet"].messages) == 1

    entries = entry.runtime_data.ledgers.audit.latest(2)
    assert [(e["outcome"], e["user"], e.get("recipient_domains")) for e in entries] == [
        ("ok", mcp_user.id, ["example.org"]),
        ("preview", mcp_user.id, ["example.org"]),
    ]
    assert entry.runtime_data.ledgers.sends.sent_today == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    key = f"{DOMAIN}.{entry.entry_id}.sends"
    assert hass_storage[key]["data"]["sent"] == 1
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.ledgers.sends.sent_today == 1  # survives the reload

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage


async def test_unload_writes_the_send_counter_at_once(
    hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser, hass_storage: dict[str, Any]
) -> None:
    entry = await add_entry(mcp_user)
    sends = entry.runtime_data.ledgers.sends
    sends.on_change = lambda: None  # no delayed save: only the flush on unload can write it
    sends.add(3)
    assert await hass.config_entries.async_unload(entry.entry_id)
    data = hass_storage[f"{DOMAIN}.{entry.entry_id}.sends"]["data"]
    assert data["sent"] == 1 and [count for _stamp, count in data["recipients"]] == [3]


async def test_rejected_smtp_login_raises_an_issue_and_asks_for_a_new_password(
    *,
    hass: HomeAssistant,
    client: TestClient,
    add_entry: AddEntry,
    mcp_user: MockUser,
    make_token: Callable[..., Awaitable[str]],
    smtp_server: FakeSmtpServer,
) -> None:
    entry = await add_entry(mcp_user, {**SEND_OPTIONS, "tools_without_confirmation": ["gmx_send_message"]})
    headers = bearer(await make_token(mcp_user))
    smtp_server.password = "revoked"
    result = await _call(client, entry, headers, "gmx_send_message", {"to": ["anna@example.org"]})
    assert result["isError"] is True and '"smtp_auth_failed"' in result["content"][0]["text"]
    await hass.async_block_till_done()
    issue_id = f"smtp_auth_failed_{entry.entry_id}"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert [f["context"]["source"] for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN)] == ["reauth"]
    # A new setup (after re-authentication) removes the issue.
    smtp_server.password = "app-password-for-tests"
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def _open(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return dict(await hass.config_entries.options.async_init(entry.entry_id))


async def test_sending_options_are_normalised_and_saved(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user)
    result = await _open(hass, entry)
    keys = {str(k) for k in result["data_schema"].schema}
    assert {
        "send_mode",
        "allowlist",
        "allow_reply_to_sender",
        "sender_name",
        "sent_copy_mode",
        "max_recipients",
        "max_send_per_day",
        "max_recipients_per_hour",
        "max_message_size_mb",
    } <= keys
    mode = next(k for k in result["data_schema"].schema if k == "send_mode")
    assert result["data_schema"].schema[mode].config["options"] == ["allowlist", "any"]  # no "off" (D46)
    result = dict(
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                CONF_ENABLED_TOOLS: ["gmx_send_message"],
                "send_mode": "any",
                "allowlist": " Anna@Example.org\n*@Firma.de, anna@example.org ",
                "allow_reply_to_sender": True,
                "sender_name": "  Haus   Assistent ",
                "sent_copy_mode": "auto",
                "max_recipients": 3,
                "max_send_per_day": 0,
                "max_recipients_per_hour": 0,
                "max_message_size_mb": 50,
            },
        )
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    options = entry.options
    assert options["send_mode"] == "any"
    assert options["allowlist"] == ["anna@example.org", "*@firma.de"]
    assert options["allow_reply_to_sender"] is True and options["sender_name"] == "Haus Assistent"
    assert options["sent_copy_mode"] == "auto"
    assert (options["max_recipients"], options["max_send_per_day"], options["max_recipients_per_hour"], options["max_message_size_mb"]) == (
        3,
        0,
        0,
        50,
    )
    # The stored list comes back one entry per line.
    again = await _open(hass, entry)
    key = next(k for k in again["data_schema"].schema if k == "allowlist")
    assert key.description["suggested_value"] == "anna@example.org\n*@firma.de"


async def test_invalid_sending_options_are_shown_and_not_saved(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user, {"send_mode": "allowlist", "allowlist": ["anna@example.org"]})
    result = await _open(hass, entry)
    result = dict(
        await hass.config_entries.options.async_configure(
            result["flow_id"], {"send_mode": "allowlist", "allowlist": "*\nanna@example.org", "sender_name": 'Evil" <x@evil.test>'}
        )
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"allowlist": "wildcard_not_allowed", "sender_name": "invalid_sender_name"}
    assert entry.options["allowlist"] == ["anna@example.org"]  # unchanged
    result = dict(await hass.config_entries.options.async_configure(result["flow_id"], {"allowlist": "kein-eintrag"}))
    assert result["errors"] == {"allowlist": "invalid_allowlist_entry"}
    result = dict(await hass.config_entries.options.async_configure(result["flow_id"], {"allowlist": "b@example.org"}))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["allowlist"] == ["b@example.org"]


async def test_stored_off_mode_is_shown_as_allowlist(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user, {"send_mode": "off"})
    result = await _open(hass, entry)
    key = next(k for k in result["data_schema"].schema if k == "send_mode")
    assert key.description["suggested_value"] == "allowlist"
