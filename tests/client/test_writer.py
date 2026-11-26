"""Tests for the organising operations (M4) against the in-memory fake server."""

from __future__ import annotations

from collections.abc import Generator
from email import message_from_bytes, policy
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.gmx_mail_mcp.client.errors import (
    GmxFolderExistsError,
    GmxFolderNotFoundError,
    GmxInvalidFolderNameError,
    GmxMessageTooLargeError,
    GmxProtectedFolderError,
    GmxProtectedTargetError,
    GmxUidValidityError,
    GmxUnsupportedError,
    GmxWrongFolderError,
)
from custom_components.gmx_mail_mcp.client.folders import map_special_folders, parse_folder_list
from custom_components.gmx_mail_mcp.client.imap import ImapSession, ServerInfo, parse_capabilities
from custom_components.gmx_mail_mcp.client.reader import MailRef
from custom_components.gmx_mail_mcp.client.upload import Attachment
from custom_components.gmx_mail_mcp.client.writer import (
    DraftInput,
    build_draft,
    copy_or_move,
    create_folder,
    expand_uid_set,
    mark_spam,
    normalize_flags,
    open_folder,
    parse_addresses,
    parse_appenduid,
    parse_copyuid,
    rename_folder,
    save_draft,
    set_flags,
    set_subscription,
)

from .fakes import EMAIL, GMX_CAPABILITIES, FakeImapServer, plain_message

GMX_INFO = ServerInfo(capabilities=parse_capabilities(GMX_CAPABILITIES), folders=())


@pytest.fixture
def server() -> Generator[FakeImapServer]:
    """INBOX with three messages (unique Message-IDs) plus the GMX folders as mailboxes."""
    fake = FakeImapServer()
    inbox = fake.mailbox("INBOX", uidvalidity=777)
    for uid in (1, 2, 3):
        inbox.messages[uid] = plain_message(subject=f"m{uid}", message_id=f"<m{uid}@example.org>")
    for name in ("Archiv", "Gesendet", "Entwürfe", "Gelöscht", "Spamverdacht"):
        fake.mailbox(name, uidvalidity=900)
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", fake):
        yield fake


def _session(server: FakeImapServer) -> ImapSession:
    client = server("imap.gmx.net", 993)
    client.login(EMAIL, server.password)
    return client


def _refs(*uids: int, validity: int = 777) -> list[MailRef]:
    return [MailRef("INBOX", uid, validity) for uid in uids]


def _cmds(server: FakeImapServer, name: str) -> list[tuple[Any, ...]]:
    return [c for c in server.commands if c[0] == name]


def _folder_map(server: FakeImapServer) -> Any:
    return map_special_folders(parse_folder_list(server.folders))


# --------------------------------------------------------------------------- parsing helpers


def test_uid_set_and_response_parsing() -> None:
    """UID sets, COPYUID (tagged text or stored code) and APPENDUID."""
    assert expand_uid_set("1:3,7,9:8") == [1, 2, 3, 7, 9, 8]
    assert parse_copyuid([b"[COPYUID 900 1:2 5:6] Done"]) == (900, [1, 2], [5, 6])
    assert parse_copyuid([b"Done", b"COPYUID 900 3 10"]) == (900, [3], [10])
    assert parse_copyuid([b"Move completed"]) is None
    assert parse_appenduid(b"[APPENDUID 900 42] Append completed") == (900, 42)
    assert parse_appenduid(b"Append completed") is None
    assert parse_appenduid(b"[APPENDUID 900 1:2] multi") is None


def test_normalize_flags() -> None:
    """Allowed: seen, flagged, answered, keywords. Not: \\Deleted, \\Draft, \\Recent, odd keywords."""
    assert normalize_flags(["Seen", "flagged", "answered", "$Steuer", "Wichtig"]) == (
        ["\\Seen", "\\Flagged", "\\Answered"],
        ["$Steuer", "Wichtig"],
    )
    # the IMAP spelling shown by search results is accepted too (\\Seen), but only for the allowed system flags
    assert normalize_flags(["\\Seen", "\\FLAGGED", " \\Answered "]) == (["\\Seen", "\\Flagged", "\\Answered"], [])
    for bad in ("\\Deleted", "deleted", "\\Draft", "\\Recent", "a b", "x" * 70, "\\", "\\$Steuer", "\\Wichtig", ""):
        with pytest.raises(ValueError):
            normalize_flags([bad])


