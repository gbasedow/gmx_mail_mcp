"""Minimal threaded fake IMAP server for offline tests of tools/gmx_probe.py.

Only implements what the probe uses. Plain text on 127.0.0.1, never talks to the internet.
Records every command together with the selected mailbox and its mode so tests can assert
that no implicit (CLOSE) or explicit mailbox-wide EXPUNGE hits a mailbox that is not ours.
"""

from __future__ import annotations

import email.parser
import email.policy
import re
import socketserver
import threading
from dataclasses import dataclass, field
from typing import Any

GMX_CAPS = (
    "IMAP4rev1 APPENDLIMIT=29360128 CHILDREN ENABLE ID IDLE LIST-EXTENDED LIST-STATUS LITERAL- MOVE NAMESPACE "
    "SASL-IR SORT SPECIAL-USE THREAD=ORDEREDSUBJECT UIDPLUS UNSELECT WITHIN"
)

FOREIGN_DELETED_ID = b"<foreign-deleted@other-client.invalid>"


def _msg(msg_id: bytes, subject: str = "foreign") -> bytes:
    return (
        b"From: someone@example.invalid\r\nTo: me@example.invalid\r\nSubject: "
        + subject.encode()
        + b"\r\nMessage-ID: "
        + msg_id
        + b"\r\n\r\nforeign body\r\n"
    )


@dataclass
class Message:
    uid: int
    raw: bytes
    flags: set[str] = field(default_factory=set)


@dataclass
class Mailbox:
    attrs: list[str] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)
    uidnext: int = 1

    def add(self, raw: bytes, flags: set[str] | None = None) -> int:
        uid = self.uidnext
        self.uidnext += 1
        self.messages.append(Message(uid, raw, set(flags or ())))
        return uid


@dataclass
class Profile:
    caps: str = GMX_CAPS
    move_sends_copyuid: bool = True  # GMX: untagged "* OK [COPYUID ...]" on MOVE


class State:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.lock = threading.RLock()
        self.boxes: dict[str, Mailbox] = {
            "INBOX": Mailbox(),
            "Gesendet": Mailbox(["\\Sent", "\\NoInferiors"]),
            "Entw&APw-rfe": Mailbox(["\\Drafts", "\\NoInferiors"]),
            "Gel&APY-scht": Mailbox(["\\Trash"]),
            "Spamverdacht": Mailbox(["\\Junk", "\\NoInferiors"]),
        }
        # Messages another client already flagged \Deleted: a CLOSE or EXPUNGE would destroy them.
        self.boxes["INBOX"].add(_msg(FOREIGN_DELETED_ID), {"\\Deleted"})
        self.boxes["INBOX"].add(_msg(b"<normal@example.invalid>"))
        self.boxes["Gel&APY-scht"].add(_msg(FOREIGN_DELETED_ID), {"\\Deleted"})
        self.boxes["Gesendet"].add(_msg(FOREIGN_DELETED_ID), {"\\Deleted"})
        self.initial_names = set(self.boxes)
        self.log: list[tuple[str | None, bool, str]] = []  # (selected, read_write, command)
        self.logins: list[tuple[str, str]] = []


