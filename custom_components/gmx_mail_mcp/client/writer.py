"""Organising IMAP operations (permission level O) - blocking; run with ``ImapWorker.async_run(readonly=False)``.

Rules (SPEC section 2 and 5, DECISIONS 37-42):

* a folder is opened read-write with SELECT and always left with UNSELECT, never with CLOSE (CLOSE would expunge
  messages that other clients marked ``\\Deleted``). Without UNSELECT the folder is re-opened read-only (EXAMINE),
  which also never expunges;
* every reference is checked against the folder's current UIDVALIDITY;
* nothing here deletes: ``\\Deleted`` cannot be set via ``set_flags``; MOVE without the MOVE capability uses
  COPY + ``\\Deleted`` + ``UID EXPUNGE`` of exactly the moved UIDs (needs UIDPLUS), never a mailbox-wide EXPUNGE;
* the new UIDs after COPY/MOVE come from COPYUID (tagged or untagged - GMX sends it untagged for MOVE), otherwise
  from matching Message-IDs in the target folder, otherwise the result says so.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formataddr, getaddresses, make_msgid
from typing import Any

from imapclient.exceptions import IMAPClientError

from ..const import FolderRole
from .errors import (
    GmxFolderExistsError,
    GmxFolderNotFoundError,
    GmxInvalidFolderNameError,
    GmxMailError,
    GmxMessageTooLargeError,
    GmxProtectedFolderError,
    GmxProtectedTargetError,
    GmxUidValidityError,
    GmxUnsupportedError,
    GmxWrongFolderError,
)
from .folders import FolderMap, parse_folder_list
from .imap import CONNECTION_ERRORS, ImapSession, ServerInfo
from .reader import SUMMARY_HEADERS, MailRef, _find_key, _header_str, parse_headers
from .upload import Attachment

SYSTEM_FLAGS: Mapping[str, str] = {"seen": "\\Seen", "flagged": "\\Flagged", "answered": "\\Answered"}
KEYWORD_RE = re.compile(r"^\$?[A-Za-z0-9_.\-]{1,64}$")
# Look-alikes of system flags are refused as keywords too: "deleted" would not delete, but would mislead.
RESERVED_KEYWORDS = frozenset({"deleted", "draft", "recent"})
FOLDER_NAME_FORBIDDEN = re.compile(r"[/%*\x00-\x1f\x7f]")
MAX_FOLDER_NAME = 100
MESSAGE_ID_FALLBACK_SCAN = 20  # newest UIDs in the target folder checked when COPYUID is missing
# Keywords for the spam state as registered in RFC 9051 section 2.3.2 ($Junk / $NotJunk).
SPAM_KEYWORD = "$Junk"
NOT_SPAM_KEYWORD = "$NotJunk"
REPLY_PREFIXES = ("re:", "aw:")  # German clients answer with "AW:"; a draft reply keeps an existing prefix
_UID_SET_RE = re.compile(r"\[?(COPYUID|APPENDUID) (\d+) ([\d:,]+)(?: ([\d:,]+))?\]?", re.IGNORECASE)


@dataclass(slots=True)
class WriteResult:
    """Result of a writing operation."""

    count: int = 0
    new_refs: list[dict[str, Any] | None] = field(default_factory=list)
    missing: list[dict[str, Any]] = field(default_factory=list)
    folders: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers


@contextmanager
def open_folder(session: ImapSession, info: ServerInfo, folder: str, *, readonly: bool) -> Iterator[dict[bytes, Any]]:
    """SELECT (or EXAMINE) ``folder``; leave with UNSELECT or a read-only re-open, never CLOSE."""
    try:
        response = session.select_folder(folder, readonly=readonly)
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError as err:
        raise GmxFolderNotFoundError(f"folder cannot be opened: {type(err).__name__}") from err
    try:
        yield response
    finally:
        with suppress(IMAPClientError):
            if info.has("UNSELECT"):
                session.unselect_folder()
            elif not readonly:
                session.select_folder(folder, readonly=True)  # leaves read-write mode without an implicit expunge


def expand_uid_set(text: str) -> list[int]:
    """Expand an IMAP UID set like ``1:3,7`` (ranges in either direction)."""
    uids: list[int] = []
    for part in text.split(","):
        if ":" in part:
            start, end = (int(x) for x in part.split(":", 1))
            step = 1 if end >= start else -1
            uids.extend(range(start, end + step, step))
        elif part:
            uids.append(int(part))
    return uids


def parse_copyuid(values: Sequence[bytes | str]) -> tuple[int, list[int], list[int]] | None:
    """Find ``COPYUID <uidvalidity> <src> <dst>`` in response texts (the last one wins)."""
    found = None
    for value in values:
        text = value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)
        for match in _UID_SET_RE.finditer(text):
            if match.group(1).upper() == "COPYUID" and match.group(4):
                found = (int(match.group(2)), expand_uid_set(match.group(3)), expand_uid_set(match.group(4)))
    return found


def parse_appenduid(value: bytes | str) -> tuple[int, int] | None:
    """Find ``APPENDUID <uidvalidity> <uid>`` in an APPEND response."""
    text = value.decode("ascii", "replace") if isinstance(value, bytes) else str(value)
    for match in _UID_SET_RE.finditer(text):
        if match.group(1).upper() == "APPENDUID":
            uids = expand_uid_set(match.group(3))
            if len(uids) == 1:
                return int(match.group(2)), uids[0]
    return None


def _response_store(session: ImapSession) -> dict[str, Any] | None:
    """imaplib keeps response codes (of tagged *and* untagged replies) in ``untagged_responses`` (internal API).

    This is the only place where GMX's untagged ``* OK [COPYUID ...]`` after MOVE is visible; imapclient does not
    return it. Guarded: if the attribute is missing, the Message-ID fallback is used.
    """
    imap = getattr(session, "_imap", None)
    store = getattr(imap, "untagged_responses", None)
    return store if isinstance(store, dict) else None


def _group_refs(refs: Sequence[MailRef]) -> dict[str, list[MailRef]]:
    groups: dict[str, list[MailRef]] = {}
    for ref in refs:
        groups.setdefault(ref.folder, []).append(ref)
    return groups


def _existing(session: ImapSession, refs: Sequence[MailRef], uidvalidity: int) -> tuple[list[MailRef], list[MailRef]]:
    """Split refs of the selected folder into existing and missing ones (UIDVALIDITY checked first)."""
    if any(ref.uidvalidity != uidvalidity for ref in refs):
        raise GmxUidValidityError("UIDVALIDITY changed")
    present = set(session.fetch([ref.uid for ref in refs], ["FLAGS"]))
    return [r for r in refs if r.uid in present], [r for r in refs if r.uid not in present]


def _message_ids(session: ImapSession, uids: Sequence[int]) -> dict[int, str]:
    if not uids:
        return {}
    data = session.fetch(list(uids), ["BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)]"])
    result: dict[int, str] = {}
    for uid, item in data.items():
        value = _header_str(parse_headers(_find_key(item, b"BODY[HEADER")), "message-id")
        if value:
            result[int(uid)] = value
    return result


# --------------------------------------------------------------------------- flags


def normalize_flags(names: Sequence[str]) -> tuple[list[str], list[str]]:
    """Return ``(system_flags, keywords)``; raises ValueError for anything not allowed (e.g. \\Deleted).

    System flags are accepted as ``seen`` or in the IMAP spelling ``\\Seen`` that search results show.
    """
    system: list[str] = []
    keywords: list[str] = []
    for raw in names:
        name = raw.strip()
        if name.startswith("\\") and name[1:].lower() in SYSTEM_FLAGS:
            system.append(SYSTEM_FLAGS[name[1:].lower()])
        elif name.lower() in SYSTEM_FLAGS:
            system.append(SYSTEM_FLAGS[name.lower()])
        elif KEYWORD_RE.match(name) and name.lower().lstrip("$") not in RESERVED_KEYWORDS:
            keywords.append(name)
        else:
            raise ValueError(name)
    return system, keywords


def set_flags(session: ImapSession, info: ServerInfo, refs: Sequence[MailRef], add: Sequence[str], remove: Sequence[str]) -> WriteResult:
    """Add/remove flags; keywords only if the folder allows them (PERMANENTFLAGS contains ``\\*``)."""
    add_system, add_keywords = normalize_flags(add)
    remove_system, remove_keywords = normalize_flags(remove)
    result = WriteResult()
    for folder, group in _group_refs(refs).items():
        with open_folder(session, info, folder, readonly=False) as response:
            permanent = [f.decode() if isinstance(f, bytes) else str(f) for f in response.get(b"PERMANENTFLAGS", ())]
            if (add_keywords or remove_keywords) and "\\*" not in permanent:
                raise GmxUnsupportedError("keywords_not_supported")
            present, missing = _existing(session, group, int(response.get(b"UIDVALIDITY", 0)))
            result.missing.extend(r.as_dict() for r in missing)
            uids = [r.uid for r in present]
            if uids and (add_system or add_keywords):
                session.add_flags(uids, [*add_system, *add_keywords], silent=True)
            if uids and (remove_system or remove_keywords):
                session.remove_flags(uids, [*remove_system, *remove_keywords], silent=True)
            result.count += len(uids)
            result.folders.append(folder)
    return result


# --------------------------------------------------------------------------- move / copy


def copy_or_move(
    session: ImapSession,
    info: ServerInfo,
    refs: Sequence[MailRef],
    target: str,
    *,
    move: bool,
    forbidden_targets: Collection[str] = (),
) -> WriteResult:
    """Copy or move messages; returns new references in the order of ``refs`` (None where unknown).

    ``forbidden_targets`` are folders reserved for other tools (the trash: deleting is level D, decision D32).
    """
    if target in forbidden_targets:
        raise GmxProtectedTargetError("protected_target")
    if move and not info.has("MOVE") and not info.has("UIDPLUS"):
        # COPY + \\Deleted would need a mailbox-wide EXPUNGE without UIDPLUS - never allowed.
        raise GmxUnsupportedError("move_unsupported")
    targets = {f.name for f in parse_folder_list(session.list_folders()) if f.selectable}
    if target not in targets:
        raise GmxFolderNotFoundError("target folder not found")
    result = WriteResult()
    mapping: dict[MailRef, dict[str, Any]] = {}
    for folder, group in _group_refs(refs).items():
        if folder == target:
            raise GmxInvalidFolderNameError("same_folder")
        with open_folder(session, info, folder, readonly=False) as response:
            present, missing = _existing(session, group, int(response.get(b"UIDVALIDITY", 0)))
            result.missing.extend(r.as_dict() for r in missing)
            if not present:
                continue
            uids = sorted(r.uid for r in present)
            ids = _message_ids(session, uids)
            copyuid = _transfer(session, info, uids, target, move=move)
            result.count += len(uids)
            result.folders.append(folder)
            if copyuid is not None:
                dst_validity, src, dst = copyuid
                pairs = dict(zip(src, dst, strict=False))
                for ref in present:
                    if ref.uid in pairs:
                        mapping[ref] = MailRef(target, pairs[ref.uid], dst_validity).as_dict()
            unmapped = [r for r in present if r not in mapping]
        if unmapped:
            found = _find_by_message_id(session, info, target, {r: ids[r.uid] for r in unmapped if r.uid in ids})
            mapping.update(found)
            if len(found) < len(unmapped):
                result.warnings.append("new_refs_incomplete")
    result.new_refs = [mapping.get(ref) for ref in refs if ref.as_dict() not in result.missing]
    return result


def _transfer(
    session: ImapSession, info: ServerInfo, uids: list[int], target: str, *, move: bool
) -> tuple[int, list[int], list[int]] | None:
    store = _response_store(session)
    if store is not None:
        store.pop("COPYUID", None)  # imaplib does not clear it between commands
    if move and info.has("MOVE"):
        tagged = session.move(uids, target)
    else:
        tagged = session.copy(uids, target)
        if move:
            # Fallback (no MOVE): COPY, mark the copied originals, expunge exactly those UIDs (UIDPLUS).
            session.add_flags(uids, ["\\Deleted"], silent=True)
            session.uid_expunge(uids)
    values: list[bytes | str] = [tagged] if isinstance(tagged, bytes | str) else []
    if store is not None:
        values += [b"COPYUID " + v for v in store.get("COPYUID", []) if isinstance(v, bytes)]
    return parse_copyuid(values)


def _find_by_message_id(
    session: ImapSession, info: ServerInfo, target: str, wanted: Mapping[MailRef, str]
) -> dict[MailRef, dict[str, Any]]:
    """Find moved/copied messages in the target by Message-ID among its newest UIDs (no SEARCH: GMX index lag)."""
    if not wanted:
        return {}
    with open_folder(session, info, target, readonly=True) as response:
        validity = int(response.get(b"UIDVALIDITY", 0))
        newest = sorted(int(u) for u in session.search(["ALL"]))[-(len(wanted) + MESSAGE_ID_FALLBACK_SCAN) :]
        by_id: dict[str, int] = {}
        for uid, message_id in _message_ids(session, newest).items():
            by_id.setdefault(message_id, uid)  # a duplicate Message-ID: the first match is used
    return {ref: MailRef(target, by_id[mid], validity).as_dict() for ref, mid in wanted.items() if mid in by_id}


# --------------------------------------------------------------------------- spam


def _try_keywords(session: ImapSession, info: ServerInfo, refs: Sequence[MailRef], add: str, remove: str) -> bool:
    """Set ``add`` and clear ``remove`` where the folder allows keywords; False if a folder does not."""
    allowed = True
    for folder, group in _group_refs(refs).items():
        with open_folder(session, info, folder, readonly=False) as response:
            permanent = [f.decode() if isinstance(f, bytes) else str(f) for f in response.get(b"PERMANENTFLAGS", ())]
            if "\\*" not in permanent:
                allowed = False
                continue
            present, _missing = _existing(session, group, int(response.get(b"UIDVALIDITY", 0)))
            uids = [r.uid for r in present]
            if uids:
                session.add_flags(uids, [add], silent=True)
                session.remove_flags(uids, [remove], silent=True)
    return allowed


def mark_spam(session: ImapSession, info: ServerInfo, folder_map: FolderMap, refs: Sequence[MailRef], *, spam: bool) -> WriteResult:
    """Spam: move into the junk folder with ``$Junk``. Not spam: from the junk folder into the inbox with ``$NotJunk``.

    The keyword is set before the move (MOVE keeps flags), so the new reference need not be known. GMX deletes mail in
    the junk folder after 30 days (GMX-NOTES row 17); whether GMX learns from these moves is unknown.
    """
    junk = folder_map.get(FolderRole.JUNK)
    if not junk:
        raise GmxFolderNotFoundError("no junk folder")
    inbox = folder_map.get(FolderRole.INBOX) or "INBOX"
    if not spam and any(ref.folder != junk for ref in refs):
        raise GmxWrongFolderError("not_in_junk")
    add, remove = (SPAM_KEYWORD, NOT_SPAM_KEYWORD) if spam else (NOT_SPAM_KEYWORD, SPAM_KEYWORD)
    if spam and any(ref.folder == junk for ref in refs):
        raise GmxInvalidFolderNameError("same_folder")
    keywords_set = _try_keywords(session, info, refs, add, remove)
    result = copy_or_move(session, info, refs, junk if spam else inbox, move=True)
    if not keywords_set:
        result.warnings.append("keywords_not_supported")
    return result


# --------------------------------------------------------------------------- folders


def _validate_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned or len(cleaned) > MAX_FOLDER_NAME or FOLDER_NAME_FORBIDDEN.search(cleaned) or cleaned in (".", ".."):
        raise GmxInvalidFolderNameError("invalid_folder_name")
    return cleaned


def _special_names(folder_map: FolderMap) -> set[str]:
    return {assignment.name for assignment in folder_map.roles.values()}


def create_folder(session: ImapSession, info: ServerInfo, folder_map: FolderMap, name: str, parent: str | None) -> str:
    """Create a folder (optionally below ``parent``).

    Allowed below every folder the server lets have children (no ``\\NoInferiors``), INBOX and special folders included,
    except the trash: subfolders there would blur the rules for deleting (D70).
    """
    cleaned = _validate_name(name)
    folders = {f.name: f for f in parse_folder_list(session.list_folders())}
    delimiter = next((f.delimiter for f in folders.values() if f.delimiter), "/")
    path = cleaned
    if parent:
        parent_folder = folders.get(parent)
        if parent_folder is None:
            raise GmxFolderNotFoundError("parent not found")
        if parent == folder_map.get(FolderRole.TRASH) or any(flag.lower() == "\\noinferiors" for flag in parent_folder.flags):
            raise GmxProtectedFolderError("no_subfolders")
        path = f"{parent}{delimiter}{cleaned}"
    if path in folders or path.upper() == "INBOX":
        raise GmxFolderExistsError("exists")
    session.create_folder(path)
    return path


def set_subscription(session: ImapSession, info: ServerInfo, path: str, *, subscribed: bool) -> None:
    """SUBSCRIBE or UNSUBSCRIBE an existing folder (only changes which folders mail clients show)."""
    if path not in {f.name for f in parse_folder_list(session.list_folders())}:
        raise GmxFolderNotFoundError("folder not found")
    if subscribed:
        session.subscribe_folder(path)
    else:
        session.unsubscribe_folder(path)


def rename_folder(session: ImapSession, info: ServerInfo, folder_map: FolderMap, path: str, new_name: str) -> str:
    """Rename a folder within its parent; special folders and INBOX cannot be renamed."""
    cleaned = _validate_name(new_name)
    folders = {f.name: f for f in parse_folder_list(session.list_folders())}
    current = folders.get(path)
    if current is None:
        raise GmxFolderNotFoundError("folder not found")
    if path.upper() == "INBOX" or path in _special_names(folder_map):
        raise GmxProtectedFolderError("special_folder")
    delimiter = current.delimiter
    new_path = f"{path.rsplit(delimiter, 1)[0]}{delimiter}{cleaned}" if delimiter and delimiter in path else cleaned
    if new_path in folders:
        raise GmxFolderExistsError("exists")
    session.rename_folder(path, new_path)
    return new_path


def _try_subscription(session: ImapSession, path: str, *, subscribed: bool) -> bool:
    """SUBSCRIBE/UNSUBSCRIBE without failing the surrounding action; connection losses still propagate."""
    try:
        if subscribed:
            session.subscribe_folder(path)
        else:
            session.unsubscribe_folder(path)
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError:
        return False
    return True


def create_folder_subscribed(
    session: ImapSession, info: ServerInfo, folder_map: FolderMap, name: str, parent: str | None
) -> tuple[str, list[str]]:
    """Create a folder and subscribe it (D44: GMX does not, GMX-NOTES row 45); warning ``subscribe_failed`` otherwise."""
    path = create_folder(session, info, folder_map, name, parent)
    return path, ([] if _try_subscription(session, path, subscribed=True) else ["subscribe_failed"])


def rename_folder_keep_subscription(
    session: ImapSession, info: ServerInfo, folder_map: FolderMap, path: str, new_name: str
) -> tuple[str, bool | None, list[str]]:
    """Rename a folder; a subscription moves to the new name (D44).

    Returns the new path, whether the new name is subscribed (None if unknown) and warnings ``subscription_unknown``,
    ``subscribe_failed``.
    """
    try:
        was_subscribed: bool | None = path in {f.name for f in parse_folder_list(session.list_sub_folders())}
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError:
        was_subscribed = None  # LSUB refused: rename without touching subscriptions
    new_path = rename_folder(session, info, folder_map, path, new_name)
    if was_subscribed is None:
        return new_path, None, ["subscription_unknown"]
    if not was_subscribed:
        return new_path, False, []
    subscribed = _try_subscription(session, new_path, subscribed=True)
    _try_subscription(session, path, subscribed=False)  # stale entry of the old name; servers may have moved it already
    return new_path, subscribed, [] if subscribed else ["subscribe_failed"]


# --------------------------------------------------------------------------- drafts


@dataclass(slots=True)
class DraftInput:
    """What ``save_draft`` stores."""

    sender: str
    to: Sequence[str] = ()
    cc: Sequence[str] = ()
    bcc: Sequence[str] = ()
    subject: str | None = None
    body_text: str = ""
    body_html: str | None = None
    in_reply_to: MailRef | None = None
    attachments: Sequence[Attachment] = ()


def parse_addresses(values: Sequence[str]) -> list[Address]:
    """Parse addresses; raises ValueError for anything that is not ``name <local@domain>`` or ``local@domain``."""
    result: list[Address] = []
    for value in values:
        if any(ch in value for ch in "\r\n\x00"):
            raise ValueError(value)
        parsed = getaddresses([value])
        if len(parsed) != 1:
            raise ValueError(value)
        name, addr = parsed[0]
        local, at, domain = addr.partition("@")
        if not at or not local or "." not in domain or " " in addr or any(not label for label in domain.split(".")):
            raise ValueError(value)
        try:
            result.append(Address(display_name=name, addr_spec=addr))
        except (HeaderParseError, IndexError) as err:  # the email library raises these for odd addresses (found by fuzzing)
            raise ValueError(value) from err
    return result


def build_draft(draft: DraftInput, reply_headers: Mapping[str, str | None] | None = None) -> EmailMessage:
    """Build the draft message (UTF-8, RFC 5322); headers are validated by the email policy."""
    message = EmailMessage(policy=SMTP)
    message["From"] = draft.sender
    for header, values in (("To", draft.to), ("Cc", draft.cc), ("Bcc", draft.bcc)):
        addresses = parse_addresses(values)
        if addresses:
            message[header] = ", ".join(formataddr((a.display_name, a.addr_spec)) for a in addresses)
    subject = draft.subject
    if reply_headers:
        original_subject = reply_headers.get("subject") or ""
        if subject is None:
            subject = original_subject if original_subject.lower().startswith(REPLY_PREFIXES) else f"Re: {original_subject}".strip()
        original_id = reply_headers.get("message-id")
        if original_id:
            message["In-Reply-To"] = original_id
            references = (reply_headers.get("references") or "").split()
            message["References"] = " ".join([*references[-10:], original_id])
    message["Subject"] = subject or ""
    message["Date"] = datetime.now(UTC)
    message["Message-ID"] = make_msgid(domain=draft.sender.rpartition("@")[2] or None)
    message.set_content(draft.body_text)
    if draft.body_html:
        message.add_alternative(draft.body_html, subtype="html")
    for item in draft.attachments:
        maintype, _, subtype = item.mime.partition("/")
        message.add_attachment(item.content, maintype=maintype, subtype=subtype or "octet-stream", filename=item.filename)
    return message


def save_draft(session: ImapSession, info: ServerInfo, folder_map: FolderMap, draft: DraftInput) -> tuple[dict[str, Any] | None, int]:
    """APPEND the draft with ``\\Draft`` and ``\\Seen``; returns (reference or None, size)."""
    drafts = folder_map.get(FolderRole.DRAFTS)
    if not drafts:
        raise GmxFolderNotFoundError("no drafts folder")
    reply_headers = None
    if draft.in_reply_to is not None:
        ref = draft.in_reply_to
        with open_folder(session, info, ref.folder, readonly=True) as response:
            if int(response.get(b"UIDVALIDITY", 0)) != ref.uidvalidity:
                raise GmxUidValidityError("UIDVALIDITY changed")
            data = session.fetch([ref.uid], [f"BODY.PEEK[HEADER.FIELDS ({' '.join([*SUMMARY_HEADERS, 'REFERENCES'])})]"]).get(ref.uid)
            if not data:
                raise GmxMailError("original message not found")
            original = parse_headers(_find_key(data, b"BODY[HEADER"))
            reply_headers = {key: _header_str(original, key, 2000) for key in ("subject", "message-id", "references")}
    message = build_draft(draft, reply_headers)
    raw = message.as_bytes()
    limit = info.append_limit
    if limit is not None and len(raw) > limit:
        raise GmxMessageTooLargeError("message_too_large")
    response = session.append(drafts, raw, flags=("\\Draft", "\\Seen"))
    appended = parse_appenduid(response) if isinstance(response, bytes | str) else None
    if appended is not None:
        return MailRef(drafts, appended[1], appended[0]).as_dict(), len(raw)
    found = _find_by_message_id(session, info, drafts, {MailRef(drafts, 0, 0): str(message["Message-ID"])})
    return next(iter(found.values()), None), len(raw)
