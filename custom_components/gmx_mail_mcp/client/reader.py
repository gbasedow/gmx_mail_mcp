"""Read-only IMAP operations (blocking; run through :meth:`ImapWorker.async_run` with ``readonly=True``).

Folders are opened with EXAMINE (read-only) and left with UNSELECT; message data is fetched with
``BODY.PEEK`` so the ``\\Seen`` flag is never set (SPEC section 2 and 3).
"""

from __future__ import annotations

import base64
import binascii
import quopri
import re
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.parser import BytesHeaderParser
from typing import Any, Literal, cast

from imapclient.exceptions import IMAPClientError, ProtocolError

from .errors import (
    GmxAttachmentTooLargeError,
    GmxFolderNotFoundError,
    GmxMessageNotFoundError,
    GmxPartNotFoundError,
    GmxSearchRejectedError,
    GmxUidValidityError,
)
from .folders import FolderMap, parse_folder_list
from .imap import CONNECTION_ERRORS, ImapSession, ServerInfo
from .sanitize import clean_inline, link_domain, sanitize_body, sanitize_html, strip_invisible
from .structure import PartInfo, attachments, find_text_parts, parse_bodystructure

SUMMARY_HEADERS = ("DATE", "FROM", "TO", "CC", "SUBJECT", "MESSAGE-ID")
SUMMARY_FETCH = f"BODY.PEEK[HEADER.FIELDS ({' '.join(SUMMARY_HEADERS)})]"
MAX_SUBJECT = 200
MAX_TO = 5
MAX_ADDRESS = 200
SNIPPET_CHARS = 160
SNIPPET_FETCH_BYTES = 2048
SNIPPET_FALLBACK_MAX_BYTES = 64 * 1024
MAX_TEXT_PART_BYTES = 2 * 1024 * 1024
CLIENT_SORT_MAX_UIDS = 2000
RECENT_WINDOW = timedelta(minutes=2)
RECENT_MAX = 50
MAX_HEADER_VALUE = 500  # standard headers (subject, message id, ...) and Authentication-Results
# Headers requested with ``headers``: shortened only with an explicit marker (value, truncated, total_chars,
# next_offset); the rest is fetched with ``header_offset``.
HEADER_REQUEST_MAX = 8000  # characters per header value
HEADER_REQUEST_BUDGET = 20000  # all requested headers together (List-Unsubscribe not counted)
LIST_UNSUBSCRIBE_MAX = 16000  # List-Unsubscribe(-Post): never shortened below this (emergency brake only)
_LIST_UNSUBSCRIBE_HEADERS = ("list-unsubscribe", "list-unsubscribe-post")
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
_ANGLE_RE = re.compile(r"<([^<>]*)>")

_PART_ID_RE = re.compile(r"^[1-9]\d{0,3}(?:\.[1-9]\d{0,3}){0,9}$")

type SortOrder = Literal["date_desc", "date_asc"]
type BodyFormat = Literal["text", "html_as_text", "html", "raw_headers"]


@dataclass(frozen=True, slots=True)
class MailRef:
    """Stable reference to a message."""

    folder: str
    uid: int
    uidvalidity: int

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON form."""
        return {"folder": self.folder, "uid": self.uid, "uidvalidity": self.uidvalidity}


@dataclass(slots=True)
class SearchQuery:
    """Search criteria; ``None`` means "not restricted"."""

    from_: str | None = None
    to: str | None = None
    cc: str | None = None
    subject: str | None = None
    text: str | None = None
    body: str | None = None
    since: Any = None  # datetime.date
    before: Any = None  # datetime.date
    seen: bool | None = None
    flagged: bool | None = None
    answered: bool | None = None
    draft: bool | None = None
    larger: int | None = None
    smaller: int | None = None
    keyword: str | None = None
    uid_range: str | None = None

    @property
    def header_terms(self) -> dict[str, str]:
        """Return the header text criteria that can also be checked on the client."""
        terms = {"from": self.from_, "to": self.to, "cc": self.cc, "subject": self.subject}
        return {k: v for k, v in terms.items() if v}

    @property
    def content_terms(self) -> dict[str, str]:
        """Return the criteria that need the server's full-text index."""
        terms = {"text": self.text, "body": self.body}
        return {k: v for k, v in terms.items() if v}

    def text_values(self) -> list[str]:
        """Return all free-text values."""
        return [*self.header_terms.values(), *self.content_terms.values()]

    def non_text_criteria(self) -> list[Any]:
        """Criteria that do not depend on the full-text index (flags, dates, sizes, UIDs)."""
        crit: list[Any] = ["UNDELETED"]
        for flag, value in (("SEEN", self.seen), ("FLAGGED", self.flagged), ("ANSWERED", self.answered), ("DRAFT", self.draft)):
            if value is not None:
                crit.append(flag if value else f"UN{flag}")
        if self.since is not None:
            crit += ["SINCE", self.since]
        if self.before is not None:
            crit += ["BEFORE", self.before]
        if self.larger is not None:
            crit += ["LARGER", self.larger]
        if self.smaller is not None:
            crit += ["SMALLER", self.smaller]
        if self.keyword:
            crit += ["KEYWORD", self.keyword]
        if self.uid_range:
            crit += ["UID", self.uid_range]
        return crit

    def criteria(self) -> list[Any]:
        """Return the full IMAP search criteria."""
        crit = self.non_text_criteria()
        for key, name in (("from", "FROM"), ("to", "TO"), ("cc", "CC"), ("subject", "SUBJECT")):
            value = self.header_terms.get(key)
            if value:
                crit += [name, value]
        for key, name in (("text", "TEXT"), ("body", "BODY")):
            value = self.content_terms.get(key)
            if value:
                crit += [name, value]
        return crit