# --------------------------------------------------------------------------- parsing


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == " ":
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while text[j] != '"':
                if text[j] == "\\":
                    j += 1
                buf.append(text[j])
                j += 1
            tokens.append("".join(buf))
            i = j + 1
        elif c == "(":
            depth, j = 0, i
            while True:
                if text[j] == "(":
                    depth += 1
                elif text[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            tokens.append(text[i : j + 1])
            i = j + 1
        else:
            j = i
            while j < len(text) and text[j] != " ":
                j += 1
            tokens.append(text[i:j])
            i = j
    return tokens


def _uid_set(spec: str, box: Mailbox) -> list[Message]:
    wanted: set[int] = set()
    top = max((m.uid for m in box.messages), default=0)
    for part in spec.split(","):
        if ":" in part:
            a, b = (top if x == "*" else int(x) for x in part.split(":"))
            wanted.update(range(min(a, b), max(a, b) + 1))
        else:
            wanted.add(top if part == "*" else int(part))
    return [m for m in box.messages if m.uid in wanted]


def _headers(raw: bytes) -> Any:
    return email.parser.BytesHeaderParser(policy=email.policy.default).parsebytes(raw)


def _body(raw: bytes) -> str:
    return raw.split(b"\r\n\r\n", 1)[-1].split(b"\n\n", 1)[-1].decode("utf-8", "replace")


def _matches(m: Message, crit: list[Any]) -> bool:
    i = 0
    while i < len(crit):
        key = str(crit[i]).upper()
        arg = crit[i + 1] if i + 1 < len(crit) else ""
        arg_s = arg.decode("utf-8") if isinstance(arg, bytes) else str(arg)
        h = _headers(m.raw)
        if key == "ALL":
            i += 1
        elif key == "CHARSET":
            i += 2
        elif key in {"SEEN", "UNSEEN", "DELETED", "UNDELETED"}:
            has = ("\\Seen" if "SEEN" in key else "\\Deleted") in m.flags
            if key.startswith("UN") == has:
                return False
            i += 1
        elif key in {"SUBJECT", "FROM"}:
            if arg_s.lower() not in str(h.get(key.capitalize(), "")).lower():
                return False
            i += 2
        elif key == "BODY":
            if arg_s.lower() not in _body(m.raw).lower():
                return False
            i += 2
        elif key == "TEXT":
            if arg_s.lower() not in m.raw.decode("utf-8", "replace").lower() + str(h.get("Subject", "")).lower():
                return False
            i += 2
        elif key == "HEADER":
            val = crit[i + 2]
            if str(val).lower() not in str(h.get(arg_s, "")).lower():
                return False
            i += 3
        elif key == "KEYWORD":
            if arg_s not in m.flags:
                return False
            i += 2
        elif key in {"LARGER", "SMALLER"}:
            n = int(arg_s)
            if (len(m.raw) <= n) if key == "LARGER" else (len(m.raw) >= n):
                return False
            i += 2
        elif key in {"SINCE", "BEFORE"}:
            i += 2
        else:
            raise ValueError(f"unsupported search key {key}")
    return True


# --------------------------------------------------------------------------- server


class Handler(socketserver.StreamRequestHandler):
    server: Server

    def setup(self) -> None:
        super().setup()
        self.selected: str | None = None
        self.rw = False

    @property
    def st(self) -> State:
        return self.server.state

    def send(self, line: str | bytes) -> None:
        data = line if isinstance(line, bytes) else line.encode("utf-8")
        self.wfile.write(data + b"\r\n")
        self.wfile.flush()

    def read_command(self) -> list[Any] | None:
        parts: list[Any] = []
        while True:
            line = self.rfile.readline()
            if not line:
                return None
            text = line.decode("utf-8", "replace").rstrip("\r\n")
            m = re.search(r"\{(\d+)\+?\}$", text)
            if not m:
                parts.extend(_tokenize(text))
                return parts
            parts.extend(_tokenize(text[: m.start()]))
            if not m.group(0).endswith("+}"):
                self.send("+ go ahead")
            parts.append(self.rfile.read(int(m.group(1))))

    def handle(self) -> None:
        self.send(f"* OK [CAPABILITY {self.st.profile.caps} AUTH=PLAIN] fake IMAP ready")
        while True:
            cmd = self.read_command()
            if cmd is None:
                return
            tag, name, args = cmd[0], str(cmd[1]).upper(), cmd[2:]
            if name == "UID":
                name, args = "UID " + str(args[0]).upper(), args[1:]
            with self.st.lock:
                self.st.log.append((self.selected, self.rw, name))
                try:
                    done = self.dispatch(tag, name, args)
                except Exception as e:  # noqa: BLE001
                    self.send(f"{tag} BAD {type(e).__name__}: {e}")
                    continue
            if done:
                return

    def box(self) -> Mailbox:
        assert self.selected is not None, "no mailbox selected"
        return self.st.boxes[self.selected]

    def expunge(self, msgs: list[Message]) -> None:
        box = self.box()
        for m in msgs:
            if "\\Deleted" in m.flags and m in box.messages:
                self.send(f"* {box.messages.index(m) + 1} EXPUNGE")
                box.messages.remove(m)

    def dispatch(self, tag: str, name: str, a: list[Any]) -> bool:
        st = self.st
        ok = f"{tag} OK {name} completed"
        if name == "CAPABILITY":
            self.send(f"* CAPABILITY {st.profile.caps}")
        elif name == "LOGIN":
            st.logins.append((str(a[0]), str(a[1])))
        elif name == "LOGOUT":
            self.send("* BYE logging out")
            self.send(ok)
            return True
        elif name == "NOOP":
            pass
        elif name == "NAMESPACE":
            self.send('* NAMESPACE (("" "/")) NIL NIL')
        elif name == "LIST":
            special_only = str(a[0]).upper() == "(SPECIAL-USE)"
            for n, b in st.boxes.items():
                if special_only and not b.attrs:
                    continue
                attrs = " ".join(["\\HasNoChildren", *b.attrs])
                self.send(f'* LIST ({attrs}) "/" "{n}"')
        elif name == "STATUS":
            b = st.boxes[str(a[0])]
            unseen = sum("\\Seen" not in m.flags for m in b.messages)
            items = {"MESSAGES": len(b.messages), "UNSEEN": unseen, "UIDNEXT": b.uidnext, "UIDVALIDITY": 1}
            want = str(a[1]).strip("()").split()
            self.send(f'* STATUS "{a[0]}" (' + " ".join(f"{k} {items[k]}" for k in want) + ")")
        elif name == "CREATE":
            if str(a[0]) in st.boxes:
                self.send(f"{tag} NO exists")
                return False
            st.boxes[str(a[0])] = Mailbox()
        elif name == "RENAME":
            st.boxes[str(a[1])] = st.boxes.pop(str(a[0]))
        elif name == "DELETE":
            if str(a[0]) in st.initial_names:
                self.send(f"{tag} NO refusing to delete a pre-existing mailbox in tests")
                return False
            del st.boxes[str(a[0])]
        elif name == "APPEND":
            flags = set(str(a[1]).strip("()").split()) if len(a) > 2 and str(a[1]).startswith("(") else set()
            uid = st.boxes[str(a[0])].add(a[-1], flags)
            ok = f"{tag} OK [APPENDUID 1 {uid}] APPEND completed"
        elif name in {"SELECT", "EXAMINE"}:
            self.selected, self.rw = str(a[0]), name == "SELECT"
            b = self.box()
            self.send("* FLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen)")
            self.send(f"* {len(b.messages)} EXISTS")
            self.send("* OK [PERMANENTFLAGS (\\Answered \\Flagged \\Draft \\Deleted \\Seen \\*)] ok")
            self.send("* OK [UIDVALIDITY 1] ok")
            ok = f"{tag} OK [{'READ-WRITE' if self.rw else 'READ-ONLY'}] {name} completed"
        elif name == "CLOSE":
            if self.rw:  # RFC 3501: CLOSE expunges \Deleted messages of a read-write mailbox (silently)
                b = self.box()
                b.messages = [m for m in b.messages if "\\Deleted" not in m.flags]
            self.selected = None
        elif name == "UNSELECT":
            if "UNSELECT" not in st.profile.caps:
                self.send(f"{tag} BAD UNSELECT not supported")
                return False
            self.selected = None
        elif name == "EXPUNGE":
            self.expunge(list(self.box().messages))
        elif name == "UID EXPUNGE":
            if "UIDPLUS" not in st.profile.caps:
                self.send(f"{tag} BAD no UIDPLUS")
                return False
            self.expunge(_uid_set(str(a[0]), self.box()))
        elif name == "UID SEARCH":
            hits = [str(m.uid) for m in self.box().messages if _matches(m, a)]
            self.send("* SEARCH" + "".join(" " + h for h in hits))
        elif name == "UID SORT":
            hits = [str(m.uid) for m in reversed(self.box().messages) if _matches(m, a[2:])]
            self.send("* SORT" + "".join(" " + h for h in hits))
        elif name == "UID STORE":
            flags = set(str(a[2]).strip("()").split())
            for m in _uid_set(str(a[0]), self.box()):
                if str(a[1]).startswith("+"):
                    m.flags |= flags
                else:
                    m.flags -= flags
        elif name == "UID FETCH":
            b = self.box()
            for m in _uid_set(str(a[0]), b):
                hdr = _headers(m.raw).get("Message-ID", "")
                data = f"Message-ID: {hdr}\r\n\r\n".encode()
                seq = b.messages.index(m) + 1
                self.send(f"* {seq} FETCH (UID {m.uid} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{len(data)}}}".encode())
                self.wfile.write(data + b")\r\n")
        elif name in {"UID COPY", "UID MOVE"}:
            if name == "UID MOVE" and "MOVE" not in st.profile.caps:
                self.send(f"{tag} BAD no MOVE")
                return False
            src, dst = self.box(), st.boxes[str(a[1])]
            msgs = _uid_set(str(a[0]), src)
            new = [dst.add(m.raw, set(m.flags) - {"\\Deleted"}) for m in msgs]
            code = f"[COPYUID 1 {','.join(str(m.uid) for m in msgs)} {','.join(map(str, new))}]"
            if name == "UID COPY":
                ok = f"{tag} OK {code} COPY completed"
            else:
                if st.profile.move_sends_copyuid:
                    self.send(f"* OK {code} moved")
                for m in msgs:
                    self.send(f"* {src.messages.index(m) + 1} EXPUNGE")
                    src.messages.remove(m)
        else:
            self.send(f"{tag} BAD unknown command {name}")
            return False
        self.send(ok)
        return False


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, state: State) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.state = state


def start(profile: Profile | None = None) -> tuple[Server, State]:
    state = State(profile or Profile())
    srv = Server(state)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state
