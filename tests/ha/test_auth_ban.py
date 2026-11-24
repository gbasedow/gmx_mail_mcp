"""D25: which 401s count towards Home Assistant's IP ban, with a real ban threshold."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import jwt
import pytest
from aiohttp.test_utils import TestClient
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components.http.ban import KEY_FAILED_LOGIN_ATTEMPTS
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, MockUser

from custom_components.gmx_mail_mcp.const import DOMAIN
from custom_components.gmx_mail_mcp.http import endpoint_path

from .conftest import MCP_HEADERS, bearer, rpc

THRESHOLD = 3


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Own config dir per test: HA persists bans in ip_bans.yaml there (the shared test dir would leak them)."""
    return hass_tmp_config_dir


@pytest.fixture
async def ban_client(hass: HomeAssistant, hass_client_no_auth: Callable[[], Awaitable[TestClient]]) -> TestClient:
    """HTTP client against an HA with the IP ban active (login_attempts_threshold set, as on the user's instance)."""
    assert await async_setup_component(hass, "http", {"http": {"login_attempts_threshold": THRESHOLD}})
    assert await async_setup_component(hass, "auth", {})
    assert await async_setup_component(hass, DOMAIN, {})
    return await hass_client_no_auth()


@pytest.fixture
async def entry(add_entry: Callable[..., Awaitable[MockConfigEntry]], mcp_user: MockUser) -> MockConfigEntry:
    return await add_entry(mcp_user)


@pytest.fixture
async def good_token(hass: HomeAssistant, mcp_user: MockUser) -> str:
    return await _token(hass, mcp_user)


def _failed(hass: HomeAssistant) -> int:
    return sum(hass.http.app[KEY_FAILED_LOGIN_ATTEMPTS].values())


async def _ping(client: TestClient, entry: MockConfigEntry, headers: dict[str, str]) -> Any:
    return await client.post(endpoint_path(entry.entry_id), json=rpc("ping"), headers=headers)


async def _token(hass: HomeAssistant, user: MockUser, lifetime: timedelta = timedelta(minutes=30)) -> str:
    """A genuine HA access token; a negative ``lifetime`` gives one that expired that long ago (no clock jump needed:
    advancing the clock would fire HA's own timers)."""
    refresh_token = await hass.auth.async_create_refresh_token(
        user, client_name=f"test {lifetime}", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, access_token_expiration=lifetime
    )
    return hass.auth.async_create_access_token(refresh_token)


EXPIRED = timedelta(minutes=-5)  # beyond HA's 10 s leeway


async def test_missing_token_is_never_counted(hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry) -> None:
    """The sign-in challenge of an OAuth client (no token yet) must not lead to a ban."""
    for _ in range(THRESHOLD * 3):
        resp = await _ping(ban_client, entry, MCP_HEADERS)
        assert resp.status == 401
        assert resp.headers["WWW-Authenticate"].endswith('/.well-known/oauth-protected-resource"')
    assert _failed(hass) == 0
    assert (await _ping(ban_client, entry, MCP_HEADERS)).status == 401  # still 401, not 403 (banned)


async def test_expired_genuine_token_is_not_counted(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, mcp_user: MockUser
) -> None:
    """A client whose 30-min access token ran out must refresh it, not get banned."""
    assert (await _ping(ban_client, entry, bearer(await _token(hass, mcp_user)))).status == 200
    expired = await _token(hass, mcp_user, EXPIRED)
    for _ in range(THRESHOLD * 3):
        resp = await _ping(ban_client, entry, bearer(expired))
        assert resp.status == 401
        assert "resource_metadata" in resp.headers["WWW-Authenticate"]
    assert _failed(hass) == 0


@pytest.mark.parametrize(
    "authorization",
    ["Bearer not-a-token", "Bearer", "Basic dXNlcjpwYXNz", "bearer lower-case-scheme"],
    ids=["garbage", "no-value", "basic", "lower-case"],
)
async def test_invalid_credentials_are_counted_and_banned(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, good_token: str, authorization: str
) -> None:
    for attempt in range(1, THRESHOLD + 1):
        resp = await _ping(ban_client, entry, {**MCP_HEADERS, "Authorization": authorization})
        assert resp.status == 401
        assert _failed(hass) == attempt
    # Banned by HA's middleware: even a valid token is refused now.
    assert (await _ping(ban_client, entry, bearer(good_token))).status == 403


