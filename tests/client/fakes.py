"""In-memory fakes for imapclient and smtplib (no sockets)."""

from __future__ import annotations

import base64
import re
import smtplib
import ssl
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from email.header import decode_header, make_header
from types import SimpleNamespace
from typing import Any

from imapclient.exceptions import IMAPClientAbortError, IMAPClientError, LoginError, ProtocolError

# Capabilities after login as measured against GMX in M0 (docs/probe-2026-11-20-v4.json).
GMX_CAPABILITIES = (
    b"IMAP4REV1 APPENDLIMIT=29360128 CHILDREN ENABLE ID IDLE LIST-EXTENDED LIST-STATUS LITERAL- MOVE NAMESPACE "
    b"SASL-IR SORT SPECIAL-USE THREAD=ORDEREDSUBJECT UIDPLUS UNSELECT WITHIN"
).split()

# Folder layout as found in M0 (GMX-NOTES row 14); "Archiv" stands in for a user folder.
GMX_FOLDERS: list[tuple[tuple[bytes, ...], bytes, str]] = [
    ((b"\\HasNoChildren",), b"/", "INBOX"),
    ((b"\\Drafts", b"\\NoInferiors"), b"/", "Entwürfe"),
    ((b"\\Trash", b"\\HasNoChildren"), b"/", "Gelöscht"),
    ((b"\\Sent", b"\\NoInferiors"), b"/", "Gesendet"),
    ((b"\\Junk", b"\\NoInferiors"), b"/", "Spamverdacht"),
    ((b"\\HasNoChildren",), b"/", "Archiv"),
]

PASSWORD = "app-password-for-tests"
EMAIL = "user@gmx.test"


@dataclass
class FakeImapServer:
    """Shared state of all fake IMAP connections."""

    password: str = PASSWORD
    capabilities: tuple[bytes, ...] = tuple(GMX_CAPABILITIES)
    folders: list[tuple[tuple[bytes, ...], bytes, str]] = field(default_factory=lambda: list(GMX_FOLDERS))
    connect_error: BaseException | None = None
    starttls_error: BaseException | None = None
    list_error: BaseException | None = None
    noop_error: BaseException | None = None
    login_error: BaseException | None = None
    logout_error: BaseException | None = None
    clients: list[FakeIMAPClient] = field(default_factory=list)
    logins: int = 0
    mailboxes: dict[str, FakeMailbox] = field(default_factory=lambda: {"INBOX": FakeMailbox()})
    status_error: BaseException | None = None
    # How COPYUID/APPENDUID are reported: "untagged" (GMX for MOVE), "tagged" (GMX for COPY), "none".
    move_copyuid: str = "untagged"
    copy_copyuid: str = "tagged"
    appenduid: bool = True
    permanent_flags: tuple[bytes, ...] = (b"\\Answered", b"\\Flagged", b"\\Draft", b"\\Deleted", b"\\Seen", b"\\*")
    write_error: BaseException | None = None  # raised by the next writing command (e.g. a dropped connection)
    # Optional hook: (folder, criteria, charset) -> UIDs; default: all non-deleted messages.
    search_hook: Any = None
    search_error: BaseException | None = None
    thread_error: BaseException | None = None
    broken_partial_fetch: bool = False
    commands: list[tuple[Any, ...]] = field(default_factory=list)
    appended: list[bytes] = field(default_factory=list)
    # LSUB result; None = every folder is subscribed (GMX default). lsub_error makes LSUB fail.
    subscribed: set[str] | None = None
    lsub_error: BaseException | None = None

    def mailbox(self, name: str, uidvalidity: int | None = None) -> FakeMailbox:
        """Return (and create) a mailbox; an explicit ``uidvalidity`` is applied."""
        box = self.mailboxes.setdefault(name, FakeMailbox())
        if uidvalidity is not None:
            box.uidvalidity = uidvalidity
        return box

    def __call__(
        self, host: str, port: int | None = None, ssl: bool = True, ssl_context: Any = None, timeout: Any = None
    ) -> FakeIMAPClient:
        """Act as the IMAPClient class."""
        if self.connect_error is not None:
            raise self.connect_error
        client = FakeIMAPClient(self, host=host, port=port, ssl=ssl, ssl_context=ssl_context)
        self.clients.append(client)
        return client

    @property
    def open_clients(self) -> list[FakeIMAPClient]:
        """Return connections that are still open."""
        return [c for c in self.clients if not c.closed]


