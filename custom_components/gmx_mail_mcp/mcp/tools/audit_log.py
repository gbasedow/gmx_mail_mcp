"""``gmx_get_audit_log`` (level R, M4): the newest audit entries of this account (D41, D42).

Entries hold no mail content, subjects, addresses or file names; folder names may come from the mailbox and are data.
"""

from __future__ import annotations

from typing import Any, Final

from ...const import CONF_AUDIT_RETENTION_DAYS, CONF_MAX_DELETE_PER_DAY, DEFAULT_AUDIT_RETENTION_DAYS, Permission
from ...envelope import fit_list
from ..protocol import Tool, ToolResult
from ..registry import ToolContext, ToolSpec
from .common import check_keys, envelope, get_int, json_result, tool_errors

NAME: Final = "gmx_get_audit_log"
MAX_LIMIT: Final = 200
DEFAULT_LIMIT: Final = 50


def build(context: ToolContext) -> Tool:
    """Create the tool for one entry."""

    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, ("limit",))
        limit = get_int(args, "limit", minimum=1, maximum=MAX_LIMIT, default=DEFAULT_LIMIT) or DEFAULT_LIMIT
        options = context.options()
        delete_limit = int(options.get(CONF_MAX_DELETE_PER_DAY, 0) or 0)
        data = {
            "entries": context.audit.latest(limit),
            "stored": len(context.audit),
            "retention_days": int(options.get(CONF_AUDIT_RETENTION_DAYS, DEFAULT_AUDIT_RETENTION_DAYS)),
            "deleted_today": context.deletes.used,
            "max_delete_per_day": delete_limit or None,
        }
        env = envelope(context.account, "get_audit_log", data)
        fit_list(env, "entries")
        return json_result(env)

    return Tool(
        name=NAME,
        title="GMX audit log",
        description=(
            "Returns the newest entries of the audit log of this mailbox: every call of an organising, deleting or sending "
            "tool (also previews, refusals and errors) with time, tool, outcome, count, folder names and a short "
            "fingerprint of the arguments - never mail content, subjects or addresses. Also the mails deleted today."
        ),
        input_schema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": DEFAULT_LIMIT}},
            "additionalProperties": False,
        },
        level=Permission.READ,
        handler=handler,
    )


SPEC = ToolSpec(name=NAME, level=Permission.READ, build=build)
