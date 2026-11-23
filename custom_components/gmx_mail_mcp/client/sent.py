"""IMAP steps after a successful SMTP send (M6) - blocking; failures here never cause a second send.

* the Sent copy: GMX does not store one for SMTP (GMX-NOTES row 22) -> APPEND with ``\\Seen`` (``sent_copy_mode``
  ``append``); ``auto`` first looks for the Message-ID among the newest messages of the Sent folder; ``none`` skips it;
* replies mark the original ``\\Answered``;
* a sent draft is removed from the drafts folder (``\\Deleted`` + UID EXPUNGE of exactly that UID, UIDPLUS only).
"""

from __future__ import annotations

from ..const import FolderRole, SentCopyMode
from .folders import FolderMap
from .imap import ImapSession, ServerInfo
from .reader import MailRef
from .writer import _existing, _message_ids, open_folder

SENT_SCAN = 20


def store_sent_copy(session: ImapSession, info: ServerInfo, folder_map: FolderMap, raw: bytes, *, message_id: str, mode: str) -> str:
    """Return ``appended``, ``server`` (copy already there), ``none`` (switched off) or ``no_sent_folder``."""
    if mode == SentCopyMode.NONE:
        return "none"
    sent = folder_map.get(FolderRole.SENT)
    if not sent:
        return "no_sent_folder"
    if mode == SentCopyMode.AUTO:
        with open_folder(session, info, sent, readonly=True):
            newest = sorted(int(u) for u in session.search(["ALL"]))[-SENT_SCAN:]
            if message_id in _message_ids(session, newest).values():
                return "server"
    session.append(sent, raw, flags=("\\Seen",))
    return "appended"


def mark_answered(session: ImapSession, info: ServerInfo, ref: MailRef) -> bool:
    """Set ``\\Answered`` on the original; returns False if it no longer exists."""
    with open_folder(session, info, ref.folder, readonly=False) as response:
        present, _missing = _existing(session, [ref], int(response.get(b"UIDVALIDITY", 0)))
        if present:
            session.add_flags([ref.uid], ["\\Answered"], silent=True)
    return bool(present)


def remove_draft(session: ImapSession, info: ServerInfo, ref: MailRef) -> bool:
    """Remove the sent draft (exactly this UID); returns False without UIDPLUS or if it is gone already."""
    if not info.has("UIDPLUS"):
        return False
    with open_folder(session, info, ref.folder, readonly=False) as response:
        present, _missing = _existing(session, [ref], int(response.get(b"UIDVALIDITY", 0)))
        if not present:
            return False
        session.add_flags([ref.uid], ["\\Deleted"], silent=True)
        session.uid_expunge([ref.uid])
    return True