class FakeIMAPClient:
    """Implements the subset of imapclient.IMAPClient used by the integration."""

    def __init__(self, server: FakeImapServer, *, host: str, port: int | None, ssl: bool, ssl_context: Any) -> None:
        """Initialise."""
        self.server = server
        self.host = host
        self.port = port
        self.ssl = ssl
        self.ssl_context = ssl_context
        self.tls = ssl
        self.logged_in = False
        self.closed = False
        self.logged_out = False
        self.noops = 0
        self.commands: list[str] = []
        self.selected: str | None = None
        self.readonly = True
        self._imap = SimpleNamespace(untagged_responses={})  # imaplib keeps response codes (e.g. COPYUID) here

    def starttls(self, ssl_context: Any = None) -> bytes:
        """Upgrade to TLS."""
        if self.server.starttls_error is not None:
            raise self.server.starttls_error
        self.tls = True
        return b"OK"

    def login(self, username: str, password: str) -> bytes:
        """Log in."""
        assert self.tls, "credentials must never be sent without TLS"
        if self.server.login_error is not None:
            raise self.server.login_error
        if password != self.server.password:
            raise LoginError("[AUTHENTICATIONFAILED] Authentication failed")
        self.logged_in = True
        self.server.logins += 1
        return b"OK"

    def capabilities(self) -> tuple[bytes, ...]:
        """Return capabilities."""
        return self.server.capabilities

    def list_folders(self, directory: str = "", pattern: str = "*") -> list[tuple[tuple[bytes, ...], bytes, str]]:
        """Return folders."""
        self._check_open()
        if self.server.list_error is not None:
            raise self.server.list_error
        return list(self.server.folders)

    def list_sub_folders(self, directory: str = "", pattern: str = "*") -> list[tuple[tuple[bytes, ...], bytes, str]]:
        """LSUB."""
        self._check_open()
        if self.server.lsub_error is not None:
            raise self.server.lsub_error
        wanted = self.server.subscribed
        return [entry for entry in self.server.folders if wanted is None or entry[2] in wanted]

    def subscribe_folder(self, folder: str) -> bytes:
        """SUBSCRIBE."""
        self._check_open()
        self.server.commands.append(("subscribe", folder))
        if self.server.subscribed is None:
            self.server.subscribed = {name for _flags, _delim, name in self.server.folders}
        self.server.subscribed.add(folder)
        return b"OK"

    def unsubscribe_folder(self, folder: str) -> bytes:
        """UNSUBSCRIBE."""
        self._check_open()
        self.server.commands.append(("unsubscribe", folder))
        if self.server.subscribed is None:
            self.server.subscribed = {name for _flags, _delim, name in self.server.folders}
        self.server.subscribed.discard(folder)
        return b"OK"

    def noop(self) -> tuple[bytes, list[Any]]:
        """NOOP."""
        self._check_open()
        self.noops += 1
        if self.server.noop_error is not None:
            raise self.server.noop_error
        return b"NOOP completed", []

    def logout(self) -> bytes:
        """Log out."""
        if self.server.logout_error is not None:
            raise self.server.logout_error
        self.logged_out = True
        self.closed = True
        return b"BYE"

    def shutdown(self) -> None:
        """Close the socket."""
        self.closed = True

    def _check_open(self) -> None:
        if self.closed:
            raise IMAPClientAbortError("socket error: EOF")

    # ------------------------------------------------------------------ mailbox commands

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, Any]:
        """SELECT/EXAMINE."""
        self._check_open()
        self.server.commands.append(("select", folder, readonly))
        box = self.server.mailboxes.get(folder)
        if box is None:
            raise IMAPClientError("select failed: NO [NONEXISTENT] Unknown folder")
        self.selected = folder
        self.readonly = readonly
        keywords = sorted({f for m in box.messages.values() for f in m.flags if not f.startswith(b"\\")})
        return {
            b"EXISTS": len(box.messages),
            b"UIDVALIDITY": box.uidvalidity,
            b"READ-WRITE": not readonly,
            b"FLAGS": (b"\\Answered", b"\\Flagged", b"\\Draft", b"\\Deleted", b"\\Seen", *keywords),
            b"PERMANENTFLAGS": self.server.permanent_flags,
        }

    def unselect_folder(self) -> bytes:
        """UNSELECT."""
        self._check_open()
        if b"UNSELECT" not in self.server.capabilities:
            raise IMAPClientError("UNSELECT not supported")
        self.server.commands.append(("unselect",))
        self.selected = None
        return b"OK"

    def folder_status(self, folder: str, what: Any = None) -> dict[bytes, Any]:
        """STATUS."""
        self._check_open()
        if self.server.status_error is not None:
            raise self.server.status_error
        box = self.server.mailboxes.get(folder)
        if box is None:
            raise IMAPClientError("status failed: NO")
        unseen = sum(1 for m in box.messages.values() if b"\\Seen" not in m.flags)
        return {
            b"MESSAGES": len(box.messages),
            b"UNSEEN": unseen,
            b"UIDNEXT": max(box.messages, default=0) + 1,
            b"UIDVALIDITY": box.uidvalidity,
        }

    def _box(self) -> FakeMailbox:
        assert self.selected is not None, "no folder selected"
        return self.server.mailboxes[self.selected]

    def search(self, criteria: Any = "ALL", charset: str | None = None) -> list[int]:
        """UID SEARCH."""
        self._check_open()
        self.server.commands.append(("search", list(criteria), charset))
        if self.server.search_error is not None:
            raise self.server.search_error
        if self.server.search_hook is not None:
            return list(self.server.search_hook(self.selected, list(criteria), charset))
        return [uid for uid, m in self._box().messages.items() if b"\\Deleted" not in m.flags]

    def sort(self, sort_criteria: Any, criteria: Any = "ALL", charset: str = "UTF-8") -> list[int]:
        """UID SORT by INTERNALDATE (good enough for tests)."""
        self._check_open()
        self.server.commands.append(("sort", list(sort_criteria), list(criteria), charset))
        if self.server.search_error is not None:
            raise self.server.search_error
        if self.server.search_hook is not None:
            uids = list(self.server.search_hook(self.selected, list(criteria), charset))
        else:
            uids = [uid for uid, m in self._box().messages.items() if b"\\Deleted" not in m.flags]
        box = self._box()
        return sorted(uids, key=lambda u: (box.messages[u].internaldate, u), reverse="REVERSE" in sort_criteria)

    def thread(self, algorithm: str = "REFERENCES", criteria: Any = "ALL", charset: str = "UTF-8") -> tuple[Any, ...]:
        """UID THREAD like ORDEREDSUBJECT: messages grouped by base subject (Re:/Fwd: removed), by date; first is the root."""
        self._check_open()
        self.server.commands.append(("thread", algorithm, list(criteria) if not isinstance(criteria, str) else criteria, charset))
        if self.server.thread_error is not None:
            raise self.server.thread_error
        box = self._box()
        groups: dict[str, list[int]] = {}
        for uid, msg in sorted(box.messages.items(), key=lambda item: (item[1].internaldate, item[0])):
            if b"\\Deleted" in msg.flags:
                continue
            match = re.search(rb"^Subject: (.*)$", msg.header, re.MULTILINE | re.IGNORECASE)
            # Like a real server: decode RFC 2047 encoded words before comparing subjects.
            subject = str(make_header(decode_header(match.group(1).decode()))).strip() if match else ""
            base = re.sub(r"^((re|fwd?|aw|wg):\s*)+", "", subject, flags=re.IGNORECASE).lower()
            groups.setdefault(base, []).append(uid)
        # ORDEREDSUBJECT: (root child child ...); a lone message is (uid,).
        return tuple((uids[0], *[(u,) for u in uids[1:]]) if len(uids) > 1 else (uids[0],) for uids in groups.values())

    def fetch(self, messages: Any, data: Any, modifiers: Any = None) -> dict[int, dict[bytes, Any]]:
        """UID FETCH of the items the integration uses."""
        self._check_open()
        items = list(data)
        self.server.commands.append(("fetch", list(messages), items))
        box = self._box()
        result: dict[int, dict[bytes, Any]] = {}
        for uid in messages:
            msg = box.messages.get(uid)
            if msg is None:
                continue
            entry: dict[bytes, Any] = {b"SEQ": uid}
            for item in items:
                entry.update(msg.fetch_item(item, broken_partial=self.server.broken_partial_fetch))
            result[uid] = entry
        return result

    # ------------------------------------------------------------------ writing commands

    def _writable(self) -> FakeMailbox:
        self._check_open()
        if self.server.write_error is not None:
            error, self.server.write_error = self.server.write_error, None
            raise error
        assert not self.readonly, "write on a read-only (EXAMINE) folder"
        return self._box()

    def add_flags(self, messages: Any, flags: Any, silent: bool = False) -> Any:
        """UID STORE +FLAGS."""
        box = self._writable()
        self.server.commands.append(("add_flags", list(messages), list(flags)))
        for uid in messages:
            if uid in box.messages:
                current = list(box.messages[uid].flags)
                current += [f.encode() for f in flags if f.encode() not in current]
                box.messages[uid].flags = tuple(current)
        return None if silent else {}

    def remove_flags(self, messages: Any, flags: Any, silent: bool = False) -> Any:
        """UID STORE -FLAGS."""
        box = self._writable()
        self.server.commands.append(("remove_flags", list(messages), list(flags)))
        for uid in messages:
            if uid in box.messages:
                box.messages[uid].flags = tuple(f for f in box.messages[uid].flags if f.decode() not in flags)
        return None if silent else {}

    def _copy_into(self, messages: Any, folder: str) -> tuple[int, list[int], list[int]]:
        target = self.server.mailboxes.get(folder)
        if target is None:
            raise IMAPClientError("COPY failed: NO [TRYCREATE]")
        source = self._box()
        src, dst = [], []
        for uid in sorted(messages):
            if uid in source.messages:
                new_uid = target.allocate()
                target.messages[new_uid] = replace(
                    source.messages[uid], flags=tuple(f for f in source.messages[uid].flags if f != b"\\Deleted")
                )
                src.append(uid)
                dst.append(new_uid)
        return target.uidvalidity, src, dst

    @staticmethod
    def _set(uids: list[int]) -> str:
        return ",".join(str(u) for u in uids)

    def copy(self, messages: Any, folder: str) -> bytes:
        """UID COPY (COPYUID in the tagged response, as GMX does)."""
        self._writable()
        self.server.commands.append(("copy", list(messages), folder))
        validity, src, dst = self._copy_into(messages, folder)
        if self.server.copy_copyuid == "tagged":
            return f"[COPYUID {validity} {self._set(src)} {self._set(dst)}] Copy completed".encode()
        return b"Copy completed"

    def move(self, messages: Any, folder: str) -> bytes:
        """UID MOVE (GMX: COPYUID untagged)."""
        box = self._writable()
        if b"MOVE" not in self.server.capabilities:
            raise IMAPClientError("MOVE not supported")
        self.server.commands.append(("move", list(messages), folder))
        validity, src, dst = self._copy_into(messages, folder)
        for uid in src:
            box.highest_uid = max(box.highest_uid, uid)  # UIDs are never reused
            del box.messages[uid]
        code = f"{validity} {self._set(src)} {self._set(dst)}".encode()
        if self.server.move_copyuid == "untagged":
            self._imap.untagged_responses.setdefault("COPYUID", []).append(code)
        elif self.server.move_copyuid == "tagged":
            return b"[COPYUID " + code + b"] Move completed"
        return b"Move completed"

    def uid_expunge(self, messages: Any) -> bytes:
        """UID EXPUNGE: removes only the given UIDs that are marked \\Deleted."""
        box = self._writable()
        if b"UIDPLUS" not in self.server.capabilities:
            raise IMAPClientError("UIDPLUS not supported")
        self.server.commands.append(("uid_expunge", list(messages)))
        for uid in messages:
            if uid in box.messages and b"\\Deleted" in box.messages[uid].flags:
                box.highest_uid = max(box.highest_uid, uid)
                del box.messages[uid]
        return b"OK"

    def expunge(self, messages: Any = None) -> None:
        """Mailbox-wide EXPUNGE is forbidden in this project."""
        raise AssertionError("mailbox-wide EXPUNGE must never be used")

    def close_folder(self) -> None:
        """CLOSE expunges implicitly - forbidden in this project."""
        raise AssertionError("CLOSE must never be used")

    def create_folder(self, folder: str) -> bytes:
        """CREATE."""
        self._check_open()
        if self.server.write_error is not None:
            error, self.server.write_error = self.server.write_error, None
            raise error
        self.server.commands.append(("create", folder))
        self.server.folders.append(((b"\\HasNoChildren",), b"/", folder))
        self.server.mailbox(folder)
        return b"OK"

    def rename_folder(self, old_name: str, new_name: str) -> bytes:
        """RENAME."""
        self._check_open()
        self.server.commands.append(("rename", old_name, new_name))
        self.server.folders = [(flags, delim, new_name if name == old_name else name) for flags, delim, name in self.server.folders]
        if old_name in self.server.mailboxes:
            self.server.mailboxes[new_name] = self.server.mailboxes.pop(old_name)
        return b"OK"

    def delete_folder(self, folder: str) -> bytes:
        """DELETE (folder)."""
        self._check_open()
        if self.server.write_error is not None:
            error, self.server.write_error = self.server.write_error, None
            raise error
        if folder not in {name for _flags, _delim, name in self.server.folders}:
            raise IMAPClientError("DELETE failed: NO")
        self.server.commands.append(("delete_folder", folder))
        self.server.folders = [entry for entry in self.server.folders if entry[2] != folder]
        self.server.mailboxes.pop(folder, None)
        return b"OK"

    def append(self, folder: str, msg: bytes, flags: Any = (), msg_time: Any = None) -> bytes:
        """APPEND (APPENDUID like GMX unless disabled)."""
        self._check_open()
        box = self.server.mailboxes.get(folder)
        if box is None:
            raise IMAPClientError("APPEND failed: NO [TRYCREATE]")
        self.server.commands.append(("append", folder, tuple(flags), len(msg)))
        header, _, body = msg.partition(b"\r\n\r\n")
        uid = box.allocate()
        structure = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", len(body), 1, None, None, None)
        box.messages[uid] = FakeMessage(
            header=header + b"\r\n\r\n", parts={"1": body}, bodystructure=structure, flags=tuple(f.encode() for f in flags), size=len(msg)
        )
        self.server.appended.append(msg)
        if self.server.appenduid:
            return f"[APPENDUID {box.uidvalidity} {uid}] Append completed".encode()
        return b"Append completed"


