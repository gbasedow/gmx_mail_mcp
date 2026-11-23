"""GMX Mail MCP: MCP server for a GMX mailbox, hosted inside Home Assistant.

Scaffold only. The client layer (``client/``, ``const.py``, ``options.py``, ``envelope.py``, ``confirm.py``)
is carried over from ``gbasedow/gmx_mail`` and is free of Home Assistant imports. The Home Assistant part
(config flow, MCP HTTP endpoint, tool registry) is still to be built, see ``SPEC.md``.
"""

from __future__ import annotations

from .const import DOMAIN  # noqa: F401
