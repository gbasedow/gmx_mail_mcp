"""Audit log and delete counter (D39, D41, D42) - free of Home Assistant imports.

The audit log records every call of a tool of level O, D or S (also previews, refusals, errors and cancellations), never
mail content, subjects, addresses or file names: time, tool, level, outcome, count, folder names, the HA user id and a
short non-reversible fingerprint of the parameters. Persistence (``Store``) is wired up in ``storage.py``; here the
objects only call ``on_change`` when something must be saved.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from typing import Any, Final

from .client.sanitize import clean_inline
from .const import DEFAULT_AUDIT_RETENTION_DAYS

MAX_ENTRIES: Final = 5000
MAX_FOLDERS_PER_ENTRY: Final = 20
MAX_FOLDER_CHARS: Final = 100
_SECONDS_PER_DAY: Final = 86_400

# Arguments that are never part of the fingerprint: a confirmation code differs between preview and execution.
_NOT_FINGERPRINTED: Final = frozenset({"confirm_token"})


def fingerprint(params: Any) -> str:
    """16 hex characters of SHA-256 over the canonical JSON of ``params`` (lets entries be correlated, not read)."""
    canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat(timespec="seconds")


def folders_of(arguments: Mapping[str, Any]) -> list[str]:
    """Folder names a call names in its arguments (refs, ref, in_reply_to, folder, target, parent)."""
    names: list[Any] = []
    for key in ("refs",):
        value = arguments.get(key)
        if isinstance(value, list):
            names.extend(item.get("folder") for item in value if isinstance(item, Mapping))
    for key in ("ref", "in_reply_to"):
        value = arguments.get(key)
        if isinstance(value, Mapping):
            names.append(value.get("folder"))
    names.extend(arguments.get(key) for key in ("folder", "target", "parent"))
    return [name for name in names if isinstance(name, str) and name]


class AuditLog:
    """Ring buffer of tool calls for one account, newest last; pruned by age and count."""

    def __init__(
        self,
        *,
        retention_days: Callable[[], int] = lambda: DEFAULT_AUDIT_RETENTION_DAYS,
        clock: Callable[[], float] = time.time,
        on_change: Callable[[], None] = lambda: None,
        entries: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        """``entries`` are stored entries (malformed ones are dropped)."""
        self._retention_days = retention_days
        self._clock = clock
        self.on_change = on_change
        self._entries: list[dict[str, Any]] = [dict(e) for e in entries if isinstance(e, Mapping) and isinstance(e.get("time"), str)]
        self._prune()

    def _prune(self) -> bool:
        cutoff = _iso(self._clock() - max(1, int(self._retention_days())) * _SECONDS_PER_DAY)
        kept = [e for e in self._entries if str(e["time"]) >= cutoff][-MAX_ENTRIES:]
        changed = len(kept) != len(self._entries)
        self._entries = kept
        return changed

    def record(
        self,
        *,
        tool: str,
        level: str,
        outcome: str,
        arguments: Mapping[str, Any],
        count: int | None = None,
        folders: Iterable[str] = (),
        user: str | None = None,
        error: str | None = None,
        confirmation: str | None = None,
    ) -> dict[str, Any]:
        """Store one entry. ``arguments`` only contribute folder names and the fingerprint, never their values."""
        names = sorted({clean_inline(name, MAX_FOLDER_CHARS) for name in (*folders_of(arguments), *folders) if name})
        entry: dict[str, Any] = {
            "time": _iso(self._clock()),
            "tool": tool,
            "level": level,
            "outcome": outcome,
            "count": count,
            "folders": names[:MAX_FOLDERS_PER_ENTRY],
            "user": user,
            "hash": fingerprint({k: v for k, v in arguments.items() if k not in _NOT_FINGERPRINTED}),
        }
        if error:
            entry["error"] = error
        if confirmation:
            entry["confirmation"] = confirmation
        self._entries.append(entry)
        self._prune()
        self.on_change()
        return entry

    def latest(self, limit: int) -> list[dict[str, Any]]:
        """Newest entries first."""
        if self._prune():
            self.on_change()
        return [dict(e) for e in reversed(self._entries[-limit:])] if limit > 0 else []

    def __len__(self) -> int:
        """Number of stored entries."""
        return len(self._entries)

    def snapshot(self) -> dict[str, Any]:
        """Data to persist."""
        return {"entries": [dict(e) for e in self._entries]}


class DeleteCounter:
    """Mails deleted (trash or permanently) on the current local day (D39)."""

    def __init__(
        self,
        *,
        today: Callable[[], str] = lambda: date.today().isoformat(),
        on_change: Callable[[], None] = lambda: None,
        stored: Mapping[str, Any] | None = None,
    ) -> None:
        """``stored`` is a snapshot; counts of an earlier day are dropped."""
        self._today = today
        self.on_change = on_change
        self._date = today()
        self._count = 0
        if stored and stored.get("date") == self._date:
            try:
                self._count = max(0, int(stored.get("deleted", 0)))
            except TypeError, ValueError:
                self._count = 0

    def _roll(self) -> None:
        today = self._today()
        if today != self._date:
            self._date, self._count = today, 0

    @property
    def used(self) -> int:
        """Deleted today."""
        self._roll()
        return self._count

    def add(self, count: int) -> None:
        """Count ``count`` deleted mails."""
        if count <= 0:
            return
        self._roll()
        self._count += count
        self.on_change()

    def remaining(self, limit: int) -> int | None:
        """Mails that may still be deleted today; None without a limit (``limit`` 0)."""
        return None if limit <= 0 else max(0, limit - self.used)

    def snapshot(self) -> dict[str, Any]:
        """Data to persist."""
        self._roll()
        return {"date": self._date, "deleted": self._count}