# --------------------------------------------------------------------------- folder handling


def test_open_folder_without_unselect_never_closes(server: FakeImapServer) -> None:
    """Without UNSELECT the folder is re-opened read-only (no CLOSE, no implicit expunge)."""
    info = ServerInfo(capabilities=frozenset({"IMAP4REV1", "MOVE", "UIDPLUS"}), folders=())
    session = _session(server)
    with open_folder(session, info, "INBOX", readonly=False):
        pass
    assert _cmds(server, "select") == [("select", "INBOX", False), ("select", "INBOX", True)]
    with open_folder(session, GMX_INFO, "INBOX", readonly=False):
        pass
    assert _cmds(server, "unselect") == [("unselect",)]


# --------------------------------------------------------------------------- flags


def test_set_flags(server: FakeImapServer) -> None:
    """Flags and keywords are stored read-write; missing UIDs are reported."""
    result = set_flags(_session(server), GMX_INFO, _refs(1, 2, 99), ["seen", "$Steuer"], [])
    assert result.count == 2
    assert result.missing == [{"folder": "INBOX", "uid": 99, "uidvalidity": 777}]
    inbox = server.mailbox("INBOX")
    assert inbox.messages[1].flags == (b"\\Seen", b"$Steuer")
    set_flags(_session(server), GMX_INFO, _refs(1), [], ["$Steuer"])
    assert inbox.messages[1].flags == (b"\\Seen",)
    assert ("select", "INBOX", False) in server.commands


def test_set_flags_checks_uidvalidity_and_keyword_support(server: FakeImapServer) -> None:
    """Old references are refused; keywords only where PERMANENTFLAGS contains \\*."""
    with pytest.raises(GmxUidValidityError):
        set_flags(_session(server), GMX_INFO, _refs(1, validity=1), ["seen"], [])
    server.permanent_flags = (b"\\Seen", b"\\Flagged")
    with pytest.raises(GmxUnsupportedError) as err:
        set_flags(_session(server), GMX_INFO, _refs(1), ["$Steuer"], [])
    assert err.value.reason == "keywords_not_supported"
    assert set_flags(_session(server), GMX_INFO, _refs(1), ["flagged"], []).count == 1


# --------------------------------------------------------------------------- move / copy


@pytest.mark.parametrize("mode", ["untagged", "tagged"])
def test_move_reads_copyuid_tagged_or_untagged(server: FakeImapServer, mode: str) -> None:
    """GMX reports COPYUID for MOVE untagged; both forms give the new references in request order."""
    server.move_copyuid = mode
    result = copy_or_move(_session(server), GMX_INFO, _refs(3, 1), "Archiv", move=True)
    assert result.count == 2
    assert result.new_refs == [{"folder": "Archiv", "uid": 2, "uidvalidity": 900}, {"folder": "Archiv", "uid": 1, "uidvalidity": 900}]
    assert result.warnings == []
    assert sorted(server.mailbox("INBOX").messages) == [2]


def test_stale_copyuid_from_an_earlier_command_is_ignored(server: FakeImapServer) -> None:
    """imaplib never clears stored response codes; a leftover COPYUID must not be mistaken for the new one."""
    session = _session(server)
    session._imap.untagged_responses["COPYUID"] = [b"900 7 77"]  # type: ignore[attr-defined]
    server.move_copyuid = "none"
    result = copy_or_move(session, GMX_INFO, _refs(1), "Archiv", move=True)
    # No COPYUID now -> Message-ID fallback finds the real UID 1 in Archiv (not the stale 77).
    assert result.new_refs == [{"folder": "Archiv", "uid": 1, "uidvalidity": 900}]


