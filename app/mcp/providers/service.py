"""Provider selection with whole-response mock fallback."""

from __future__ import annotations

from datetime import date

from app.config import settings
from app.mcp.providers.mock import search_flights_mock, search_hotels_mock
from app.mcp.providers.openai_web import search_flights_web, search_hotels_web
from app.schemas import FlightSearchResult, HotelSearchResult


def search_flights(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
) -> FlightSearchResult:
    if settings.use_mock_travel_data:
        return search_flights_mock(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
        )
    try:
        return search_flights_web(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
        )
    except Exception as exc:
        fallback = search_flights_mock(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
        )
        fallback.warning = f"Live flight search không dùng được ({type(exc).__name__}); dùng mock."
        return fallback


def search_hotels(
    *,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
    max_price: int | None,
) -> HotelSearchResult:
    if settings.use_mock_travel_data:
        return search_hotels_mock(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
    try:
        return search_hotels_web(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
    except Exception as exc:
        fallback = search_hotels_mock(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
        fallback.warning = f"Live hotel search không dùng được ({type(exc).__name__}); dùng mock."
        return fallback

