"""Tests for folder parsing and special-folder detection."""

from __future__ import annotations

from custom_components.gmx_mail_mcp.client.folders import FolderInfo, map_special_folders, parse_folder_list
from custom_components.gmx_mail_mcp.const import FolderRole

from .fakes import GMX_FOLDERS


def _folders(*entries: tuple[tuple[str, ...], str]) -> list[FolderInfo]:
    return [FolderInfo(name=name, delimiter="/", flags=flags) for flags, name in entries]


def test_parse_folder_list_decodes_bytes() -> None:
    """Flags and delimiters arrive as bytes from imapclient."""
    folders = parse_folder_list(GMX_FOLDERS)
    assert folders[1] == FolderInfo(name="Entwürfe", delimiter="/", flags=("\\Drafts", "\\NoInferiors"))
    assert parse_folder_list([((), None, "INBOX")])[0].delimiter is None


def test_gmx_layout_uses_special_use() -> None:
    """The layout measured in M0 maps unambiguously via SPECIAL-USE."""
    result = map_special_folders(parse_folder_list(GMX_FOLDERS))
    assert {role: a.name for role, a in result.roles.items()} == {
        FolderRole.INBOX: "INBOX",
        FolderRole.DRAFTS: "Entwürfe",
        FolderRole.TRASH: "Gelöscht",
        FolderRole.SENT: "Gesendet",
        FolderRole.JUNK: "Spamverdacht",
    }
    assert all(a.source in ("inbox", "special_use") for a in result.roles.values())
    assert not result.ambiguous
    assert not result.invalid_overrides


def test_heuristic_without_special_use_prefers_gmx_names_and_top_level() -> None:
    """Without SPECIAL-USE, German GMX names win and top-level folders beat nested ones."""
    folders = _folders(
        ((), "INBOX"),
        ((), "Archiv/Gesendet"),
        ((), "Gesendet"),
        ((), "Papierkorb"),
        ((), "Drafts"),
        ((), "spam"),
    )
    result = map_special_folders(folders)
    assert result.get(FolderRole.SENT) == "Gesendet"
    assert result.get(FolderRole.TRASH) == "Papierkorb"
    assert result.get(FolderRole.DRAFTS) == "Drafts"
    assert result.get(FolderRole.JUNK) == "spam"
    assert result.roles[FolderRole.SENT].source == "heuristic"
    # Two candidates for "sent" (top level and nested) -> reported as ambiguous.
    assert result.ambiguous == {FolderRole.SENT}


def test_sent_beside_gesendet_is_ambiguous_but_special_use_wins() -> None:
    """A leftover "Sent" folder from another client next to the SPECIAL-USE folder is reported."""
    folders = _folders(((), "INBOX"), (("\\Sent",), "Gesendet"), ((), "Sent"))
    result = map_special_folders(folders)
    assert result.get(FolderRole.SENT) == "Gesendet"
    assert result.roles[FolderRole.SENT].source == "special_use"
    assert result.ambiguous == {FolderRole.SENT}


def test_multiple_special_use_prefers_heuristic_match() -> None:
    """Two folders flagged \\Trash: the one with the expected name is used."""
    folders = _folders(((), "INBOX"), (("\\Trash",), "Old"), (("\\Trash",), "Gelöscht"))
    result = map_special_folders(folders)
    assert result.get(FolderRole.TRASH) == "Gelöscht"
    assert FolderRole.TRASH in result.ambiguous


def test_overrides() -> None:
    """A valid override beats SPECIAL-USE; an unknown one is ignored and reported."""
    folders = parse_folder_list(GMX_FOLDERS)
    result = map_special_folders(folders, {FolderRole.SENT: "Archiv", FolderRole.TRASH: "Does not exist"})
    assert result.get(FolderRole.SENT) == "Archiv"
    assert result.roles[FolderRole.SENT].source == "override"
    assert result.get(FolderRole.TRASH) == "Gelöscht"
    assert result.invalid_overrides == {FolderRole.TRASH}
    assert result.as_dict()["invalid_overrides"] == ["trash"]


def test_noselect_folders_are_never_assigned() -> None:
    """\\Noselect containers are skipped."""
    folders = _folders(((), "INBOX"), (("\\Noselect", "\\Sent"), "Gesendet"), ((), "Sent"))
    result = map_special_folders(folders)
    assert result.get(FolderRole.SENT) == "Sent"
    assert not folders[1].selectable


def test_missing_roles_stay_unassigned() -> None:
    """Unknown folders produce no assignment."""
    result = map_special_folders(_folders(((), "INBOX"), ((), "Privat")))
    assert set(result.roles) == {FolderRole.INBOX}
    assert result.get(FolderRole.JUNK) is None
