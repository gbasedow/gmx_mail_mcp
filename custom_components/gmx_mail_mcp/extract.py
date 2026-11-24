"""Attachment type detection and text extraction (D10, D27). Blocking; no Home Assistant imports.

Attachments are untrusted. The type is taken from the content, never from the declared MIME type or the file name.
Nothing is executed: PDF via ``pypdf`` (text only), DOCX/XLSX parsed as ZIP + XML with the standard library. Limits guard
against ZIP bombs, entity tricks and very long documents; the extracted text goes through the same sanitizer as mail text.
"""

from __future__ import annotations

import io
import logging
import re
import zipfile
from dataclasses import dataclass, field
from typing import Final
from xml.etree import ElementTree as ET

from .client.sanitize import sanitize_body

_LOGGER = logging.getLogger(__name__)

MIME_PDF: Final = "application/pdf"
MIME_DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_ZIP: Final = "application/zip"
MIME_OCTET: Final = "application/octet-stream"
IMAGE_TYPES: Final = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})

MAX_TEXT_CHARS: Final = 2_000_000  # extracted text before paging; beyond: truncated
MAX_PDF_PAGES: Final = 500
MAX_ZIP_MEMBERS: Final = 5_000
MAX_ZIP_TOTAL: Final = 100 * 1024 * 1024  # uncompressed bytes of all members read
MAX_ZIP_MEMBER: Final = 30 * 1024 * 1024  # uncompressed bytes of one XML member
MAX_ZIP_RATIO: Final = 200  # uncompressed : compressed per member
MAX_XLSX_CELLS: Final = 500_000

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class ExtractionError(Exception):
    """The attachment cannot be turned into text; ``code`` is a stable reason."""

    def __init__(self, code: str) -> None:
        """Store the reason."""
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class Extracted:
    """Sanitized text and what happened on the way."""

    text: str
    kind: str  # pdf, docx, xlsx, text, html
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- type detection


