"""Tests for the deleting operations (M5) and the confirmation codes against the in-memory fake server."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.gmx_mail_mcp.client.deleter import (
    PREVIEW_DETAILS,
    check_delete_folder,
    delete_folder,
    delete_permanently,
    preview_permanent,
    trash,
)
from custom_components.gmx_mail_mcp.client.errors import (
    GmxDeleteRuleError,
    GmxFolderNotFoundError,
    GmxProtectedFolderError,
    GmxUidValidityError,
    GmxUnsupportedError,
)
from custom_components.gmx_mail_mcp.client.folders import map_special_folders, parse_folder_list
from custom_components.gmx_mail_mcp.client.imap import ImapSession, ServerInfo, parse_capabilities
from custom_components.gmx_mail_mcp.client.reader import MailRef
from custom_components.gmx_mail_mcp.confirm import ConfirmTokens, InvalidTokenError

from .fakes import EMAIL, GMX_CAPABILITIES, FakeImapServer, plain_message

GMX_INFO = ServerInfo(capabilities=parse_capabilities(GMX_CAPABILITIES), folders=())


@pytest.fixture
def server() -> Generator[FakeImapServer]:
    """INBOX with three messages, the trash with two, spam with one; all GMX folders exist."""
    fake = FakeImapServer()
    inbox = fake.mailbox("INBOX", uidvalidity=777)
    for uid in (1, 2, 3):
        inbox.messages[uid] = plain_message(subject=f"m{uid}", message_id=f"<m{uid}@example.org>")
    trash_box = fake.mailbox("Gelöscht", uidvalidity=900)
    for uid in (1, 2):
        trash_box.messages[uid] = plain_message(subject=f"t{uid}", message_id=f"<t{uid}@example.org>")
    fake.mailbox("Spamverdacht", uidvalidity=901).messages[1] = plain_message(subject="s1", message_id="<s1@example.org>")
    for name in ("Archiv", "Gesendet", "Entwürfe"):
        fake.mailbox(name, uidvalidity=902)
    with patch("custom_components.gmx_mail_mcp.client.imap.IMAPClient", fake):
        yield fake


def _session(server: FakeImapServer) -> ImapSession:
    client = server("imap.gmx.net", 993)
    client.login(EMAIL, server.password)
    return client


def _map(server: FakeImapServer) -> Any:
    return map_special_folders(parse_folder_list(server.folders))


def _cmds(server: FakeImapServer, name: str) -> list[tuple[Any, ...]]:
    return [c for c in server.commands if c[0] == name]


# --------------------------------------------------------------------------- trash


def test_trash_moves_into_the_trash_with_new_references(server: FakeImapServer) -> None:
    """Trash = MOVE into the trash folder; the new references allow moving messages back."""
    target, result = trash(_session(server), GMX_INFO, _map(server), [MailRef("INBOX", 2, 777)])
    assert target == "Gelöscht"
    assert result.count == 1 and result.new_refs == [{"folder": "Gelöscht", "uid": 3, "uidvalidity": 900}]
    assert 2 not in server.mailboxes["INBOX"].messages
    assert not _cmds(server, "uid_expunge")  # MOVE, nothing expunged


def test_trash_refuses_messages_already_in_the_trash(server: FakeImapServer) -> None:
    """'Delete in the trash' means 'delete for good' at GMX - never by accident (DECISIONS Nr. 46)."""
    with pytest.raises(GmxDeleteRuleError) as err:
        trash(_session(server), GMX_INFO, _map(server), [MailRef("INBOX", 1, 777), MailRef("Gelöscht", 1, 900)])
    assert err.value.reason == "already_in_trash"
    assert not _cmds(server, "move") and len(server.mailboxes["INBOX"].messages) == 3


def test_trash_without_trash_folder(server: FakeImapServer) -> None:
    """Without a known trash folder nothing happens."""
    server.folders = [f for f in server.folders if f[2] != "Gelöscht"]
    with pytest.raises(GmxDeleteRuleError) as err:
        trash(_session(server), GMX_INFO, _map(server), [MailRef("INBOX", 1, 777)])
    assert err.value.reason == "no_trash_folder"


# --------------------------------------------------------------------------- delete permanently


def test_preview_lists_details_and_missing_without_changing_anything(server: FakeImapServer) -> None:
    """The preview is read-only (EXAMINE) and shows date, sender and subject."""
    refs = [MailRef("Gelöscht", 1, 900), MailRef("Gelöscht", 9, 900), MailRef("Spamverdacht", 1, 901)]
    preview = preview_permanent(_session(server), GMX_INFO, _map(server), refs)
    assert preview["count"] == 2 and preview["folders"] == ["Gelöscht", "Spamverdacht"]
    assert preview["missing"] == [{"folder": "Gelöscht", "uid": 9, "uidvalidity": 900}]
    assert [m["subject"] for m in preview["messages"]] == ["t1", "s1"]
    assert set(preview["messages"][0]) == {"ref", "date", "from", "subject"} and preview["more"] == 0
    assert not _cmds(server, "add_flags") and not _cmds(server, "uid_expunge")


def test_preview_details_are_capped(server: FakeImapServer) -> None:
    """At most PREVIEW_DETAILS messages are listed; the rest is counted."""
    box = server.mailboxes["Gelöscht"]
    for uid in range(3, 16):
        box.messages[uid] = plain_message(subject=f"t{uid}")
    refs = [MailRef("Gelöscht", uid, 900) for uid in range(1, 16)]
    preview = preview_permanent(_session(server), GMX_INFO, _map(server), refs)
    assert preview["count"] == 15 and len(preview["messages"]) == PREVIEW_DETAILS and preview["more"] == 5


def test_delete_permanently_only_from_trash_or_spam(server: FakeImapServer) -> None:
    """Messages elsewhere must go to the trash first (DECISIONS Nr. 47)."""
    session = _session(server)
    for func in (preview_permanent, delete_permanently):
        with pytest.raises(GmxDeleteRuleError) as err:
            func(session, GMX_INFO, _map(server), [MailRef("Gelöscht", 1, 900), MailRef("INBOX", 1, 777)])
        assert err.value.reason == "not_in_trash_or_junk"
    assert not _cmds(server, "add_flags")


def test_delete_permanently_expunges_exactly_these_uids(server: FakeImapServer) -> None:
    """\\Deleted + UID EXPUNGE of the given UIDs only; a foreign \\Deleted message in the same folder survives."""
    box = server.mailboxes["Gelöscht"]
    box.messages[2].flags = (b"\\Deleted",)  # marked by another client, not ours to expunge
    result = delete_permanently(_session(server), GMX_INFO, _map(server), [MailRef("Gelöscht", 1, 900), MailRef("Gelöscht", 7, 900)])
    assert result.count == 1 and result.missing == [{"folder": "Gelöscht", "uid": 7, "uidvalidity": 900}]
    assert _cmds(server, "uid_expunge") == [("uid_expunge", [1])]
    assert set(box.messages) == {2}  # the foreign one is still there
    assert not result.warnings


def test_delete_permanently_needs_uidplus(server: FakeImapServer) -> None:
    """Without UIDPLUS only a mailbox-wide EXPUNGE would remain - refused."""
    info = ServerInfo(capabilities=parse_capabilities(tuple(c for c in GMX_CAPABILITIES if c != b"UIDPLUS")), folders=())
    with pytest.raises(GmxUnsupportedError) as err:
        delete_permanently(_session(server), info, _map(server), [MailRef("Gelöscht", 1, 900)])
    assert err.value.reason == "uidplus_required"


def test_delete_permanently_checks_uidvalidity(server: FakeImapServer) -> None:
    """A changed UIDVALIDITY means the references point to other messages: refuse."""
    with pytest.raises(GmxUidValidityError):
        delete_permanently(_session(server), GMX_INFO, _map(server), [MailRef("Gelöscht", 1, 123)])
    assert not _cmds(server, "add_flags")


def test_delete_permanently_reports_messages_the_server_kept(server: FakeImapServer) -> None:
    """If the server did not expunge (should not happen), the result says so instead of claiming success."""
    session = _session(server)
    with patch.object(type(session), "uid_expunge", lambda self, uids: b"OK"):
        result = delete_permanently(session, GMX_INFO, _map(server), [MailRef("Gelöscht", 1, 900)])
    assert result.count == 0 and result.warnings == ["not_expunged"]


# --------------------------------------------------------------------------- delete folder


def test_delete_folder_rules(server: FakeImapServer) -> None:
    """Only empty folders without subfolders; INBOX and special folders are protected."""
    session = _session(server)
    folder_map = _map(server)
    for path in ("INBOX", "Gelöscht", "Spamverdacht", "Entwürfe", "Gesendet"):
        with pytest.raises(GmxProtectedFolderError):
            check_delete_folder(session, folder_map, path)
    with pytest.raises(GmxFolderNotFoundError):
        check_delete_folder(session, folder_map, "Gibtsnicht")
    server.folders.append(((b"\\HasNoChildren",), b"/", "Archiv/2025"))
    server.mailbox("Archiv/2025")
    with pytest.raises(GmxDeleteRuleError) as err:
        check_delete_folder(session, folder_map, "Archiv")
    assert err.value.reason == "folder_has_children"
    server.mailboxes["Archiv/2025"].messages[1] = plain_message()
    with pytest.raises(GmxDeleteRuleError) as err:
        delete_folder(session, folder_map, "Archiv/2025")
    assert err.value.reason == "folder_not_empty"
    del server.mailboxes["Archiv/2025"].messages[1]
    assert check_delete_folder(session, folder_map, "Archiv/2025") == {"path": "Archiv/2025", "messages": 0}
    assert delete_folder(session, folder_map, "Archiv/2025") == "Archiv/2025"
    assert _cmds(server, "delete_folder") == [("delete_folder", "Archiv/2025")]


# --------------------------------------------------------------------------- confirmation codes


def test_confirmation_codes() -> None:
    """Valid once, for exactly the action and parameters, for 5 minutes, and only with this key."""
    now = [1000.0]
    tokens = ConfirmTokens(clock=lambda: now[0])
    params = {"refs": [{"folder": "Gelöscht", "uid": 1, "uidvalidity": 900}]}
    code = tokens.issue("delete_permanently", params)
    with pytest.raises(InvalidTokenError):
        tokens.consume(code, "delete_folder", params)  # other action
    with pytest.raises(InvalidTokenError):
        tokens.consume(code, "delete_permanently", {"refs": []})  # other messages
    with pytest.raises(InvalidTokenError):
        ConfirmTokens(clock=lambda: now[0]).consume(code, "delete_permanently", params)  # other key (restart/reload)
    with pytest.raises(InvalidTokenError):
        tokens.consume(code[:-2] + ("AA" if not code.endswith("AA") else "BB"), "delete_permanently", params)  # forged
    with pytest.raises(InvalidTokenError):
        tokens.consume("garbage", "delete_permanently", params)
    tokens.consume(code, "delete_permanently", params)
    with pytest.raises(InvalidTokenError):
        tokens.consume(code, "delete_permanently", params)  # single use
    late = tokens.issue("delete_permanently", params)
    now[0] += 301
    with pytest.raises(InvalidTokenError):
        tokens.consume(late, "delete_permanently", params)  # expired
    assert tokens.ttl == 300