_PEEK_RE = re.compile(r"^BODY\.PEEK\[([^\]]*)\](?:<0\.(\d+)>)?$")


@dataclass
class FakeMessage:
    """A stored message: header block, raw (transfer-encoded) parts and the BODYSTRUCTURE."""

    header: bytes
    parts: dict[str, bytes]
    bodystructure: Any
    internaldate: datetime = field(default_factory=lambda: datetime(2026, 11, 20, 10, 0, tzinfo=UTC))
    flags: tuple[bytes, ...] = ()
    size: int = 0

    def fetch_item(self, item: str, *, broken_partial: bool) -> dict[bytes, Any]:
        """Return the response entries for one FETCH item."""
        upper = item.upper()
        if upper == "FLAGS":
            return {b"FLAGS": self.flags}
        if upper == "RFC822.SIZE":
            return {b"RFC822.SIZE": self.size or len(self.header) + sum(len(p) for p in self.parts.values())}
        if upper == "INTERNALDATE":
            return {b"INTERNALDATE": self.internaldate}
        if upper == "BODYSTRUCTURE":
            return {b"BODYSTRUCTURE": self.bodystructure}
        match = _PEEK_RE.match(item)
        assert match, f"unexpected FETCH item {item!r} (reads must use BODY.PEEK)"
        section, partial = match.group(1), match.group(2)
        if section == "":
            return {b"BODY[]": self.header + b"".join(self.parts.values())}  # whole message (simple single-part fakes)
        if section.upper().startswith("HEADER"):
            return {f"BODY[{section}]".encode(): self.header}
        content = self.parts.get(section)
        if content is None:
            return {}
        if partial is not None:
            if broken_partial:
                raise ProtocolError("uneven number of response items")
            return {f"BODY[{section}]<0>".encode(): content[: int(partial)]}
        return {f"BODY[{section}]".encode(): content}


