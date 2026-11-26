"""Request size (D65): HA accepts 16 MiB; the admitted user may send files up to the upload limit as base64."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp.test_utils import TestClient
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import CONF_ENABLED_TOOLS
from custom_components.gmx_mail_mcp.http import HA_MAX_REQUEST_BYTES, endpoint_path, max_request_bytes
from tests.client.fakes import FakeImapServer

from .conftest import bearer, rpc

AddEntry = Callable[..., Awaitable[MockConfigEntry]]
MB = 1024 * 1024


def _draft_call(size_mb: int) -> dict[str, Any]:
    data = os.urandom(size_mb * MB)
    attachment = {"filename": "gross.bin", "content_base64": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}
    return rpc("tools/call", {"name": "gmx_save_draft", "arguments": {"subject": "Gross", "attachments": [attachment]}})


@pytest.mark.parametrize(
    ("upload_mb", "expected"), [(10, HA_MAX_REQUEST_BYTES), (20, 4 * (20 * MB // 3 + 1) + 2 * MB), ("x", HA_MAX_REQUEST_BYTES)]
)
def test_max_request_bytes_follows_the_upload_limit(upload_mb: Any, expected: int) -> None:
    assert max_request_bytes(upload_mb) == expected
    assert max_request_bytes(500) == max_request_bytes(20)  # never above the largest upload limit


async def test_a_13_mb_file_reaches_the_tool_with_upload_limit_20(
    client: TestClient,
    add_entry: AddEntry,
    mcp_user: MockUser,
    make_token: Callable[..., Awaitable[str]],
    imap_server: FakeImapServer,
) -> None:
    imap_server.mailbox("Entwürfe", 900)
    entry = await add_entry(mcp_user, {CONF_ENABLED_TOOLS: ["gmx_save_draft"], "max_upload_mb": 20})
    resp = await client.post(endpoint_path(entry.entry_id), json=_draft_call(13), headers=bearer(await make_token(mcp_user)))
    assert resp.status == 200, await resp.text()
    result = (await resp.json())["result"]
    assert result["isError"] is False, result
    assert json.loads(result["content"][0]["text"])["data"]["attachments"][0]["size"] == 13 * MB


async def test_the_default_upload_limit_keeps_ha_s_request_limit(
    client: TestClient, add_entry: AddEntry, mcp_user: MockUser, make_token: Callable[..., Awaitable[str]]
) -> None:
    entry = await add_entry(mcp_user, {CONF_ENABLED_TOOLS: ["gmx_save_draft"]})
    resp = await client.post(endpoint_path(entry.entry_id), json=_draft_call(13), headers=bearer(await make_token(mcp_user)))
    assert resp.status == 413


async def test_without_a_token_a_large_body_is_never_read(client: TestClient, add_entry: AddEntry, mcp_user: MockUser) -> None:
    entry = await add_entry(mcp_user, {CONF_ENABLED_TOOLS: ["gmx_save_draft"], "max_upload_mb": 20})
    resp = await client.post(endpoint_path(entry.entry_id), json=_draft_call(13))
    assert resp.status == 401
