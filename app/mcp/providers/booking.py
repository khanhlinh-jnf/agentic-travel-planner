"""Normalize Booking.com hotel data returned by the Flightpowers MCP connector."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlparse

from app.config import settings
from app.mcp import client as mcp_client
from app.schemas import HotelDetailResult, HotelOption, HotelSearchResult


class InsufficientBookingEvidence(RuntimeError):
    """The Booking MCP response did not contain a usable hotel price."""


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if urlparse(value).scheme in {"http", "https"} else None


def _number(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(value) if value >= 0 else None
    if isinstance(value, dict):
        for key in ("value", "amount", "price", "price_as_number"):
            if key in value:
                return _number(value[key])
        return None
    if isinstance(value, str):
        digits = re.sub(r"[^0-9]", "", value)
        return int(digits) if digits else None
    return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "hotels", "properties", "data"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        if isinstance(nested, dict):
            return [nested]
    if payload.get("name") or payload.get("hotel_name"):
        return [payload]
    return []


def normalize_hotels(
    payload: Any,
    *,
    destination: str,
    check_in_date: date,
    check_out_date: date,
) -> list[HotelOption]:
    nights = max(1, (check_out_date - check_in_date).days)
    observed = datetime.now(UTC)
    results: list[HotelOption] = []
    for raw in _items(payload):
        explicit_nightly = next(
            (
                price
                for price in (
                    _number(raw.get("nightly_price")),
                    _number(raw.get("price_per_night")),
                )
                if price is not None
            ),
            None,
        )
        stay_total = next(
            (
                price
                for price in (
                    _number(raw.get("total_price")),
                    _number(raw.get("price")),
                    _number(raw.get("price_as_number")),
                )
                if price is not None
            ),
            None,
        )
        nightly = explicit_nightly or (
            round(stay_total / nights) if stay_total is not None else None
        )
        total = stay_total if stay_total is not None else (
            explicit_nightly * nights if explicit_nightly is not None else None
        )
        name = raw.get("name") or raw.get("hotel_name")
        if not name or nightly is None or total is None:
            continue
        amenities = raw.get("amenities") or raw.get("filters") or []
        if isinstance(amenities, str):
            amenities = [amenities]
        rating = _float(raw.get("review_score") or raw.get("rating"))
        stars = _number(raw.get("stars") or raw.get("star_rating"))
        if stars is not None and not 1 <= stars <= 5:
            stars = None
        results.append(
            HotelOption(
                id=f"BH{len(results) + 1}",
                name=str(name),
                destination=destination,
                area=raw.get("location") or raw.get("area"),
                rating=rating if rating is None or 0 <= rating <= 10 else None,
                stars=stars,
                nightly_price=nightly,
                nights=nights,
                total_price=total,
                amenities=[str(value) for value in amenities],
                room_type=raw.get("room_type"),
                review_count=_number(raw.get("review_count")),
                source="booking",
                source_url=_safe_url(
                    raw.get("link") or raw.get("booking_link") or raw.get("url")
                ),
                image_url=_safe_url(
                    raw.get("thumbnail")
                    or raw.get("image_url")
                    or raw.get("photo_url")
                    or raw.get("image")
                ),
                observed_at=observed,
            )
        )
        if len(results) >= settings.travel_search_max_results:
            break
    return results


async def search_hotels_live(
    *,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
    max_price: int | None,
) -> HotelSearchResult:
    arguments: dict[str, Any] = {
        "destination": destination,
        "checkin_date": check_in_date.isoformat(),
        "checkout_date": check_out_date.isoformat(),
        "adults": adults,
        "currency": "vnd",
    }
    if max_price is not None:
        arguments["budget_per_night"] = max_price
    payload = await mcp_client.call_provider_tool("booking", "search_hotels", arguments)
    results = normalize_hotels(
        payload,
        destination=destination,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
    )
    if not results:
        raise InsufficientBookingEvidence("Booking MCP returned no hotel with a usable price")
    return HotelSearchResult(
        source="booking",
        warning="Giá Booking.com thay đổi nhanh; hãy mở link và kiểm tra lại trước khi đặt.",
        results=results,
        provider_call_count=1,
    )


async def find_hotel_by_name_live(
    *,
    hotel_name: str,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
) -> HotelDetailResult:
    payload = await mcp_client.call_provider_tool(
        "booking",
        "find_hotel_by_name",
        {
            "hotel_name": f"{hotel_name} {destination}",
            "checkin_date": check_in_date.isoformat(),
            "checkout_date": check_out_date.isoformat(),
            "adults": adults,
            "currency": "vnd",
        },
    )
    results = normalize_hotels(
        payload,
        destination=destination,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
    )
    if not results:
        raise InsufficientBookingEvidence("Booking MCP returned no matching hotel")
    return HotelDetailResult(
        source="booking",
        warning="Thông tin vừa được tra lại; giá vẫn có thể đổi trước bước checkout.",
        hotel=results[0],
        provider_call_count=1,
    )