@dataclass
class FakeMailbox:
    """A folder with messages by UID."""

    uidvalidity: int = 1234
    messages: dict[int, FakeMessage] = field(default_factory=dict)
    highest_uid: int = 0

    def allocate(self) -> int:
        """Return the next UID (never reused, like a real server)."""
        self.highest_uid = max(self.highest_uid, max(self.messages, default=0)) + 1
        return self.highest_uid


def plain_message(
    *,
    subject: str = "Hallo",
    sender: str = "Anna Beispiel <anna@example.org>",
    to: str = "user@gmx.test",
    body: str = "Hallo Welt",
    date: datetime | None = None,
    extra_headers: str = "",
    flags: tuple[bytes, ...] = (),
    message_id: str = "<m1@example.org>",
) -> FakeMessage:
    """A single-part text/plain message (base64-encoded body, part '1')."""
    from email.header import Header  # noqa: PLC0415

    header = (
        f"From: {sender}\r\nTo: {to}\r\nSubject: {Header(subject, 'utf-8').encode()}\r\n"
        f"Date: Fri, 20 Nov 2026 10:00:00 +0100\r\nMessage-ID: {message_id}\r\n{extra_headers}\r\n"
    ).encode()
    raw = base64.encodebytes(body.encode("utf-8"))
    structure = (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"base64", len(raw), 1, None, None, None)
    message = FakeMessage(header=header, parts={"1": raw}, bodystructure=structure, flags=flags)
    if date is not None:
        message.internaldate = date
    return message


