"""Deleting tools (level D, M4): trash, delete permanently, delete empty folders.

All are off by default (D12). Rules from the client layer (``client/deleter.py``): permanent deletion only in the trash or
spam folder with ``\\Deleted`` + ``UID EXPUNGE`` of exactly these UIDs (never a mailbox-wide EXPUNGE, never CLOSE);
folders only when empty and without subfolders (D40). Every check runs again when the action is executed.

Two steps for ``gmx_delete_permanently`` and ``gmx_delete_folder``: without ``confirm_token`` nothing is deleted and a
preview plus a code is returned; the code is bound to exactly these parameters, valid 5 minutes, usable once and lost on
a restart (``confirm.py``). It forces a step in which the client should ask the person; it proves no consent. The person
can switch the step off per tool (``tools_without_confirmation``, D45). ``gmx_trash`` is reversible and has no code (D38).
Daily limit (D39): ``max_delete_per_day``, 0 = none; trash and permanent deletion count together.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

from ...client.deleter import check_delete_folder, delete_folder, delete_permanently, preview_permanent, trash
from ...client.reader import MailRef
from ...confirm import InvalidTokenError
from ...const import CONF_ALLOW_PERMANENT_DELETE, CONF_MAX_DELETE_PER_DAY, MAX_DELETE_PER_CALL, Permission
from ..protocol import Tool, ToolResult
from ..registry import ToolContext, ToolSpec, skips_confirmation
from .common import (
    MAX_FOLDER,
    REF_SCHEMA,
    ToolInputError,
    check_keys,
    envelope,
    error_result,
    get_ref,
    get_str,
    json_result,
    tool_errors,
)
from .read import server_state

TRASH: Final = "gmx_trash"
DELETE_PERMANENTLY: Final = "gmx_delete_permanently"
DELETE_FOLDER: Final = "gmx_delete_folder"
MAX_TOKEN: Final = 512

_REFS_SCHEMA: Final[dict[str, Any]] = {
    "type": "array",
    "minItems": 1,
    "maxItems": MAX_DELETE_PER_CALL,
    "items": REF_SCHEMA,
    "description": f"Message references exactly as returned by gmx_search (1 to {MAX_DELETE_PER_CALL}).",
}
_TOKEN_SCHEMA: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "maxLength": MAX_TOKEN,
    "description": "confirm_token from the preview of the first call (same arguments, within 5 minutes).",
}
_TWO_STEPS: Final = (
    "Two steps: the first call (without confirm_token) deletes nothing and returns a preview and a confirm_token; show the "
    "preview to the person and ask. Only after their consent call again with the same arguments and the confirm_token "
    "(valid 5 minutes, once). If the person switched confirmation off for this tool, the first call deletes at once "
    "(the result says confirmation: skipped)."
)


def _get_delete_refs(args: Mapping[str, Any]) -> list[MailRef]:
    value = args.get("refs")
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DELETE_PER_CALL:
        raise ToolInputError(f"refs must be a list of 1 to {MAX_DELETE_PER_CALL} references (as returned by gmx_search)")
    refs: list[MailRef] = []
    for index, item in enumerate(value):
        ref = get_ref({"ref": item}, "ref")
        if ref in refs:
            raise ToolInputError(f"refs[{index}] is listed twice")
        refs.append(ref)
    return refs


def _canonical(refs: Sequence[MailRef]) -> dict[str, Any]:
    """The code is bound to the set of messages, not to their order."""
    return {"refs": sorted((r.as_dict() for r in refs), key=lambda d: (d["folder"], d["uid"], d["uidvalidity"]))}


def _limit_error(context: ToolContext, count: int) -> ToolResult | None:
    """Daily limit (D39); None when the call fits (or there is no limit)."""
    limit = int(context.options().get(CONF_MAX_DELETE_PER_DAY, 0) or 0)
    remaining = context.deletes.remaining(limit)
    if remaining is None or count <= remaining:
        return None
    return error_result(
        "daily_delete_limit",
        "The daily limit for deleting mails set in the integration options would be exceeded.",
        limit=limit,
        remaining=remaining,
    )


def _token(args: Mapping[str, Any]) -> str | None:
    return get_str(args, "confirm_token", max_len=MAX_TOKEN)


def _token_error() -> ToolResult:
    return error_result(
        "confirm_token_invalid",
        "The confirm_token is invalid, expired, already used or for other arguments; call again without it for a new preview.",
    )


# --------------------------------------------------------------------------- gmx_trash


def _build_trash(context: ToolContext) -> Tool:
    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, ("refs",))
        refs = _get_delete_refs(args)
        if (refused := _limit_error(context, len(refs))) is not None:
            return refused
        info, folder_map = await server_state(context)
        target, result = await context.worker.async_run(lambda s: trash(s, info, folder_map, refs), readonly=False)
        context.deletes.add(result.count)
        data: dict[str, Any] = {"count": result.count, "folders": result.folders, "trash_folder": target}
        if result.new_refs:
            data["new_refs"] = result.new_refs
        if result.missing:
            data["missing"] = result.missing
        return json_result(envelope(context.account, "trash", data, warnings=result.warnings))

    return Tool(
        name=TRASH,
        title="Move GMX mails to the trash",
        description=(
            f"Moves 1 to {MAX_DELETE_PER_CALL} mails into the trash (Gelöscht) and returns their new references; undo with "
            "gmx_move. Mails already in the trash are refused (deleting there is permanent: gmx_delete_permanently). "
            "Counts towards the daily delete limit if one is set."
        ),
        input_schema={
            "type": "object",
            "properties": {"refs": _REFS_SCHEMA},
            "required": ["refs"],
            "additionalProperties": False,
        },
        level=Permission.DELETE,
        handler=handler,
    )


# --------------------------------------------------------------------------- gmx_delete_permanently


def _build_delete_permanently(context: ToolContext) -> Tool:
    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, ("refs", "confirm_token"))
        refs = _get_delete_refs(args)
        token = _token(args)
        if not context.options().get(CONF_ALLOW_PERMANENT_DELETE):
            return error_result(
                "permanent_delete_disabled",
                "Permanent deletion is switched off in the integration options; use gmx_trash instead.",
            )
        if (refused := _limit_error(context, len(refs))) is not None:
            return refused
        params = _canonical(refs)
        skip = skips_confirmation(context.options(), DELETE_PERMANENTLY)
        info, folder_map = await server_state(context)
        if not skip and token is None:
            preview = await context.worker.async_run(lambda s: preview_permanent(s, info, folder_map, refs), readonly=True)
            data: dict[str, Any] = {"preview": preview, "deleted": 0}
            if preview["count"]:
                data |= {"confirm_token": context.tokens.issue(DELETE_PERMANENTLY, params), "expires_in": context.tokens.ttl}
            facts = {"outcome": "preview", "count": preview["count"], "folders": preview["folders"]}
            return json_result(envelope(context.account, "delete_permanently", data), audit=facts)
        if not skip:
            try:
                context.tokens.consume(token or "", DELETE_PERMANENTLY, params)
            except InvalidTokenError:
                return _token_error()
        result = await context.worker.async_run(lambda s: delete_permanently(s, info, folder_map, refs), readonly=False)
        context.deletes.add(result.count)
        confirmation = "skipped" if skip else "code"
        data = {"count": result.count, "folders": result.folders, "confirmation": confirmation}
        if result.missing:
            data["missing"] = result.missing
        env = envelope(context.account, "delete_permanently", data, warnings=result.warnings)
        return json_result(env, audit={"confirmation": confirmation})

    return Tool(
        name=DELETE_PERMANENTLY,
        title="Delete GMX mails permanently",
        description=(
            f"Deletes 1 to {MAX_DELETE_PER_CALL} mails for good; they cannot be restored. Only mails in the trash or the "
            "spam folder (move others with gmx_trash first), and only if permanent deletion is allowed in the integration "
            f"options. {_TWO_STEPS}"
        ),
        input_schema={
            "type": "object",
            "properties": {"refs": _REFS_SCHEMA, "confirm_token": _TOKEN_SCHEMA},
            "required": ["refs"],
            "additionalProperties": False,
        },
        level=Permission.DELETE,
        handler=handler,
    )


# --------------------------------------------------------------------------- gmx_delete_folder


def _build_delete_folder(context: ToolContext) -> Tool:
    @tool_errors
    async def handler(args: dict[str, Any]) -> ToolResult:
        check_keys(args, ("folder", "confirm_token"))
        folder = get_str(args, "folder", max_len=MAX_FOLDER, required=True) or ""
        token = _token(args)
        params = {"path": folder}
        skip = skips_confirmation(context.options(), DELETE_FOLDER)
        _info, folder_map = await server_state(context)
        if not skip and token is None:
            preview = await context.worker.async_run(lambda s: check_delete_folder(s, folder_map, folder), readonly=True)
            data: dict[str, Any] = {
                "preview": preview,
                "deleted": False,
                "confirm_token": context.tokens.issue(DELETE_FOLDER, params),
                "expires_in": context.tokens.ttl,
            }
            return json_result(envelope(context.account, "delete_folder", data), audit={"outcome": "preview", "folders": [folder]})
        if not skip:
            try:
                context.tokens.consume(token or "", DELETE_FOLDER, params)
            except InvalidTokenError:
                return _token_error()
        deleted = await context.worker.async_run(lambda s: delete_folder(s, folder_map, folder), readonly=False)
        confirmation = "skipped" if skip else "code"
        data = {"deleted_folder": deleted, "count": 1, "confirmation": confirmation}
        return json_result(envelope(context.account, "delete_folder", data), audit={"confirmation": confirmation})

    return Tool(
        name=DELETE_FOLDER,
        title="Delete an empty GMX folder",
        description=(
            "Deletes an empty folder without subfolders; INBOX and the special folders (Sent, Drafts, Trash, Spam) cannot "
            f"be deleted. Move or trash its mails first. {_TWO_STEPS}"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "folder": {"type": "string", "minLength": 1, "maxLength": MAX_FOLDER, "description": "Folder path from gmx_list_folders."},
                "confirm_token": _TOKEN_SCHEMA,
            },
            "required": ["folder"],
            "additionalProperties": False,
        },
        level=Permission.DELETE,
        handler=handler,
    )


SPECS: Final = (
    ToolSpec(name=TRASH, level=Permission.DELETE, build=_build_trash),
    ToolSpec(name=DELETE_PERMANENTLY, level=Permission.DELETE, build=_build_delete_permanently, confirmable=True),
    ToolSpec(name=DELETE_FOLDER, level=Permission.DELETE, build=_build_delete_folder, confirmable=True),
)
