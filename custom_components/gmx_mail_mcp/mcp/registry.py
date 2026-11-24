"""Tool registry: catalogue of all tools, default switch state (D12) and the per-entry filter.

Free of Home Assistant imports. The catalogue is static (``TOOL_SPECS``) so that the options flow can list every tool
even while the entry is not loaded; the tool objects themselves are built per entry from a :class:`ToolContext`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import ssl
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..audit import AuditLog, DeleteCounter, SendCounter
from ..client.imap import Executor, ImapWorker
from ..client.smtp import SmtpSettings
from ..confirm import ConfirmTokens
from ..const import CONF_ENABLED_TOOLS, CONF_KNOWN_TOOLS, CONF_TOOLS_WITHOUT_CONFIRMATION, Permission
from .protocol import REQUEST_USER, Tool, ToolResult

_LOGGER = logging.getLogger(__name__)


async def _run_inline[T](target: Callable[..., T], *args: Any) -> T:
    return target(*args)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """What a tool may use. Grows with the milestones (SMTP, limits, audit)."""

    worker: ImapWorker
    # Current entry options; read per call so that option changes apply without a reload.
    options: Callable[[], Mapping[str, Any]] = dict
    # Runs CPU-bound work (text extraction) outside the event loop; in HA: hass.async_add_executor_job.
    executor: Executor = _run_inline
    # Label of the mailbox in responses (entry title), so a model with several accounts can tell them apart (D11).
    account: str = ""
    # Address of the account: the sender of drafts (GMX only accepts own addresses, GMX-NOTES row 21).
    email: str = ""
    # Audit log of calls of level O/D/S (D41), deletions of the day (D39), confirmation codes (in memory only).
    audit: AuditLog = field(default_factory=AuditLog)
    deletes: DeleteCounter = field(default_factory=DeleteCounter)
    tokens: ConfirmTokens = field(default_factory=ConfirmTokens)
    # Sending (M5): SMTP settings (None = not configured), a TLS context factory (blocking, called in the executor),
    # messages of the day and recipients of the last hour (D48), one send at a time per account, and what to do when
    # GMX rejects the SMTP login (repair issue, reauth).
    smtp: SmtpSettings | None = None
    tls_context: Callable[[], ssl.SSLContext] = ssl.create_default_context
    sends: SendCounter = field(default_factory=SendCounter)
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    on_smtp_auth_failed: Callable[[], None] = lambda: None


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of a tool: name and level are known without a loaded entry."""

    name: str
    level: Permission
    build: Callable[[ToolContext], Tool]
    # Two steps (preview + confirmation code) unless chosen in ``tools_without_confirmation`` (D45).
    confirmable: bool = False


def skips_confirmation(options: Mapping[str, Any], name: str) -> bool:
    """Whether ``name`` runs without preview and code (D45); only confirmable tools can be chosen."""
    chosen = options.get(CONF_TOOLS_WITHOUT_CONFIRMATION)
    # Defined before the catalogue is built: tool modules import it.
    return name in CONFIRMABLE_TOOLS and isinstance(chosen, list) and name in chosen


def _late_recorder(
    audit: AuditLog, tool: str, level: str, arguments: Mapping[str, Any], user: str | None
) -> Callable[[asyncio.Future[ToolResult]], None]:
    """Audit entry for an action that ended after its call was cancelled (D58, D61); never content."""

    def _record(task: asyncio.Future[ToolResult]) -> None:
        try:
            facts: dict[str, Any] = {}
            if task.cancelled():
                outcome = "late_cancelled"
            elif task.exception() is not None:
                outcome = "late_internal_error"
            else:
                facts = dict(task.result().audit or {})
                outcome = f"late_{facts.get('outcome', 'ok')}"
            audit.record(
                tool=tool,
                level=level,
                outcome=outcome,
                arguments=arguments,
                count=facts.get("count"),
                folders=facts.get("folders") or (),
                user=user,
                error=facts.get("error"),
                confirmation=facts.get("confirmation"),
                recipient_domains=facts.get("recipient_domains") or (),
            )
        except Exception:
            _LOGGER.exception("Audit entry for the late end of %s could not be written", tool)

    return _record