@dataclass(slots=True)
class SearchResult:
    """One page of search results."""

    uidvalidity: int
    total: int
    messages: list[dict[str, Any]]
    search_mode: str
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- folder handling


@contextmanager
def examine(session: ImapSession, info: ServerInfo, folder: str) -> Iterator[int]:
    """Open ``folder`` read-only and yield its UIDVALIDITY; leave it with UNSELECT (never CLOSE)."""
    try:
        response = session.select_folder(folder, readonly=True)
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError as err:
        raise GmxFolderNotFoundError(f"folder cannot be opened: {type(err).__name__}") from err
    try:
        yield int(response.get(b"UIDVALIDITY", 0))
    finally:
        if info.has("UNSELECT"):
            # A failing UNSELECT on a read-only folder is harmless; the next SELECT replaces it.
            with suppress(IMAPClientError):
                session.unselect_folder()


def _check_uidvalidity(ref: MailRef, current: int) -> None:
    if current != ref.uidvalidity:
        raise GmxUidValidityError("UIDVALIDITY changed")


def list_folders(session: ImapSession, info: ServerInfo, folder_map: FolderMap, *, include_counts: bool) -> list[dict[str, Any]]:
    """Return all folders with their special role and optionally message counts."""
    folders = parse_folder_list(session.list_folders())
    roles = {assignment.name: role.value for role, assignment in folder_map.roles.items()}
    result: list[dict[str, Any]] = []
    for folder in folders:
        entry: dict[str, Any] = {
            "name": folder.leaf,
            "path": folder.name,
            "role": roles.get(folder.name),
            "delimiter": folder.delimiter,
            "flags": [f for f in folder.flags if f.lower() not in ("\\haschildren", "\\hasnochildren")],
            "selectable": folder.selectable,
        }
        if include_counts and folder.selectable:
            try:
                status = session.folder_status(folder.name, ["MESSAGES", "UNSEEN"])
            except CONNECTION_ERRORS:
                raise
            except IMAPClientError:
                entry["counts_unavailable"] = True  # one broken folder must not hide the others
            else:
                entry["messages"] = int(status.get(b"MESSAGES", 0))
                entry["unseen"] = int(status.get(b"UNSEEN", 0))
        result.append(entry)
    return result


def unseen_count(session: ImapSession, folder: str) -> int:
    """Return the number of unseen messages (STATUS, does not select the folder)."""
    try:
        status = session.folder_status(folder, ["UNSEEN"])
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError as err:
        raise GmxFolderNotFoundError("folder not found") from err
    return int(status.get(b"UNSEEN", 0))


# --------------------------------------------------------------------------- header helpers


def _find_key(data: Mapping[bytes, Any], prefix: bytes) -> Any:
    for key, value in data.items():
        if isinstance(key, bytes) and key.upper().startswith(prefix):
            return value
    return None


def parse_headers(raw: bytes | None) -> EmailMessage:
    """Parse a header block with the modern email policy (decodes RFC 2047)."""
    return BytesHeaderParser(policy=policy.default).parsebytes(raw or b"")