def _zip_kind(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile, ValueError:
        return MIME_OCTET
    if "word/document.xml" in names:
        return MIME_DOCX
    if "xl/workbook.xml" in names:
        return MIME_XLSX
    return MIME_ZIP


def _looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError as err:
        # A multi-byte character cut at the sample end is fine; anything else is not UTF-8.
        if err.start < len(sample) - 3:
            return _mostly_printable_latin1(sample)
    return True


def _mostly_printable_latin1(sample: bytes) -> bool:
    text = sample.decode("latin-1")
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\r\n\t")
    return printable >= 0.95 * len(text)


_SIGNATURES: Final = (
    (b"%PDF-", MIME_PDF),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(data: bytes, declared: str | None = None) -> str:
    """MIME type from the content. ``declared`` only refines text (e.g. text/html, text/csv)."""
    mime = next((kind for magic, kind in _SIGNATURES if data.startswith(magic)), None)
    if mime is None and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    if mime is None and data.startswith(b"PK\x03\x04"):
        mime = _zip_kind(data)
    if mime is not None:
        return mime
    if not data or not _looks_like_text(data):
        return MIME_OCTET
    declared = (declared or "").lower()
    if declared.startswith("text/") or declared in ("application/json", "application/xml"):
        return declared
    return "text/plain"


# --------------------------------------------------------------------------- extraction


def extract_text(data: bytes, mime: str, charset: str | None = None) -> Extracted:
    """Return sanitized text for ``mime`` (the sniffed type) or raise :class:`ExtractionError`."""
    try:
        return _extract(data, mime, charset)
    except ExtractionError:
        raise
    except Exception as err:
        # Parsers of untrusted files fail in many ways (zlib.error, EOFError, RuntimeError for encrypted ZIPs, ...);
        # every one of them means "cannot extract". Only the type is logged, never content.
        _LOGGER.debug("Text extraction failed: %s", type(err).__name__)
        raise ExtractionError("extraction_failed") from err


def _extract(data: bytes, mime: str, charset: str | None) -> Extracted:
    warnings: list[str] = []
    if mime == MIME_PDF:
        text, warnings = _pdf_text(data)
        kind = "pdf"
    elif mime == MIME_DOCX:
        text, kind = _docx_text(data), "docx"
    elif mime == MIME_XLSX:
        text, kind = _xlsx_text(data), "xlsx"
    elif mime.startswith("text/") or mime in ("application/json", "application/xml"):
        text = _decode(data, charset)
        kind = "html" if mime == "text/html" else "text"
    else:
        raise ExtractionError("unsupported_type")
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
        warnings.append("text_truncated_at_source")
    sanitized = sanitize_body(text, is_html=kind == "html")
    warnings += sanitized.warnings
    if not sanitized.text.strip():
        warnings.append("no_text_found")  # e.g. a scanned PDF (images only)
    return Extracted(text=sanitized.text, kind=kind, warnings=sorted(set(warnings)))


def _decode(data: bytes, charset: str | None) -> str:
    for candidate in (charset, "utf-8"):
        if not candidate:
            continue
        try:
            return data.decode(candidate)
        except LookupError, UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _pdf_text(data: bytes) -> tuple[str, list[str]]:
    # Imported lazily: only needed when a PDF is actually extracted.
    from pypdf import PdfReader  # noqa: PLC0415
    from pypdf.errors import FileNotDecryptedError, PdfReadError  # noqa: PLC0415

    warnings: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ExtractionError("pdf_encrypted")
        parts: list[str] = []
        total = 0
        has_text = False
        for number, page in enumerate(reader.pages, start=1):
            if number > MAX_PDF_PAGES:
                warnings.append("pdf_pages_truncated")
                break
            page_text = page.extract_text() or ""
            parts.append(f"[page {number}]\n{page_text}")
            total += len(page_text)
            has_text = has_text or bool(page_text.strip())
            if total > MAX_TEXT_CHARS:
                warnings.append("text_truncated_at_source")
                break
        if not has_text:
            warnings.append("no_text_found")  # only page markers: e.g. a scanned PDF (images only)
    except FileNotDecryptedError as err:
        raise ExtractionError("pdf_encrypted") from err
    except PdfReadError as err:
        raise ExtractionError("extraction_failed") from err
    return "\n\n".join(parts), warnings


def _read_member(archive: zipfile.ZipFile, name: str, budget: list[int]) -> bytes:
    """Read one XML member within the ZIP-bomb limits; ``budget`` holds the remaining total."""
    info = archive.getinfo(name)
    if info.file_size > MAX_ZIP_MEMBER or info.file_size > budget[0]:
        raise ExtractionError("archive_too_large")
    if info.compress_size and info.file_size / info.compress_size > MAX_ZIP_RATIO:
        raise ExtractionError("archive_suspicious")
    with archive.open(info) as handle:
        content = handle.read(MAX_ZIP_MEMBER + 1)  # the header sizes can lie: never read more than allowed
    if len(content) > MAX_ZIP_MEMBER or len(content) > budget[0]:
        raise ExtractionError("archive_too_large")
    budget[0] -= len(content)
    return content


def _parse_xml(content: bytes) -> ET.Element:
    # OOXML never contains a DTD; refusing it rules out entity expansion and external entities.
    if b"<!DOCTYPE" in content[:4096].upper() or b"<!ENTITY" in content.upper():
        raise ExtractionError("archive_suspicious")
    try:
        return ET.fromstring(content)  # noqa: S314 - DTDs refused above
    except ET.ParseError as err:
        raise ExtractionError("extraction_failed") from err


def _open_zip(data: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as err:
        raise ExtractionError("extraction_failed") from err
    if len(archive.infolist()) > MAX_ZIP_MEMBERS:
        archive.close()
        raise ExtractionError("archive_suspicious")
    return archive


def _docx_text(data: bytes) -> str:
    with _open_zip(data) as archive:
        root = _parse_xml(_read_member(archive, "word/document.xml", [MAX_ZIP_TOTAL]))
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{_W}p"):
        pieces: list[str] = []
        for node in paragraph.iter():
            if node.tag == f"{_W}t" and node.text:
                pieces.append(node.text)
            elif node.tag == f"{_W}tab":
                pieces.append("\t")
            elif node.tag in (f"{_W}br", f"{_W}cr"):
                pieces.append("\n")
        paragraphs.append("".join(pieces))
    return "\n".join(paragraphs)


def _xlsx_sheets(archive: zipfile.ZipFile, budget: list[int]) -> list[tuple[str, str]]:
    """(sheet name, member path) in workbook order."""
    workbook = _parse_xml(_read_member(archive, "xl/workbook.xml", budget))
    targets: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in archive.namelist():
        rels = _parse_xml(_read_member(archive, "xl/_rels/workbook.xml.rels", budget))
        for rel in rels.iter(f"{_PKG_REL}Relationship"):
            target = rel.get("Target", "")
            target = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
            targets[rel.get("Id", "")] = re.sub(r"/\./", "/", target)
    sheets = []
    for sheet in workbook.iter(f"{_S}sheet"):
        path = targets.get(sheet.get(f"{_R}id", ""))
        if path and path in archive.namelist():
            sheets.append((sheet.get("name") or "", path))
    return sheets


def _xlsx_text(data: bytes) -> str:
    budget = [MAX_ZIP_TOTAL]
    with _open_zip(data) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = _parse_xml(_read_member(archive, "xl/sharedStrings.xml", budget))
            shared = ["".join(t.text or "" for t in item.iter(f"{_S}t")) for item in strings.iter(f"{_S}si")]
        lines: list[str] = []
        cells = 0
        for name, path in _xlsx_sheets(archive, budget):
            lines.append(f"[sheet {name}]")
            sheet = _parse_xml(_read_member(archive, path, budget))
            for row in sheet.iter(f"{_S}row"):
                values: list[str] = []
                for cell in row.iter(f"{_S}c"):
                    cells += 1
                    if cells > MAX_XLSX_CELLS:
                        lines.append("[cell limit reached]")
                        return "\n".join(lines)
                    values.append(_cell_value(cell, shared))
                if any(values):
                    lines.append("\t".join(values))
    return "\n".join(lines)


def _cell_value(cell: ET.Element, shared: list[str]) -> str:
    kind = cell.get("t")
    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(f"{_S}t"))
    value = cell.find(f"{_S}v")
    text = value.text if value is not None and value.text else ""
    if kind == "s":
        try:
            return shared[int(text)]
        except ValueError, IndexError:
            return ""
    if kind == "b":
        return "TRUE" if text == "1" else "FALSE"
    return text  # numbers as stored (dates are serial numbers; formats are not applied)
