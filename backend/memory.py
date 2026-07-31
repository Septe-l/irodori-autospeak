"""Read-only client for the memory-mcp server (Streamable HTTP transport).

Only `memory_search` is used — this tool reads long-term memory into a turn's
prompt and never writes to it.
"""

from __future__ import annotations

import json
import logging

import httpx

from .config import Settings

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
HEADERS = {
    "Content-Type": "application/json",
    # Streamable HTTP servers may answer either way, so accept both.
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": PROTOCOL_VERSION,
}


def _parse(body: str) -> dict:
    """Read a JSON-RPC reply, whether it came back plain or as SSE."""
    body = body.strip()
    if body.startswith("{"):
        return json.loads(body)
    for line in body.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk and chunk != "[DONE]":
                return json.loads(chunk)
    raise ValueError(f"unexpected MCP response: {body[:200]}")


class MemoryClient:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._session_id: str | None = None

    async def _rpc(self, client: httpx.AsyncClient, method: str, params: dict, rpc_id: int) -> dict:
        headers = dict(HEADERS)
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        resp = await client.post(
            self.s.mcp_url,
            headers=headers,
            content=json.dumps(
                {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        session = resp.headers.get("mcp-session-id")
        if session:
            self._session_id = session
        resp.raise_for_status()
        return _parse(resp.text)

    async def _initialize(self, client: httpx.AsyncClient) -> dict:
        return await self._rpc(
            client,
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "irodori-autospeak", "version": "0.1.0"},
            },
            1,
        )

    async def ping(self) -> dict:
        """Handshake plus tool listing, for the WebUI's connection test."""
        try:
            async with httpx.AsyncClient(timeout=self.s.mcp_timeout) as client:
                init = await self._initialize(client)
                info = (init.get("result") or {}).get("serverInfo") or {}
                listed = await self._rpc(client, "tools/list", {}, 2)
                tools = [t["name"] for t in (listed.get("result") or {}).get("tools", [])]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        return {
            "ok": True,
            "server": info.get("name"),
            "version": info.get("version"),
            "tools": tools,
            "has_search": "memory_search" in tools,
        }

    async def search(self, query: str, limit: int | None = None) -> str:
        """Memories relevant to `query`, already formatted as text by the server."""
        args: dict = {"limit": limit or self.s.mcp_limit}
        if query.strip():
            args["query"] = query.strip()
        try:
            async with httpx.AsyncClient(timeout=self.s.mcp_timeout) as client:
                if self._session_id is None:
                    await self._initialize(client)
                reply = await self._rpc(
                    client, "tools/call", {"name": "memory_search", "arguments": args}, 3
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("memory search failed: %s: %s", type(e).__name__, e)
            return ""

        if "error" in reply:
            logger.warning("memory search error: %s", reply["error"])
            return ""

        parts = [
            block.get("text", "")
            for block in (reply.get("result") or {}).get("content", [])
            if block.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p).strip()
