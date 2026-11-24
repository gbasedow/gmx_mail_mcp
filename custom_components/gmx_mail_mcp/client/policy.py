"""Who may receive mail (SPEC section 4.3; D46, D47) - pure functions, no I/O.

* send mode ``allowlist``: exact addresses or ``*@domain`` (exact domain, no subdomains); an empty list allows nobody,
  and a ``*`` entry is ignored here (the options flow refuses it: "any address" is a mode of its own);
* ``any``: every address (the allowlist stays stored for switching back); ``off`` (template, not offered): nobody;
* the option "allow replies to the sender" covers exactly the **From** address of the original message. A different
  Reply-To address (a classic phishing trick) and every further recipient of "reply all" must pass the allowlist.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from ..const import ALLOWLIST_WILDCARD, SendMode


def normalize_address(address: str) -> str:
    """Lower-case and trim an address for comparison."""
    return address.strip().lower()


def domain_of(address: str) -> str:
    """Return the domain part (lower case)."""
    return normalize_address(address).rpartition("@")[2]


def allowed_by_list(address: str, allowlist: Iterable[str]) -> bool:
    """Exact address, ``*@domain`` or ``*``."""
    addr = normalize_address(address)
    domain = domain_of(addr)
    for entry in allowlist:
        item = normalize_address(entry)
        if item in (ALLOWLIST_WILDCARD, addr) or (item.startswith("*@") and item[2:] == domain):
            return True
    return False


@dataclass(frozen=True, slots=True)
class RecipientPolicy:
    """The send-related options of one account."""

    send_mode: str
    allowlist: Sequence[str]
    allow_reply_to_sender: bool

    @property
    def entries(self) -> list[str]:
        """Allowlist entries that count in mode ``allowlist`` (without ``*``)."""
        return [e for e in self.allowlist if normalize_address(e) != ALLOWLIST_WILDCARD]

    @property
    def sending_enabled(self) -> bool:
        """Return whether anything may be sent at all."""
        return self.send_mode == SendMode.ANY or (self.send_mode == SendMode.ALLOWLIST and bool(self.entries))

    def blocked(self, recipients: Iterable[str], *, original_sender: str | None = None) -> list[str]:
        """Return the recipients that may not receive the message (empty list = all allowed).

        ``original_sender`` is the From address of the message being answered (replies only).
        """
        if not self.sending_enabled:
            return [normalize_address(r) for r in recipients]
        if self.send_mode == SendMode.ANY:
            return []
        sender = normalize_address(original_sender) if original_sender else None
        entries = self.entries
        result: list[str] = []
        for recipient in recipients:
            addr = normalize_address(recipient)
            if allowed_by_list(addr, entries):
                continue
            if self.allow_reply_to_sender and sender is not None and addr == sender:
                continue
            result.append(addr)
        return result
