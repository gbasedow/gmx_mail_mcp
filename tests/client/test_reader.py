"""Tests for the read-only IMAP operations against the in-memory fake server."""

from __future__ import annotations

import base64
from collections.abc import Generator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from imapclient.exceptions import IMAPClientAbortError, IMAPClientError

from custom_components.gmx_mail_mcp.client.errors import (
    GmxAttachmentTooLargeError,
    GmxFolderNotFoundError,
    GmxMessageNotFoundError,
    GmxPartNotFoundError,
    GmxSearchRejectedError,
    GmxUidValidityError,
)
from custom_components.gmx_mail_mcp.client.folders import map_special_folders, parse_folder_list
from custom_components.gmx_mail_mcp.client.imap import ImapSession, ServerInfo, parse_capabilities
from custom_components.gmx_mail_mcp.client.reader import (
    CLIENT_SORT_MAX_UIDS,
    HEADER_REQUEST_BUDGET,
    HEADER_REQUEST_MAX,
    LIST_UNSUBSCRIBE_MAX,
    MailRef,
    SearchQuery,
    decode_charset,
    decode_transfer,
    get_attachment,
    get_message,
    list_folders,
    search,
    uid_in_range,
    unseen_count,
)

from .fakes import EMAIL, GMX_CAPABILITIES, FakeImapServer, mixed_message, plain_message

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def server() -> Generator[FakeImapServer]:
    """Fake server with an INBOX of three messages."""
    fake = FakeImapServer()
    inbox = fake.mailbox("INBOX", uidvalidity=777)
    inbox.messages[10] = plain_message(subject="Älteste", date=NOW - timedelta(days=3), flags=(b"\\Seen",))
    inbox.messages[11] = plain_message(subject="Mittlere", date=NOW - timedelta(days=2))
    inbox.messages[12] = mixed_message(html="<p>Hi</p>")
    inbox.messages[12].internaldate = NOW - timedelta(days=1)
    fake.mailbox("Gesendet")
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", fake):
        yield fake


def _session(server: FakeImapServer) -> ImapSession:
    client = server("imap.gmx.net", 993)
    client.login(EMAIL, server.password)
    return client


def _info(caps: tuple[bytes, ...] = tuple(GMX_CAPABILITIES)) -> ServerInfo:
    return ServerInfo(capabilities=parse_capabilities(caps), folders=())


def _search(server: FakeImapServer, query: SearchQuery, info: ServerInfo | None = None, **kwargs: Any) -> Any:
    params: dict[str, Any] = {"order": "date_desc", "limit": 20, "offset": 0, "snippet": False, "now": NOW}
    params.update(kwargs)
    return search(_session(server), info or _info(), "INBOX", query, **params)


def _commands(server: FakeImapServer, name: str) -> list[tuple[Any, ...]]:
    return [c for c in server.commands if c[0] == name]


# --------------------------------------------------------------------------- folders


def test_list_folders_with_roles_and_counts(server: FakeImapServer) -> None:
    """Roles come from the folder map; counts from STATUS; \\HasNoChildren is dropped."""
    session = _session(server)
    folder_map = map_special_folders(parse_folder_list(session.list_folders()))
    server.mailbox("Gelöscht")
    server.mailbox("Entwürfe")
    server.mailbox("Spamverdacht")
    server.mailbox("Archiv")
    folders = list_folders(session, _info(), folder_map, include_counts=True)
    by_path = {f["path"]: f for f in folders}
    assert by_path["INBOX"] == {
        "name": "INBOX",
        "path": "INBOX",
        "role": "inbox",
        "delimiter": "/",
        "flags": [],
        "selectable": True,
        "messages": 3,
        "unseen": 2,
    }
    assert by_path["Gesendet"]["role"] == "sent"
    assert by_path["Gesendet"]["flags"] == ["\\Sent", "\\NoInferiors"]
    assert by_path["Archiv"]["role"] is None
    without = list_folders(session, _info(), folder_map, include_counts=False)
    assert "messages" not in without[0]
    assert unseen_count(session, "INBOX") == 2
    with pytest.raises(GmxFolderNotFoundError):
        unseen_count(session, "Nope")


