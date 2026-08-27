"""Independent Travel Search MCP server (stdio transport)."""

from __future__ import annotations

from datetime import date

from mcp.server.fastmcp import FastMCP

from app.mcp.providers.service import search_flights, search_hotels

mcp = FastMCP("travel-search")


@mcp.tool(name="search_flights")
def search_flights_tool(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str = "",
    adults: int = 1,
    currency: str = "VND",
) -> dict:
    """Search and normalize flight options. Prices are estimates; no booking occurs."""
    if currency.upper() != "VND":
        raise ValueError("MVP only supports VND")
    result = search_flights(
        origin=origin,
        destination=destination,
        departure_date=date.fromisoformat(departure_date),
        return_date=date.fromisoformat(return_date) if return_date else None,
        adults=adults,
    )
    return result.model_dump(mode="json")


@mcp.tool(name="search_hotels")
def search_hotels_tool(
    destination: str,
    check_in_date: str,
    check_out_date: str,
    adults: int = 1,
    currency: str = "VND",
    max_price: int | None = None,
) -> dict:
    """Search and normalize hotel options. Prices are estimates; no booking occurs."""
    if currency.upper() != "VND":
        raise ValueError("MVP only supports VND")
    result = search_hotels(
        destination=destination,
        check_in_date=date.fromisoformat(check_in_date),
        check_out_date=date.fromisoformat(check_out_date),
        adults=adults,
        max_price=max_price,
    )
    return result.model_dump(mode="json")


@mcp.resource("travel://capabilities")
def capabilities() -> str:
    return "Flight and hotel recommendation evidence; no booking or payment."


if __name__ == "__main__":
    mcp.run(transport="stdio")