def mixed_message(*, html: str, pdf: bytes = b"%PDF-1.4 test", filename_param: bytes = b"utf-8''Rechnung%20M%C3%A4rz.pdf") -> FakeMessage:
    """multipart/mixed with alternative (plain 1.1 + html 1.2) and a PDF attachment (part 2)."""
    header = b"From: shop@example.com\r\nTo: user@gmx.test\r\nSubject: Rechnung\r\nReply-To: other@evil.example\r\n\r\n"
    plain_raw = b"Plain body"
    html_raw = html.encode()
    pdf_raw = base64.encodebytes(pdf)
    structure = (
        [
            (
                [
                    (b"text", b"plain", (b"charset", b"utf-8"), None, None, b"7bit", len(plain_raw), 1, None, None, None),
                    (b"text", b"html", (b"charset", b"utf-8"), None, None, b"7bit", len(html_raw), 1, None, None, None),
                ],
                b"alternative",
                (b"boundary", b"b1"),
                None,
                None,
            ),
            (
                b"APPLICATION",
                b"PDF",
                None,
                None,
                None,
                b"base64",
                len(pdf_raw),
                None,
                (b"attachment", (b"filename*", filename_param)),
                None,
            ),
        ],
        b"mixed",
        (b"boundary", b"b0"),
        None,
        None,
    )
    return FakeMessage(header=header, parts={"1.1": plain_raw, "1.2": html_raw, "2": pdf_raw}, bodystructure=structure)


