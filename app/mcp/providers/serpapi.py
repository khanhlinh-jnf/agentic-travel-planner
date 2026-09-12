"""Build SerpApi MCP calls and normalize Google Flights payloads."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import quote_plus, urlparse

from app.config import settings
from app.mcp import client as mcp_client
from app.mcp.providers.mock import city_code
from app.schemas import (
    FlightBookingOffer,
    FlightBookingOptionsResult,
    FlightLeg,
    FlightOption,
    FlightPreferences,
    FlightSearchResult,
    FlightSegment,
    PlaceOption,
    PlaceSearchResult,
)


class InsufficientSerpApiEvidence(RuntimeError):
    """The provider answered, but no usable flight evidence was found."""


def _safe_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value if urlparse(value).scheme in {"http", "https"} else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(value) if value >= 0 else None
    if isinstance(value, str):
        digits = re.sub(r"[^0-9]", "", value)
        return int(digits) if digits else None
    return None


def _clock(value: Any) -> time | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    try:
        return time(hour=int(match.group(1)), minute=int(match.group(2)))
    except ValueError:
        return None


def _date_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}", value)
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(0))
    except ValueError:
        return None


def _flight_leg(
    raw: dict[str, Any],
    *,
    direction: str,
    price_per_person: int | None = None,
    adults: int = 1,
) -> FlightLeg | None:
    segments: list[FlightSegment] = []
    for raw_segment in raw.get("flights") or []:
        if not isinstance(raw_segment, dict):
            continue
        departure = raw_segment.get("departure_airport") or {}
        arrival = raw_segment.get("arrival_airport") or {}
        departure_at = _date_time(departure.get("time"))
        arrival_at = _date_time(arrival.get("time"))
        if not departure_at or not arrival_at:
            continue
        segments.append(
            FlightSegment(
                airline=str(raw_segment.get("airline") or "Unknown airline"),
                flight_number=raw_segment.get("flight_number"),
                origin_airport=str(departure.get("id") or "?"),
                origin_airport_name=departure.get("name"),
                destination_airport=str(arrival.get("id") or "?"),
                destination_airport_name=arrival.get("name"),
                departure_at=departure_at,
                arrival_at=arrival_at,
                duration_minutes=_integer(raw_segment.get("duration")),
                aircraft=raw_segment.get("airplane"),
                travel_class=raw_segment.get("travel_class"),
            )
        )
    if not segments:
        return None
    return FlightLeg(
        direction=direction,
        segments=segments,
        duration_minutes=_integer(raw.get("total_duration")),
        stops=len(raw.get("layovers") or []) or max(0, len(segments) - 1),
        price_per_person=price_per_person,
        total_price=price_per_person * adults if price_per_person is not None else None,
    )


def _payload_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise InsufficientSerpApiEvidence("SerpApi MCP returned a non-object payload")
    for key in ("result", "data", "response"):
        nested = payload.get(key)
        if isinstance(nested, dict) and (
            "best_flights" in nested
            or "other_flights" in nested
            or "booking_options" in nested
        ):
            return nested
    return payload


def flight_search_params(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
    preferences: FlightPreferences | None = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "engine": "google_flights",
        "departure_id": city_code(origin),
        "arrival_id": city_code(destination),
        "outbound_date": departure_date.isoformat(),
        "type": "1" if return_date else "2",
        "adults": adults,
        "currency": "VND",
        "hl": "vi",
        "gl": "vn",
        "sort_by": "1",
    }
    if return_date:
        params["return_date"] = return_date.isoformat()
    if preferences:
        if preferences.prefer_direct:
            params["stops"] = "1"
        if preferences.max_budget:
            params["max_price"] = max(1, preferences.max_budget // adults)
        if preferences.departure_after or preferences.departure_before:
            start = preferences.departure_after.hour if preferences.departure_after else 0
            end = preferences.departure_before.hour if preferences.departure_before else 23
            params["outbound_times"] = f"{start},{end}"
    return params


def normalize_flight_search(
    payload: Any,
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
) -> FlightSearchResult:
    body = _payload_dict(payload)
    raw_options = [
        *body.get("best_flights", []),
        *body.get("other_flights", []),
    ]
    source_url = _safe_url(
        body.get("search_metadata", {}).get("google_flights_url")
        or body.get("search_metadata", {}).get("google_url")
    ) or "https://www.google.com/travel/flights"
    observed = datetime.now(UTC)
    results: list[FlightOption] = []
    for raw in raw_options:
        if not isinstance(raw, dict):
            continue
        segments = raw.get("flights") or []
        if not segments or not isinstance(segments, list):
            continue
        first = segments[0] if isinstance(segments[0], dict) else {}
        last = segments[-1] if isinstance(segments[-1], dict) else {}
        price = _integer(raw.get("price"))
        if not price:
            continue
        airlines = list(
            dict.fromkeys(
                str(segment.get("airline"))
                for segment in segments
                if isinstance(segment, dict) and segment.get("airline")
            )
        )
        numbers = [
            str(segment.get("flight_number"))
            for segment in segments
            if isinstance(segment, dict) and segment.get("flight_number")
        ]
        carbon = raw.get("carbon_emissions") or {}
        outbound_leg = _flight_leg(
            raw,
            direction="outbound",
            price_per_person=price if return_date is None else None,
            adults=adults,
        )
        results.append(
            FlightOption(
                id=f"SF{len(results) + 1}",
                airline=" + ".join(airlines) or "Unknown airline",
                flight_number=" / ".join(numbers) or None,
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                departure_time=_clock((first.get("departure_airport") or {}).get("time")),
                arrival_time=_clock((last.get("arrival_airport") or {}).get("time")),
                duration_minutes=_integer(raw.get("total_duration")),
                stops=len(raw.get("layovers") or []) or max(0, len(segments) - 1),
                price_per_person=price,
                total_price=price * adults,
                source="serpapi",
                source_url=source_url,
                observed_at=observed,
                booking_token=raw.get("booking_token"),
                departure_token=raw.get("departure_token"),
                carbon_emissions_grams=_integer(carbon.get("this_flight")),
                outbound_leg=outbound_leg,
                price_scope="round_trip" if return_date else "one_way",
                price_note=(
                    "SerpApi cung cấp giá khứ hồi gộp, không tách giá từng chiều."
                    if return_date
                    else None
                ),
            )
        )
        if len(results) >= settings.travel_search_max_results:
            break
    if not results:
        raise InsufficientSerpApiEvidence("SerpApi returned no flight with a usable price")
    return FlightSearchResult(source="serpapi", results=results, provider_call_count=1)


async def search_flights_live(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
    preferences: FlightPreferences | None = None,
) -> FlightSearchResult:
    if return_date:
        try:
            return await _search_round_trip_with_departure_token(
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                adults=adults,
                preferences=preferences,
            )
        except InsufficientSerpApiEvidence:
            return await _search_round_trip_as_two_one_ways(
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                adults=adults,
                preferences=preferences,
                prior_call_count=2,
            )

    params = flight_search_params(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        adults=adults,
        preferences=preferences,
    )
    payload = await mcp_client.call_provider_tool(
        "serpapi", "search", {"params": params, "mode": "complete"}
    )
    outbound = normalize_flight_search(
        payload,
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        adults=adults,
    )
    return outbound


async def _search_round_trip_with_departure_token(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date,
    adults: int,
    preferences: FlightPreferences | None,
) -> FlightSearchResult:
    params = flight_search_params(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        adults=adults,
        preferences=preferences,
    )
    payload = await mcp_client.call_provider_tool(
        "serpapi", "search", {"params": params, "mode": "complete"}
    )
    outbound = normalize_flight_search(
        payload,
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        adults=adults,
    )

    selected_outbound = outbound.results[0]
    if not selected_outbound.departure_token or not selected_outbound.outbound_leg:
        raise InsufficientSerpApiEvidence(
            "SerpApi returned no departure token for round-trip details"
        )
    return_payload = await mcp_client.call_provider_tool(
        "serpapi",
        "search",
        {
            "params": {**params, "departure_token": selected_outbound.departure_token},
            "mode": "complete",
        },
    )
    return_body = _payload_dict(return_payload)
    raw_returns = [
        *return_body.get("best_flights", []),
        *return_body.get("other_flights", []),
    ]
    combinations: list[FlightOption] = []
    for raw in raw_returns:
        if not isinstance(raw, dict):
            continue
        price = _integer(raw.get("price"))
        return_leg = _flight_leg(raw, direction="return")
        if not price or not return_leg:
            continue
        outbound_numbers = [
            segment.flight_number
            for segment in selected_outbound.outbound_leg.segments
            if segment.flight_number
        ]
        return_numbers = [
            segment.flight_number for segment in return_leg.segments if segment.flight_number
        ]
        airlines = list(
            dict.fromkeys(
                [
                    *(segment.airline for segment in selected_outbound.outbound_leg.segments),
                    *(segment.airline for segment in return_leg.segments),
                ]
            )
        )
        combinations.append(
            selected_outbound.model_copy(
                update={
                    "id": f"SF{len(combinations) + 1}",
                    "airline": " + ".join(airlines),
                    "flight_number": " / ".join([*outbound_numbers, *return_numbers]) or None,
                    "price_per_person": price,
                    "total_price": price * adults,
                    "booking_token": raw.get("booking_token"),
                    "return_leg": return_leg,
                    "price_scope": "round_trip",
                    "price_note": (
                        "Giá là tổng khứ hồi; Google Flights không tách giá riêng từng chiều."
                    ),
                }
            )
        )
        if len(combinations) >= settings.travel_search_max_results:
            break
    if not combinations:
        raise InsufficientSerpApiEvidence("SerpApi returned no usable return-flight option")
    return FlightSearchResult(
        source="serpapi",
        results=combinations,
        provider_call_count=2,
        warning=(
            "Các lựa chọn đang ghép với chuyến đi được xếp hạng cao nhất; "
            "giá hiển thị là tổng khứ hồi."
        ),
    )


async def _search_round_trip_as_two_one_ways(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date,
    adults: int,
    preferences: FlightPreferences | None,
    prior_call_count: int,
) -> FlightSearchResult:
    """Recover live evidence when the provider omits round-trip continuation tokens."""
    outbound = await search_flights_live(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=None,
        adults=adults,
        preferences=preferences,
    )
    returning = await search_flights_live(
        origin=destination,
        destination=origin,
        departure_date=return_date,
        return_date=None,
        adults=adults,
        preferences=preferences,
    )
    combinations: list[FlightOption] = []
    for outbound_option in outbound.results:
        if not outbound_option.outbound_leg:
            continue
        for return_option in returning.results:
            if not return_option.outbound_leg:
                continue
            return_leg = return_option.outbound_leg.model_copy(update={"direction": "return"})
            airlines = list(
                dict.fromkeys(
                    [
                        *(segment.airline for segment in outbound_option.outbound_leg.segments),
                        *(segment.airline for segment in return_leg.segments),
                    ]
                )
            )
            numbers = [
                *(segment.flight_number for segment in outbound_option.outbound_leg.segments),
                *(segment.flight_number for segment in return_leg.segments),
            ]
            price_per_person = (
                outbound_option.price_per_person + return_option.price_per_person
            )
            combinations.append(
                outbound_option.model_copy(
                    update={
                        "id": f"SF{len(combinations) + 1}",
                        "airline": " + ".join(airlines),
                        "flight_number": " / ".join(number for number in numbers if number)
                        or None,
                        "return_date": return_date,
                        "price_per_person": price_per_person,
                        "total_price": price_per_person * adults,
                        "return_leg": return_leg,
                        "price_scope": "round_trip",
                        "price_note": (
                            "Giá khứ hồi được cộng từ hai kết quả một chiều do SerpApi "
                            "không trả token ghép chiều về."
                        ),
                    }
                )
            )
            if len(combinations) >= settings.travel_search_max_results:
                return FlightSearchResult(
                    source="serpapi",
                    results=combinations,
                    provider_call_count=(
                        prior_call_count
                        + outbound.provider_call_count
                        + returning.provider_call_count
                    ),
                    warning=(
                        "SerpApi thiếu token khứ hồi; đã ghép hai kết quả một chiều "
                        "thay vì dùng dữ liệu mock."
                    ),
                )
    if not combinations:
        raise InsufficientSerpApiEvidence("SerpApi returned no usable one-way combination")
    return FlightSearchResult(
        source="serpapi",
        results=combinations,
        provider_call_count=(
            prior_call_count + outbound.provider_call_count + returning.provider_call_count
        ),
        warning=(
            "SerpApi thiếu token khứ hồi; đã ghép hai kết quả một chiều thay vì dùng dữ liệu mock."
        ),
    )


def normalize_places(payload: Any) -> list[PlaceOption]:
    if not isinstance(payload, dict):
        return []
    body = payload
    for key in ("result", "data", "response"):
        nested = body.get(key)
        if isinstance(nested, dict) and (
            "local_results" in nested or "place_results" in nested
        ):
            body = nested
            break
    raw_places = body.get("local_results") or []
    if isinstance(body.get("place_results"), dict):
        raw_places = [body["place_results"], *raw_places]
    observed = datetime.now(UTC)
    results: list[PlaceOption] = []
    for raw in raw_places:
        if not isinstance(raw, dict) or not raw.get("title"):
            continue
        name = str(raw["title"])
        address = raw.get("address")
        maps_query = quote_plus(f"{name} {address or ''}")
        maps_url = f"https://www.google.com/maps/search/?api=1&query={maps_query}"
        try:
            rating = float(raw["rating"]) if raw.get("rating") is not None else None
        except (TypeError, ValueError):
            rating = None
        if rating is not None and not 0 <= rating <= 5:
            rating = None
        results.append(
            PlaceOption(
                id=f"SP{len(results) + 1}",
                name=name,
                category=raw.get("type"),
                address=address,
                rating=rating,
                review_count=_integer(raw.get("reviews")),
                price_level=raw.get("price"),
                source="serpapi",
                source_url=maps_url,
                image_url=_safe_url(raw.get("thumbnail") or raw.get("image")),
                observed_at=observed,
            )
        )
        if len(results) >= 8:
            break
    return results


async def search_places_live(*, destination: str) -> PlaceSearchResult:
    payload = await mcp_client.call_provider_tool(
        "serpapi",
        "search",
        {
            "params": {
                "engine": "google_maps",
                "type": "search",
                "q": f"địa điểm tham quan nhà hàng quán ăn nổi tiếng ở {destination}",
                "hl": "vi",
                "gl": "vn",
            },
            "mode": "complete",
        },
    )
    results = normalize_places(payload)
    if not results:
        raise InsufficientSerpApiEvidence("SerpApi Google Maps returned no usable place")
    return PlaceSearchResult(source="serpapi", results=results, provider_call_count=1)


def _booking_offer(
    raw: dict[str, Any], currency: str, direction: str
) -> FlightBookingOffer | None:
    seller = raw.get("book_with")
    if not seller:
        return None
    request = raw.get("booking_request") or {}
    post_data = request.get("post_data") if isinstance(request, dict) else None
    direct_url = request.get("url") if isinstance(request, dict) and not post_data else None
    baggage = raw.get("baggage_prices") or []
    if isinstance(baggage, str):
        baggage = [baggage]
    return FlightBookingOffer(
        seller=str(seller),
        price=_integer(raw.get("price")),
        currency=currency,
        marketed_as=[str(value) for value in raw.get("marketed_as") or []],
        baggage=[str(value) for value in baggage],
        is_airline=bool(raw.get("airline", False)),
        booking_url=_safe_url(direct_url),
        requires_provider_form=bool(post_data),
        direction=direction,
    )


def normalize_booking_options(payload: Any) -> list[FlightBookingOffer]:
    body = _payload_dict(payload)
    offers: list[FlightBookingOffer] = []
    for option in body.get("booking_options", []):
        if not isinstance(option, dict):
            continue
        for key in ("together", "departing", "returning"):
            raw = option.get(key)
            if isinstance(raw, dict):
                direction = {
                    "together": "round_trip",
                    "departing": "outbound",
                    "returning": "return",
                }[key]
                offer = _booking_offer(raw, "VND", direction)
                if offer:
                    offers.append(offer)
    return offers[: settings.travel_search_max_results]


async def flight_booking_options_live(
    *,
    flight: FlightOption,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
) -> FlightBookingOptionsResult:
    params = flight_search_params(
        origin=origin,
        destination=destination,
        departure_date=departure_date,
        return_date=return_date,
        adults=adults,
    )
    booking_token = flight.booking_token
    calls = 0
    warning: str | None = None
    if not booking_token and flight.departure_token:
        return_params = {**params, "departure_token": flight.departure_token}
        returned = await mcp_client.call_provider_tool(
            "serpapi", "search", {"params": return_params, "mode": "complete"}
        )
        calls += 1
        returned_body = _payload_dict(returned)
        candidates = [
            *returned_body.get("best_flights", []),
            *returned_body.get("other_flights", []),
        ]
        booking_token = next(
            (
                candidate.get("booking_token")
                for candidate in candidates
                if isinstance(candidate, dict) and candidate.get("booking_token")
            ),
            None,
        )
        warning = "Demo ghép lựa chọn chiều về được SerpApi xếp đầu tiên trước khi lấy seller."
    if not booking_token:
        raise InsufficientSerpApiEvidence("Selected flight has no booking token")

    booking_params = {**params, "booking_token": booking_token}
    payload = await mcp_client.call_provider_tool(
        "serpapi", "search", {"params": booking_params, "mode": "complete"}
    )
    calls += 1
    offers = normalize_booking_options(payload)
    if not offers:
        raise InsufficientSerpApiEvidence("SerpApi returned no usable booking options")
    safety = "Chỉ hiển thị lựa chọn; giá chưa được giữ và ứng dụng không gửi yêu cầu đặt vé."
    warning = f"{warning} {safety}" if warning else safety
    return FlightBookingOptionsResult(
        source="serpapi",
        warning=warning,
        results=offers,
        provider_call_count=calls,
    )
