"""Tests for files that come with a tool call (D17): base64, SHA-256, file name, type by content, limits."""

from __future__ import annotations

import base64
import hashlib

import pytest

from custom_components.gmx_mail_mcp.client.upload import (
    MAX_FILENAME,
    MAX_FILES,
    Upload,
    UploadError,
    clean_filename,
    decode_uploads,
    resolve_mime,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
PDF = b"%PDF-1.4\n" + b"x" * 100


def _upload(content: bytes, name: str = "datei.bin", mime: str | None = None, sha: str | None = None) -> Upload:
    return Upload(
        filename=name,
        content_base64=base64.b64encode(content).decode(),
        sha256=sha if sha is not None else hashlib.sha256(content).hexdigest(),
        mime=mime,
    )


def test_decode_ok_with_type_from_content() -> None:
    """A PNG declared as PDF is stored as PNG and reported as type_mismatch; line breaks in base64 are fine."""
    upload = _upload(PNG, "bild.pdf", "application/pdf")
    wrapped = Upload(upload.filename, "\n".join([upload.content_base64[:20], upload.content_base64[20:]]), upload.sha256, upload.mime)
    files, warnings = decode_uploads([wrapped, _upload(PDF, "rechnung.pdf")], max_bytes=10_000)
    assert [(f.filename, f.mime, f.content) for f in files] == [("bild.pdf", "image/png", PNG), ("rechnung.pdf", "application/pdf", PDF)]
    assert warnings == ["type_mismatch"]


@pytest.mark.parametrize(
    ("upload", "code"),
    [
        (_upload(PDF, sha="0" * 64), "sha256_mismatch"),
        (_upload(PDF, sha="kein-hash"), "invalid_sha256"),
        (Upload("a.pdf", "@@@ kein base64 @@@", "0" * 64), "invalid_base64"),
        (_upload(b"x" * 2000), "attachment_too_large"),
    ],
)
def test_refused_uploads(upload: Upload, code: str) -> None:
    """Every refusal has a stable code and the position of the file."""
    with pytest.raises(UploadError) as err:
        decode_uploads([_upload(b"ok"), upload], max_bytes=1000)
    assert (err.value.code, err.value.index) == (code, 1)


def test_limits_before_decoding_sum_and_count() -> None:
    """Oversized base64 is refused before decoding; the sum of all files counts; at most MAX_FILES files."""
    with pytest.raises(UploadError) as err:
        decode_uploads([Upload("x", "A" * 10_000_000, "0" * 64)], max_bytes=1000)
    assert err.value.code == "attachment_too_large"
    with pytest.raises(UploadError) as err:
        decode_uploads([_upload(b"a" * 600), _upload(b"b" * 600)], max_bytes=1000)
    assert (err.value.code, err.value.index) == ("attachments_too_large", 1)
    with pytest.raises(UploadError) as err:
        decode_uploads([_upload(b"a")] * (MAX_FILES + 1), max_bytes=1000)
    assert err.value.code == "too_many_attachments"


def test_clean_filename() -> None:
    """Paths, control, reserved and invisible characters go; long names keep their extension."""
    assert clean_filename("../../etc/passwd") == "passwd"
    assert clean_filename("C:\\Users\\x\\Rechnung März.pdf") == "Rechnung März.pdf"
    assert clean_filename("a\x00b\r\n<c>.txt") == "ab__c_.txt"  # NUL removed (invisible), CR/LF replaced
    assert clean_filename("rech\u202enung.pdf") == "rechnung.pdf"  # right-to-left override (fake extension trick)
    assert clean_filename(" .. ") == "anhang"
    long_name = clean_filename("x" * 400 + ".pdf")
    assert len(long_name) == MAX_FILENAME and long_name.endswith(".pdf")


def test_resolve_mime() -> None:
    """Recognised content wins; otherwise the declared type (if well-formed), then the file name."""
    assert resolve_mime(PDF, None, "a.bin") == ("application/pdf", False)
    assert resolve_mime(b"Hallo", "text/csv", "a.csv") == ("text/csv", False)
    assert resolve_mime(b"\x00\x01\x02", "application/x-custom", "a.dat") == ("application/x-custom", False)
    assert resolve_mime(b"\x00\x01\x02", "kein typ\r\n", "a.zip") == ("application/zip", False)
    assert resolve_mime(b"\x00\x01\x02", None, "ohne") == ("application/octet-stream", False)


def test_oversized_base64_is_never_decoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """The size check runs on the base64 text, so an oversized file is refused without allocating its bytes."""

    def no_decode(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("decoded although too large")

    monkeypatch.setattr("custom_components.gmx_mail_mcp.client.upload.base64.b64decode", no_decode)
    with pytest.raises(UploadError) as err:
        decode_uploads([Upload("x", "A" * 1400, "0" * 64)], max_bytes=1000)
    assert err.value.code == "attachment_too_large"