def _header_str(msg: EmailMessage, name: str, max_len: int = MAX_HEADER_VALUE) -> str | None:
    try:
        value = msg.get(name)
    except ValueError, IndexError, TypeError, AttributeError:  # malformed header in a hostile mail
        value = None
    if value is None:
        return None
    return clean_inline(str(value), max_len) or None


def _format_address(address: Address) -> str:
    name = clean_inline(address.display_name, 100)
    spec = clean_inline(address.addr_spec, MAX_ADDRESS)
    return f"{name} <{spec}>" if name and spec else (spec or name)


def _addresses(msg: EmailMessage, name: str) -> list[str]:
    try:
        header = msg.get(name)
        addresses = getattr(header, "addresses", None)
    except ValueError, IndexError, TypeError, AttributeError:
        addresses = None
    if addresses is None:
        raw = _header_str(msg, name)
        return [raw] if raw else []
    return [formatted for a in addresses if (formatted := _format_address(a))]


def _iso_date(msg: EmailMessage, internaldate: Any) -> str | None:
    try:
        header = msg.get("date")
        value = getattr(header, "datetime", None)
    except ValueError, IndexError, TypeError, AttributeError, OverflowError:
        value = None
    if not isinstance(value, datetime):
        value = internaldate if isinstance(internaldate, datetime) else None
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


# --------------------------------------------------------------------------- body decoding


def decode_transfer(data: bytes, encoding: str) -> bytes:
    """Undo a Content-Transfer-Encoding."""
    encoding = encoding.lower()
    if encoding == "base64":
        cleaned = re.sub(rb"[^A-Za-z0-9+/=]", b"", data)
        cleaned = cleaned[: len(cleaned) - len(cleaned) % 4]
        try:
            return base64.b64decode(cleaned)
        except binascii.Error, ValueError:
            return b""
    if encoding == "quoted-printable":
        return quopri.decodestring(data)
    return data


def decode_charset(data: bytes, charset: str | None) -> str:
    """Decode bytes with the declared charset, falling back to UTF-8 with replacement."""
    for candidate in (charset, "utf-8"):
        if not candidate or candidate.lower() in ("unknown-8bit", "x-unknown"):
            continue
        try:
            return data.decode(candidate, "replace")
        except LookupError:
            continue
    return data.decode("utf-8", "replace")


def _fetch_partial(session: ImapSession, uids: Sequence[int], part_id: str, max_bytes: int) -> dict[int, dict[bytes, Any]] | None:
    """FETCH the first ``max_bytes`` of a part; ``None`` if the response cannot be parsed.

    Some servers (seen with GreenMail) answer partial fetches without the space before the literal, which
    imapclient rejects with a ProtocolError. The connection stays usable; callers fall back to a full fetch.
    """
    try:
        return session.fetch(list(uids), [f"BODY.PEEK[{part_id}]<0.{max_bytes}>"])
    except ProtocolError:
        return None


def _fetch_part(session: ImapSession, uid: int, part_id: str, *, max_bytes: int | None = None) -> bytes:
    response = _fetch_partial(session, [uid], part_id, max_bytes) if max_bytes else None
    if response is None:
        response = session.fetch([uid], [f"BODY.PEEK[{part_id}]"])
    data = response.get(uid)
    if not data:
        raise GmxMessageNotFoundError("message vanished")
    raw = _find_key(data, f"BODY[{part_id}]".encode())
    if raw is None:
        raise GmxPartNotFoundError("part not returned by the server")
    raw = bytes(raw)
    return raw[:max_bytes] if max_bytes else raw


def _part_text(session: ImapSession, uid: int, part: PartInfo, max_bytes: int | None = None) -> str:
    raw = _fetch_part(session, uid, part.part_id, max_bytes=max_bytes)
    return decode_charset(decode_transfer(raw, part.encoding), part.charset)


# --------------------------------------------------------------------------- search


def _check_value(value: str) -> str:
    """Reject values imapclient cannot send (CR, LF, NUL) and trim them."""
    if any(ch in value for ch in "\r\n\x00"):
        raise GmxSearchRejectedError("line breaks are not allowed in search values")
    return value.strip()


