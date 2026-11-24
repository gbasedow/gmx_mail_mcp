"""Tool registry: catalogue of all tools, default switch state (D12) and the per-entry filter.

Free of Home Assistant imports. The catalogue is static (``TOOL_SPECS``) so that the options flow can list every tool
even while the entry is not loaded; the tool objects themselves are built per entry from a :class:`ToolContext`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..client.imap import Executor, ImapWorker
from ..const import CONF_ENABLED_TOOLS, Permission
from .protocol import Tool


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


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of a tool: name and level are known without a loaded entry."""

    name: str
    level: Permission
    build: Callable[[ToolContext], Tool]


def _specs() -> tuple[ToolSpec, ...]:
    # Imported here: tool modules import this module's ToolContext/ToolSpec.
    from .tools import attachment, read, status  # noqa: PLC0415

    return (status.SPEC, *read.SPECS, attachment.SPEC)


TOOL_SPECS: tuple[ToolSpec, ...] = _specs()
TOOL_NAMES: frozenset[str] = frozenset(spec.name for spec in TOOL_SPECS)


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


def build_tools(context: ToolContext) -> tuple[Tool, ...]:
    """Build every tool for one entry, in catalogue order."""
    tools = tuple(spec.build(context) for spec in TOOL_SPECS)
    for spec, tool in zip(TOOL_SPECS, tools, strict=True):
        if (tool.name, tool.level) != (spec.name, spec.level):
            raise ValueError(f"tool {tool.name} does not match its spec")
    return tools


def enabled_tools(tools: Iterable[Tool], options: Mapping[str, Any]) -> tuple[Tool, ...]:
    """Filter the built tools by the switches in ``options``."""
    names = enabled_tool_names(options)
    return tuple(tool for tool in tools if tool.name in names)
