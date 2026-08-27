"""Flight and Hotel specialist logic shared by hierarchy and revision swarm."""

from __future__ import annotations

from app.mcp import client as mcp_client
from app.schemas import (
    FlightOption,
    FlightSearchResult,
    HotelOption,
    HotelSearchResult,
    TripRequest,
)


def rank_flights(options: list[FlightOption], request: TripRequest) -> list[FlightOption]:
    prefs = request.flight_preferences

    def hard_match(option: FlightOption) -> bool:
        if prefs.departure_after and option.departure_time:
            if option.departure_time < prefs.departure_after:
                return False
        if prefs.departure_before and option.departure_time:
            if option.departure_time > prefs.departure_before:
                return False
        if prefs.airline and prefs.airline.lower() not in option.airline.lower():
            return False
        if prefs.max_budget and option.total_price > prefs.max_budget:
            return False
        return True

    matching = [option for option in options if hard_match(option)]
    candidates = matching or options
    return sorted(
        candidates,
        key=lambda option: (
            0 if option.stops == 0 else 1,
            option.total_price,
            option.duration_minutes or 10**9,
        ),
    )


def rank_hotels(options: list[HotelOption], request: TripRequest) -> list[HotelOption]:
    prefs = request.hotel_preferences

    def hard_match(option: HotelOption) -> bool:
        if prefs.max_nightly_price and option.nightly_price > prefs.max_nightly_price:
            return False
        if prefs.min_stars and (option.stars or 0) < prefs.min_stars:
            return False
        if prefs.min_rating and (option.rating or 0) < prefs.min_rating:
            return False
        if prefs.preferred_area and option.area:
            if prefs.preferred_area.lower() not in option.area.lower():
                return False
        return True

    matching = [option for option in options if hard_match(option)]
    candidates = matching or options
    return sorted(
        candidates,
        key=lambda option: (
            option.nightly_price,
            -(option.rating or 0),
        ),
    )


async def search_flights(request: TripRequest) -> FlightSearchResult:
    assert request.origin and request.destination and request.departure_date
    assert request.travelers
    payload = await mcp_client.call_travel_tool(
        "search_flights",
        {
            "origin": request.origin,
            "destination": request.destination,
            "departure_date": request.departure_date.isoformat(),
            "return_date": request.return_date.isoformat() if request.return_date else "",
            "adults": request.travelers,
            "currency": request.currency,
        },
    )
    result = FlightSearchResult.model_validate(payload)
    result.results = rank_flights(result.results, request)
    return result


async def search_hotels(request: TripRequest) -> HotelSearchResult:
    assert request.destination and request.departure_date and request.return_date
    assert request.travelers
    payload = await mcp_client.call_travel_tool(
        "search_hotels",
        {
            "destination": request.destination,
            "check_in_date": request.departure_date.isoformat(),
            "check_out_date": request.return_date.isoformat(),
            "adults": request.travelers,
            "currency": request.currency,
            "max_price": request.hotel_preferences.max_nightly_price,
        },
    )
    result = HotelSearchResult.model_validate(payload)
    result.results = rank_hotels(result.results, request)
    return result