def test_message_id_fallback_and_incomplete_warning(server: FakeImapServer) -> None:
    """Without COPYUID the new UIDs come from Message-IDs; unmatched ones give a warning."""
    server.move_copyuid = "none"
    inbox = server.mailbox("INBOX")
    inbox.messages[4] = plain_message(message_id="")  # no Message-ID at all
    result = copy_or_move(_session(server), GMX_INFO, _refs(2, 4), "Archiv", move=True)
    assert result.new_refs[0] == {"folder": "Archiv", "uid": 1, "uidvalidity": 900}
    assert result.new_refs[1] is None
    assert result.warnings == ["new_refs_incomplete"]


def test_copy_keeps_the_original(server: FakeImapServer) -> None:
    """COPY returns the copies' references (tagged COPYUID) and leaves the source untouched."""
    result = copy_or_move(_session(server), GMX_INFO, _refs(1, 2), "Archiv", move=False)
    assert [r["uid"] for r in result.new_refs if r] == [1, 2]
    assert sorted(server.mailbox("INBOX").messages) == [1, 2, 3]


def test_move_without_move_capability_expunges_only_the_moved_uids(server: FakeImapServer) -> None:
    """Fallback COPY + \\Deleted + UID EXPUNGE must not touch other messages already marked \\Deleted."""
    server.capabilities = tuple(c for c in server.capabilities if c != b"MOVE")
    info = ServerInfo(capabilities=parse_capabilities(server.capabilities), folders=())
    inbox = server.mailbox("INBOX")
    inbox.messages[3].flags = (b"\\Deleted",)  # marked by another client, not ours
    result = copy_or_move(_session(server), info, _refs(1), "Archiv", move=True)
    assert result.count == 1
    assert sorted(inbox.messages) == [2, 3]  # 3 is still there
    assert _cmds(server, "uid_expunge") == [("uid_expunge", [1])]
    assert _cmds(server, "move") == []


def test_move_needs_move_or_uidplus(server: FakeImapServer) -> None:
    """Without MOVE and UIDPLUS moving is refused (a mailbox-wide EXPUNGE is never an option)."""
    info = ServerInfo(capabilities=frozenset({"IMAP4REV1"}), folders=())
    with pytest.raises(GmxUnsupportedError) as err:
        copy_or_move(_session(server), info, _refs(1), "Archiv", move=True)
    assert err.value.reason == "move_unsupported"
    assert copy_or_move(_session(server), info, _refs(1), "Archiv", move=False).count == 1  # copying still works


def test_move_target_and_source_checks(server: FakeImapServer) -> None:
    """Unknown target folder, same folder, missing messages, old references."""
    with pytest.raises(GmxFolderNotFoundError):
        copy_or_move(_session(server), GMX_INFO, _refs(1), "Nope", move=True)
    with pytest.raises(GmxInvalidFolderNameError) as err:
        copy_or_move(_session(server), GMX_INFO, [MailRef("Archiv", 1, 900)], "Archiv", move=True)
    assert err.value.reason == "same_folder"
    result = copy_or_move(_session(server), GMX_INFO, _refs(99), "Archiv", move=True)
    assert result.count == 0 and result.new_refs == [] and result.missing == [{"folder": "INBOX", "uid": 99, "uidvalidity": 777}]
    with pytest.raises(GmxUidValidityError):
        copy_or_move(_session(server), GMX_INFO, _refs(1, validity=5), "Archiv", move=True)


# --------------------------------------------------------------------------- folders


