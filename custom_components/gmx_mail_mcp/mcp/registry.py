"""Tool registry: catalogue of all tools, default switch state (D12) and the per-entry filter.

Free of Home Assistant imports. The catalogue is static (``TOOL_SPECS``) so that the options flow can list every tool
even while the entry is not loaded; the tool objects themselves are built per entry from a :class:`ToolContext`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ..client.imap import Executor, ImapWorker
from ..const import CONF_ENABLED_TOOLS, CONF_KNOWN_TOOLS, Permission
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
    # Address of the account: the sender of drafts (GMX only accepts own addresses, GMX-NOTES row 21).
    email: str = ""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Static description of a tool: name and level are known without a loaded entry."""

    name: str
    level: Permission
    build: Callable[[ToolContext], Tool]


def _specs() -> tuple[ToolSpec, ...]:
    # Imported here: tool modules import this module's ToolContext/ToolSpec.
    from .tools import attachment, organize, read, status  # noqa: PLC0415

    return (status.SPEC, *read.SPECS, attachment.SPEC, *organize.SPECS)


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
