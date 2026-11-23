"""Tests fuer die reinen Hilfsfunktionen von tools/gmx_probe.py (keine Netzwerkzugriffe)."""

from __future__ import annotations

import argparse
from typing import Any

import gmx_probe


def test_utf7_encode_known_values() -> None:
    assert gmx_probe.utf7_encode("Entwürfe") == "Entw&APw-rfe"
    assert gmx_probe.utf7_encode("Gelöscht") == "Gel&APY-scht"
    assert gmx_probe.utf7_encode("INBOX") == "INBOX"
    assert gmx_probe.utf7_encode("A&B") == "A&-B"


def test_utf7_roundtrip() -> None:
    for name in ["Entwürfe", "HA-Probe-ÄA", "Rechnungen/2026", "日本語", "A&B", "Größe & Übung"]:
        assert gmx_probe.utf7_decode(gmx_probe.utf7_encode(name)) == name


def test_decode_list() -> None:
    lines = [b'(\\HasNoChildren \\Sent) "/" "Gesendet"', b'(\\HasNoChildren) "/" "Entw&APw-rfe"', None]
    parsed = gmx_probe.decode_list(lines)
    assert parsed[0]["name"] == "Gesendet" and "\\Sent" in parsed[0]["flags"]
    assert parsed[1]["name"] == "Entwürfe" and parsed[1]["delimiter"] == "/"


def test_decode_list_nil_delimiter_and_literal() -> None:
    lines: list[Any] = [
        b"(\\Noselect) NIL INBOX",
        (b'(\\HasNoChildren) "/" {12}', b'Mit "Quote"'),
        b"",
    ]
    parsed = gmx_probe.decode_list(lines)
    assert parsed[0]["delimiter"] is None and parsed[0]["name"] == "INBOX"
    assert parsed[1]["name"] == 'Mit "Quote"'
    assert len(parsed) == 2


def test_parse_uid() -> None:
    assert gmx_probe.parse_uid([b"[APPENDUID 1234 56] APPEND completed"], "APPENDUID") == [56]
    assert gmx_probe.parse_uid([b"[COPYUID 1234 7 56] COPY completed"], "COPYUID") == [56]
    assert gmx_probe.parse_uid([b"[COPYUID 1234 7:8 56:57] done"], "COPYUID") == [56, 57]
    assert gmx_probe.parse_uid([b"[COPYUID 1234 7,9 58:57] done"], "COPYUID") == [57, 58]  # descending range
    assert gmx_probe.parse_uid([b"OK done"], "COPYUID") == []
    assert gmx_probe.parse_uid(None, "COPYUID") == []


def test_imap_date_is_locale_independent() -> None:
    assert gmx_probe.imap_date(0) == "01-Jan-1970"
    assert gmx_probe.imap_date(1767225600) == "01-Jan-2026"  # 2026-01-01 00:00 UTC


class _FakeM:
    def __init__(self, store: dict[str, list[bytes]]) -> None:
        self.untagged_responses = store


def _probe_with(store: dict[str, list[bytes]]) -> gmx_probe.Probe:
    args = argparse.Namespace(imap_host="127.0.0.1", imap_port=1, cafile=None)
    p = gmx_probe.Probe("u", "p", args)
    p.M = _FakeM(store)  # type: ignore[assignment]
    return p


def test_copyuid_seen_tagged() -> None:
    # imaplib also files the TAGGED response code under "COPYUID"
    p = _probe_with({"COPYUID": [b"1 7 56"]})
    assert p.copyuid_seen([b"[COPYUID 1 7 56] done"]) == {"tagged": True, "untagged": False, "dest_uids": [56]}


def test_copyuid_seen_untagged_only() -> None:
    p = _probe_with({"COPYUID": [b"1 7 56"]})
    assert p.copyuid_seen([b"MOVE completed"]) == {"tagged": False, "untagged": True, "dest_uids": [56]}


def test_copyuid_seen_none() -> None:
    p = _probe_with({})
    assert p.copyuid_seen([b"MOVE completed"]) == {"tagged": False, "untagged": False, "dest_uids": []}