def _run_search(
    session: ImapSession, info: ServerInfo, criteria: list[Any], charset: str | None, order: SortOrder
) -> tuple[list[int], list[str]]:
    """Return ordered UIDs (and warnings) using SORT when available."""
    warnings: list[str] = []
    try:
        if info.has("SORT"):
            sort_key = ["REVERSE", "DATE"] if order == "date_desc" else ["DATE"]
            return [int(u) for u in session.sort(sort_key, criteria, charset or "US-ASCII")], warnings
        uids = sorted(int(u) for u in session.search(criteria, charset))
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError as err:
        raise GmxSearchRejectedError(f"search rejected: {type(err).__name__}") from err
    if len(uids) > CLIENT_SORT_MAX_UIDS:
        warnings.append("sort_partial")
        uids = uids[-CLIENT_SORT_MAX_UIDS:]
    dates = session.fetch(uids, ["INTERNALDATE"]) if uids else {}
    epoch = datetime.min.replace(tzinfo=UTC)

    def _key(uid: int) -> tuple[datetime, int]:
        value = dates.get(uid, {}).get(b"INTERNALDATE")
        if isinstance(value, datetime):
            return (value if value.tzinfo else value.replace(tzinfo=UTC), uid)
        return (epoch, uid)

    return sorted(uids, key=_key, reverse=order == "date_desc"), warnings


def _header_matches(msg: EmailMessage, terms: Mapping[str, str]) -> bool:
    for key, needle in terms.items():
        haystack = " ".join(_addresses(msg, key)) if key in ("from", "to", "cc") else (_header_str(msg, key, 10_000) or "")
        if needle.casefold() not in haystack.casefold():
            return False
    return True


def _recent_matches(session: ImapSession, query: SearchQuery, found: set[int], now: datetime) -> tuple[list[int], bool]:
    """Index-lag fallback (GMX-NOTES row 25): check headers of messages from the last ~2 minutes on the client.

    Returns UIDs that match the header criteria but were missed by the server, and whether recent messages
    exist that could only be checked by the (lagging) full-text index.
    """
    criteria = [*query.non_text_criteria(), "SINCE", (now - timedelta(days=1)).date()]
    try:
        found_recent = session.search(criteria)
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError as err:
        raise GmxSearchRejectedError(f"search rejected: {type(err).__name__}") from err
    candidates = sorted(int(u) for u in found_recent)[-RECENT_MAX:]
    candidates = [uid for uid in candidates if uid not in found]
    if not candidates:
        return [], False
    data = session.fetch(candidates, ["INTERNALDATE", SUMMARY_FETCH])
    threshold = now - RECENT_WINDOW
    matches: list[tuple[datetime, int]] = []
    content_unchecked = False
    for uid in candidates:
        item = data.get(uid, {})
        internal = item.get(b"INTERNALDATE")
        if not isinstance(internal, datetime):
            continue
        internal = internal if internal.tzinfo else internal.replace(tzinfo=UTC)
        if internal < threshold:
            continue
        if query.content_terms:
            content_unchecked = True
            continue
        if _header_matches(parse_headers(_find_key(item, b"BODY[HEADER")), query.header_terms):
            matches.append((internal, uid))
    return [uid for _, uid in sorted(matches, reverse=True)], content_unchecked


def _snippets(session: ImapSession, snippet_parts: Mapping[int, PartInfo]) -> dict[int, str]:
    """Fetch the first bytes of each text part (one FETCH per distinct part id) and return short previews."""
    by_part: dict[str, list[int]] = {}
    for uid, part in snippet_parts.items():
        by_part.setdefault(part.part_id, []).append(uid)
    texts: dict[int, str] = {}
    for part_id, part_uids in by_part.items():
        fetched = _fetch_partial(session, part_uids, part_id, SNIPPET_FETCH_BYTES)
        if fetched is None:
            # Fallback: whole part, but only for small parts (no snippet for large newsletters).
            small = [uid for uid in part_uids if snippet_parts[uid].size <= SNIPPET_FALLBACK_MAX_BYTES]
            fetched = session.fetch(small, [f"BODY.PEEK[{part_id}]"]) if small else {}
        for uid in part_uids:
            raw = _find_key(fetched.get(uid, {}), f"BODY[{part_id}]".encode())
            if raw is None:
                continue
            part = snippet_parts[uid]
            text = decode_charset(decode_transfer(bytes(raw), part.encoding), part.charset)
            texts[uid] = clean_inline(sanitize_body(text, is_html=part.mime == "text/html").text, SNIPPET_CHARS)
    return texts