# --------------------------------------------------------------------------- search


def test_search_uses_examine_peek_and_unselect(server: FakeImapServer) -> None:
    """Folders are opened read-only and left with UNSELECT; summaries are complete."""
    result = _search(server, SearchQuery())
    assert result.uidvalidity == 777
    assert result.total == 3
    assert result.search_mode == "server"
    assert [m["ref"]["uid"] for m in result.messages] == [12, 11, 10]
    assert _commands(server, "select") == [("select", "INBOX", True)]
    assert _commands(server, "unselect") == [("unselect",)]
    newest = result.messages[0]
    assert newest["ref"] == {"folder": "INBOX", "uid": 12, "uidvalidity": 777}
    assert newest["has_attachments"] is True
    assert newest["from"] == "shop@example.com"
    oldest = result.messages[2]
    assert oldest["subject"] == "Älteste"
    assert oldest["from"] == "Anna Beispiel <anna@example.org>"
    assert oldest["flags"] == ["\\Seen"]
    assert oldest["date"] == "2026-11-20T10:00:00+01:00"
    assert "snippet" not in oldest


def test_search_criteria_and_charset(server: FakeImapServer) -> None:
    """All criteria are mapped; UTF-8 is only announced for non-ASCII values; deleted mails are excluded."""
    query = SearchQuery(
        from_="Müller",
        subject="Rechnung",
        since=date(2026, 1, 1),
        before=date(2026, 12, 1),
        seen=False,
        flagged=True,
        answered=False,
        draft=False,
        larger=10,
        smaller=10_000,
        keyword="$label1",
        uid_range="1:100",
    )
    _search(server, query)
    sort = _commands(server, "sort")[0]
    assert sort[1] == ["REVERSE", "DATE"]
    assert sort[3] == "UTF-8"
    criteria = sort[2]
    assert criteria[0] == "UNDELETED"
    for token in (
        "UNSEEN",
        "FLAGGED",
        "UNANSWERED",
        "UNDRAFT",
        "SINCE",
        "BEFORE",
        "LARGER",
        "SMALLER",
        "KEYWORD",
        "UID",
        "FROM",
        "SUBJECT",
    ):
        assert token in criteria
    assert criteria[criteria.index("FROM") + 1] == "Müller"

    server.commands.clear()
    _search(server, SearchQuery(subject="ascii"), order="date_asc")
    sort = _commands(server, "sort")[0]
    assert sort[1] == ["DATE"]
    assert sort[3] == "US-ASCII"


def test_search_values_with_line_breaks_are_rejected(server: FakeImapServer) -> None:
    """CR/LF would break the IMAP command line."""
    with pytest.raises(GmxSearchRejectedError):
        _search(server, SearchQuery(subject="a\r\nb"))


def test_search_rejected_by_server(server: FakeImapServer) -> None:
    """NO/BAD answers (e.g. BADCHARSET) become a search_rejected error; the folder is still unselected."""
    server.search_error = IMAPClientError("SORT command error: NO [BADCHARSET]")
    with pytest.raises(GmxSearchRejectedError):
        _search(server, SearchQuery(subject="Größe"))
    assert _commands(server, "unselect")


def test_connection_errors_are_not_masked(server: FakeImapServer) -> None:
    """A dropped connection must reach the worker (which reconnects), not become 'search rejected'."""
    server.search_error = IMAPClientAbortError("socket error: EOF")
    with pytest.raises(IMAPClientAbortError):
        _search(server, SearchQuery())


def test_folder_not_found(server: FakeImapServer) -> None:
    """Unknown folders are reported as such."""
    with pytest.raises(GmxFolderNotFoundError):
        search(_session(server), _info(), "Nope", SearchQuery(), order="date_desc", limit=5, offset=0, snippet=False)


