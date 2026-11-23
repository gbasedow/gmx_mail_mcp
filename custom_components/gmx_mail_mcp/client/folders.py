"""Folder list parsing and special-folder detection.

Order of precedence (SPEC section 2, "Ordner-Erkennung"):
1. option override (if the folder exists),
2. SPECIAL-USE attribute (RFC 6154),
3. name heuristic (German GMX names first, then common English names).

imapclient decodes modified UTF-7 folder names already (``folder_encode=True``), so all names here are
plain ``str``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from ..const import FolderRole

type RawFolder = tuple[Sequence[bytes | str], bytes | str | None, str]
type MappingSource = Literal["inbox", "override", "special_use", "heuristic"]

SPECIAL_USE_FLAGS: dict[FolderRole, str] = {
    FolderRole.SENT: "\\sent",
    FolderRole.DRAFTS: "\\drafts",
    FolderRole.TRASH: "\\trash",
    FolderRole.JUNK: "\\junk",
}

# Lower-case, ordered by preference. GMX uses the first entry of each list (M0, GMX-NOTES row 14).
HEURISTIC_NAMES: dict[FolderRole, tuple[str, ...]] = {
    FolderRole.SENT: ("gesendet", "sent", "sent items", "sent messages", "gesendete objekte", "gesendete elemente"),
    FolderRole.DRAFTS: ("entwürfe", "drafts", "entwuerfe"),
    FolderRole.TRASH: ("gelöscht", "papierkorb", "trash", "deleted items", "deleted messages", "gelöschte elemente"),
    FolderRole.JUNK: ("spamverdacht", "spam", "junk", "junk e-mail", "junk-e-mail"),
}

_NOT_SELECTABLE = frozenset({"\\noselect", "\\nonexistent"})


@dataclass(frozen=True, slots=True)
class FolderInfo:
    """One mailbox as returned by LIST."""

    name: str
    delimiter: str | None
    flags: tuple[str, ...]

    @property
    def selectable(self) -> bool:
        """Return whether the folder can be selected."""
        return not any(flag.lower() in _NOT_SELECTABLE for flag in self.flags)

    @property
    def leaf(self) -> str:
        """Return the last path component."""
        if self.delimiter and self.delimiter in self.name:
            return self.name.rsplit(self.delimiter, 1)[1]
        return self.name

    @property
    def depth(self) -> int:
        """Return the nesting depth (0 = top level)."""
        return self.name.count(self.delimiter) if self.delimiter else 0


@dataclass(frozen=True, slots=True)
class FolderAssignment:
    """Folder chosen for a role and why."""

    name: str
    source: MappingSource


@dataclass(slots=True)
class FolderMap:
    """Result of the special-folder detection."""

    roles: dict[FolderRole, FolderAssignment] = field(default_factory=dict)
    ambiguous: set[FolderRole] = field(default_factory=set)
    invalid_overrides: set[FolderRole] = field(default_factory=set)

    def get(self, role: FolderRole) -> str | None:
        """Return the folder name for a role, if known."""
        assignment = self.roles.get(role)
        return assignment.name if assignment else None

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable representation (folder names only, no content)."""
        return {
            "roles": {role.value: {"name": a.name, "source": a.source} for role, a in self.roles.items()},
            "ambiguous": sorted(role.value for role in self.ambiguous),
            "invalid_overrides": sorted(role.value for role in self.invalid_overrides),
        }


def _to_str(value: bytes | str) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else value


def parse_folder_list(raw: Iterable[RawFolder]) -> list[FolderInfo]:
    """Convert imapclient ``list_folders()`` output into :class:`FolderInfo` objects."""
    folders: list[FolderInfo] = []
    for flags, delimiter, name in raw:
        folders.append(
            FolderInfo(
                name=_to_str(name),
                delimiter=_to_str(delimiter) if delimiter is not None else None,
                flags=tuple(_to_str(flag) for flag in flags),
            )
        )
    return folders


def _heuristic_rank(folder: FolderInfo, role: FolderRole) -> int | None:
    names = HEURISTIC_NAMES[role]
    leaf = folder.leaf.casefold()
    return names.index(leaf) if leaf in names else None


def map_special_folders(folders: Sequence[FolderInfo], overrides: Mapping[FolderRole, str] | None = None) -> FolderMap:
    """Assign special roles to folders."""
    result = FolderMap()
    selectable = [f for f in folders if f.selectable]
    by_name = {f.name: f for f in selectable}

    inbox = next((f for f in selectable if f.name.upper() == "INBOX"), None)
    if inbox is not None:
        result.roles[FolderRole.INBOX] = FolderAssignment(inbox.name, "inbox")

    for role, special_flag in SPECIAL_USE_FLAGS.items():
        override = (overrides or {}).get(role, "").strip()
        if override:
            if override in by_name:
                result.roles[role] = FolderAssignment(override, "override")
                continue
            result.invalid_overrides.add(role)

        special = [f for f in selectable if any(flag.lower() == special_flag for flag in f.flags)]
        heuristic = sorted(
            ((rank, f.depth, idx, f) for idx, f in enumerate(selectable) if (rank := _heuristic_rank(f, role)) is not None),
            key=lambda item: item[:3],
        )
        heuristic_folders = [item[3] for item in heuristic]

        if special:
            # Prefer a SPECIAL-USE folder whose name also matches the heuristic, else the first one.
            chosen = next((f for f in heuristic_folders if f in special), special[0])
            result.roles[role] = FolderAssignment(chosen.name, "special_use")
            others = [f for f in heuristic_folders if f is not chosen]
            if len(special) > 1 or others:
                result.ambiguous.add(role)
        elif heuristic_folders:
            result.roles[role] = FolderAssignment(heuristic_folders[0].name, "heuristic")
            if len(heuristic_folders) > 1:
                result.ambiguous.add(role)

    return result