def _summaries(session: ImapSession, ref_folder: str, uidvalidity: int, uids: Sequence[int], *, snippet: bool) -> list[dict[str, Any]]:
    if not uids:
        return []
    data = session.fetch(list(uids), ["FLAGS", "RFC822.SIZE", "INTERNALDATE", "BODYSTRUCTURE", SUMMARY_FETCH])
    result: list[dict[str, Any]] = []
    snippet_parts: dict[int, PartInfo] = {}
    for uid in uids:
        item = data.get(uid)
        if item is None:  # expunged meanwhile
            continue
        msg = parse_headers(_find_key(item, b"BODY[HEADER"))
        parts = parse_bodystructure(item.get(b"BODYSTRUCTURE"))
        to = _addresses(msg, "to")
        flags = [f.decode("ascii", "replace") if isinstance(f, bytes) else str(f) for f in item.get(b"FLAGS", ())]
        entry: dict[str, Any] = {
            "ref": MailRef(ref_folder, uid, uidvalidity).as_dict(),
            "date": _iso_date(msg, item.get(b"INTERNALDATE")),
            "from": ", ".join(_addresses(msg, "from")) or None,
            "to": to[:MAX_TO],
            "subject": _header_str(msg, "subject", MAX_SUBJECT),
            "size": int(item.get(b"RFC822.SIZE", 0)),
            "flags": flags,
            "has_attachments": any(not p.embedded for p in attachments(parts)),
        }
        if len(to) > MAX_TO:
            entry["to_more"] = len(to) - MAX_TO
        result.append(entry)
        if snippet:
            plain, html = find_text_parts(parts)
            if plain or html:
                snippet_parts[uid] = cast(PartInfo, plain or html)
    if snippet_parts:
        texts = _snippets(session, snippet_parts)
        for entry in result:
            if entry["ref"]["uid"] in texts:
                entry["snippet"] = texts[entry["ref"]["uid"]]
    return result


def uid_in_range(uid: int, uid_range: str) -> bool:
    """Check ``uid`` against a UID set such as ``17:*`` or ``3,5:9`` as the caller means it.

    IMAP reads ``*`` as the highest UID in the folder, so ``17:*`` also matches the last message when every UID
    is below 17 (RFC 3501 6.4.8: ``17:*`` equals ``*:17``). A range with a number therefore only counts as open
    upwards here; a lone ``*`` (the last message) is left to the server.
    """
    for part in uid_range.split(","):
        if part == "*":
            return True
        low_text, _, high_text = part.partition(":")
        if not high_text:
            if uid == int(low_text):
                return True
            continue
        bounds = [int(b) for b in (low_text, high_text) if b != "*"]
        if not bounds:  # "*:*" is the last message, like "*"
            return True
        if len(bounds) == 1:
            if uid >= bounds[0]:
                return True
        elif min(bounds) <= uid <= max(bounds):
            return True
    return False


def search(
    session: ImapSession,
    info: ServerInfo,
    folder: str,
    query: SearchQuery,
    *,
    order: SortOrder,
    limit: int,
    offset: int,
    snippet: bool,
    now: datetime | None = None,
    expected_uidvalidity: int | None = None,
) -> SearchResult:
    """Search ``folder`` and return one page of message summaries."""
    for attr in ("from_", "to", "cc", "subject", "text", "body", "keyword"):
        value = getattr(query, attr)
        if value is not None:
            setattr(query, attr, _check_value(value) or None)
    charset = "UTF-8" if any(not v.isascii() for v in query.text_values()) else None
    now = now or datetime.now(UTC)
    with examine(session, info, folder) as uidvalidity:
        if expected_uidvalidity is not None and expected_uidvalidity != uidvalidity:
            raise GmxUidValidityError("UIDVALIDITY changed since the first page")
        uids, warnings = _run_search(session, info, query.criteria(), charset, order)
        mode = "server"
        if query.text_values():
            extra, content_unchecked = _recent_matches(session, query, set(uids), now)
            if extra:
                mode = "server+recent_headers"
                uids = [*extra, *uids] if order == "date_desc" else [*uids, *reversed(extra)]
            if content_unchecked:
                warnings.append("index_lag")
        if query.uid_range:
            uids = [uid for uid in uids if uid_in_range(uid, query.uid_range)]
        page = uids[offset : offset + limit]
        messages = _summaries(session, folder, uidvalidity, page, snippet=snippet)
    return SearchResult(uidvalidity=uidvalidity, total=len(uids), messages=messages, search_mode=mode, warnings=warnings)


