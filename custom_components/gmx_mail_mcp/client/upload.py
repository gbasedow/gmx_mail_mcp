"""Files that come with a tool call (D17): base64 content, checked name, type and SHA-256 - no Home Assistant imports.

Used by ``save_draft`` (M3) and later by the sending tools (M5). Rules (SPEC section 5):

* the content arrives as base64 together with its SHA-256, so a file the model garbled on the way is refused;
* the file name is reduced to a plain name (no path, no control or invisible characters, bounded length);
* the type is taken from the content where it can be recognised (PDF, images, Office files); the declared type is only
  used for content that cannot be recognised, and a contradiction is reported as ``type_mismatch``;
* size limits apply per file and for all files together, before and after decoding.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from ..extract import MIME_OCTET, sniff_mime
from .sanitize import strip_invisible

MAX_FILENAME: Final = 150
MAX_FILES: Final = 10
DEFAULT_FILENAME: Final = "anhang"
_MIME_RE: Final = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}$")
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_UNSAFE_NAME_RE: Final = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\/]')
# Types recognised from the content are authoritative; for these two the content says little.
_GENERIC: Final = frozenset({MIME_OCTET, "text/plain"})


@dataclass(slots=True)
class Attachment:
    """A file to attach."""

    filename: str
    content: bytes
    mime: str = ""

    def __post_init__(self) -> None:
        """Guess the MIME type from the file name if none was given."""
        if not self.mime:
            self.mime = mimetypes.guess_type(self.filename)[0] or MIME_OCTET


class UploadError(ValueError):
    """A file from a tool call is refused; ``code`` is a stable reason, ``index`` the position in the list."""

    def __init__(self, code: str, index: int) -> None:
        """Remember the reason (no content, no file name: both come from the model)."""
        super().__init__(f"{code} (attachment {index})")
        self.code = code
        self.index = index


@dataclass(frozen=True, slots=True)
class Upload:
    """One file as given in a tool call (still base64)."""

    filename: str
    content_base64: str
    sha256: str
    mime: str | None = None


def clean_filename(name: str) -> str:
    """Plain file name: last path component, no control/invisible/reserved characters, at most ``MAX_FILENAME`` chars."""
    leaf = re.split(r"[\\/]", name)[-1]
    text = _UNSAFE_NAME_RE.sub("_", strip_invisible(leaf))
    text = " ".join(text.split()).strip(" .")
    if not text:
        return DEFAULT_FILENAME
    if len(text) > MAX_FILENAME:
        stem, dot, ext = text.rpartition(".")
        keep = f"{dot}{ext}" if dot and 0 < len(ext) <= 10 else ""
        text = (stem if keep else text)[: MAX_FILENAME - len(keep)].rstrip(" .") + keep
    return text


def _decode_base64(text: str, max_bytes: int, index: int) -> bytes:
    compact = "".join(text.split())
    if len(compact) > (max_bytes + 2) // 3 * 4:
        raise UploadError("attachment_too_large", index)  # checked before decoding: no large allocation
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as err:
        raise UploadError("invalid_base64", index) from err


def resolve_mime(content: bytes, declared: str | None, filename: str) -> tuple[str, bool]:
    """Return ``(mime, mismatch)``: recognised content wins; otherwise the declared type, then the file name."""
    declared_clean = (declared or "").strip().lower()
    if declared_clean and not _MIME_RE.match(declared_clean):
        declared_clean = ""
    detected = sniff_mime(content, declared_clean or None)
    if detected not in _GENERIC:
        return detected, bool(declared_clean) and declared_clean != detected and declared_clean not in _GENERIC
    guessed = mimetypes.guess_type(filename)[0]
    if declared_clean:
        return declared_clean, False
    return guessed or detected, False


def decode_uploads(uploads: Sequence[Upload], *, max_bytes: int) -> tuple[list[Attachment], list[str]]:
    """Decode and check all files; ``max_bytes`` applies per file and to the sum. Returns attachments and warnings."""
    if len(uploads) > MAX_FILES:
        raise UploadError("too_many_attachments", len(uploads) - 1)
    result: list[Attachment] = []
    warnings: list[str] = []
    total = 0
    for index, upload in enumerate(uploads):
        if not _SHA256_RE.match(upload.sha256.strip().lower()):
            raise UploadError("invalid_sha256", index)
        content = _decode_base64(upload.content_base64, max_bytes, index)
        if len(content) > max_bytes:
            raise UploadError("attachment_too_large", index)
        total += len(content)
        if total > max_bytes:
            raise UploadError("attachments_too_large", index)
        if hashlib.sha256(content).hexdigest() != upload.sha256.strip().lower():
            raise UploadError("sha256_mismatch", index)
        filename = clean_filename(upload.filename)
        mime, mismatch = resolve_mime(content, upload.mime, filename)
        if mismatch and "type_mismatch" not in warnings:
            warnings.append("type_mismatch")
        result.append(Attachment(filename=filename, content=content, mime=mime))
    return result, warnings