def test_create_folder(server: FakeImapServer) -> None:
    """Top level and below a user folder; not below special folders; names validated; no duplicates."""
    session = _session(server)
    folder_map = _folder_map(server)
    assert create_folder(session, GMX_INFO, folder_map, "Rechnungen", None) == "Rechnungen"
    assert create_folder(session, GMX_INFO, folder_map, " 2026 ", "Rechnungen") == "Rechnungen/2026"
    for parent in ("Gesendet", "Spamverdacht", "Entwürfe", "Gelöscht"):  # \\NoInferiors at GMX; the trash by rule (D70)
        with pytest.raises(GmxProtectedFolderError):
            create_folder(session, GMX_INFO, folder_map, "x", parent)
    # INBOX may have subfolders (D70); they are ordinary folders.
    assert create_folder(session, GMX_INFO, folder_map, "Rechnungen", "INBOX") == "INBOX/Rechnungen"
    assert rename_folder(session, GMX_INFO, folder_map, "INBOX/Rechnungen", "Belege") == "INBOX/Belege"
    with pytest.raises(GmxFolderNotFoundError):
        create_folder(session, GMX_INFO, folder_map, "x", "Nope")
    for bad in ("", "a/b", "50%", "a*", "..", "x" * 101, "tab\tname"):
        with pytest.raises(GmxInvalidFolderNameError):
            create_folder(session, GMX_INFO, folder_map, bad, None)
    with pytest.raises(GmxFolderExistsError):
        create_folder(session, GMX_INFO, folder_map, "Archiv", None)
    with pytest.raises(GmxFolderExistsError):
        create_folder(session, GMX_INFO, folder_map, "inbox", None)


def test_create_folder_respects_noinferiors(server: FakeImapServer) -> None:
    """A folder that cannot have children is refused as parent."""
    server.folders.append(((b"\\NoInferiors",), b"/", "Flach"))
    with pytest.raises(GmxProtectedFolderError):
        create_folder(_session(server), GMX_INFO, _folder_map(server), "x", "Flach")


def test_rename_folder(server: FakeImapServer) -> None:
    """Renaming stays within the parent; INBOX and special folders are protected."""
    session = _session(server)
    folder_map = _folder_map(server)
    create_folder(session, GMX_INFO, folder_map, "Rechnungen", None)
    create_folder(session, GMX_INFO, folder_map, "2026", "Rechnungen")
    assert rename_folder(session, GMX_INFO, folder_map, "Rechnungen/2026", "2026 erledigt") == "Rechnungen/2026 erledigt"
    assert rename_folder(session, GMX_INFO, folder_map, "Archiv", "Ablage") == "Ablage"
    for protected in ("INBOX", "Gesendet", "Gelöscht"):
        with pytest.raises(GmxProtectedFolderError):
            rename_folder(session, GMX_INFO, folder_map, protected, "x")
    with pytest.raises(GmxFolderNotFoundError):
        rename_folder(session, GMX_INFO, folder_map, "Nope", "x")
    with pytest.raises(GmxFolderExistsError):
        rename_folder(session, GMX_INFO, folder_map, "Ablage", "Rechnungen")


# --------------------------------------------------------------------------- drafts


def test_build_draft_headers() -> None:
    """UTF-8 subject, addresses, HTML alternative, own Message-ID; reply headers threaded."""
    draft = DraftInput(
        sender=EMAIL,
        to=["Jörg Müller <joerg@example.org>"],
        bcc=["geheim@example.org"],
        subject="Größe ✓",
        body_text="Hallo",
        body_html="<p>Hallo</p>",
    )
    message = build_draft(draft)
    parsed = message_from_bytes(message.as_bytes(), policy=policy.default)
    assert parsed["Subject"] == "Größe ✓"
    assert parsed["To"] == "Jörg Müller <joerg@example.org>"
    assert parsed["Bcc"] == "geheim@example.org"  # kept in the draft (like mail clients do)
    assert parsed["Message-ID"].endswith("@gmx.test>")
    assert parsed.get_content_type() == "multipart/alternative"
    reply = build_draft(
        DraftInput(sender=EMAIL, body_text="x"), {"subject": "Rechnung", "message-id": "<o@x>", "references": "<a@x> <b@x>"}
    )
    assert reply["Subject"] == "Re: Rechnung"
    assert reply["In-Reply-To"] == "<o@x>"
    assert reply["References"] == "<a@x> <b@x> <o@x>"
    assert build_draft(DraftInput(sender=EMAIL, body_text="x"), {"subject": "RE: alt", "message-id": None})["Subject"] == "RE: alt"