@dataclass
class FakeSmtpServer:
    """Shared state of fake SMTP connections."""

    password: str = PASSWORD
    starttls_offered: bool = True
    size: str = "141557760"
    connect_error: BaseException | None = None
    sessions: list[FakeSMTP] = field(default_factory=list)
    refused_recipients: set[str] = field(default_factory=set)
    sender_code: int = 250
    data_error: BaseException | None = None  # raised by DATA (e.g. a dropped connection)
    data_code: int = 250
    delivered: list[tuple[str, list[str], bytes]] = field(default_factory=list)

    def factory(self, *, implicit_tls: bool) -> Any:
        """Return a replacement for smtplib.SMTP or smtplib.SMTP_SSL."""

        def _make(host: str, port: int, timeout: float = 30, context: ssl.SSLContext | None = None) -> FakeSMTP:
            if self.connect_error is not None:
                raise self.connect_error
            session = FakeSMTP(self, tls=implicit_tls)
            self.sessions.append(session)
            return session

        return _make


class FakeSMTP:
    """Implements the subset of smtplib.SMTP used by the integration."""

    def __init__(self, server: FakeSmtpServer, *, tls: bool) -> None:
        """Initialise."""
        self.server = server
        self.tls = tls
        self.esmtp_features: dict[str, str] = {}
        self.closed = False
        self.quit_called = False

    def ehlo(self) -> tuple[int, bytes]:
        """EHLO."""
        if self.tls:
            self.esmtp_features = {"8bitmime": "", "auth": "LOGIN PLAIN", "size": self.server.size}
        else:
            self.esmtp_features = {"8bitmime": "", "size": self.server.size}
            if self.server.starttls_offered:
                self.esmtp_features["starttls"] = ""
        return 250, b"OK"

    def has_extn(self, name: str) -> bool:
        """Return whether the extension is offered."""
        return name.lower() in self.esmtp_features

    def starttls(self, context: ssl.SSLContext | None = None) -> tuple[int, bytes]:
        """Upgrade to TLS."""
        self.tls = True
        return 220, b"Ready"

    def login(self, user: str, password: str) -> tuple[int, bytes]:
        """Log in."""
        assert self.tls, "credentials must never be sent without TLS"
        if password != self.server.password:
            raise smtplib.SMTPAuthenticationError(535, b"Authentication credentials invalid")
        return 235, b"OK"

    def mail(self, sender: str, options: Any = ()) -> tuple[int, bytes]:
        """MAIL FROM."""
        assert self.tls
        self._sender, self._rcpts = sender, []
        return self.server.sender_code, b"OK"

    def rcpt(self, recipient: str, options: Any = ()) -> tuple[int, bytes]:
        """RCPT TO."""
        if recipient in self.server.refused_recipients:
            return 550, b"no such user"
        self._rcpts.append(recipient)
        return 250, b"OK"

    def rset(self) -> tuple[int, bytes]:
        """RSET."""
        self._rcpts = []
        return 250, b"OK"

    def data(self, msg: bytes) -> tuple[int, bytes]:
        """DATA."""
        if self.server.data_error is not None:
            raise self.server.data_error
        if self.server.data_code == 250:
            self.server.delivered.append((self._sender, list(self._rcpts), bytes(msg)))
        return self.server.data_code, b"queued"

    def quit(self) -> tuple[int, bytes]:
        """QUIT."""
        self.quit_called = True
        self.closed = True
        return 221, b"Bye"

    def close(self) -> None:
        """Close."""
        self.closed = True


