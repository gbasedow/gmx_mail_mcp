"""Full probe run against a local fake IMAP server (no network, SMTP stubbed).

Main safety property: messages that ANOTHER client flagged \\Deleted in INBOX, Sent or Trash must survive
the probe - i.e. no CLOSE (implicit expunge) and no bare EXPUNGE on a mailbox that is not one of our test folders.
"""

from __future__ import annotations

import json
import smtplib
import sys
import time
from collections.abc import Iterator
from email.message import EmailMessage
from typing import Any

import fake_imap
import gmx_probe
import pytest

FAKE_USER = "probe-user@example.invalid"
FAKE_PASS = "not-a-real-password-123"

PROFILES = {
    "gmx": fake_imap.Profile(),
    "move_without_copyuid": fake_imap.Profile(move_sends_copyuid=False),
    # no UIDPLUS, no UNSELECT: probe must leave \Deleted messages alone and deselect via EXAMINE
    "no_uidplus": fake_imap.Profile(caps="IMAP4rev1 LIST-EXTENDED MOVE NAMESPACE SPECIAL-USE SORT"),
    "no_move": fake_imap.Profile(caps="IMAP4rev1 LIST-EXTENDED NAMESPACE SPECIAL-USE SORT UIDPLUS UNSELECT"),
}


class FakeSMTP:
    """Stands in for smtplib.SMTP; delivers sent mail into the fake INBOX."""

    state: fake_imap.State

    def __init__(self, *_: Any, **__: Any) -> None:
        self.esmtp_features = {"8bitmime": "", "size": "141557760", "starttls": ""}

    def ehlo(self) -> None:
        pass

    def starttls(self, **_: Any) -> None:
        self.esmtp_features = {"8bitmime": "", "size": "141557760", "auth": "LOGIN PLAIN"}

    def login(self, user: str, password: str) -> None:
        assert (user, password) == (FAKE_USER, FAKE_PASS)

    def send_message(self, msg: EmailMessage) -> dict[str, Any]:
        with self.state.lock:
            self.state.boxes["INBOX"].add(msg.as_bytes())
        return {}

    def quit(self) -> None:
        pass


@pytest.fixture(params=list(PROFILES))
def run_probe(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Iterator[tuple[str, fake_imap.State, dict[str, Any], str]]:
    srv, state = fake_imap.start(PROFILES[request.param])
    FakeSMTP.state = state
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(time, "sleep", lambda _s: None)  # polling loops are bounded by time.monotonic()
    monkeypatch.setattr(gmx_probe.Probe, "sieve_port", lambda self: {"skipped": "offline test"})
    monkeypatch.setenv("GMX_USER", FAKE_USER)
    monkeypatch.setenv("GMX_PASS", FAKE_PASS)
    port = srv.server_address[1]
    argv = ["gmx_probe.py", "--no-tls", "--imap-host", "127.0.0.1", "--imap-port", str(port)]
    argv += ["--send-self-test", "--send-wait", "0"]
    monkeypatch.setattr(sys, "argv", argv)
    try:
        rc = gmx_probe.main()
        out = capsys.readouterr().out
        assert rc == 0
        yield request.param, state, json.loads(out), out
    finally:
        srv.shutdown()
        srv.server_close()


def _foreign_ids(box: fake_imap.Mailbox) -> list[bytes]:
    return [m.raw for m in box.messages if fake_imap.FOREIGN_DELETED_ID in m.raw]


def test_foreign_deleted_messages_survive(run_probe: tuple[str, fake_imap.State, dict[str, Any], str]) -> None:
    _, state, _, _ = run_probe
    for name in ("INBOX", "Gesendet", "Gel&APY-scht"):
        assert len(_foreign_ids(state.boxes[name])) == 1, f"foreign \\Deleted message expunged in {name}"


def test_no_expunge_or_close_outside_test_folders(run_probe: tuple[str, fake_imap.State, dict[str, Any], str]) -> None:
    _, state, _, _ = run_probe
    for selected, rw, cmd in state.log:
        if selected is None or selected.startswith("HA-Probe-"):
            continue
        assert cmd != "EXPUNGE", f"mailbox-wide EXPUNGE in {selected}"
        assert not (cmd == "CLOSE" and rw), f"CLOSE (implicit expunge) on read-write {selected}"


def test_cleanup_restores_mailbox_list(run_probe: tuple[str, fake_imap.State, dict[str, Any], str]) -> None:
    _, state, result, _ = run_probe
    assert set(state.boxes) == state.initial_names
    assert all(v == "OK" for v in result["cleanup"].values())


def test_no_credentials_in_output(run_probe: tuple[str, fake_imap.State, dict[str, Any], str]) -> None:
    _, _, _, out = run_probe
    assert FAKE_PASS not in out
    assert FAKE_USER not in out
    assert "probe-user" not in out


def test_results(run_probe: tuple[str, fake_imap.State, dict[str, Any], str]) -> None:
    profile, state, result, _ = run_probe
    ft = result["folder_tests"]
    assert result["login"]["ok"] is True
    assert result["meta"]["version"] == gmx_probe.PROBE_VERSION
    assert ft["copy_to_b"]["copyuid_seen"] == {"tagged": True, "untagged": False, "dest_uids": [1]}
    assert ft["search_index_delay"]["polls"][0]["subject_probe"] == 2
    if profile == "gmx":
        assert ft["move_to_b"]["copyuid_seen"]["untagged"] is True
        assert ft["trash_move"]["copyuid"]["untagged"] is True
        # our own message was removed from Trash again, nothing else
        assert ft["trash_move"]["trash_after_cleanup"] == ft["trash_move"]["trash_before"]
        sent = result["send_self_test"]["copies"]
        assert sent["INBOX"]["copies_found"] == 1
        assert sent["sent[0]"]["copies_found"] == 0
        assert all(b"haprobe-" not in m.raw for b in state.boxes.values() for m in b.messages)
    elif profile == "move_without_copyuid":
        # earlier COPY left a COPYUID entry in imaplib's store - it must not be reported for MOVE
        assert ft["move_to_b"]["copyuid_seen"] == {"tagged": False, "untagged": False, "dest_uids": []}
    elif profile == "no_uidplus":
        assert ft["uid_expunge"] == "skipped (no UIDPLUS)"
        assert "no UIDPLUS" in ft["trash_move"].get("note", ""), ft["trash_move"]
        assert ft["trash_move"]["trash_after_cleanup"] == ft["trash_move"]["trash_before"] + 1
    else:
        assert ft["move_to_b"].startswith("skipped")
        # B only holds the \Deleted-flagged copy -> nothing safe to move to Trash
        assert ft["trash_move"] == {"skipped": "no test message left in B"}
