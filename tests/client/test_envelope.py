"""Tests for the response envelope, budget handling and cursors."""

from __future__ import annotations

import base64

import pytest

from custom_components.gmx_mail_mcp.envelope import (
    CONTENT_TRUST,
    RESPONSE_BUDGET_BYTES,
    InvalidCursorError,
    build_envelope,
    decode_cursor,
    encode_cursor,
    fit_list,
    fit_text,
    json_size,
    query_hash,
)


def test_envelope_fields() -> None:
    """Every response carries account, action, trust label, request id and sorted unique warnings."""
    envelope = build_envelope(account="a@gmx.de", action="search", data={"x": 1}, warnings=["b", "a", "b"])
    assert envelope["content_trust"] == CONTENT_TRUST == "untrusted_external_email"
    assert envelope["warnings"] == ["a", "b"]
    assert envelope["truncated"] is False
    assert len(envelope["request_id"]) == 32
    assert "page" not in envelope
    assert build_envelope(account="a", action="b", data={}, page={"limit": 1})["page"] == {"limit": 1}


def test_fit_list_drops_from_the_end() -> None:
    """Items are removed until the JSON fits the budget."""
    envelope = build_envelope(account="a", action="search", data={"messages": [{"s": "x" * 1000} for _ in range(50)]})
    assert json_size(envelope) > RESPONSE_BUDGET_BYTES
    kept = fit_list(envelope, "messages")
    assert 0 < kept < 50
    assert json_size(envelope) <= RESPONSE_BUDGET_BYTES
    assert envelope["truncated"] is True
    small = build_envelope(account="a", action="search", data={"messages": [1, 2]})
    assert fit_list(small, "messages") == 2 and small["truncated"] is False


def test_fit_text_finds_largest_fitting_length() -> None:
    """Binary search over the text length; multi-byte characters count with their UTF-8 size."""
    text = "ä" * 40_000
    envelope = build_envelope(account="a", action="get_message", data={"body": {}})

    def render(n: int) -> None:
        envelope["data"]["body"]["text"] = text[:n]

    n = fit_text(envelope, render, 32_000)
    assert json_size(envelope) <= RESPONSE_BUDGET_BYTES
    render(n + 1)
    assert json_size(envelope) > RESPONSE_BUDGET_BYTES
    render(n)
    assert envelope["truncated"] is True
    short = build_envelope(account="a", action="get_message", data={"body": {}})
    assert fit_text(short, lambda k: short["data"]["body"].update(text="x" * k), 100) == 100
    assert short["truncated"] is False


def test_cursor_roundtrip_and_validation() -> None:
    """A cursor only works for the same folder and query."""
    qhash = query_hash({"subject": "x", "folder": "INBOX"})
    assert qhash == query_hash({"folder": "INBOX", "subject": "x"})  # order-independent
    cursor = encode_cursor(folder="Entwürfe", uidvalidity=42, offset=20, qhash=qhash)
    assert "=" not in cursor
    assert decode_cursor(cursor, folder="Entwürfe", qhash=qhash) == (42, 20)
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, folder="INBOX", qhash=qhash)
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor, folder="Entwürfe", qhash=query_hash({"subject": "y"}))
    for bad in ("!!!", base64.urlsafe_b64encode(b"[1]").decode(), base64.urlsafe_b64encode(b'{"v":2}').decode()):
        with pytest.raises(InvalidCursorError):
            decode_cursor(bad, folder="INBOX", qhash=qhash)
    forged = base64.urlsafe_b64encode(b'{"v":1,"f":"INBOX","u":1,"o":-5,"q":"' + qhash.encode() + b'"}').decode()
    with pytest.raises(InvalidCursorError):
        decode_cursor(forged, folder="INBOX", qhash=qhash)
