"""MCP endpoint inside Home Assistant: authentication (HA token), user check (D9), transport rules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient
from homeassistant.auth.models import User
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import DOMAIN
from custom_components.gmx_mail_mcp.http import endpoint_path
from custom_components.gmx_mail_mcp.mcp.protocol import LATEST_PROTOCOL_VERSION

from .conftest import MCP_HEADERS, bearer, rpc

type MakeToken = Callable[[User], Awaitable[str]]
type AddEntry = Callable[[User], Awaitable[MockConfigEntry]]


@pytest.fixture
async def entry(add_entry: AddEntry, mcp_user: MockUser) -> MockConfigEntry:
    return await add_entry(mcp_user)


@pytest.fixture
async def token(make_token: MakeToken, mcp_user: MockUser) -> str:
    return await make_token(mcp_user)


@pytest.fixture
async def foreign_token(make_token: MakeToken, other_admin: MockUser) -> str:
    return await make_token(other_admin)


async def _post(client: TestClient, entry_id: str, body: Any, headers: dict[str, str]) -> Any:
    return await client.post(endpoint_path(entry_id), json=body, headers=headers)


# --------------------------------------------------------------------------- happy path


async def test_initialize_list_and_call_with_long_lived_token(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await _post(
        client, entry.entry_id, rpc("initialize", {"protocolVersion": LATEST_PROTOCOL_VERSION, "capabilities": {}}), bearer(token)
    )
    assert resp.status == 200
    assert resp.content_type == "application/json"
    body = await resp.json()
    assert body["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert body["result"]["serverInfo"]["name"] == "gmx_mail_mcp"

    headers = {**bearer(token), "MCP-Protocol-Version": LATEST_PROTOCOL_VERSION}
    resp = await _post(client, entry.entry_id, {"jsonrpc": "2.0", "method": "notifications/initialized"}, headers)
    assert resp.status == 202
    assert await resp.read() == b""

    resp = await _post(client, entry.entry_id, rpc("tools/list", request_id=2), headers)
    assert [t["name"] for t in (await resp.json())["result"]["tools"]] == ["gmx_spike_status"]

    resp = await _post(client, entry.entry_id, rpc("tools/call", {"name": "gmx_spike_status", "arguments": {}}, 3), headers)
    result = (await resp.json())["result"]
    assert result["isError"] is False
    assert "transport and authentication OK" in result["content"][0]["text"]


async def test_ping(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await _post(client, entry.entry_id, rpc("ping", request_id="p"), bearer(token))
    assert await resp.json() == {"jsonrpc": "2.0", "id": "p", "result": {}}


# --------------------------------------------------------------------------- authentication (HA) and user check (D9)


@pytest.mark.parametrize(
    "headers",
    [
        MCP_HEADERS,
        bearer("not-a-token"),
        bearer(""),
        {**MCP_HEADERS, "Authorization": "Basic dXNlcjpwYXNz"},
        {**MCP_HEADERS, "Authorization": "Bearer"},
    ],
    ids=["missing", "invalid", "empty", "basic", "no-value"],
)
async def test_unauthenticated_is_401(client: TestClient, entry: MockConfigEntry, headers: dict[str, str]) -> None:
    resp = await _post(client, entry.entry_id, rpc("ping"), headers)
    assert resp.status == 401


async def test_token_in_query_is_not_accepted(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await client.post(f"{endpoint_path(entry.entry_id)}?access_token={token}", json=rpc("ping"), headers=MCP_HEADERS)
    assert resp.status == 401


class _ControlView(HomeAssistantView):
    """GET and POST behind HA authentication, to show that a signed path works for GET only."""

    url = "/api/gmx_mail_mcp_test/control"
    name = "api:gmx_mail_mcp_test:control"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def post(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")


async def test_signed_path_does_not_authenticate_post(
    hass: HomeAssistant, hass_client_no_auth: Callable[[], Awaitable[TestClient]], add_entry: AddEntry, mcp_user: MockUser
) -> None:
    """HA accepts signed paths (authSig) for GET/HEAD only; POST to the MCP endpoint still needs the bearer token."""
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, DOMAIN, {})
    hass.http.register_view(_ControlView())
    client = await hass_client_no_auth()
    entry = await add_entry(mcp_user)
    refresh_token = await hass.auth.async_create_refresh_token(mcp_user, client_id="https://client.example/")

    def sign(path: str) -> str:
        return async_sign_path(hass, path, timedelta(minutes=5), refresh_token_id=refresh_token.id)

    # Control: the signature is valid for GET, and the same signature does not authenticate POST.
    assert (await client.get(sign(_ControlView.url))).status == 200
    assert (await client.post(sign(_ControlView.url))).status == 401
    # The MCP endpoint (POST only).
    resp = await client.post(sign(endpoint_path(entry.entry_id)), json=rpc("ping"), headers=MCP_HEADERS)
    assert resp.status == 401


async def test_token_of_other_user_is_403(client: TestClient, entry: MockConfigEntry, other_admin: MockUser, make_token: MakeToken) -> None:
    resp = await _post(client, entry.entry_id, rpc("ping"), bearer(await make_token(other_admin)))
    assert resp.status == 403
    assert "result" not in await resp.text()


async def test_system_user_is_403(hass: HomeAssistant, client: TestClient, add_entry: AddEntry) -> None:
    """Even if an entry were bound to a system user (e.g. Supervisor), it is never admitted."""
    system_user = await hass.auth.async_create_system_user("system test")
    entry = await add_entry(system_user)
    refresh_token = await hass.auth.async_create_refresh_token(system_user)
    resp = await _post(client, entry.entry_id, rpc("ping"), bearer(hass.auth.async_create_access_token(refresh_token)))
    assert resp.status == 403


async def test_deactivated_user_is_rejected(
    hass: HomeAssistant, client: TestClient, entry: MockConfigEntry, mcp_user: MockUser, token: str
) -> None:
    await hass.auth.async_deactivate_user(mcp_user)
    resp = await _post(client, entry.entry_id, rpc("ping"), bearer(token))
    assert resp.status in (401, 403)


async def test_revoked_token_is_401(hass: HomeAssistant, client: TestClient, entry: MockConfigEntry, token: str) -> None:
    refresh_token = hass.auth.async_validate_access_token(token)
    assert refresh_token is not None
    hass.auth.async_remove_refresh_token(refresh_token)
    resp = await _post(client, entry.entry_id, rpc("ping"), bearer(token))
    assert resp.status == 401


async def test_each_entry_admits_only_its_user(
    client: TestClient, add_entry: AddEntry, make_token: MakeToken, mcp_user: MockUser, other_admin: MockUser
) -> None:
    """D11: several entries, each with its own endpoint and allowed user."""
    entry_a = await add_entry(mcp_user)
    entry_b = await add_entry(other_admin)
    token_a, token_b = await make_token(mcp_user), await make_token(other_admin)
    assert (await _post(client, entry_a.entry_id, rpc("ping"), bearer(token_a))).status == 200
    assert (await _post(client, entry_b.entry_id, rpc("ping"), bearer(token_b))).status == 200
    assert (await _post(client, entry_a.entry_id, rpc("ping"), bearer(token_b))).status == 403
    assert (await _post(client, entry_b.entry_id, rpc("ping"), bearer(token_a))).status == 403


# --------------------------------------------------------------------------- endpoint lookup


async def test_unknown_entry_is_404(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await _post(client, "does-not-exist", rpc("ping"), bearer(token))
    assert resp.status == 404


async def test_entry_of_other_domain_is_404(hass: HomeAssistant, client: TestClient, entry: MockConfigEntry, token: str) -> None:
    foreign = MockConfigEntry(domain="other_domain", data={})
    foreign.add_to_hass(hass)
    resp = await _post(client, foreign.entry_id, rpc("ping"), bearer(token))
    assert resp.status == 404


async def test_unloaded_entry_is_404(hass: HomeAssistant, client: TestClient, entry: MockConfigEntry, token: str) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    resp = await _post(client, entry.entry_id, rpc("ping"), bearer(token))
    assert resp.status == 404


async def test_unauthenticated_unknown_entry_is_401_not_404(client: TestClient, entry: MockConfigEntry) -> None:
    """Without a token nothing is revealed about which entries exist."""
    resp = await _post(client, "does-not-exist", rpc("ping"), MCP_HEADERS)
    assert resp.status == 401


# --------------------------------------------------------------------------- transport rules


@pytest.mark.parametrize("method", ["get", "delete", "put"])
async def test_other_methods_are_405(client: TestClient, entry: MockConfigEntry, token: str, method: str) -> None:
    resp = await client.request(method.upper(), endpoint_path(entry.entry_id), headers=bearer(token))
    assert resp.status == 405


async def test_invalid_json_is_400_parse_error(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await client.post(endpoint_path(entry.entry_id), data=b"{not json", headers=bearer(token))
    assert resp.status == 400
    body = await resp.json()
    assert body["error"]["code"] == -32700
    assert body["id"] is None


async def test_invalid_utf8_is_400(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await client.post(endpoint_path(entry.entry_id), data=b'{"a": "\xff"}', headers=bearer(token))
    assert resp.status == 400


async def test_batch_is_400(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await _post(client, entry.entry_id, [rpc("ping")], bearer(token))
    assert resp.status == 400
    assert (await resp.json())["error"]["code"] == -32600


async def test_unknown_method_is_200_with_jsonrpc_error(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await _post(client, entry.entry_id, rpc("resources/list"), bearer(token))
    assert resp.status == 200
    assert (await resp.json())["error"]["code"] == -32601


async def test_wrong_content_type_is_415(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    headers = {**bearer(token), "Content-Type": "text/plain"}
    resp = await client.post(endpoint_path(entry.entry_id), data=b'{"jsonrpc":"2.0","id":1,"method":"ping"}', headers=headers)
    assert resp.status == 415


@pytest.mark.parametrize(("accept", "status"), [("text/html", 406), ("text/event-stream", 406), ("*/*", 200), ("application/json", 200)])
async def test_accept_header(client: TestClient, entry: MockConfigEntry, token: str, accept: str, status: int) -> None:
    resp = await _post(client, entry.entry_id, rpc("ping"), {**bearer(token), "Accept": accept})
    assert resp.status == status


@pytest.mark.parametrize(("version", "status"), [("2025-06-18", 200), ("2025-03-26", 200), ("2024-11-05", 400), ("garbage", 400)])
async def test_protocol_version_header(client: TestClient, entry: MockConfigEntry, token: str, version: str, status: int) -> None:
    resp = await _post(client, entry.entry_id, rpc("ping"), {**bearer(token), "MCP-Protocol-Version": version})
    assert resp.status == status


async def test_foreign_origin_is_403(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    resp = await _post(client, entry.entry_id, rpc("ping"), {**bearer(token), "Origin": "https://evil.example"})
    assert resp.status == 403


async def test_same_origin_is_allowed(client: TestClient, entry: MockConfigEntry, token: str) -> None:
    origin = f"http://{client.server.host}:{client.server.port}"
    resp = await _post(client, entry.entry_id, rpc("ping"), {**bearer(token), "Origin": origin})
    assert resp.status == 200


async def test_token_never_logged(
    client: TestClient, entry: MockConfigEntry, token: str, foreign_token: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    foreign = foreign_token
    await _post(client, entry.entry_id, rpc("tools/call", {"name": "gmx_spike_status"}), bearer(token))
    await _post(client, entry.entry_id, rpc("ping"), bearer(foreign))
    assert token not in caplog.text
    assert foreign not in caplog.text


async def test_401_points_to_ha_oauth_metadata(client: TestClient, entry: MockConfigEntry) -> None:
    """HA adds an RFC 9728 challenge to the 401; OAuth clients such as claude.ai discover HA itself as authorization server."""
    resp = await _post(client, entry.entry_id, rpc("ping"), MCP_HEADERS)
    assert resp.status == 401
    challenge = resp.headers.get("WWW-Authenticate", "")
    assert challenge.startswith("Bearer resource_metadata=")
    assert challenge.endswith('/.well-known/oauth-protected-resource"')
    metadata = await client.get("/.well-known/oauth-protected-resource")
    assert metadata.status == 200
    authorization_server = (await metadata.json())["authorization_servers"][0]
    oauth = await (await client.get("/.well-known/oauth-authorization-server")).json()
    assert oauth["authorization_endpoint"] == f"{authorization_server}/auth/authorize"
    assert oauth["client_id_metadata_document_supported"] is True