def test_client_side_sort_without_sort_capability(server: FakeImapServer) -> None:
    """Without SORT, UIDs come from SEARCH and are ordered by INTERNALDATE; without UNSELECT nothing is closed."""
    caps = (b"IMAP4REV1", b"UIDPLUS", b"MOVE")
    server.mailbox("INBOX").messages[9] = plain_message(subject="late arrival, old date", date=NOW - timedelta(days=10))
    result = _search(server, SearchQuery(), info=_info(caps))
    assert [m["ref"]["uid"] for m in result.messages] == [12, 11, 10, 9]
    assert _commands(server, "sort") == []
    assert _commands(server, "unselect") == []
    result = _search(server, SearchQuery(), info=_info(caps), order="date_asc")
    assert [m["ref"]["uid"] for m in result.messages] == [9, 10, 11, 12]


def test_client_side_sort_is_limited(server: FakeImapServer) -> None:
    """More than 2000 hits without SORT: only the newest 2000 UIDs are sorted, with a warning."""
    uids = list(range(1, CLIENT_SORT_MAX_UIDS + 11))
    server.search_hook = lambda folder, criteria, charset: uids
    box = server.mailbox("INBOX")
    for uid in uids:
        box.messages.setdefault(uid, plain_message(date=NOW - timedelta(minutes=uid)))
    result = _search(server, SearchQuery(), info=_info((b"IMAP4REV1",)), limit=5)
    assert result.warnings == ["sort_partial"]
    assert result.total == CLIENT_SORT_MAX_UIDS


def test_pagination_offsets(server: FakeImapServer) -> None:
    """Offset and limit select the page; total stays the full count."""
    result = _search(server, SearchQuery(), limit=2, offset=1)
    assert result.total == 3
    assert [m["ref"]["uid"] for m in result.messages] == [11, 10]


def test_uidvalidity_change_between_pages(server: FakeImapServer) -> None:
    """A cursor from before a UIDVALIDITY change is refused."""
    with pytest.raises(GmxUidValidityError):
        _search(server, SearchQuery(), expected_uidvalidity=1)


def test_snippets_and_partial_fetch_fallback(server: FakeImapServer) -> None:
    """Snippets are short, sanitised, and fall back to a full fetch if partial FETCH responses break imapclient."""
    server.mailbox("INBOX").messages[11] = plain_message(body=f"Hallo{chr(0x200B)} Welt https://track.example/x?id=1\n" + "x" * 500)
    result = _search(server, SearchQuery(), snippet=True)
    snippets = {m["ref"]["uid"]: m.get("snippet") for m in result.messages}
    assert snippets[11].startswith("Hallo Welt [link](track.example)")
    assert len(snippets[11]) <= 160
    assert snippets[12] == "Plain body"

    server.broken_partial_fetch = True
    result = _search(server, SearchQuery(), snippet=True)
    assert {m["ref"]["uid"]: m.get("snippet") for m in result.messages}[12] == "Plain body"


def test_recent_header_fallback_for_index_lag(server: FakeImapServer) -> None:
    """GMX indexes new mail with ~30 s delay: recent mails are matched on their headers (GMX-NOTES row 25)."""
    box = server.mailbox("INBOX")
    box.messages[20] = plain_message(subject="Neue Rechnung", date=NOW - timedelta(seconds=20))
    box.messages[21] = plain_message(subject="Anderes", date=NOW - timedelta(seconds=10))
    box.messages[22] = plain_message(subject="Rechnung alt", date=NOW - timedelta(minutes=5))

    def lagging(folder: str, criteria: list[Any], charset: str | None) -> list[int]:
        if "SUBJECT" in criteria or "BODY" in criteria:
            return [12]  # index knows only the old mail
        return [uid for uid in box.messages if uid >= 20]  # SINCE yesterday

    server.search_hook = lagging
    result = _search(server, SearchQuery(subject="rechnung"))
    assert result.search_mode == "server+recent_headers"
    assert [m["ref"]["uid"] for m in result.messages] == [20, 12]  # 22 is too old for the fallback window
    assert result.warnings == []

    asc = _search(server, SearchQuery(subject="rechnung"), order="date_asc")
    assert [m["ref"]["uid"] for m in asc.messages] == [12, 20]

    body = _search(server, SearchQuery(body="rechnung"))
    assert body.search_mode == "server"
    assert body.warnings == ["index_lag"]  # body text of recent mails cannot be checked locally


