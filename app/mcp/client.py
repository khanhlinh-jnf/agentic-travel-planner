"""MCP clients for live providers and the deterministic local fallback."""

from __future__ import annotations

import json
import re
import sys
from typing import Any, Literal

from app.config import settings
from app.services.observability import tool_span

ProviderName = Literal["mock", "serpapi", "booking"]
_cached_tools: dict[ProviderName, dict[str, Any]] = {}


class MCPEmptyResponseError(RuntimeError):
    """A remote MCP tool completed without structured content or text."""


def _connection(provider: ProviderName) -> dict[str, Any]:
    if provider == "mock":
        return {
            "command": sys.executable,
            "args": ["-m", "app.mcp.server"],
            "cwd": str(settings.project_root),
            "transport": "stdio",
        }
    if provider == "serpapi":
        return {
            "url": settings.serpapi_mcp_url,
            "transport": "streamable_http",
            "headers": {"Authorization": f"Bearer {settings.serpapi_api_key}"},
        }

    connection: dict[str, Any] = {
        "url": settings.booking_mcp_url,
        "transport": "streamable_http",
    }
    if settings.rapidapi_key:
        connection["headers"] = {"x-rapidapi-key": settings.rapidapi_key}
    return connection


async def get_provider_tools(provider: ProviderName) -> dict[str, Any]:
    if provider in _cached_tools:
        return _cached_tools[provider]

    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient({provider: _connection(provider)})
    tools = await client.get_tools(server_name=provider)
    _cached_tools[provider] = {tool.name: tool for tool in tools}
    return _cached_tools[provider]


def _parse_json_text(value: str) -> Any:
    text = value.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.I)
        if fenced:
            return json.loads(fenced.group(1))
        starts = [position for position in (text.find("{"), text.find("[")) if position >= 0]
        if starts:
            start = min(starts)
            end = max(text.rfind("}"), text.rfind("]"))
            if end > start:
                return json.loads(text[start : end + 1])
        raise


def _coerce_payload(value: Any) -> Any:
    if isinstance(value, str):
        return _parse_json_text(value)
    if isinstance(value, list):
        if value and all(isinstance(item, dict) and "type" not in item for item in value):
            return value
        text_parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
            elif hasattr(item, "text"):
                text_parts.append(str(item.text))
        if text_parts:
            text = "".join(text_parts).strip()
            if not text:
                raise MCPEmptyResponseError("MCP tool returned an empty text response")
            return _parse_json_text(text)
        return value
    if isinstance(value, dict):
        for key in ("structuredContent", "structured_content"):
            if key in value and value[key] is not None:
                return _coerce_payload(value[key])
        if set(value) == {"content"}:
            return _coerce_payload(value["content"])
        return value
    if hasattr(value, "model_dump"):
        return _coerce_payload(value.model_dump(mode="json"))
    raise TypeError(f"Unsupported MCP result type: {type(value).__name__}")


async def call_provider_tool(provider: ProviderName, name: str, arguments: dict) -> Any:
    with tool_span(provider, name):
        tools = await get_provider_tools(provider)
        if name not in tools:
            raise LookupError(f"MCP tool not found on {provider}: {name}")
        return _coerce_payload(await tools[name].ainvoke(arguments))


def reset_cache() -> None:
    _cached_tools.clear()
