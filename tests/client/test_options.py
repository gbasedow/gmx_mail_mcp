"""Tests for the option helpers of the options form (allowlist, sending mode, sender name, folder overrides)."""

from __future__ import annotations

import pytest

from custom_components.gmx_mail_mcp.const import CONF_ALLOWLIST, CONF_FOLDER_SENT, CONF_SEND_MODE, CONF_SENDER_NAME, FolderRole
from custom_components.gmx_mail_mcp.options import (
    check_send_options,
    folder_overrides,
    is_valid_allowlist_entry,
    normalize_allowlist,
    normalize_sender_name,
)


@pytest.mark.parametrize(
    ("entry", "valid"),
    [
        ("anna@example.org", True),
        ("*@example.org", True),
        ("*", False),  # "any address" is a mode of its own (D46)
        ("anna@localhost", False),
        ("anna", False),
        ("*@*.org", False),
        ("a*b@example.org", False),
        ("@example.org", False),
        ("anna@example.", False),
        ("anna@@example.org", False),
    ],
)
def test_allowlist_entries(entry: str, valid: bool) -> None:
    """Only exact addresses and *@domain are accepted."""
    assert is_valid_allowlist_entry(entry) is valid


def test_allowlist_normalisation() -> None:
    """Entries are split, trimmed, lower-cased and de-duplicated."""
    assert normalize_allowlist([" Anna@Example.org ", "anna@example.org, *@Firma.de", ""]) == ["anna@example.org", "*@firma.de"]
    assert normalize_allowlist("x@y.de") == ["x@y.de"]
    assert normalize_allowlist(None) == []


@pytest.mark.parametrize(
    ("mode", "allowlist", "name", "errors"),
    [
        ("allowlist", "", "", {}),
        ("any", "", "", {}),
        ("any", "a@b.de", "", {}),
        ("off", "", "", {CONF_SEND_MODE: "invalid_send_mode"}),  # not offered in the form (D46)
        ("allowlist", "*", "", {CONF_ALLOWLIST: "wildcard_not_allowed"}),
        ("any", "*", "", {CONF_ALLOWLIST: "wildcard_not_allowed"}),
        ("allowlist", "anna@example.org\nkein-eintrag", "", {CONF_ALLOWLIST: "invalid_allowlist_entry"}),
        ("allowlist", "", 'Evil" <x@evil.test>', {CONF_SENDER_NAME: "invalid_sender_name"}),
    ],
)
def test_check_send_options_of_the_form(mode: str, allowlist: str, name: str, errors: dict[str, str]) -> None:
    """The options form: two modes, no '*', addresses and *@domain one per line, safe sender name (D46, D52)."""
    assert check_send_options(mode, allowlist, name)[1] == errors


def test_check_send_options_normalises() -> None:
    """Lines and commas split, lower case, duplicates removed; whitespace in the name collapsed."""
    values, errors = check_send_options("allowlist", " Anna@Example.org\n*@Firma.de, anna@example.org ", "  Haus \n Assistent ")
    assert errors == {}
    assert values == {CONF_SEND_MODE: "allowlist", CONF_ALLOWLIST: ["anna@example.org", "*@firma.de"], CONF_SENDER_NAME: "Haus Assistent"}
    assert check_send_options(None, None, None)[0] == {CONF_SEND_MODE: "allowlist", CONF_ALLOWLIST: [], CONF_SENDER_NAME: ""}


def test_sender_name_rules() -> None:
    """Whitespace collapsed (no line breaks), at most 100 characters, no <, > or quotes (D52)."""
    assert normalize_sender_name("  Haus \n  Assistent ") == ("Haus Assistent", True)
    for bad in ('Evil" <x@evil.test>', "a" * 101, "Name\x00"):
        assert normalize_sender_name(bad)[1] is False


def test_folder_overrides_are_trimmed_and_empty_ones_dropped() -> None:
    assert folder_overrides({CONF_FOLDER_SENT: "  Gesendet "}) == {FolderRole.SENT: "Gesendet"}
    assert folder_overrides({CONF_FOLDER_SENT: ""}) == {}
