"""Deterministic local flight/hotel provider used by tests and fallback."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from functools import lru_cache

from app.config import settings
from app.schemas import FlightOption, FlightSearchResult, HotelOption, HotelSearchResult

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
        results.append(
            FlightOption(
                id=f"MF{index}",
                airline=item["airline"],
                flight_number=item.get("flight_number"),
                origin=origin,
                destination=destination,
                departure_date=departure_date,
                return_date=return_date,
                departure_time=time.fromisoformat(item["departure_time"]),
                arrival_time=(
                    time.fromisoformat(item["arrival_time"])
                    if item.get("arrival_time")
                    else None
                ),
                duration_minutes=item.get("duration_minutes"),
                stops=item.get("stops"),
                price_per_person=price,
                total_price=price * adults,
                source="mock",
                observed_at=observed,
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
                observed_at=observed,
            )
        )
    return HotelSearchResult(
        source="mock",
        warning="Đang dùng dữ liệu khách sạn demo, không phải availability thực tế.",
        results=results,
    )