# --------------------------------------------------------------------------- aioimaplib fake (IDLE watcher)


@dataclass
class FakeResponse:
    """aioimaplib Response stand-in."""

    result: str
    lines: list[bytes] = field(default_factory=list)


class FakeIdleClient:
    """Scriptable stand-in for aioimaplib.IMAP4; pushes are fed through ``push()``."""

    def __init__(self, server: FakeIdleServer, on_lost: Any) -> None:
        """Initialise."""
        import asyncio  # noqa: PLC0415

        self.server = server
        self.on_lost = on_lost
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.pending_idle = False
        self.logged_out = False
        self.selected: list[str] = []
        self.state = "NONAUTH"
        self.idle_starts = 0
        self.done_calls = 0

    async def wait_hello_from_server(self) -> None:
        """Greeting."""
        if self.server.connect_error is not None:
            raise self.server.connect_error

    async def login(self, user: str, password: str) -> FakeResponse:
        """LOGIN."""
        if password != self.server.password:
            return FakeResponse("NO")
        self.state = "AUTH"
        return FakeResponse("OK")

    async def select(self, mailbox: str = "INBOX") -> FakeResponse:
        """SELECT."""
        self.selected.append(mailbox)
        self.state = "SELECTED"
        return FakeResponse("OK")

    async def examine(self, mailbox: str = "INBOX") -> FakeResponse:
        """EXAMINE - like aioimaplib 2.0.3 it does NOT enter the SELECTED state."""
        return FakeResponse("OK")

    def has_capability(self, capability: str) -> bool:
        """IDLE is offered unless disabled."""
        return capability != "IDLE" or self.server.idle_capability

    async def idle_start(self, timeout: float = 0) -> Any:
        """Start IDLE; the renewal timer is simulated by ``renew()``."""
        import asyncio  # noqa: PLC0415

        from aioimaplib import Abort  # noqa: PLC0415

        if self.state != "SELECTED":  # aioimaplib refuses IDLE outside the SELECTED state
            raise Abort(f"command IDLE illegal in state {self.state}")
        self.idle_starts += 1
        self.pending_idle = True
        self.server.started.set()
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        future.set_result(FakeResponse("OK"))
        return future

    async def wait_server_push(self, timeout: float = 0) -> Any:
        """Wait for the next push."""
        return await self.queue.get()

    def idle_done(self) -> None:
        """DONE."""
        self.done_calls += 1
        self.pending_idle = False

    def has_pending_idle(self) -> bool:
        """IDLE active."""
        return self.pending_idle

    async def logout(self) -> FakeResponse:
        """LOGOUT."""
        self.logged_out = True
        return FakeResponse("OK")

    def push(self, *lines: bytes) -> None:
        """Simulate untagged responses during IDLE."""
        self.queue.put_nowait(list(lines))

    def renew(self) -> None:
        """Simulate the renewal timer (aioimaplib's stop marker)."""
        from aioimaplib import STOP_WAIT_SERVER_PUSH  # noqa: PLC0415

        self.queue.put_nowait(STOP_WAIT_SERVER_PUSH)

    def lose_connection(self) -> None:
        """Simulate a dropped socket (aioimaplib calls conn_lost_cb)."""
        self.on_lost(ConnectionResetError())


@dataclass
class FakeIdleServer:
    """Factory and shared state for FakeIdleClient."""

    password: str = PASSWORD
    idle_capability: bool = True
    connect_error: BaseException | None = None
    clients: list[FakeIdleClient] = field(default_factory=list)
    started: Any = None

    def __post_init__(self) -> None:
        """Create the start event lazily inside the running loop."""
        import asyncio  # noqa: PLC0415

        self.started = asyncio.Event()

    def __call__(self, settings: Any, context: Any, on_lost: Any) -> FakeIdleClient:
        """Act as the client factory."""
        client = FakeIdleClient(self, on_lost)
        self.clients.append(client)
        return client
