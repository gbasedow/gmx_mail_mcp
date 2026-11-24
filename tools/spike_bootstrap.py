"""Bootstrap a FRESH, throw-away Home Assistant for end-to-end tests of the MCP endpoint (M0, M1).

Refuses to run unless the instance is not onboarded yet, so it cannot touch a real installation. It
1. onboards an owner (random password, never printed),
2. creates a dedicated non-admin user "MCP Client" with local credentials (D9),
3. creates a long-lived access token for each of them (as the profile page does),
and writes the two tokens to ``<out>/owner.token`` and ``<out>/mcp.token`` (mode 600). Prints only the id of the MCP user.
The entry itself needs a mail login (config flow since M1) and is set up separately.

Usage: python tools/spike_bootstrap.py http://127.0.0.1:18123 <out-dir>   (needs aiohttp, e.g. from the HA venv)
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import aiohttp

CLIENT_ID = "http://127.0.0.1/spike"  # IndieAuth client id; must share the host of redirect_uri


async def _token_from_code(session: aiohttp.ClientSession, base: str, code: str) -> str:
    async with session.post(f"{base}/auth/token", data={"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}) as resp:
        resp.raise_for_status()
        return str((await resp.json())["access_token"])


async def _ws(session: aiohttp.ClientSession, base: str, token: str, *commands: dict[str, Any]) -> list[Any]:
    results = []
    async with session.ws_connect(base.replace("http", "ws", 1) + "/api/websocket") as ws:
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": token})
        if (await ws.receive_json())["type"] != "auth_ok":
            raise RuntimeError("websocket auth failed")
        for number, command in enumerate(commands, start=1):
            await ws.send_json({"id": number, **command})
            reply = await ws.receive_json()
            if not reply.get("success"):
                raise RuntimeError(f"websocket command {command['type']} failed: {reply.get('error')}")
            results.append(reply["result"])
    return results


async def _login(session: aiohttp.ClientSession, base: str, username: str, password: str) -> str:
    body = {"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": f"{CLIENT_ID}/cb"}
    async with session.post(f"{base}/auth/login_flow", json=body) as resp:
        flow_id = (await resp.json())["flow_id"]
    async with session.post(
        f"{base}/auth/login_flow/{flow_id}", json={"username": username, "password": password, "client_id": CLIENT_ID}
    ) as resp:
        result = await resp.json()
    if result.get("type") != "create_entry":
        raise RuntimeError("login failed")
    return await _token_from_code(session, base, result["result"])


def _write_secret(path: Path, value: str) -> None:
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(value)


async def main(base: str, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/api/onboarding") as resp:
            steps = await resp.json() if resp.status == 200 else None
        if not steps or any(step["step"] == "user" and step["done"] for step in steps):
            sys.exit("Refusing: instance is already onboarded (not a fresh throw-away instance).")

        owner_password = secrets.token_urlsafe(24)
        body = {"client_id": CLIENT_ID, "name": "Spike Owner", "username": "owner", "password": owner_password, "language": "en"}
        async with session.post(f"{base}/api/onboarding/users", json=body) as resp:
            resp.raise_for_status()
            owner_session = await _token_from_code(session, base, (await resp.json())["auth_code"])

        mcp_password = secrets.token_urlsafe(24)
        (mcp_user,) = await _ws(
            session,
            base,
            owner_session,
            {"type": "config/auth/create", "name": "MCP Client", "group_ids": ["system-users"], "local_only": False},
        )
        mcp_user_id = mcp_user["user"]["id"]
        owner_llat, _ = await _ws(
            session,
            base,
            owner_session,
            {"type": "auth/long_lived_access_token", "client_name": "spike owner", "lifespan": 1},
            {"type": "config/auth_provider/homeassistant/create", "user_id": mcp_user_id, "username": "mcp", "password": mcp_password},
        )
        mcp_session = await _login(session, base, "mcp", mcp_password)
        (mcp_llat,) = await _ws(
            session, base, mcp_session, {"type": "auth/long_lived_access_token", "client_name": "spike mcp", "lifespan": 1}
        )

    _write_secret(out / "owner.token", owner_llat)
    _write_secret(out / "mcp.token", mcp_llat)
    print(json.dumps({"mcp_user_id": mcp_user_id}))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1].rstrip("/"), Path(sys.argv[2])))