def test_recent_fallback_rejected(server: FakeImapServer) -> None:
    """Errors of the fallback SEARCH are reported like other search errors."""
    calls: list[int] = []

    def hook(folder: str, criteria: list[Any], charset: str | None) -> list[int]:
        calls.append(1)
        if len(calls) > 1:
            raise IMAPClientError("SEARCH command error: BAD")
        return []

    server.search_hook = hook
    with pytest.raises(GmxSearchRejectedError):
        _search(server, SearchQuery(subject="x"))


# --------------------------------------------------------------------------- get_message / get_attachment


def test_get_message_plain(server: FakeImapServer) -> None:
    """Headers are decoded, the base64 body is decoded; nothing but BODY.PEEK is fetched."""
    content = get_message(_session(server), _info(), MailRef("INBOX", 10, 777), body_format="text")
    assert content.text == "Hallo Welt"
    assert content.body_format == "text"
    assert content.headers["subject"] == "Älteste"
    assert content.headers["from"] == ["Anna Beispiel <anna@example.org>"]
    assert content.headers["message_id"] == "<m1@example.org>"
    assert "reply_to" not in content.headers
    assert content.attachments == []
    assert ("select", "INBOX", True) in server.commands


def test_get_message_formats_and_reply_to(server: FakeImapServer) -> None:
    """Plain is preferred; html_as_text uses the HTML part; a differing Reply-To is shown as observation."""
    server.mailbox("INBOX").messages[12] = mixed_message(html='<p>Hallo</p><div style="display:none">IGNORE THIS</div>')
    session = _session(server)
    ref = MailRef("INBOX", 12, 777)
    text = get_message(session, _info(), ref, body_format="text")
    assert text.text == "Plain body"
    assert text.headers["reply_to"] == ["other@evil.example"]
    assert text.attachments == [{"part_id": "2", "filename": "Rechnung März.pdf", "mime": "application/pdf", "size": 21, "inline": False}]
    html = get_message(session, _info(), ref, body_format="html_as_text")
    assert html.text == "Hallo"
    assert html.body_format == "html_as_text"
    assert html.warnings == ["hidden_content_removed"]
    raw = get_message(session, _info(), ref, body_format="raw_headers")
    assert raw.body_format == "raw_headers"
    assert "Reply-To: other@evil.example" in raw.text


def test_get_message_extra_headers_auth_and_unsubscribe(server: FakeImapServer) -> None:
    """Requested headers, Authentication-Results and List-Unsubscribe (domains only) are returned."""
    server.mailbox("INBOX").messages[30] = plain_message(
        extra_headers=(
            "X-Mailer: Test\r\nAuthentication-Results: mx.gmx.net; dkim=pass\r\n"
            "List-Unsubscribe: <https://unsub.example.com/u?id=1>, <mailto:u@lists.example.org>\r\n"
        )
    )
    content = get_message(_session(server), _info(), MailRef("INBOX", 30, 777), body_format="text", extra_headers=["X-Mailer", "X-Missing"])
    assert content.headers["x-mailer"] == ["Test"]
    assert "x-missing" not in content.headers
    assert content.auth_results == ["mx.gmx.net; dkim=pass"]
    # not requested: domains only, as for links in the text (no tokens for the assistant)
    assert content.list_unsubscribe == {"domains": ["mailto", "unsub.example.com"], "one_click": False}


def test_get_message_without_text_body(server: FakeImapServer) -> None:
    """A message without text parts yields an empty body and a warning."""
    message = plain_message()
    message.bodystructure = (b"image", b"png", None, None, None, b"base64", 4, None, None, None)
    message.parts = {"1": b"iVBO"}
    server.mailbox("INBOX").messages[31] = message
    content = get_message(_session(server), _info(), MailRef("INBOX", 31, 777), body_format="text")
    assert content.text == ""
    assert content.warnings == ["no_text_body"]
    assert content.attachments[0]["mime"] == "image/png"


