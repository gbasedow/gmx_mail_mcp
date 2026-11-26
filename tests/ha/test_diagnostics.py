"""Diagnostics without credentials, addresses or mail content (D63)."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiohttp.test_utils import TestClient
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import CONF_ENABLED_TOOLS
from custom_components.gmx_mail_mcp.diagnostics import REDACTED, async_get_config_entry_diagnostics
from custom_components.gmx_mail_mcp.http import endpoint_path
from tests.client.fakes import PASSWORD, FakeImapServer, plain_message

from .conftest import bearer, rpc

AddEntry = Callable[..., Awaitable[MockConfigEntry]]
SECRET_SUBJECT = "Vertraulicher Betreff 0815"
OPTIONS = {
    CONF_ENABLED_TOOLS: ["gmx_list_folders", "gmx_set_flags", "gmx_send_message"],
    "send_mode": "allowlist",
    "allowlist": ["anna.privat@example.org", "*@firma.de"],
    "sender_name": "Erika Mustermann",
}


async def test_diagnostics_contain_no_credentials_addresses_or_content(
    *,
    hass: HomeAssistant,
    client: TestClient,
    add_entry: AddEntry,
    mcp_user: MockUser,
    make_token: Callable[..., Awaitable[str]],
    imap_server: FakeImapServer,
) -> None:
    imap_server.mailbox("INBOX").messages[1] = plain_message(subject=SECRET_SUBJECT)
    entry = await add_entry(mcp_user, OPTIONS)
    headers = bearer(await make_token(mcp_user))
    arguments = {"refs": [{"folder": "INBOX", "uid": 1, "uidvalidity": 1}], "add": ["flagged"]}
    resp = await client.post(
        endpoint_path(entry.entry_id), json=rpc("tools/call", {"name": "gmx_set_flags", "arguments": arguments}), headers=headers
    )
    assert resp.status == 200
    diagnostics: dict[str, Any] = await async_get_config_entry_diagnostics(hass, entry)
    dump = json.dumps(diagnostics, ensure_ascii=False)
    for secret in (PASSWORD, entry.data["email"], entry.title, mcp_user.id, "anna.privat", "Erika", SECRET_SUBJECT):
        assert secret not in dump, secret
    assert diagnostics["entry"]["email"] == REDACTED and diagnostics["entry"]["imap_host"] == "imap.gmx.net"
    assert diagnostics["options"]["allowlist"] == {"count": 2, "domains": ["example.org", "firma.de"]}
    assert diagnostics["options"]["sender_name"] == {"set": True}
    assert diagnostics["tools"]["enabled"] == ["gmx_list_folders", "gmx_send_message", "gmx_set_flags"]
    assert diagnostics["connection"]["connected"] is True
    assert "IMAP4REV1" in diagnostics["connection"]["server"]["capabilities"]
    latest = diagnostics["audit_latest"][0]
    assert latest["tool"] == "gmx_set_flags" and latest["user"] == REDACTED
    assert diagnostics["audit_stored"] == 1


async def test_diagnostics_without_sender_name_or_allowlist(hass: HomeAssistant, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["options"]["allowlist"] == {"count": 0, "domains": []}
    assert diagnostics["options"]["sender_name"] == {"set": False}
    assert diagnostics["audit_latest"] == [] and diagnostics["counters"]["sent_today"] == 0