def test_parse_addresses() -> None:
    """Header injection and malformed addresses are refused."""
    assert [a.addr_spec for a in parse_addresses(["a@b.de", "Name <c@d.org>"])] == ["a@b.de", "c@d.org"]
    for bad in ("nobody", "a@b", "a@b.de\r\nBcc: evil@x.de", "a@b.de, c@d.de", "@b.de", "a b@c.de"):
        with pytest.raises(ValueError):
            parse_addresses([bad])


def test_save_draft(server: FakeImapServer) -> None:
    """APPEND to Entwürfe with \\Draft and \\Seen; the reference comes from APPENDUID."""
    folder_map = _folder_map(server)
    draft = DraftInput(sender=EMAIL, to=["anna@example.org"], subject="Entwurf", body_text="Hallo", in_reply_to=MailRef("INBOX", 2, 777))
    ref, size = save_draft(_session(server), GMX_INFO, folder_map, draft)
    assert ref == {"folder": "Entwürfe", "uid": 1, "uidvalidity": 900}
    append = _cmds(server, "append")[0]
    assert append[1:3] == ("Entwürfe", ("\\Draft", "\\Seen"))
    stored = message_from_bytes(server.appended[0], policy=policy.default)
    assert stored["In-Reply-To"] == "<m2@example.org>"
    assert size == len(server.appended[0])
    # The original was only read (EXAMINE), never opened read-write.
    assert ("select", "INBOX", True) in server.commands and ("select", "INBOX", False) not in server.commands


def test_save_draft_fallbacks_and_limits(server: FakeImapServer) -> None:
    """Without APPENDUID the draft is found by Message-ID; APPENDLIMIT and a missing drafts folder are errors."""
    server.appenduid = False
    folder_map = _folder_map(server)
    ref, _size = save_draft(_session(server), GMX_INFO, folder_map, DraftInput(sender=EMAIL, body_text="x"))
    assert ref == {"folder": "Entwürfe", "uid": 1, "uidvalidity": 900}
    small = ServerInfo(capabilities=parse_capabilities([b"IMAP4REV1", b"APPENDLIMIT=100"]), folders=())
    with pytest.raises(GmxMessageTooLargeError):
        save_draft(_session(server), small, folder_map, DraftInput(sender=EMAIL, body_text="x" * 500))
    with pytest.raises(GmxFolderNotFoundError):
        save_draft(_session(server), GMX_INFO, map_special_folders([]), DraftInput(sender=EMAIL, body_text="x"))
    with pytest.raises(GmxUidValidityError):
        save_draft(_session(server), GMX_INFO, folder_map, DraftInput(sender=EMAIL, body_text="x", in_reply_to=MailRef("INBOX", 2, 1)))


# --------------------------------------------------------------------------- M3: protected targets, spam, subscriptions


def test_copy_or_move_refuses_forbidden_target(server: FakeImapServer) -> None:
    """The trash is reserved for gmx_trash (level D): nothing is copied or moved there, the source stays untouched."""
    for move in (True, False):
        with pytest.raises(GmxProtectedTargetError):
            copy_or_move(_session(server), GMX_INFO, _refs(1), "Gelöscht", move=move, forbidden_targets={"Gelöscht"})
    assert sorted(server.mailbox("INBOX").messages) == [1, 2, 3]
    assert not server.mailbox("Gelöscht").messages
    assert not _cmds(server, "select")  # refused before any folder was opened