def test_get_message_huge_text_part_is_cut_at_source(server: FakeImapServer) -> None:
    """Text parts above 2 MB are only fetched partially (with fallback for broken partial responses)."""
    message = plain_message()
    message.bodystructure = (b"text", b"plain", None, None, None, b"7bit", 3 * 1024 * 1024, 1, None, None, None)
    message.parts = {"1": b"a" * (3 * 1024 * 1024)}
    server.mailbox("INBOX").messages[32] = message
    content = get_message(_session(server), _info(), MailRef("INBOX", 32, 777), body_format="text")
    assert len(content.text) == 2 * 1024 * 1024
    assert "body_truncated_at_source" in content.warnings
    server.broken_partial_fetch = True
    content = get_message(_session(server), _info(), MailRef("INBOX", 32, 777), body_format="text")
    assert len(content.text) == 2 * 1024 * 1024


def test_get_message_errors(server: FakeImapServer) -> None:
    """Changed UIDVALIDITY and missing messages are distinct errors."""
    session = _session(server)
    with pytest.raises(GmxUidValidityError):
        get_message(session, _info(), MailRef("INBOX", 10, 1), body_format="text")
    with pytest.raises(GmxMessageNotFoundError):
        get_message(session, _info(), MailRef("INBOX", 99, 777), body_format="text")
    with pytest.raises(GmxFolderNotFoundError):
        get_message(session, _info(), MailRef("Nope", 1, 777), body_format="text")


def test_get_attachment(server: FakeImapServer) -> None:
    """The part is decoded; size limits apply before and after download; part ids are validated."""
    session = _session(server)
    ref = MailRef("INBOX", 12, 777)
    part, content = get_attachment(session, _info(), ref, "2", max_bytes=1000)
    assert content == b"%PDF-1.4 test"
    assert part.filename == "Rechnung März.pdf"
    with pytest.raises(GmxAttachmentTooLargeError):
        get_attachment(session, _info(), ref, "2", max_bytes=5)
    for bad in ("0", "a", "1..2", "../2", "2.0"):
        with pytest.raises(GmxPartNotFoundError):
            get_attachment(session, _info(), ref, bad, max_bytes=1000)
    with pytest.raises(GmxPartNotFoundError):
        get_attachment(session, _info(), ref, "3", max_bytes=1000)


def test_get_attachment_refuses_large_parts_before_download(server: FakeImapServer) -> None:
    """The BODYSTRUCTURE size is checked first, so a huge attachment is never downloaded."""
    message = mixed_message(html="<p>x</p>", pdf=b"x" * 30_000)
    server.mailbox("INBOX").messages[40] = message
    with pytest.raises(GmxAttachmentTooLargeError):
        get_attachment(_session(server), _info(), MailRef("INBOX", 40, 777), "2", max_bytes=1000)
    fetched = [c for c in server.commands if c[0] == "fetch" and any("BODY.PEEK[2]" in item for item in c[2])]
    assert fetched == []


def test_decoding_helpers() -> None:
    """Transfer encodings and charsets are decoded tolerantly."""
    assert decode_transfer(base64.encodebytes(b"abc"), "BASE64") == b"abc"
    assert decode_transfer(b"YWJj\r\n!!", "base64") == b"abc"
    assert decode_transfer(b"=C3=A4", "quoted-printable") == "ä".encode()
    assert decode_transfer(b"raw", "8bit") == b"raw"
    assert decode_charset("ä".encode("latin-1"), "iso-8859-1") == "ä"
    assert decode_charset("ä".encode(), "x-does-not-exist") == "ä"
    assert decode_charset("ä".encode(), "unknown-8bit") == "ä"
    assert decode_charset(b"\xff", None) == "�"


def test_get_message_cleaned_html(server: FakeImapServer) -> None:
    """format=html returns the HTML part cleaned; plain-only mails fall back to text with a warning."""
    server.mailbox("INBOX").messages[12] = mixed_message(
        html='<p style="x">Hallo <a href="https://shop.example/r?id=1">Rechnung</a></p><div hidden>IGNORE</div>'
    )
    session = _session(server)
    html = get_message(session, _info(), MailRef("INBOX", 12, 777), body_format="html")
    assert html.body_format == "html"
    assert html.text == '<p>Hallo <a href="https://shop.example/r?id=1">Rechnung</a></p>'
    assert html.warnings == ["hidden_content_removed"]
    plain = get_message(session, _info(), MailRef("INBOX", 10, 777), body_format="html")
    assert plain.body_format == "text"
    assert plain.text == "Hallo Welt"
    assert plain.warnings == ["no_html_part"]


