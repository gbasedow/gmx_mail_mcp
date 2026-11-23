"""Deleting IMAP operations (M5, permission level D) - blocking; run with ``ImapWorker.async_run``.

Rules (SPEC section 5, DECISIONS 46-53):

* ``trash`` moves messages into the trash folder (reversible with ``move``); messages already in the trash are refused,
  because "delete in the trash" means "delete for good" in the GMX webmailer;
* ``delete_permanently`` only works on messages in the trash or the spam folder (everything else goes to the trash
  first) and only with UIDPLUS: ``\\Deleted`` + ``UID EXPUNGE`` of exactly these UIDs - never a mailbox-wide EXPUNGE,
  never CLOSE, so messages other clients marked ``\\Deleted`` are never removed;
* ``delete_folder`` only removes empty folders without subfolders; INBOX and special folders are protected.

Every check runs again at execution time (the mailbox may have changed since the preview).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..const import FolderRole
from .errors import GmxDeleteRuleError, GmxFolderNotFoundError, GmxProtectedFolderDeleteError, GmxUnsupportedError
from .folders import FolderMap, parse_folder_list
from .imap import ImapSession, ServerInfo
from .reader import MailRef, _summaries
from .writer import WriteResult, _existing, _group_refs, copy_or_move, open_folder

PREVIEW_DETAILS = 10  # messages listed one by one in a preview; the rest is only counted


def _role_folder(folder_map: FolderMap, role: FolderRole) -> str | None:
    return folder_map.get(role)


def _permanent_folders(folder_map: FolderMap) -> set[str]:
    return {name for role in (FolderRole.TRASH, FolderRole.JUNK) if (name := _role_folder(folder_map, role))}


# --------------------------------------------------------------------------- trash


def trash(session: ImapSession, info: ServerInfo, folder_map: FolderMap, refs: Sequence[MailRef]) -> tuple[str, WriteResult]:
    """Move messages into the trash folder; returns ``(trash folder, result with new references)``."""
    target = _role_folder(folder_map, FolderRole.TRASH)
    if not target:
        raise GmxDeleteRuleError("no_trash_folder")
    if any(ref.folder == target for ref in refs):
        raise GmxDeleteRuleError("already_in_trash")
    return target, copy_or_move(session, info, refs, target, move=True)


# --------------------------------------------------------------------------- delete permanently


def _check_permanent(info: ServerInfo, folder_map: FolderMap, refs: Sequence[MailRef]) -> None:
    if not info.has("UIDPLUS"):
        raise GmxUnsupportedError("uidplus_required")  # without it only a mailbox-wide EXPUNGE would remain
    allowed = _permanent_folders(folder_map)
    if not allowed or any(ref.folder not in allowed for ref in refs):
        raise GmxDeleteRuleError("not_in_trash_or_junk")


def preview_permanent(session: ImapSession, info: ServerInfo, folder_map: FolderMap, refs: Sequence[MailRef]) -> dict[str, Any]:
    """Read-only preview: which of ``refs`` exist, with date, sender and subject of the first ``PREVIEW_DETAILS``."""
    _check_permanent(info, folder_map, refs)
    present: list[MailRef] = []
    missing: list[dict[str, Any]] = []
    details: list[dict[str, Any]] = []
    for folder, group in _group_refs(refs).items():
        with open_folder(session, info, folder, readonly=True) as response:
            validity = int(response.get(b"UIDVALIDITY", 0))
            found, gone = _existing(session, group, validity)
            present.extend(found)
            missing.extend(r.as_dict() for r in gone)
            room = PREVIEW_DETAILS - len(details)
            if room > 0 and found:
                uids = sorted(r.uid for r in found)[:room]
                for summary in _summaries(session, folder, validity, uids, snippet=False):
                    details.append({key: summary.get(key) for key in ("ref", "date", "from", "subject")})
    return {
        "count": len(present),
        "folders": sorted({r.folder for r in present}),
        "messages": details,
        "more": max(0, len(present) - len(details)),
        "missing": missing,
    }


def delete_permanently(session: ImapSession, info: ServerInfo, folder_map: FolderMap, refs: Sequence[MailRef]) -> WriteResult:
    """``\\Deleted`` + ``UID EXPUNGE`` of exactly these messages (trash or spam folder only)."""
    _check_permanent(info, folder_map, refs)
    result = WriteResult()
    for folder, group in _group_refs(refs).items():
        with open_folder(session, info, folder, readonly=False) as response:
            present, missing = _existing(session, group, int(response.get(b"UIDVALIDITY", 0)))
            result.missing.extend(r.as_dict() for r in missing)
            uids = sorted(r.uid for r in present)
            if not uids:
                continue
            session.add_flags(uids, ["\\Deleted"], silent=True)
            session.uid_expunge(uids)
            remaining = set(session.fetch(uids, ["FLAGS"])) if uids else set()
            if remaining:  # the server kept some (should not happen); report honestly
                result.warnings.append("not_expunged")
            result.count += len(uids) - len(remaining)
            result.folders.append(folder)
    return result


# --------------------------------------------------------------------------- delete folder


def check_delete_folder(session: ImapSession, folder_map: FolderMap, path: str) -> dict[str, Any]:
    """Verify that ``path`` may be deleted (exists, not special, no subfolders, empty); returns a preview."""
    folders = {f.name: f for f in parse_folder_list(session.list_folders())}
    folder = folders.get(path)
    if folder is None:
        raise GmxFolderNotFoundError("folder not found")
    special = {assignment.name for assignment in folder_map.roles.values()}
    if path.upper() == "INBOX" or path in special:
        raise GmxProtectedFolderDeleteError("special_folder")
    delimiter = folder.delimiter or "/"
    if any(name.startswith(path + delimiter) for name in folders):
        raise GmxDeleteRuleError("folder_has_children")
    if folder.selectable:
        messages = int(session.folder_status(path, ["MESSAGES"]).get(b"MESSAGES", 0))
        if messages:
            raise GmxDeleteRuleError("folder_not_empty")
    return {"path": path, "messages": 0}


def delete_folder(session: ImapSession, folder_map: FolderMap, path: str) -> str:
    """Delete an empty folder without subfolders (checked again right before DELETE)."""
    check_delete_folder(session, folder_map, path)
    session.delete_folder(path)
    return path
