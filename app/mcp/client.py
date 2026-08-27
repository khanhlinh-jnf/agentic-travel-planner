"""Async MCP client used by specialist agents."""

from __future__ import annotations

import json
import sys
from typing import Any

from app.config import settings

_cached_tools: dict[str, Any] | None = None


async def get_travel_tools() -> dict[str, Any]:
    global _cached_tools
    if _cached_tools is not None:
        return _cached_tools

    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {
            "travel": {
                "command": sys.executable,
                "args": ["-m", "app.mcp.server"],
                "cwd": str(settings.project_root),
                "transport": "stdio",
            }
        }
    )
    tools = await client.get_tools()
    _cached_tools = {tool.name: tool for tool in tools}
    return _cached_tools


def _coerce_payload(value: Any) -> dict:
    if isinstance(value, dict):
        if "structuredContent" in value and isinstance(value["structuredContent"], dict):
            return value["structuredContent"]
        return value
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, list):
        text_parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif hasattr(item, "text"):
                text_parts.append(item.text)
        return json.loads("".join(text_parts))
    if hasattr(value, "model_dump"):
        return _coerce_payload(value.model_dump(mode="json"))
    raise TypeError(f"Unsupported MCP result type: {type(value).__name__}")


async def call_travel_tool(name: str, arguments: dict) -> dict:
    tools = await get_travel_tools()
    if name not in tools:
        raise LookupError(f"MCP tool not found: {name}")
    return _coerce_payload(await tools[name].ainvoke(arguments))


def reset_cache() -> None:
    global _cached_tools
    _cached_tools = None