def test_mark_spam_moves_with_keywords(server: FakeImapServer) -> None:
    """Spam: $Junk set (and $NotJunk cleared) before the move into the junk folder; MOVE keeps the flags."""
    folder_map = _folder_map(server)
    result = mark_spam(_session(server), GMX_INFO, folder_map, _refs(2), spam=True)
    assert result.count == 1
    assert result.new_refs == [{"folder": "Spamverdacht", "uid": 1, "uidvalidity": 900}]
    assert result.warnings == []
    assert server.mailbox("Spamverdacht").messages[1].flags == (b"$Junk",)
    assert 2 not in server.mailbox("INBOX").messages
    with pytest.raises(GmxInvalidFolderNameError):
        mark_spam(_session(server), GMX_INFO, folder_map, [MailRef("Spamverdacht", 1, 900)], spam=True)


def test_mark_not_spam_only_from_junk(server: FakeImapServer) -> None:
    """Not spam: only mail in the junk folder; it goes to the inbox with $NotJunk instead of $Junk."""
    folder_map = _folder_map(server)
    mark_spam(_session(server), GMX_INFO, folder_map, _refs(2), spam=True)
    with pytest.raises(GmxWrongFolderError) as err:
        mark_spam(_session(server), GMX_INFO, folder_map, _refs(1), spam=False)
    assert err.value.reason == "not_in_junk"
    result = mark_spam(_session(server), GMX_INFO, folder_map, [MailRef("Spamverdacht", 1, 900)], spam=False)
    assert result.new_refs == [{"folder": "INBOX", "uid": 4, "uidvalidity": 777}]
    assert server.mailbox("INBOX").messages[4].flags == (b"$NotJunk",)
    assert not server.mailbox("Spamverdacht").messages


def test_mark_spam_without_keywords_or_junk_folder(server: FakeImapServer) -> None:
    """No \\* in PERMANENTFLAGS: moved anyway, with a warning; no junk folder: error before anything happens."""
    server.permanent_flags = (b"\\Seen",)
    result = mark_spam(_session(server), GMX_INFO, _folder_map(server), _refs(1), spam=True)
    assert result.count == 1
    assert result.warnings == ["keywords_not_supported"]
    with pytest.raises(GmxFolderNotFoundError):
        mark_spam(_session(server), GMX_INFO, map_special_folders([]), _refs(2), spam=True)
    assert 2 in server.mailbox("INBOX").messages


def test_set_subscription(server: FakeImapServer) -> None:
    """SUBSCRIBE / UNSUBSCRIBE only for existing folders."""
    set_subscription(_session(server), GMX_INFO, "Archiv", subscribed=False)
    set_subscription(_session(server), GMX_INFO, "Archiv", subscribed=True)
    assert _cmds(server, "unsubscribe") == [("unsubscribe", "Archiv")]
    assert _cmds(server, "subscribe") == [("subscribe", "Archiv")]
    with pytest.raises(GmxFolderNotFoundError):
        set_subscription(_session(server), GMX_INFO, "Gibt es nicht", subscribed=True)


def test_draft_with_attachments_and_german_reply_prefix(server: FakeImapServer) -> None:
    """Attachments become MIME parts; an "AW:" subject is kept as it is (no "Re: AW:")."""
    files = [Attachment("plan.pdf", b"%PDF-1.4 x", "application/pdf"), Attachment("notiz.txt", "Grüße".encode(), "text/plain")]
    draft = DraftInput(sender=EMAIL, to=["anna@example.org"], subject="Mit Anhang", body_text="Hallo", attachments=files)
    ref, _size = save_draft(_session(server), GMX_INFO, _folder_map(server), draft)
    assert ref is not None
    stored = message_from_bytes(server.appended[0], policy=policy.default)
    assert stored.get_content_type() == "multipart/mixed"
    parts = [(p.get_filename(), p.get_content_type(), p.get_payload(decode=True)) for p in stored.iter_attachments()]
    assert parts == [("plan.pdf", "application/pdf", b"%PDF-1.4 x"), ("notiz.txt", "text/plain", "Grüße".encode())]
    reply = build_draft(DraftInput(sender=EMAIL, body_text="x"), {"subject": "AW: Termin", "message-id": "<o@x>"})
    assert reply["Subject"] == "AW: Termin"
