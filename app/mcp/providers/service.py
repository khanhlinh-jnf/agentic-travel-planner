"""Async provider facade: live MCP first, local MCP fallback as one response."""

from __future__ import annotations

from datetime import date

from app.config import settings
from app.mcp import client as mcp_client
from app.mcp.client import MCPEmptyResponseError
from app.mcp.providers.booking import find_hotel_by_name_live, search_hotels_live
from app.mcp.providers.serpapi import (
    flight_booking_options_live,
    search_flights_live,
    search_places_live,
)
from app.schemas import (
    FlightBookingOptionsResult,
    FlightOption,
    FlightPreferences,
    FlightSearchResult,
    HotelDetailResult,
    HotelOption,
    HotelSearchResult,
    PlaceSearchResult,
)

_DEFAULT_BOOKING_MCP_URL = "https://hotels.flightpowers.com/mcp"


def _booking_live_configured() -> bool:
    return bool(
        settings.rapidapi_key
        or settings.booking_mcp_url.rstrip("/") != _DEFAULT_BOOKING_MCP_URL.rstrip("/")
    )


async def _mock_flights(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
) -> FlightSearchResult:
    payload = await mcp_client.call_provider_tool(
        "mock",
        "search_flights_mock",
        {
            "origin": origin,
            "destination": destination,
            "departure_date": departure_date.isoformat(),
            "return_date": return_date.isoformat() if return_date else "",
            "adults": adults,
            "currency": "VND",
        },
    )
    return FlightSearchResult.model_validate(payload)


async def _mock_hotels(
    *,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
    max_price: int | None,
) -> HotelSearchResult:
    payload = await mcp_client.call_provider_tool(
        "mock",
        "search_hotels_mock",
        {
            "destination": destination,
            "checkin_date": check_in_date.isoformat(),
            "checkout_date": check_out_date.isoformat(),
            "adults": adults,
            "currency": "VND",
            "budget_per_night": max_price,
        },
    )
    return HotelSearchResult.model_validate(payload)


async def _mock_places(*, destination: str) -> PlaceSearchResult:
    payload = await mcp_client.call_provider_tool(
        "mock", "search_places_mock", {"destination": destination}
    )
    return PlaceSearchResult.model_validate(payload)


async def search_flights(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
    preferences: FlightPreferences | None = None,
    allow_live: bool = True,
) -> FlightSearchResult:
    if settings.use_mock_travel_data or not allow_live:
        return await _mock_flights(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
        )
    if not settings.serpapi_api_key:
        fallback = await _mock_flights(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
        )
        fallback.warning = "SERPAPI_API_KEY chưa cấu hình; đang dùng flight mock."
        return fallback
    try:
        return await search_flights_live(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
            preferences=preferences,
        )
    except Exception as exc:
        fallback = await _mock_flights(
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
        )
        fallback.provider_call_count = 2 if return_date else 1
        fallback.warning = f"SerpApi MCP lỗi ({type(exc).__name__}); đang dùng flight mock."
        return fallback


async def search_hotels(
    *,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
    max_price: int | None,
    allow_live: bool = True,
) -> HotelSearchResult:
    if settings.use_mock_travel_data or not allow_live:
        return await _mock_hotels(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
    if not _booking_live_configured():
        fallback = await _mock_hotels(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
        fallback.warning = (
            "Booking MCP chưa có RapidAPI key hoặc gateway URL; đang dùng hotel mock."
        )
        return fallback
    try:
        return await search_hotels_live(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
    except MCPEmptyResponseError:
        fallback = await _mock_hotels(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
        fallback.provider_call_count = 1
        fallback.warning = (
            "Booking MCP trả dữ liệu rỗng từ upstream; hãy kiểm tra RapidAPI subscription/quota. "
            "Đang dùng hotel mock."
        )
        return fallback
    except Exception as exc:
        fallback = await _mock_hotels(
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
            max_price=max_price,
        )
        fallback.provider_call_count = 1
        fallback.warning = f"Booking MCP lỗi ({type(exc).__name__}); đang dùng hotel mock."
        return fallback


async def search_places(
    *, destination: str, allow_live: bool = True
) -> PlaceSearchResult:
    if settings.use_mock_travel_data or not allow_live:
        return await _mock_places(destination=destination)
    if not settings.serpapi_api_key:
        fallback = await _mock_places(destination=destination)
        fallback.warning = "SERPAPI_API_KEY chưa cấu hình; đang dùng địa điểm mock."
        return fallback
    try:
        return await search_places_live(destination=destination)
    except Exception as exc:
        fallback = await _mock_places(destination=destination)
        fallback.provider_call_count = 1
        fallback.warning = (
            f"SerpApi Google Maps lỗi ({type(exc).__name__}); đang dùng địa điểm mock."
        )
        return fallback


async def get_flight_booking_options(
    *,
    flight: FlightOption,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
    allow_live: bool = True,
) -> FlightBookingOptionsResult:
    if (
        settings.use_mock_travel_data
        or not allow_live
        or flight.source == "mock"
        or not settings.serpapi_api_key
    ):
        payload = await mcp_client.call_provider_tool(
            "mock", "flight_booking_options_mock", {"flight_id": flight.id}
        )
        return FlightBookingOptionsResult.model_validate(payload)
    try:
        return await flight_booking_options_live(
            flight=flight,
            origin=origin,
            destination=destination,
            departure_date=departure_date,
            return_date=return_date,
            adults=adults,
        )
    except Exception as exc:
        return FlightBookingOptionsResult(
            source="mock",
            provider_call_count=1 if flight.booking_token else 2,
            warning=(
                f"Không lấy được nơi bán thật ({type(exc).__name__}). "
                "Không hiển thị seller mô phỏng để tránh gây nhầm lẫn."
            ),
        )


async def get_hotel_detail(
    *,
    hotel: HotelOption,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
    allow_live: bool = True,
) -> HotelDetailResult:
    if (
        settings.use_mock_travel_data
        or not allow_live
        or hotel.source == "mock"
        or not _booking_live_configured()
    ):
        payload = await mcp_client.call_provider_tool(
            "mock",
            "find_hotel_by_name_mock",
            {
                "hotel_name": hotel.name,
                "destination": destination,
                "checkin_date": check_in_date.isoformat(),
                "checkout_date": check_out_date.isoformat(),
                "adults": adults,
                "currency": "VND",
            },
        )
        return HotelDetailResult.model_validate(payload)
    try:
        return await find_hotel_by_name_live(
            hotel_name=hotel.name,
            destination=destination,
            check_in_date=check_in_date,
            check_out_date=check_out_date,
            adults=adults,
        )
    except Exception as exc:
        payload = await mcp_client.call_provider_tool(
            "mock",
            "find_hotel_by_name_mock",
            {
                "hotel_name": hotel.name,
                "destination": destination,
                "checkin_date": check_in_date.isoformat(),
                "checkout_date": check_out_date.isoformat(),
                "adults": adults,
                "currency": "VND",
            },
        )
        fallback = HotelDetailResult.model_validate(payload)
        fallback.provider_call_count = 1
        fallback.warning = (
            f"Không tra lại được hotel thật ({type(exc).__name__}); "
            "đang hiển thị dữ liệu mô phỏng."
        )
        return fallback