# --------------------------------------------------------------------------- single message


@dataclass(slots=True)
class MessageContent:
    """Everything ``get_message`` needs before framing and pagination."""

    headers: dict[str, Any]
    text: str
    body_format: str
    attachments: list[dict[str, Any]]
    flags: list[str]
    size: int
    warnings: list[str]
    auth_results: list[str]
    list_unsubscribe: dict[str, Any] | None


def _load_message(session: ImapSession, ref: MailRef, items: Sequence[str]) -> dict[bytes, Any]:
    data = session.fetch([ref.uid], list(items)).get(ref.uid)
    if not data or b"BODYSTRUCTURE" not in data:
        raise GmxMessageNotFoundError("no such message")
    return data


def unfold_header(value: str) -> str:
    """Unfold a header value (RFC 5322 2.2.3: line breaks removed, the whitespace after them kept).

    Invisible and control characters are removed as everywhere else; nothing is decoded (``%0A`` stays ``%0A``),
    whitespace is not collapsed and nothing is shortened.
    """
    return strip_invisible(_LINE_BREAK_RE.sub("", value).replace("\t", " ")).strip()


def header_entry(value: str, offset: int, limit: int) -> str | dict[str, Any]:
    """Return a header value, or - if it is cut or read from an offset - an explicitly marked part of it."""
    total = len(value)
    chunk = value[offset : offset + max(0, limit)]
    end = min(offset, total) + len(chunk)
    if offset == 0 and end >= total:
        return value
    return {
        "value": chunk,
        "truncated": end < total,
        "total_chars": total,
        "offset": offset,
        "next_offset": end if end < total else None,
    }


def parse_list_unsubscribe(values: Sequence[str], post_values: Sequence[str], *, full: bool) -> dict[str, Any]:
    """Split List-Unsubscribe (RFC 2369) into its entries; full addresses only if ``full`` (explicitly requested).

    Entries are the ``<...>`` parts, separated by commas, also across folded lines. Whitespace inside the angle
    brackets is ignored (RFC 2369 section 2); the addresses are neither decoded nor called.
    """
    joined = ", ".join(unfold_header(v) for v in values)
    truncated = len(joined) > LIST_UNSUBSCRIBE_MAX
    if truncated:
        joined = joined[:LIST_UNSUBSCRIBE_MAX]  # an entry cut in the middle has no ">" and is dropped
    targets = [t for t in ("".join(m.split()) for m in _ANGLE_RE.findall(joined)) if t]
    one_click = any(unfold_header(v).lower() == "list-unsubscribe=one-click" for v in post_values)
    result: dict[str, Any] = {"domains": sorted({link_domain(t) for t in targets}), "one_click": one_click}
    if full:
        result["urls"] = [t for t in targets if t.lower().startswith(("https://", "http://"))]
        result["mailto"] = [t for t in targets if t.lower().startswith("mailto:")]
    if truncated:
        result["truncated"] = True
    return result


def requested_headers(msg: EmailMessage, names: Sequence[str], offset: int) -> dict[str, list[str | dict[str, Any]]]:
    """Values of the headers requested with ``headers`` (lower-case names, each occurrence one entry).

    List-Unsubscribe(-Post) up to LIST_UNSUBSCRIBE_MAX characters, others up to HEADER_REQUEST_MAX each and
    HEADER_REQUEST_BUDGET together; anything beyond is marked, never cut silently.
    """
    unique = list(dict.fromkeys(name.lower() for name in names))
    unique.sort(key=lambda name: name not in _LIST_UNSUBSCRIBE_HEADERS)  # never cut by the shared budget
    budget = HEADER_REQUEST_BUDGET
    result: dict[str, list[str | dict[str, Any]]] = {}
    for name in unique:
        entries: list[str | dict[str, Any]] = []
        for raw in msg.get_all(name) or []:
            value = unfold_header(str(raw))
            if name in _LIST_UNSUBSCRIBE_HEADERS:
                entries.append(header_entry(value, offset, LIST_UNSUBSCRIBE_MAX))
                continue
            entry = header_entry(value, offset, min(HEADER_REQUEST_MAX, budget))
            budget -= len(entry) if isinstance(entry, str) else len(entry["value"])
            entries.append(entry)
        if entries:
            result[name] = entries
    return result


