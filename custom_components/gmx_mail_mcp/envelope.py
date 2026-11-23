"""Response envelope (SPEC section 2, "Antwortformat"): size budget, pagination cursor, trust label."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any, Final

RESPONSE_BUDGET_BYTES: Final = 24 * 1024
CONTENT_TRUST: Final = "untrusted_external_email"
CURSOR_VERSION: Final = 1


class InvalidCursorError(ValueError):
    """The cursor is malformed or belongs to a different query."""


def json_size(value: Any) -> int:
    """Return the size of ``value`` serialised as compact UTF-8 JSON."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def build_envelope(
    *,
    account: str,
    action: str,
    data: Mapping[str, Any],
    page: Mapping[str, Any] | None = None,
    truncated: bool = False,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Wrap action data in the common response format."""
    envelope: dict[str, Any] = {
        "account": account,
        "action": action,
        "data": dict(data),
        "truncated": truncated,
        "warnings": sorted(set(warnings or [])),
        "content_trust": CONTENT_TRUST,
        "request_id": uuid.uuid4().hex,
    }
    if page is not None:
        envelope["page"] = dict(page)
    return envelope


def query_hash(query: Mapping[str, Any]) -> str:
    """Return a short hash identifying a search query (to reject a cursor used with other criteria)."""
    canonical = json.dumps(query, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def encode_cursor(*, folder: str, uidvalidity: int, offset: int, qhash: str) -> str:
    """Return an opaque cursor for the next page."""
    payload = {"v": CURSOR_VERSION, "f": folder, "u": uidvalidity, "o": offset, "q": qhash}
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, folder: str, qhash: str) -> tuple[int, int]:
    """Return ``(uidvalidity, offset)`` from a cursor or raise :class:`InvalidCursorError`."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        payload = json.loads(raw)
    except (binascii.Error, ValueError, UnicodeDecodeError) as err:
        raise InvalidCursorError("malformed cursor") from err
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise InvalidCursorError("unknown cursor version")
    if payload.get("f") != folder or payload.get("q") != qhash:
        raise InvalidCursorError("cursor belongs to another query")
    uidvalidity, offset = payload.get("u"), payload.get("o")
    if not isinstance(uidvalidity, int) or not isinstance(offset, int) or offset < 0:
        raise InvalidCursorError("malformed cursor")
    return uidvalidity, offset


def fit_list(envelope: dict[str, Any], key: str, budget: int = RESPONSE_BUDGET_BYTES) -> int:
    """Drop items from the end of ``envelope['data'][key]`` until the envelope fits; return how many remain."""
    items: list[Any] = envelope["data"][key]
    if json_size(envelope) <= budget:
        return len(items)
    envelope["truncated"] = True
    # Remove items until it fits (sizes are small and at most 50 items, so a linear walk is fine).
    while items and json_size(envelope) > budget:
        items.pop()
    return len(items)


def fit_text(envelope: dict[str, Any], render: Any, max_chars: int, budget: int = RESPONSE_BUDGET_BYTES) -> int:
    """Find the largest ``n <= max_chars`` for which ``render(n)`` fits the budget; returns ``n``.

    ``render(n)`` must update ``envelope`` in place to contain ``n`` characters of text.
    """
    render(max_chars)
    if json_size(envelope) <= budget:
        return max_chars
    envelope["truncated"] = True  # set first: it changes the serialised size
    low, high = 0, max_chars
    while low < high:
        mid = (low + high + 1) // 2
        render(mid)
        if json_size(envelope) <= budget:
            low = mid
        else:
            high = mid - 1
    render(low)
    return low
