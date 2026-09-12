"""Deterministic local flight/hotel provider used by tests and fallback."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

from app.config import settings
from app.schemas import (
    FlightBookingOffer,
    FlightBookingOptionsResult,
    FlightLeg,
    FlightOption,
    FlightSearchResult,
    FlightSegment,
    HotelDetailResult,
    HotelOption,
    HotelSearchResult,
    PlaceOption,
    PlaceSearchResult,
)

_CITY_CODES = {
    "tp.hcm": "SGN",
    "tp hcm": "SGN",
    "hồ chí minh": "SGN",
    "ho chi minh": "SGN",
    "sài gòn": "SGN",
    "saigon": "SGN",
    "sgn": "SGN",
    "đà nẵng": "DAD",
    "da nang": "DAD",
    "dad": "DAD",
    "phú quốc": "PQC",
    "phu quoc": "PQC",
    "pqc": "PQC",
    "tokyo": "NRT",
    "narita": "NRT",
    "nrt": "NRT",
}


def city_code(value: str) -> str:
    normalized = " ".join(value.lower().strip().split())
    return _CITY_CODES.get(normalized, value.strip().upper()[:3])


@lru_cache(maxsize=1)
def _flight_templates() -> list[dict]:
    path = settings.project_root / "data" / "mock_flights.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _hotel_templates() -> list[dict]:
    path = settings.project_root / "data" / "mock_hotels.json"
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _place_templates() -> list[dict]:
    path = settings.project_root / "data" / "mock_places.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _mock_leg(
    *,
    direction: str,
    airline: str,
    flight_number: str | None,
    origin: str,
    destination: str,
    travel_date: date,
    departure_time: time,
    arrival_time: time | None,
    duration_minutes: int | None,
    price_per_person: int,
    adults: int,
) -> FlightLeg:
    departure_at = datetime.combine(travel_date, departure_time)
    if arrival_time is not None:
        arrival_at = datetime.combine(travel_date, arrival_time)
        if arrival_at < departure_at:
            arrival_at += timedelta(days=1)
    else:
        arrival_at = departure_at + timedelta(minutes=duration_minutes or 120)
    segment = FlightSegment(
        airline=airline,
        flight_number=flight_number,
        origin_airport=city_code(origin),
        destination_airport=city_code(destination),
        departure_at=departure_at,
        arrival_at=arrival_at,
        duration_minutes=duration_minutes,
    )
    return FlightLeg(
        direction=direction,
        segments=[segment],
        duration_minutes=duration_minutes,
        stops=0,
        price_per_person=price_per_person,
        total_price=price_per_person * adults,
    )


def search_flights_mock(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
) -> FlightSearchResult:
    origin_code = city_code(origin)
    destination_code = city_code(destination)
    route = f"{origin_code}-{destination_code}"
    templates = [item for item in _flight_templates() if item["route"] == route]
    if not templates:
        templates = [item for item in _flight_templates() if item["route"] == "ANY"]

    observed = datetime.now(UTC)
    results = []
    for index, item in enumerate(templates[:4], start=1):
        price = int(item["price_per_person"])
        outbound_price = price if return_date is None else price // 2
        return_price = price - outbound_price
        departure_clock = time.fromisoformat(item["departure_time"])
        arrival_clock = (
            time.fromisoformat(item["arrival_time"])
            if item.get("arrival_time")
            else None
        )
        outbound_leg = _mock_leg(
            direction="outbound",
            airline=item["airline"],
            flight_number=item.get("flight_number"),
            origin=origin,
            destination=destination,
            travel_date=departure_date,
            departure_time=departure_clock,
            arrival_time=arrival_clock,
            duration_minutes=item.get("duration_minutes"),
            price_per_person=outbound_price,
            adults=adults,
        )
        return_leg = None
        if return_date:
            return_leg = _mock_leg(
                direction="return",
                airline=item["airline"],
                flight_number=(f"{item['flight_number']}-R" if item.get("flight_number") else None),
                origin=destination,
                destination=origin,
                travel_date=return_date,
                departure_time=time(hour=17, minute=(index - 1) * 20),
                arrival_time=None,
                duration_minutes=item.get("duration_minutes"),
                price_per_person=return_price,
                adults=adults,
            )
        results.append(
            FlightOption(
                id=f"MF{index}",
                airline=item["airline"],
                flight_number=item.get("flight_number"),
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                departure_time=departure_clock,
                arrival_time=arrival_clock,
                duration_minutes=item.get("duration_minutes"),
                stops=item.get("stops"),
                price_per_person=price,
                total_price=price * adults,
                source="mock",
                observed_at=observed,
                outbound_leg=outbound_leg,
                return_leg=return_leg,
                price_scope="round_trip" if return_date else "one_way",
                price_note="Giá demo được tách theo từng chiều." if return_date else None,
            )
        )
    return FlightSearchResult(
        source="mock",
        warning="Đang dùng dữ liệu vé máy bay demo, không phải giá đặt chỗ thực tế.",
        results=results,
    )


def search_hotels_mock(
    *,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
    max_price: int | None,
) -> HotelSearchResult:
    del adults
    destination_code = city_code(destination)
    templates = [
        item for item in _hotel_templates() if item["destination"] == destination_code
    ]
    if not templates:
        templates = [item for item in _hotel_templates() if item["destination"] == "ANY"]

    if max_price is not None:
        within_budget = [item for item in templates if item["nightly_price"] <= max_price]
        if within_budget:
            templates = within_budget

    nights = max(1, (check_out_date - check_in_date).days)
    observed = datetime.now(UTC)
    results = []
    for index, item in enumerate(templates[:4], start=1):
        nightly = int(item["nightly_price"])
        results.append(
            HotelOption(
                id=f"MH{index}",
                name=item["name"],
                destination=destination,
                area=item.get("area"),
                rating=item.get("rating"),
                stars=item.get("stars"),
                nightly_price=nightly,
                nights=nights,
                total_price=nightly * nights,
                amenities=item.get("amenities", []),
                source="mock",
                image_url=item.get("image_url"),
                observed_at=observed,
            )
        )
    return HotelSearchResult(
        source="mock",
        warning="Đang dùng dữ liệu khách sạn demo, không phải availability thực tế.",
        results=results,
    )


def search_places_mock(*, destination: str) -> PlaceSearchResult:
    destination_code = city_code(destination)
    templates = [
        item for item in _place_templates() if item["destination"] == destination_code
    ]
    if not templates:
        templates = [item for item in _place_templates() if item["destination"] == "ANY"]
    observed = datetime.now(UTC)
    results = [
        PlaceOption(
            id=f"MP{index}",
            name=item["name"],
            category=item.get("category"),
            address=item.get("address"),
            rating=item.get("rating"),
            review_count=item.get("review_count"),
            price_level=item.get("price_level"),
            source="mock",
            source_url=item.get("source_url"),
            image_url=item.get("image_url"),
            observed_at=observed,
        )
        for index, item in enumerate(templates[:8], start=1)
    ]
    return PlaceSearchResult(
        source="mock",
        warning="Địa điểm và rating đang dùng dữ liệu demo; hãy kiểm tra lại trước khi đi.",
        results=results,
    )


def flight_booking_options_mock(flight_id: str) -> FlightBookingOptionsResult:
    return FlightBookingOptionsResult(
        source="mock",
        warning="Đây là seller option mô phỏng; không phải giá hay link đặt vé thực tế.",
        results=[
            FlightBookingOffer(
                seller=f"Demo seller for {flight_id}",
                price=None,
                marketed_as=[flight_id],
                baggage=["Kiểm tra hành lý trực tiếp với hãng"],
            )
        ],
    )


def find_hotel_by_name_mock(
    *,
    hotel_name: str,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
) -> HotelDetailResult:
    search = search_hotels_mock(
        destination=destination,
        check_in_date=check_in_date,
        check_out_date=check_out_date,
        adults=adults,
        max_price=None,
    )
    exact = next(
        (hotel for hotel in search.results if hotel.name.casefold() == hotel_name.casefold()),
        None,
    )
    hotel = exact or (search.results[0] if search.results else None)
    return HotelDetailResult(
        source="mock",
        warning="Đây là chi tiết khách sạn mô phỏng; không phải availability thực tế.",
        hotel=hotel,
    )