def get_message(
    session: ImapSession,
    info: ServerInfo,
    ref: MailRef,
    *,
    body_format: BodyFormat,
    extra_headers: Sequence[str] = (),
    header_offset: int = 0,
) -> MessageContent:
    """Load headers, the readable text and the attachment list of one message."""
    with examine(session, info, ref.folder) as uidvalidity:
        _check_uidvalidity(ref, uidvalidity)
        data = _load_message(session, ref, ["FLAGS", "RFC822.SIZE", "INTERNALDATE", "BODYSTRUCTURE", "BODY.PEEK[HEADER]"])
        raw_header = _find_key(data, b"BODY[HEADER")
        msg = parse_headers(raw_header)
        parts = parse_bodystructure(data.get(b"BODYSTRUCTURE"))
        plain, html = find_text_parts(parts)
        warnings: list[str] = []

        if body_format == "raw_headers":
            sanitized = sanitize_body(decode_charset(bytes(raw_header or b""), "utf-8"), is_html=False)
            body_text, chosen = sanitized.text, "raw_headers"
            warnings += sanitized.warnings
        else:
            part = (html or plain) if body_format in ("html_as_text", "html") else (plain or html)
            if part is None:
                body_text, chosen = "", "none"
                warnings.append("no_text_body")
            else:
                limit = MAX_TEXT_PART_BYTES if part.size > MAX_TEXT_PART_BYTES else None
                if limit:
                    warnings.append("body_truncated_at_source")
                raw_text = _part_text(session, ref.uid, part, max_bytes=limit)
                if body_format == "html" and part.mime == "text/html":
                    cleaned = sanitize_html(raw_text)
                    body_text, chosen = cleaned.html, "html"
                    warnings += cleaned.warnings
                else:
                    if body_format == "html":
                        warnings.append("no_html_part")  # plain-text-only mail: returned as text
                    sanitized = sanitize_body(raw_text, is_html=part.mime == "text/html")
                    body_text = sanitized.text
                    chosen = "html_as_text" if part.mime == "text/html" else "text"
                    warnings += sanitized.warnings

    headers: dict[str, Any] = {
        "from": _addresses(msg, "from"),
        "to": _addresses(msg, "to"),
        "cc": _addresses(msg, "cc"),
        "date": _iso_date(msg, data.get(b"INTERNALDATE")),
        "subject": _header_str(msg, "subject"),
        "message_id": _header_str(msg, "message-id"),
        "in_reply_to": _header_str(msg, "in-reply-to"),
    }
    reply_to = _addresses(msg, "reply-to")
    if reply_to:
        # Observation only (SPEC section 5, measure 7): a different Reply-To is a classic phishing trick.
        headers["reply_to"] = reply_to
    headers.update(requested_headers(msg, extra_headers, header_offset))
    auth = [clean_inline(str(v), MAX_HEADER_VALUE) for v in (msg.get_all("authentication-results") or [])][:3]
    unsubscribe = [str(v) for v in msg.get_all("list-unsubscribe") or []]
    list_unsubscribe = None
    if unsubscribe:
        # Full addresses only when the caller asked for the header itself (it then gets the raw value anyway);
        # otherwise domains only, as for links in the text (SPEC section 5, DECISIONS no. 79).
        full = any(name.lower() == "list-unsubscribe" for name in extra_headers)
        post = [str(v) for v in msg.get_all("list-unsubscribe-post") or []]
        list_unsubscribe = parse_list_unsubscribe(unsubscribe, post, full=full)

    attachment_list = [
        {"part_id": p.part_id, "filename": p.filename, "mime": p.mime, "size": p.size, "inline": p.inline} for p in attachments(parts)
    ]
    flags = [f.decode("ascii", "replace") if isinstance(f, bytes) else str(f) for f in data.get(b"FLAGS", ())]
    return MessageContent(
        headers=headers,
        text=body_text,
        body_format=chosen,
        attachments=attachment_list,
        flags=flags,
        size=int(data.get(b"RFC822.SIZE", 0)),
        warnings=warnings,
        auth_results=auth,
        list_unsubscribe=list_unsubscribe,
    )


