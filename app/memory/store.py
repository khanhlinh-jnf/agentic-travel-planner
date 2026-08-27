"""Small JSON preference store for the local single-user demo."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.config import settings
from app.schemas import TripRequest, UserPreferences

_LOCK = threading.Lock()


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_preferences(user_id: str, *, path: Path | None = None) -> UserPreferences:
    memory_path = path or settings.memory_file
    with _LOCK:
        data = _read(memory_path)
    return UserPreferences.model_validate(data.get(user_id, {}))


def save_preferences(
    user_id: str, preferences: UserPreferences, *, path: Path | None = None
) -> None:
    memory_path = path or settings.memory_file
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        data = _read(memory_path)
        data[user_id] = preferences.model_dump(mode="json")
        temporary = memory_path.with_suffix(memory_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(memory_path)


def preferences_from_request(request: TripRequest) -> UserPreferences:
    departure_after = request.flight_preferences.departure_after
    return UserPreferences(
        avoid_early_flights=bool(departure_after),
        preferred_departure_after=departure_after,
        travel_pace=request.travel_pace,
        preferred_hotel_area=request.hotel_preferences.preferred_area,
        preferred_min_stars=request.hotel_preferences.min_stars,
    )


def apply_preferences(request: TripRequest, preferences: UserPreferences) -> TripRequest:
    values = request.model_dump()
    flight = request.flight_preferences.model_dump()
    hotel = request.hotel_preferences.model_dump()

    if flight["departure_after"] is None and preferences.preferred_departure_after:
        flight["departure_after"] = preferences.preferred_departure_after
    if hotel["preferred_area"] is None and preferences.preferred_hotel_area:
        hotel["preferred_area"] = preferences.preferred_hotel_area
    if hotel["min_stars"] is None and preferences.preferred_min_stars:
        hotel["min_stars"] = preferences.preferred_min_stars
    if values["travel_pace"] is None and preferences.travel_pace:
        values["travel_pace"] = preferences.travel_pace

    values["flight_preferences"] = flight
    values["hotel_preferences"] = hotel
    return TripRequest.model_validate(values)