async def run_to_end(
    audit: AuditLog, *, tool: str, level: Permission, arguments: Mapping[str, Any], task: asyncio.Future[ToolResult]
) -> ToolResult:
    """Await ``task`` (an action that changes the mailbox) so that a timeout or abort of the call does not stop it.

    Once an IMAP or SMTP command has begun it may complete on the server anyway; cutting it off would lose the counting
    and leave the outcome open. The call is cancelled as usual (the audit wrapper records ``cancelled``); the action runs
    to its end and records a second entry ``late_ok`` / ``late_error`` / ... (D58 for sending, D61 for deleting).
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        task.add_done_callback(_late_recorder(audit, tool, level.value, dict(arguments), REQUEST_USER.get()))
        raise


def _specs() -> tuple[ToolSpec, ...]:
    # Imported here: tool modules import this module's ToolContext/ToolSpec.
    from .tools import attachment, audit_log, delete, organize, read, send, status  # noqa: PLC0415

    return (status.SPEC, *read.SPECS, attachment.SPEC, audit_log.SPEC, *organize.SPECS, *delete.SPECS, *send.SPECS)


TOOL_SPECS: tuple[ToolSpec, ...] = _specs()
TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in TOOL_SPECS)
CONFIRMABLE_TOOLS: tuple[str, ...] = tuple(spec.name for spec in TOOL_SPECS if spec.confirmable)


def default_enabled_tools() -> list[str]:
    """Default switch state: every tool of level READ on, everything else off (D12)."""
    return [spec.name for spec in TOOL_SPECS if spec.level is Permission.READ]


def enabled_tool_names(options: Mapping[str, Any]) -> frozenset[str]:
    """Names switched on in the entry options; unknown names (removed tools) are ignored.

    Without a saved choice the defaults apply. Once saved, the list is explicit: a READ tool added by a later
    version stays off until it is switched on.
    """
    if CONF_ENABLED_TOOLS not in options:
        return frozenset(default_enabled_tools())
    return frozenset(str(name) for name in options[CONF_ENABLED_TOOLS] or ()) & TOOL_NAMES


# Catalogue of 0.0.4, the last version that did not record ``known_tools`` (D31). Saved options without that record
# were saved by 0.0.4 or earlier; every tool outside this set is new to them.
TOOLS_WITHOUT_RECORD: frozenset[str] = frozenset(
    {
        "gmx_get_status",
        "gmx_list_folders",
        "gmx_search",
        "gmx_get_message",
        "gmx_get_attachment",
        "gmx_get_raw_message",
        "gmx_get_thread",
        "gmx_list_keywords",
    }
)


def new_tool_names(options: Mapping[str, Any]) -> list[str]:
    """Tools added since the switches were last saved (catalogue order); they stay off until chosen (D23, D31).

    Without a saved choice the defaults apply and nothing counts as new.
    """
    if CONF_ENABLED_TOOLS not in options:
        return []
    recorded = options.get(CONF_KNOWN_TOOLS)
    known = {str(name) for name in recorded} if isinstance(recorded, list) else TOOLS_WITHOUT_RECORD
    return [spec.name for spec in TOOL_SPECS if spec.name not in known]


def _audited(tool: Tool, audit: AuditLog) -> Tool:
    """Record every call of a writing tool (D41): result, refusal, error, cancellation (also by the timeout)."""
    inner = tool.handler

    def _record(arguments: Any, facts: Mapping[str, Any]) -> None:
        try:
            audit.record(
                tool=tool.name,
                level=tool.level.value,
                outcome=str(facts.get("outcome", "ok")),
                arguments=arguments if isinstance(arguments, Mapping) else {},
                count=facts.get("count"),
                folders=facts.get("folders") or (),
                user=REQUEST_USER.get(),
                error=facts.get("error"),
                confirmation=facts.get("confirmation"),
                recipient_domains=facts.get("recipient_domains") or (),
            )
        except Exception:
            _LOGGER.exception("Audit entry for %s could not be written", tool.name)  # never fails the call itself

    async def handler(arguments: dict[str, Any]) -> ToolResult:
        try:
            result = await inner(arguments)
        except asyncio.CancelledError:
            # Timeout or aborted request: the action may have run on the server anyway, so it is recorded.
            _record(arguments, {"outcome": "cancelled"})
            raise
        except Exception:
            _record(arguments, {"outcome": "internal_error"})
            raise
        facts = dict(result.audit or {})
        if "outcome" not in facts:
            facts["outcome"] = "error" if result.is_error else "ok"
        _record(arguments, facts)
        return result

    return dataclasses.replace(tool, handler=handler)


def build_tools(context: ToolContext) -> tuple[Tool, ...]:
    """Build every tool for one entry, in catalogue order; tools of level O/D/S are audited (D41)."""
    tools = tuple(spec.build(context) for spec in TOOL_SPECS)
    for spec, tool in zip(TOOL_SPECS, tools, strict=True):
        if (tool.name, tool.level) != (spec.name, spec.level):
            raise ValueError(f"tool {tool.name} does not match its spec")
    return tuple(tool if tool.level is Permission.READ else _audited(tool, context.audit) for tool in tools)


def enabled_tools(tools: Iterable[Tool], options: Mapping[str, Any]) -> tuple[Tool, ...]:
    """Filter the built tools by the switches in ``options``."""
    names = enabled_tool_names(options)
    return tuple(tool for tool in tools if tool.name in names)
