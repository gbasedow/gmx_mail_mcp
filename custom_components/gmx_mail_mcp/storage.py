"""Persistence of the audit log, the delete counter and the send counter in ``.storage`` (D39, D41, D42, D48)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .audit import AuditLog, DeleteCounter, SendCounter
from .const import CONF_AUDIT_RETENTION_DAYS, DEFAULT_AUDIT_RETENTION_DAYS, DOMAIN

STORAGE_VERSION = 1
SAVE_DELAY_S = 5


def _audit_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.audit"


def _deletes_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.deletes"


def _sends_key(entry_id: str) -> str:
    return f"{DOMAIN}.{entry_id}.sends"


@dataclass(slots=True)
class Ledgers:
    """Audit log, delete counter and send counter of one entry with their stores."""

    audit: AuditLog
    deletes: DeleteCounter
    sends: SendCounter
    audit_store: Store[dict[str, Any]]
    deletes_store: Store[dict[str, Any]]
    sends_store: Store[dict[str, Any]]

    async def async_flush(self) -> None:
        """Write all now (unload)."""
        await self.audit_store.async_save(self.audit.snapshot())
        await self.deletes_store.async_save(self.deletes.snapshot())
        await self.sends_store.async_save(self.sends.snapshot())


def _retention(options: Mapping[str, Any]) -> int:
    try:
        return int(options.get(CONF_AUDIT_RETENTION_DAYS, DEFAULT_AUDIT_RETENTION_DAYS))
    except TypeError, ValueError:
        return DEFAULT_AUDIT_RETENTION_DAYS


async def async_load_ledgers(hass: HomeAssistant, entry_id: str, options: Any) -> Ledgers:
    """Load all; ``options`` is called per use so that a changed retention applies at once."""
    audit_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, _audit_key(entry_id), private=True)
    deletes_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, _deletes_key(entry_id), private=True)
    sends_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, _sends_key(entry_id), private=True)
    stored_audit = await audit_store.async_load() or {}
    stored_deletes = await deletes_store.async_load() or {}
    stored_sends = await sends_store.async_load() or {}
    entries = stored_audit.get("entries") if isinstance(stored_audit, dict) else None
    audit = AuditLog(
        retention_days=lambda: _retention(options()),
        entries=entries if isinstance(entries, list) else (),
    )
    deletes = DeleteCounter(
        today=lambda: dt_util.now().date().isoformat(),  # local day of the HA instance
        stored=stored_deletes if isinstance(stored_deletes, dict) else None,
    )
    sends = SendCounter(
        today=lambda: dt_util.now().date().isoformat(),
        stored=stored_sends if isinstance(stored_sends, dict) else None,
    )
    audit.on_change = lambda: audit_store.async_delay_save(audit.snapshot, SAVE_DELAY_S)
    deletes.on_change = lambda: deletes_store.async_delay_save(deletes.snapshot, SAVE_DELAY_S)
    # A message that went out must be counted after a crash too: saved at once, not delayed.
    sends.on_change = lambda: sends_store.async_delay_save(sends.snapshot, 0)
    return Ledgers(
        audit=audit,
        deletes=deletes,
        sends=sends,
        audit_store=audit_store,
        deletes_store=deletes_store,
        sends_store=sends_store,
    )


async def async_remove_ledgers(hass: HomeAssistant, entry_id: str) -> None:
    """Delete all stores (entry removed, D42)."""
    await Store[dict[str, Any]](hass, STORAGE_VERSION, _audit_key(entry_id)).async_remove()
    await Store[dict[str, Any]](hass, STORAGE_VERSION, _deletes_key(entry_id)).async_remove()
    await Store[dict[str, Any]](hass, STORAGE_VERSION, _sends_key(entry_id)).async_remove()