async def test_forged_token_with_real_issuer_is_counted(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, mcp_user: MockUser
) -> None:
    """A token naming an existing refresh token but signed with another key is a forgery, expired or not."""
    genuine = await _token(hass, mcp_user)
    claims = jwt.decode(genuine, options={"verify_signature": False})
    for exp_offset in (-3600, 3600):
        forged = jwt.encode({**claims, "exp": claims["iat"] + exp_offset}, "w" * 64, algorithm="HS256")
        assert (await _ping(ban_client, entry, bearer(forged))).status == 401
    assert _failed(hass) == 2


async def test_revoked_and_deactivated_are_counted(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, mcp_user: MockUser, other_admin: MockUser
) -> None:
    revoked = await _token(hass, mcp_user)
    refresh_token = hass.auth.async_validate_access_token(revoked)
    assert refresh_token is not None
    hass.auth.async_remove_refresh_token(refresh_token)
    assert (await _ping(ban_client, entry, bearer(revoked))).status == 401
    assert _failed(hass) == 1

    # Expired token of a user who was deactivated meanwhile: not "genuine but expired" any more.
    stale = await _token(hass, other_admin, EXPIRED)
    await hass.auth.async_deactivate_user(other_admin)
    assert (await _ping(ban_client, entry, bearer(stale))).status == 401
    assert _failed(hass) == 2

    # Expired token whose refresh token was revoked: counted as well.
    revoked_expired = await _token(hass, mcp_user, EXPIRED)
    claims = jwt.decode(revoked_expired, options={"verify_signature": False})
    gone = hass.auth.async_get_refresh_token(claims["iss"])
    assert gone is not None
    hass.auth.async_remove_refresh_token(gone)
    assert (await _ping(ban_client, entry, bearer(revoked_expired))).status == 401
    assert _failed(hass) == 3


@pytest.mark.parametrize("scheme", ["bearer", "Basic", "Token", "BEARER"])
async def test_valid_token_with_wrong_scheme_is_counted(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, mcp_user: MockUser, scheme: str
) -> None:
    """Like HA: only the exact scheme "Bearer" is accepted, even with a valid token."""
    token = await _token(hass, mcp_user)
    resp = await _ping(ban_client, entry, {**MCP_HEADERS, "Authorization": f"{scheme} {token}"})
    assert resp.status == 401
    assert _failed(hass) == 1


async def test_expired_token_of_inactive_user_is_counted(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, other_admin: MockUser
) -> None:
    """Refresh token still present but the user inactive: no longer a legitimate client."""
    stale = await _token(hass, other_admin, EXPIRED)
    other_admin.is_active = False  # without async_deactivate_user, which would also delete the refresh tokens
    assert (await _ping(ban_client, entry, bearer(stale))).status == 401
    assert _failed(hass) == 1


async def test_local_only_user_from_outside_is_counted(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, mcp_user: MockUser, make_token: Callable[..., Awaitable[str]]
) -> None:
    token = await make_token(mcp_user)
    with patch("custom_components.gmx_mail_mcp.auth.async_user_not_allowed_do_auth", return_value="User cannot authenticate remotely"):
        assert (await _ping(ban_client, entry, bearer(token))).status == 401
    assert _failed(hass) == 1


async def test_valid_token_of_other_user_is_403_not_counted(
    hass: HomeAssistant, ban_client: TestClient, entry: MockConfigEntry, other_admin: MockUser, make_token: Callable[..., Awaitable[str]]
) -> None:
    """Authenticated but not allowed (D9): 403, which HA does not count either."""
    token = await make_token(other_admin)
    for _ in range(THRESHOLD + 1):
        assert (await _ping(ban_client, entry, bearer(token))).status == 403
    assert _failed(hass) == 0