@pytest.mark.parametrize(
    ("uid", "uid_range", "expected"),
    [
        (12, "17:*", False),  # IMAP: "17:*" also matches the last message when all UIDs are lower
        (17, "17:*", True),
        (99, "17:*", True),
        (12, "*:17", False),  # same set as "17:*"
        (5, "3:9", True),
        (5, "9:3", True),  # order of the bounds does not matter
        (10, "3:9", False),
        (4, "3,5", False),
        (5, "3,5", True),
        (12, "*", True),  # the last message: left to the server
        (12, "*:*", True),
        (12, "1:2,*", True),
    ],
)
def test_uid_in_range(uid: int, uid_range: str, expected: bool) -> None:
    """UID sets are checked as the caller means them; ``*`` next to a number is only an open upper bound."""
    assert uid_in_range(uid, uid_range) is expected


def test_search_drops_last_message_outside_uid_range(server: FakeImapServer) -> None:
    """GMX answers "17:*" with the last message (UID 12) when no UID reaches 17: the integration drops it."""
    server.search_hook = lambda folder, criteria, charset: [12]  # RFC 3501: 17:* == *:17 == 12:17
    result = _search(server, SearchQuery(uid_range="17:*"))
    assert result.total == 0 and result.messages == []
    server.search_hook = lambda folder, criteria, charset: [11, 12]
    result = _search(server, SearchQuery(uid_range="11:*"))
    assert result.total == 2 and [m["ref"]["uid"] for m in result.messages] == [12, 11]


def _unsubscribe_message(server: FakeImapServer, uid: int, extra_headers: str) -> MailRef:
    server.mailbox("INBOX").messages[uid] = plain_message(extra_headers=extra_headers)
    return MailRef("INBOX", uid, 777)


LONG_URL = "https://unsub.example.com/u?token=" + "Ab1-" * 400  # 1634 characters, like real tracking tokens


def test_list_unsubscribe_very_long_url_is_never_cut(server: FakeImapServer) -> None:
    """A List-Unsubscribe URL of more than 1500 characters comes back complete (it was cut at 500 with "…")."""
    assert len(LONG_URL) > 1500
    ref = _unsubscribe_message(server, 40, f"List-Unsubscribe: <{LONG_URL}>\r\n")
    content = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["List-Unsubscribe"])
    assert content.headers["list-unsubscribe"] == [f"<{LONG_URL}>"]
    assert content.list_unsubscribe == {"domains": ["unsub.example.com"], "one_click": False, "urls": [LONG_URL], "mailto": []}
    assert "…" not in str(content.headers)


def test_list_unsubscribe_several_urls_mailto_and_one_click(server: FakeImapServer) -> None:
    """Several entries in angle brackets are split; mailto keeps its query; one-click from List-Unsubscribe-Post."""
    mailto = "mailto:unsub@lists.example.org?subject=unsubscribe%20me&body=x%0Ay"
    ref = _unsubscribe_message(
        server,
        41,
        f"List-Unsubscribe: <https://a.example.com/u?t=1>, <{mailto}>,<http://b.example.net/x?y=%0A>\r\n"
        "List-Unsubscribe-Post: List-Unsubscribe=One-Click\r\n",
    )
    content = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["list-unsubscribe", "List-Unsubscribe-Post"])
    assert content.list_unsubscribe == {
        "domains": ["a.example.com", "b.example.net", "mailto"],
        "one_click": True,
        "urls": ["https://a.example.com/u?t=1", "http://b.example.net/x?y=%0A"],  # %0A stays encoded
        "mailto": [mailto],
    }
    assert content.headers["list-unsubscribe-post"] == ["List-Unsubscribe=One-Click"]


