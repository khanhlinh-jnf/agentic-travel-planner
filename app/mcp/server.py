"""Local stdio MCP server used only for deterministic fallback and tests."""

from __future__ import annotations

from datetime import date

from mcp.server.fastmcp import FastMCP

from app.mcp.providers.mock import (
    find_hotel_by_name_mock,
    flight_booking_options_mock,
    search_flights_mock,
    search_hotels_mock,
    search_places_mock,
)

mcp = FastMCP("travel-mock-fallback")


@mcp.tool(name="search_flights_mock")
def search_flights_tool(
    origin: str,
    destination: str,
    departure_date: str,
    return_date: str = "",
    adults: int = 1,
    currency: str = "VND",
) -> dict:
    """Return deterministic demo flights. This tool never performs a live search."""
    if currency.upper() != "VND":
        raise ValueError("MVP only supports VND")
    result = search_flights_mock(
        origin=origin,
        destination=destination,
        departure_date=date.fromisoformat(departure_date),
        return_date=date.fromisoformat(return_date) if return_date else None,
        adults=adults,
    )
    return result.model_dump(mode="json")


@mcp.tool(name="search_hotels_mock")
def search_hotels_tool(
    destination: str,
    checkin_date: str,
    checkout_date: str,
    adults: int = 1,
    currency: str = "VND",
    budget_per_night: int | None = None,
) -> dict:
    """Return deterministic demo hotels. This tool never performs a live search."""
    if currency.upper() != "VND":
        raise ValueError("MVP only supports VND")
    result = search_hotels_mock(
        destination=destination,
        check_in_date=date.fromisoformat(checkin_date),
        check_out_date=date.fromisoformat(checkout_date),
        adults=adults,
        max_price=budget_per_night,
    )
    return result.model_dump(mode="json")


@mcp.tool(name="search_places_mock")
def search_places_tool(destination: str) -> dict:
    """Return deterministic places with address and rating for offline fallback."""
    return search_places_mock(destination=destination).model_dump(mode="json")


@mcp.tool(name="flight_booking_options_mock")
def flight_booking_options_tool(flight_id: str) -> dict:
    """Return read-only demo seller options; no booking request is executed."""
    return flight_booking_options_mock(flight_id).model_dump(mode="json")


@mcp.tool(name="find_hotel_by_name_mock")
def find_hotel_by_name_tool(
    hotel_name: str,
    destination: str,
    checkin_date: str,
    checkout_date: str,
    adults: int = 1,
    currency: str = "VND",
) -> dict:
    """Return one deterministic demo hotel by name."""
    if currency.upper() != "VND":
        raise ValueError("MVP only supports VND")
    result = find_hotel_by_name_mock(
        hotel_name=hotel_name,
        destination=destination,
        check_in_date=date.fromisoformat(checkin_date),
        check_out_date=date.fromisoformat(checkout_date),
        adults=adults,
    )
    return result.model_dump(mode="json")


@mcp.resource("travel://capabilities")
def capabilities() -> str:
    return "Deterministic fallback evidence and read-only details; no booking or payment."


if __name__ == "__main__":
    mcp.run(transport="stdio")
