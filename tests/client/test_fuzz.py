"""Property-based tests ("fuzzing", M7): hostile or broken input must never crash the parsers or break the limits.

Hypothesis generates thousands of inputs per run (DECISIONS Nr. 67). The properties:

* sanitizers, header/MIME parsers and decoders never raise, whatever bytes a mail contains;
* sanitized text never contains invisible or bidi control characters;
* address parsing either succeeds with clean addresses or raises ``ValueError`` - never anything else;
* the response envelope always fits the 24 KB budget.
"""

from __future__ import annotations

import os
import string
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from custom_components.gmx_mail_mcp.client.reader import _addresses, _header_str, decode_charset, decode_transfer, parse_headers
from custom_components.gmx_mail_mcp.client.sanitize import _INVISIBLE_RE, clean_inline, sanitize_body, sanitize_html
from custom_components.gmx_mail_mcp.client.structure import attachments, find_text_parts, parse_bodystructure
from custom_components.gmx_mail_mcp.client.writer import parse_addresses
from custom_components.gmx_mail_mcp.envelope import RESPONSE_BUDGET_BYTES, build_envelope, fit_text, json_size

# FUZZ_EXAMPLES=5000 for a deep local run; CI keeps the default short.
FUZZ = settings(max_examples=int(os.environ.get("FUZZ_EXAMPLES", "300")), deadline=None, suppress_health_check=[HealthCheck.too_slow])

_TAGS = ["div", "p", "span", "a", "b", "script", "style", "table", "td", "img", "br", "!--", "template", "svg"]
_ATTRS = ['style="display:none"', 'style="font-size:0"', 'href="https://x.example/a?b=c"', "hidden", 'aria-hidden="true"',
          'style="color:#fff"', 'style="position:absolute;left:-9999px"', 'alt="bild"', 'onclick="x()"']  # fmt: skip
_SPECIAL = "\u200b\u200e\u202e\u2066\ufeff\u00ad\U000e0041<>&\"'\x00\x07\r\n\t"


@st.composite
def html_documents(draw: Any) -> str:
    """Random, often broken HTML with hiding tricks, entities and invisible characters."""
    parts: list[str] = []
    for _ in range(draw(st.integers(0, 25))):
        kind = draw(st.integers(0, 4))
        if kind == 0:
            parts.append(f"<{draw(st.sampled_from(_TAGS))} {draw(st.sampled_from(_ATTRS))}>")
        elif kind == 1:
            parts.append(f"</{draw(st.sampled_from(_TAGS))}>")
        elif kind == 2:
            parts.append(draw(st.sampled_from(["&amp;", "&#x202e;", "&#8203;", "&nbsp;", "&lt;script&gt;", "&#0;", "&#xFFFFFF;"])))
        else:
            parts.append(draw(st.text(alphabet=st.sampled_from(string.printable + _SPECIAL), max_size=40)))
    return "".join(parts)


@FUZZ
@given(html_documents())
def test_html_sanitizers_never_crash_and_strip_invisible(html: str) -> None:
    """Text view and cleaned HTML: no exception, no invisible or bidi characters, no script content."""
    text = sanitize_body(html, is_html=True).text
    assert not _INVISIBLE_RE.search(text)
    cleaned = sanitize_html(html).html
    assert "<script" not in cleaned.lower() and "onclick" not in cleaned.lower()


@FUZZ
@given(st.text(max_size=500))
def test_plain_text_sanitizer(text: str) -> None:
    """Plain text: no exception, no invisible characters; clean_inline respects its length and is one line."""
    assert not _INVISIBLE_RE.search(sanitize_body(text, is_html=False).text)
    inline = clean_inline(text, 50)
    assert len(inline) <= 50 and "\n" not in inline and "\r" not in inline


@FUZZ
@given(st.binary(max_size=600))
def test_header_parsing_never_crashes(raw: bytes) -> None:
    """Arbitrary header bytes (including broken RFC 2047 words) can be parsed and read."""
    message = parse_headers(raw)
    for name in ("from", "to", "cc", "reply-to", "subject", "message-id", "date"):
        _addresses(message, name)
        _header_str(message, name)


@FUZZ
@given(st.lists(st.sampled_from(["=?utf-8?b?", "=?iso-8859-1?q?", "?=", "Subject: ", "From: ", "\r\n", " ", "=E4", "w6Q=", "a@b.c",
                                 "<x@y>", ",", "\"", "(", ")", "=?", "\r\n\t"]), max_size=40))  # fmt: skip
def test_encoded_words_never_crash(pieces: list[str]) -> None:
    """Fragments of encoded words in headers."""
    message = parse_headers(("".join(pieces) + "\r\n\r\n").encode("utf-8", "replace"))
    _header_str(message, "subject")
    _addresses(message, "from")


def _bodystructure() -> Any:
    leaf = st.one_of(st.none(), st.integers(-5, 10**9), st.binary(max_size=30), st.sampled_from([b"text", b"plain", b"html", b"base64",
                     b"attachment", b"inline", b"utf-8", b"name", b"filename*", b"NIL"]))  # fmt: skip
    return st.recursive(
        leaf, lambda children: st.one_of(st.lists(children, max_size=12), st.lists(children, max_size=12).map(tuple)), max_leaves=60
    )


@FUZZ
@given(_bodystructure())
def test_bodystructure_never_crashes(structure: Any) -> None:
    """Any nesting of what imapclient may return for BODYSTRUCTURE."""
    parts = parse_bodystructure(structure)
    attachments(parts)
    find_text_parts(parts)
    for part in parts:
        assert part.filename is None or not _INVISIBLE_RE.search(part.filename)


@FUZZ
@given(st.binary(max_size=400), st.sampled_from(["base64", "quoted-printable", "7bit", "8bit", "binary", "x-uuencode", ""]),
       st.sampled_from(["utf-8", "iso-8859-1", "windows-1252", "x-unknown", "kaputt", "utf-16", "", "unknown-8bit"]))  # fmt: skip
def test_decoders_never_crash(data: bytes, encoding: str, charset: str) -> None:
    """Transfer and charset decoding of broken parts."""
    assert isinstance(decode_charset(decode_transfer(data, encoding), charset or None), str)


@FUZZ
@given(st.text(max_size=80))
def test_address_parsing_is_strict(value: str) -> None:
    """Either clean addresses or ValueError - never another exception, never line breaks (header injection)."""
    try:
        parsed = parse_addresses([value])
    except ValueError:
        return
    for address in parsed:
        assert "\r" not in address.addr_spec and "\n" not in address.addr_spec and "@" in address.addr_spec


@FUZZ
@given(st.text(min_size=0, max_size=60_000))
def test_envelope_always_fits_the_budget(text: str) -> None:
    """Whatever the text, the response stays within 24 KB (multi-byte characters included)."""
    envelope = build_envelope(account="user@gmx.test", action="get_message", data={"body": {"text": ""}})

    def render(n: int) -> None:
        envelope["data"]["body"]["text"] = text[:n]

    kept = fit_text(envelope, render, len(text))
    assert json_size(envelope) <= RESPONSE_BUDGET_BYTES
    assert envelope["truncated"] == (kept < len(text))