def test_list_unsubscribe_folded_header(server: FakeImapServer) -> None:
    """A folded header (RFC 5322 line break plus whitespace, also inside the brackets) is unfolded, not decoded."""
    ref = _unsubscribe_message(
        server,
        42,
        "List-Unsubscribe: <mailto:u@lists.example.org?subject=unsub>,\r\n"
        " <https://c.example.com/unsubscribe?id=12345&sig=abc%2Fdef\r\n\t&x=1>\r\n",
    )
    content = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["List-Unsubscribe"])
    assert content.list_unsubscribe is not None
    assert content.list_unsubscribe["urls"] == ["https://c.example.com/unsubscribe?id=12345&sig=abc%2Fdef&x=1"]
    assert content.list_unsubscribe["mailto"] == ["mailto:u@lists.example.org?subject=unsub"]
    (value,) = content.headers["list-unsubscribe"]
    assert isinstance(value, str) and "\n" not in value and "\r" not in value and "%2F" in value


def test_list_unsubscribe_missing(server: FakeImapServer) -> None:
    """Without the header there is neither a header entry nor a list_unsubscribe field."""
    ref = _unsubscribe_message(server, 43, "X-Other: 1\r\n")
    content = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["List-Unsubscribe"])
    assert "list-unsubscribe" not in content.headers
    assert content.list_unsubscribe is None


def test_long_other_header_is_marked_and_can_be_continued(server: FakeImapServer) -> None:
    """Above 8000 characters another header is cut visibly (truncated, total_chars) and continues at next_offset."""
    long_value = "".join(f"{i:05d}," for i in range(2000))  # 12000 characters
    ref = _unsubscribe_message(server, 44, f"X-Long: {long_value}\r\n")
    first = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["X-Long"])
    (entry,) = first.headers["x-long"]
    assert entry == {
        "value": long_value[:HEADER_REQUEST_MAX],
        "truncated": True,
        "total_chars": 12000,
        "offset": 0,
        "next_offset": HEADER_REQUEST_MAX,
    }
    rest = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["X-Long"], header_offset=HEADER_REQUEST_MAX)
    (tail,) = rest.headers["x-long"]
    assert tail["value"] == long_value[HEADER_REQUEST_MAX:] and tail["truncated"] is False and tail["next_offset"] is None
    assert entry["value"] + tail["value"] == long_value


def test_requested_headers_share_a_budget_but_list_unsubscribe_comes_first(server: FakeImapServer) -> None:
    """All requested headers together at most HEADER_REQUEST_BUDGET characters; List-Unsubscribe is not counted."""
    values = {name: name[-1] * 7000 for name in ("X-A", "X-B", "X-C")}
    headers = "".join(f"{name}: {value}\r\n" for name, value in values.items()) + f"List-Unsubscribe: <{LONG_URL}>\r\n"
    ref = _unsubscribe_message(server, 45, headers)
    content = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["X-A", "X-B", "X-C", "List-Unsubscribe"])
    assert content.headers["x-a"] == [values["X-A"]] and content.headers["x-b"] == [values["X-B"]]
    (third,) = content.headers["x-c"]
    assert third["truncated"] is True and len(third["value"]) == HEADER_REQUEST_BUDGET - 14000 and third["total_chars"] == 7000
    assert content.headers["list-unsubscribe"] == [f"<{LONG_URL}>"]


def test_list_unsubscribe_emergency_brake(server: FakeImapServer) -> None:
    """A crafted header beyond 16000 characters is cut visibly, never silently; a cut entry is dropped."""
    urls = [f"https://u{i}.example.com/" + "x" * 3000 for i in range(8)]  # about 24800 characters
    ref = _unsubscribe_message(server, 46, "List-Unsubscribe: " + ", ".join(f"<{u}>" for u in urls) + "\r\n")
    content = get_message(_session(server), _info(), ref, body_format="text", extra_headers=["List-Unsubscribe"])
    (entry,) = content.headers["list-unsubscribe"]
    assert entry["truncated"] is True and len(entry["value"]) == LIST_UNSUBSCRIBE_MAX and entry["next_offset"] == LIST_UNSUBSCRIBE_MAX
    assert content.list_unsubscribe is not None and content.list_unsubscribe["truncated"] is True
    assert content.list_unsubscribe["urls"] == urls[:5]  # the sixth entry is cut in the middle and left out