def get_attachment(session: ImapSession, info: ServerInfo, ref: MailRef, part_id: str, *, max_bytes: int) -> tuple[PartInfo, bytes]:
    """Return a decoded message part. The size is checked before and after downloading."""
    if not _PART_ID_RE.match(part_id):
        raise GmxPartNotFoundError("invalid part id")
    with examine(session, info, ref.folder) as uidvalidity:
        _check_uidvalidity(ref, uidvalidity)
        data = _load_message(session, ref, ["BODYSTRUCTURE"])
        part = next((p for p in parse_bodystructure(data.get(b"BODYSTRUCTURE")) if p.part_id == part_id), None)
        if part is None:
            raise GmxPartNotFoundError("no such part")
        # base64 inflates by 4/3; allow some slack for line breaks before refusing without download.
        if part.size > max_bytes * 4 // 3 + 4096:
            raise GmxAttachmentTooLargeError("attachment too large")
        content = decode_transfer(_fetch_part(session, ref.uid, part_id), part.encoding)
    if len(content) > max_bytes:
        raise GmxAttachmentTooLargeError("attachment too large")
    return part, content


# --------------------------------------------------------------------------- polling (coordinator, M3)


@dataclass(slots=True)
class PollResult:
    """Counters and newly arrived messages of one poll."""

    unseen_inbox: int
    inbox_messages: int
    junk_messages: int | None
    uidvalidity: int
    last_uid: int
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    skipped_new: int = 0
    uidvalidity_changed: bool = False


def _status(session: ImapSession, folder: str, items: list[str]) -> dict[bytes, Any] | None:
    try:
        return session.folder_status(folder, items)
    except CONNECTION_ERRORS:
        raise
    except IMAPClientError:
        return None


def poll(
    session: ImapSession,
    info: ServerInfo,
    *,
    inbox: str,
    junk: str | None,
    last_uid: int | None,
    known_uidvalidity: int | None,
    max_events: int,
    include_headers: bool,
) -> PollResult:
    """Read counters with STATUS and find messages newer than ``last_uid`` (blocking).

    * first run (``last_uid`` is None) or a changed UIDVALIDITY only sets the baseline - no events;
    * at most ``max_events`` of the newest new messages are returned; older ones are only counted.
    """
    status = _status(session, inbox, ["MESSAGES", "UNSEEN", "UIDNEXT", "UIDVALIDITY"])
    if status is None:
        raise GmxFolderNotFoundError("inbox not found")
    uidvalidity = int(status.get(b"UIDVALIDITY", 0))
    uidnext = status.get(b"UIDNEXT")
    junk_status = _status(session, junk, ["MESSAGES"]) if junk else None
    result = PollResult(
        unseen_inbox=int(status.get(b"UNSEEN", 0)),
        inbox_messages=int(status.get(b"MESSAGES", 0)),
        junk_messages=int(junk_status.get(b"MESSAGES", 0)) if junk_status is not None else None,
        uidvalidity=uidvalidity,
        last_uid=last_uid or 0,
    )
    changed = known_uidvalidity is not None and known_uidvalidity != uidvalidity
    result.uidvalidity_changed = changed
    baseline = last_uid is None or changed
    if uidnext is not None:
        if baseline:
            result.last_uid = int(uidnext) - 1  # baseline without searching the whole inbox
            return result
        if int(uidnext) - 1 <= (last_uid or 0):
            return result  # nothing new (cheap path: no SELECT)

    with examine(session, info, inbox) as current_validity:
        uids = sorted(int(u) for u in session.search(["UID", f"{(0 if baseline else last_uid or 0) + 1}:*"]))
        # "n:*" always matches the highest UID, even if it is below n - filter explicitly.
        newer = [uid for uid in uids if baseline or uid > (last_uid or 0)]
        if not newer:
            if baseline:
                result.last_uid = 0
            return result
        result.last_uid = max(newer)
        result.uidvalidity = current_validity
        if baseline:
            return result
        announced = newer[-max_events:]
        result.skipped_new = len(newer) - len(announced)
        items = ["INTERNALDATE"] + ([SUMMARY_FETCH] if include_headers else [])
        data = session.fetch(announced, items)
        for uid in announced:
            entry: dict[str, Any] = {"ref": MailRef(inbox, uid, current_validity).as_dict()}
            if include_headers:
                msg = parse_headers(_find_key(data.get(uid, {}), b"BODY[HEADER"))
                entry["from"] = ", ".join(_addresses(msg, "from")) or None
                entry["subject"] = _header_str(msg, "subject", MAX_SUBJECT)
            result.new_messages.append(entry)
    return result
