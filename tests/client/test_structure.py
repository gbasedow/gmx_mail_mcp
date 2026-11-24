"""Tests for BODYSTRUCTURE parsing."""

from __future__ import annotations

import base64

from custom_components.gmx_mail_mcp.client.structure import PartInfo, _params, attachments, find_text_parts, parse_bodystructure

from .bodystructures import ALTERNATIVE_WITH_PDF, FORWARDED_MESSAGE, HTML_ONLY, PLAIN, RELATED_INLINE_IMAGE, TEXT_ATTACHMENT


def test_single_part_is_part_1() -> None:
    """A non-multipart message has the single part '1'."""
    (part,) = parse_bodystructure(PLAIN)
    assert (part.part_id, part.mime, part.charset, part.encoding, part.size) == ("1", "text/plain", "utf-8", "8bit", 15)
    assert not part.is_attachment
    assert find_text_parts([part]) == (part, None)
    assert parse_bodystructure(None) == []


def test_html_only() -> None:
    """HTML-only mails have no plain part."""
    parts = parse_bodystructure(HTML_ONLY)
    plain, html = find_text_parts(parts)
    assert plain is None and html is not None and html.part_id == "1"


def test_nested_alternative_with_attachment() -> None:
    """Part numbers follow RFC 3501 (1.1, 1.2, 2); the RFC 2231 file name is decoded."""
    parts = parse_bodystructure(ALTERNATIVE_WITH_PDF)
    assert [(p.part_id, p.mime) for p in parts] == [("1.1", "text/plain"), ("1.2", "text/html"), ("2", "application/pdf")]
    plain, html = find_text_parts(parts)
    assert plain is not None and plain.part_id == "1.1"
    assert html is not None and html.part_id == "1.2"
    (pdf,) = attachments(parts)
    assert pdf.filename == "Rechnung März.pdf"
    assert pdf.disposition == "attachment" and not pdf.inline


def test_inline_image_is_listed_as_inline() -> None:
    """Embedded images are attachments marked inline (they do not count as 'has_attachments')."""
    (image,) = attachments(parse_bodystructure(RELATED_INLINE_IMAGE))
    assert image.mime == "image/png" and image.inline and image.content_id == "img1@x"
    assert image.embedded


def test_inline_pdf_still_counts_as_attachment() -> None:
    """GMX system mails mark PDFs as inline; they are files, not embedded images (found live on 21.11.2026)."""
    from custom_components.gmx_mail_mcp.client.structure import PartInfo  # noqa: PLC0415

    pdf = PartInfo("1.2", "application/pdf", 367088, "base64", None, "AGB.pdf", "inline", None)
    assert pdf.is_attachment and pdf.inline and not pdf.embedded


def test_forwarded_message_is_one_attachment() -> None:
    """message/rfc822 is not expanded; its disposition sits at index 11."""
    parts = parse_bodystructure(FORWARDED_MESSAGE)
    assert [(p.part_id, p.mime) for p in parts] == [("1", "text/plain"), ("2", "message/rfc822")]
    assert parts[1].disposition == "attachment"


def test_text_file_attachment_is_not_the_body() -> None:
    """A second text/plain part with a file name is an attachment, the first is the body."""
    parts = parse_bodystructure(TEXT_ATTACHMENT)
    plain, _html = find_text_parts(parts)
    assert plain is not None and plain.part_id == "1"
    assert [p.filename for p in attachments(parts)] == ["notes.txt"]


def test_parameter_decoding() -> None:
    """RFC 2231 (single and continuations) and RFC 2047 names; control characters are removed."""
    assert _params((b"FILENAME*", b"utf-8''Rechnung%20M%C3%A4rz.pdf")) == {"filename": "Rechnung März.pdf"}
    assert _params((b"NAME*0*", b"utf-8''Gro%C3%9F", b"NAME*1", b"e%20Datei.txt")) == {"name": "Große%20Datei.txt"}
    assert _params((b"NAME*", b"iso-8859-1'de'M%E4rz.txt")) == {"name": "März.txt"}
    assert _params((b"NAME*", b"x-unknown''abc")) == {"name": "abc"}
    assert _params((b"NAME", b"=?UTF-8?Q?M=C3=A4rz.pdf?=")) == {"name": "März.pdf"}
    assert _params(None) == {}
    assert _params(b"garbage") == {}
    (part,) = parse_bodystructure(
        (b"APPLICATION", b"OCTET-STREAM", (b"NAME", b"evil\xe2\x80\xaefdp.exe"), None, None, b"base64", 3, None, None, None)
    )
    assert part.filename == "evilfdp.exe"  # bidi override removed


def test_tolerates_short_and_malformed_structures() -> None:
    """Missing optional fields do not crash the parser."""
    (part,) = parse_bodystructure((b"text", b"plain", None, None, None, None, None))
    assert part.size == 0 and part.encoding == "7bit" and part.charset is None
    (part,) = parse_bodystructure((b"application", b"zip", None, None, None, b"base64", b"x"))
    assert part.size == 0 and part.is_attachment


def test_size_estimated_from_encoded_size() -> None:
    """base64 with 76-character lines + CRLF: exact (live case from M2: 72 110 encoded = 52 695-byte PDF)."""

    def part(size: int, encoding: str) -> PartInfo:
        return PartInfo("2", "application/pdf", size, encoding, None, "a.pdf", "attachment", None)

    assert part(72_110, "base64").size_estimated == 52_695
    for length in (1, 57, 58, 1000, 52_695, 300_001):
        encoded = base64.encodebytes(bytes(length)).replace(b"\n", b"\r\n")  # MIME: 76 characters per line, CRLF
        assert part(len(encoded), "base64").size_estimated in (length, length + 1, length + 2)  # padding not visible
    assert part(500, "quoted-printable").size_estimated == 500  # upper bound
    assert part(500, "7bit").size_estimated == 500
    assert part(0, "base64").size_estimated == 0
