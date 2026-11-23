"""Confirmation codes for irreversible actions (SPEC section 3 "Zwei-Schritt-Ablauf", DECISIONS Nr. 48).

A code is ``<payload>.<signature>``: the payload (action, hash of the exact parameters, expiry, random id) is signed
with HMAC-SHA256 under a key that exists only in memory for the lifetime of the loaded config entry. So a code

* is valid for one account, one action and exactly the parameters shown in the preview;
* expires after ``TOKEN_TTL_S`` seconds and can be used only once;
* becomes invalid when Home Assistant restarts or the entry is reloaded (the key is gone).

A code forces an extra step in which the assistant should ask the person; it does **not** prove that a person agreed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from typing import Any

TOKEN_TTL_S = 300


class InvalidTokenError(Exception):
    """The confirmation code is malformed, forged, expired, already used or for other parameters."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def params_hash(params: Any) -> str:
    """Stable hash of the call parameters the code is bound to."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class ConfirmTokens:
    """Issues and checks single-use confirmation codes for one config entry."""

    def __init__(self, *, ttl: float = TOKEN_TTL_S, clock: Callable[[], float] = time.time) -> None:
        """Create a fresh in-memory key."""
        self._key = secrets.token_bytes(32)
        self._ttl = ttl
        self._clock = clock
        self._used: dict[str, float] = {}  # token id -> expiry

    @property
    def ttl(self) -> int:
        """Return the validity in seconds."""
        return int(self._ttl)

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self, action: str, params: Any) -> str:
        """Return a code for ``action`` with exactly ``params``."""
        body = {"a": action, "h": params_hash(params), "e": int(self._clock() + self._ttl), "n": secrets.token_hex(8)}
        payload = _b64(json.dumps(body, separators=(",", ":")).encode())
        return f"{payload}.{self._sign(payload)}"

    def consume(self, token: str, action: str, params: Any) -> None:
        """Accept the code once; raises :class:`InvalidTokenError` otherwise."""
        now = self._clock()
        self._used = {k: v for k, v in self._used.items() if v >= now}
        payload, _, signature = token.partition(".")
        if not payload or not signature or not hmac.compare_digest(signature, self._sign(payload)):
            raise InvalidTokenError("bad signature")
        try:
            body = json.loads(_unb64(payload))
            expiry, nonce = float(body["e"]), str(body["n"])
        except (ValueError, KeyError, TypeError) as err:
            raise InvalidTokenError("malformed") from err
        if body.get("a") != action or not hmac.compare_digest(str(body.get("h")), params_hash(params)):
            raise InvalidTokenError("other action or parameters")
        if expiry < now:
            raise InvalidTokenError("expired")
        if nonce in self._used:
            raise InvalidTokenError("already used")
        self._used[nonce] = expiry
