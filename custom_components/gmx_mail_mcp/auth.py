"""Bearer authentication of the MCP endpoint (D25).

The view does not use ``requires_auth``: Home Assistant's middleware would count *every* 401 as a failed login, including
the sign-in challenge an OAuth client (claude.ai) receives before it has a token and requests with an access token that
merely expired (HA access tokens live 30 minutes). With ``login_attempts_threshold`` set, that bans the client's IP after a
few reconnects - for all of Home Assistant, ha-mcp included.

The token itself is still checked by Home Assistant: :meth:`AuthManager.async_validate_access_token` (the function the HTTP
middleware uses) and :func:`async_user_not_allowed_do_auth` (local-only users). Only the bookkeeping differs:

* no ``Authorization`` header             -> 401 with the RFC 9728 challenge, not counted (no credential was presented);
* genuine HA access token, only expired   -> 401 with challenge, not counted (signature valid for an existing refresh token);
* anything else that fails                -> 401, counted with HA's ``process_wrong_login`` (the IP ban works as before).

Banned IPs are still rejected by HA's ban middleware before this code runs. Tokens are read from the header only - never from
the query string or a signed path.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum

import jwt
from aiohttp import hdrs, web
from homeassistant.auth import jwt_wrapper
from homeassistant.auth.models import User
from homeassistant.components.http.auth_util import async_user_not_allowed_do_auth
from homeassistant.components.http.ban import process_wrong_login
from homeassistant.core import HomeAssistant
from homeassistant.helpers.network import NoURLAvailableError, get_url


class AuthOutcome(StrEnum):
    """Result of checking the Authorization header."""

    OK = "ok"
    MISSING = "missing"  # no Authorization header
    EXPIRED = "expired"  # genuine HA access token past its expiry
    INVALID = "invalid"  # wrong scheme, forged, revoked, unknown, inactive user
    NOT_ALLOWED = "not_allowed"  # valid token of a local-only user from outside


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Outcome and, for ``OK``, the authenticated user."""

    outcome: AuthOutcome
    user: User | None = None

    @property
    def counts_as_failed_login(self) -> bool:
        """Whether HA's failed-login counter (IP ban) must be increased."""
        return self.outcome in (AuthOutcome.INVALID, AuthOutcome.NOT_ALLOWED)


def authenticate(hass: HomeAssistant, request: web.Request) -> AuthResult:
    """Check the bearer token of ``request`` with Home Assistant's own validation."""
    header = request.headers.get(hdrs.AUTHORIZATION)
    if header is None:
        return AuthResult(AuthOutcome.MISSING)
    scheme, _, token = header.partition(" ")
    if scheme != "Bearer" or not token:
        return AuthResult(AuthOutcome.INVALID)

    refresh_token = hass.auth.async_validate_access_token(token)
    if refresh_token is not None:
        if async_user_not_allowed_do_auth(hass, refresh_token.user, request) is not None:
            return AuthResult(AuthOutcome.NOT_ALLOWED)
        return AuthResult(AuthOutcome.OK, refresh_token.user)
    if _is_genuine_but_expired(hass, token):
        return AuthResult(AuthOutcome.EXPIRED)
    return AuthResult(AuthOutcome.INVALID)


def _is_genuine_but_expired(hass: HomeAssistant, token: str) -> bool:
    """Signature valid for an existing refresh token of an active user, and only ``exp`` lies in the past."""
    try:
        claims = jwt_wrapper.unverified_hs256_token_decode(token)
    except jwt.InvalidTokenError:
        return False
    issuer = claims.get("iss")
    expiry = claims.get("exp")
    if not isinstance(issuer, str) or not isinstance(expiry, int | float) or isinstance(expiry, bool):
        return False
    refresh_token = hass.auth.async_get_refresh_token(issuer)
    if refresh_token is None or not refresh_token.user.is_active:
        return False
    try:
        jwt_wrapper.verify_and_decode(
            token, refresh_token.jwt_key, algorithms=["HS256"], issuer=refresh_token.id, options={"verify_exp": False}
        )
    except jwt.InvalidTokenError, jwt.InvalidKeyError:
        return False
    return expiry < time.time()


async def async_unauthorized(hass: HomeAssistant, request: web.Request, result: AuthResult) -> web.Response:
    """401 with the RFC 9728 challenge (as HA sends it); count real failures for the IP ban."""
    if result.counts_as_failed_login:
        await process_wrong_login(request)
    headers: dict[str, str] = {}
    try:
        base = get_url(hass, require_current_request=True)
    except NoURLAvailableError:
        pass  # like HA: omit the header rather than leak a configured URL
    else:
        headers[hdrs.WWW_AUTHENTICATE] = f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'
    return web.Response(status=401, text="401: Unauthorized", headers=headers)
