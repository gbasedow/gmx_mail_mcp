"""Parse IMAP BODYSTRUCTURE responses (RFC 3501 section 7.4.2) into a flat part list.

Only the structure is read here; part contents are fetched separately with ``BODY.PEEK[<part>]`` so that
attachments are never downloaded just to show the text of a message. Embedded ``message/rfc822`` parts are
treated as attachments (their inner parts are not listed).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from email.header import decode_header, make_header
from typing import Any
from urllib.parse import unquote_to_bytes

from .sanitize import clean_inline


@dataclass(frozen=True, slots=True)
class PartInfo:
    """One leaf part of a message."""

    part_id: str
    mime: str
    size: int
    encoding: str
    charset: str | None
    filename: str | None
    disposition: str | None
    content_id: str | None

    @property
    def is_attachment(self) -> bool:
        """Return whether the part is an attachment rather than message text."""
        if self.disposition == "attachment":
            return True
        if self.mime in ("text/plain", "text/html") and self.disposition in (None, "inline"):
            return self.filename is not None and self.disposition != "inline"
        return True

    @property
    def size_estimated(self) -> int:
        """Approximate size of the decoded file; ``size`` is the transfer-encoded size from BODYSTRUCTURE.

        base64 (the usual case): lines of 76 characters plus CRLF, 4 characters per 3 bytes - exact for the usual
        line length, slightly low for base64 without line breaks. quoted-printable: the encoded size (an upper bound).
        """
        if self.encoding != "base64" or self.size <= 0:
            return self.size
        lines = -(-self.size // 78)  # ceiling division
        return max(0, self.size - 2 * lines) * 3 // 4

    @property
    def inline(self) -> bool:
        """Return whether the part is marked inline (e.g. an embedded image)."""
        return self.disposition == "inline" or (self.content_id is not None and self.disposition is None)

    @property
    def embedded(self) -> bool:
        """Return whether the part is an inline image shown inside the HTML text (not a file for the reader).

        Only images count: some senders (seen with GMX system mails) mark PDF attachments as inline.
        """
        return self.inline and self.mime.startswith("image/")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _params(raw: Any) -> dict[str, str]:
    """Turn ``(b'CHARSET', b'UTF-8', b'NAME', b'x')`` into a dict, decoding RFC 2231 and RFC 2047 values."""
    if not isinstance(raw, Sequence) or isinstance(raw, bytes | str):
        return {}
    items = [_text(v) or "" for v in raw]
    plain: dict[str, str] = {}
    extended: dict[str, str] = {}  # name* (single encoded value)
    chunks: dict[str, list[tuple[int, bool, str]]] = {}  # name*0, name*1* ... (RFC 2231 section 3)
    for raw_key, value in zip(items[0::2], items[1::2], strict=False):
        key = raw_key.lower()
        base, star, rest = key.partition("*")
        if not star:
            plain[key] = value
        elif rest == "":
            extended[base] = value
        elif rest.rstrip("*").isdigit():
            chunks.setdefault(base, []).append((int(rest.rstrip("*")), rest.endswith("*"), value))
    decoded = {key: _decode_words(value) for key, value in plain.items()}
    for key, value in extended.items():
        decoded[key] = _decode_rfc2231(value)
    for key, parts in chunks.items():
        decoded[key] = _join_continuations(sorted(parts))
    return decoded


def _split_charset(value: str) -> tuple[str, str]:
    """Split ``charset'language'text``; returns ``("", value)`` if there is no charset prefix."""
    charset, sep, rest = value.partition("'")
    if sep:
        _language, sep2, text = rest.partition("'")
        if sep2:
            return charset, text
    return "", value


def _decode_bytes(raw: bytes, charset: str) -> str:
    try:
        return raw.decode(charset or "utf-8", "replace")
    except LookupError:
        return raw.decode("utf-8", "replace")


def _decode_rfc2231(value: str) -> str:
    """Decode an RFC 2231 extended value ``charset'language'percent-encoded-text``."""
    charset, text = _split_charset(value)
    return _decode_bytes(unquote_to_bytes(text), charset)


def _join_continuations(parts: list[tuple[int, bool, str]]) -> str:
    """Join ``name*0*``/``name*1`` segments; only segments marked with ``*`` are percent-encoded."""
    charset = ""
    raw = b""
    for index, (_number, encoded, value) in enumerate(parts):
        if encoded:
            text = value
            if index == 0:
                charset, text = _split_charset(value)
            raw += unquote_to_bytes(text)
        else:
            raw += value.encode("utf-8")
    return _decode_bytes(raw, charset)


def _decode_words(value: str) -> str:
    """Decode RFC 2047 encoded words (some clients use them in filenames)."""
    if "=?" not in value:
        return value
    try:
        return str(make_header(decode_header(value)))
    except LookupError, ValueError, UnicodeError:
        return value


def _disposition(raw: Any) -> tuple[str | None, dict[str, str]]:
    if not isinstance(raw, Sequence) or isinstance(raw, bytes | str) or not raw:
        return None, {}
    kind = (_text(raw[0]) or "").lower() or None
    return kind, _params(raw[1]) if len(raw) > 1 else {}


def _leaf(body: Sequence[Any], part_id: str) -> PartInfo:
    maintype = (_text(body[0]) or "application").lower()
    subtype = (_text(body[1]) or "octet-stream").lower()
    params = _params(body[2]) if len(body) > 2 else {}
    content_id = _text(body[3]) if len(body) > 3 else None
    encoding = (_text(body[5]) or "7bit").lower() if len(body) > 5 else "7bit"
    try:
        size = int(body[6]) if len(body) > 6 and body[6] is not None else 0
    except TypeError, ValueError:
        size = 0
    # Position of the disposition depends on the type (RFC 3501 body-ext-1part).
    if maintype == "text":
        disp_index = 9
    elif maintype == "message" and subtype == "rfc822":
        disp_index = 11
    else:
        disp_index = 8
    disposition, disp_params = _disposition(body[disp_index]) if len(body) > disp_index else (None, {})
    filename = disp_params.get("filename") or params.get("name")
    return PartInfo(
        part_id=part_id,
        mime=f"{maintype}/{subtype}",
        size=size,
        encoding=encoding,
        charset=params.get("charset"),
        filename=clean_inline(filename, 255) if filename else None,
        disposition=disposition,
        content_id=content_id.strip("<>") if content_id else None,
    )


MAX_NESTING = 20  # deeper multipart nesting is not plausible for real mail; ignore it instead of recursing


def parse_bodystructure(body: Any, prefix: str = "") -> list[PartInfo]:
    """Return the leaf parts of a BODYSTRUCTURE in document order with IMAP part numbers.

    Malformed structures (empty, wrong types, absurd nesting) yield fewer parts instead of an exception.
    """
    if not isinstance(body, Sequence) or isinstance(body, bytes | str) or not body or prefix.count(".") >= MAX_NESTING:
        return []
    if isinstance(body[0], list):  # multipart: [subparts], subtype, ...
        parts: list[PartInfo] = []
        for index, sub in enumerate(body[0], start=1):
            parts.extend(parse_bodystructure(sub, f"{prefix}{index}."))
        return parts
    if len(body) < 2:
        return []
    return [_leaf(body, prefix.rstrip(".") or "1")]


def find_text_parts(parts: Sequence[PartInfo]) -> tuple[PartInfo | None, PartInfo | None]:
    """Return the first non-attachment text/plain and text/html parts."""
    plain = next((p for p in parts if p.mime == "text/plain" and not p.is_attachment), None)
    html = next((p for p in parts if p.mime == "text/html" and not p.is_attachment), None)
    return plain, html


def attachments(parts: Sequence[PartInfo]) -> list[PartInfo]:
    """Return all parts that are not the message text."""
    plain, html = find_text_parts(parts)
    return [p for p in parts if p is not plain and p is not html and p.is_attachment]
