"""Best-effort live travel evidence via OpenAI Responses Web Search."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import urlparse

from openai import OpenAI

from app.config import settings
from app.schemas import FlightOption, FlightSearchResult, HotelOption, HotelSearchResult


class InsufficientWebEvidence(RuntimeError):
    """Raised when Web Search did not yield enough attributable options."""


def _client() -> OpenAI:
    if not settings.primary_api_key:
        raise InsufficientWebEvidence("OPENAI_API_KEYS chưa được cấu hình")
    return OpenAI(
        api_key=settings.primary_api_key,
        timeout=settings.web_search_timeout_seconds,
    )


def _response_payload(prompt: str) -> tuple[dict[str, Any], str]:
    response = _client().responses.create(
        model=settings.web_search_model,
        tools=[{"type": "web_search"}],
        tool_choice="required",
        include=["web_search_call.action.sources"],
        max_tool_calls=1,
        max_output_tokens=1400,
        input=prompt,
    )
    return response.model_dump(mode="json"), response.output_text


def _collect_urls(value: Any) -> set[str]:
    urls: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "url" and isinstance(child, str) and child.startswith("http"):
                urls.add(child)
            else:
                urls.update(_collect_urls(child))
    elif isinstance(value, list):
        for child in value:
            urls.update(_collect_urls(child))
    return urls


def _has_web_search_call(payload: dict[str, Any]) -> bool:
    return any(item.get("type") == "web_search_call" for item in payload.get("output", []))


def _parse_json_array(text: str) -> list[dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.S)
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        raise InsufficientWebEvidence("Web Search không trả về JSON array")
    parsed = json.loads(cleaned[start : end + 1])
    if not isinstance(parsed, list):
        raise InsufficientWebEvidence("Kết quả Web Search sai định dạng")
    return [item for item in parsed if isinstance(item, dict)]


def _valid_source_url(item: dict[str, Any], allowed_urls: set[str]) -> str | None:
    url = item.get("source_url")
    if not isinstance(url, str):
        return None
    candidate_host = urlparse(url).netloc.lower().removeprefix("www.")
    matches_known_source = any(
        candidate_host
        and candidate_host == urlparse(allowed).netloc.lower().removeprefix("www.")
        for allowed in allowed_urls
    )
    return url if matches_known_source else None


def _vnd_integer(value: Any) -> int | None:
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return round(value) if value > 0 else None
    if not isinstance(value, str):
        return None
    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits and int(digits) > 0 else None


def search_flights_web(
    *,
    origin: str,
    destination: str,
    departure_date: date,
    return_date: date | None,
    adults: int,
) -> FlightSearchResult:
    prompt = f"""Search the public web for current-ish round-trip flight price evidence.
Route: {origin} to {destination}. Departure: {departure_date.isoformat()}.
Return: {return_date.isoformat() if return_date else 'one-way'}. Adults: {adults}.
Prefer airline or reputable travel search pages displaying VND prices.

Return ONLY a JSON array with up to {settings.web_search_max_results} objects:
airline, flight_number (nullable), departure_time HH:MM (nullable),
arrival_time HH:MM (nullable), duration_minutes (nullable), stops (nullable),
price_per_person (integer VND), source_url. Do not invent missing fields or URLs.
Prices are estimates, not guaranteed availability."""
    payload, text = _response_payload(prompt)
    if not _has_web_search_call(payload):
        raise InsufficientWebEvidence("Response không chứa Web Search call")
    urls = _collect_urls(payload)
    raw = _parse_json_array(text)
    observed = datetime.now(UTC)
    results: list[FlightOption] = []
    for item in raw:
        source_url = _valid_source_url(item, urls)
        price = _vnd_integer(item.get("price_per_person"))
        if not source_url or price is None:
            continue
        try:
            results.append(
                FlightOption(
                    id=f"WF{len(results) + 1}",
                    airline=str(item.get("airline") or "Unknown airline"),
                    flight_number=item.get("flight_number"),
                    origin=origin,
                    destination=destination,
                    departure_date=departure_date,
                    return_date=return_date,
                    departure_time=(
                        time.fromisoformat(item["departure_time"])
                        if item.get("departure_time")
                        else None
                    ),
                    arrival_time=(
                        time.fromisoformat(item["arrival_time"])
                        if item.get("arrival_time")
                        else None
                    ),
                    duration_minutes=item.get("duration_minutes"),
                    stops=item.get("stops"),
                    price_per_person=price,
                    total_price=price * adults,
                    source="web",
                    source_url=source_url,
                    observed_at=observed,
                )
            )
        except (TypeError, ValueError):
            continue
    if len(results) < 2:
        raise InsufficientWebEvidence("Không đủ ít nhất 2 lựa chọn vé có giá và nguồn")
    return FlightSearchResult(source="web", results=results, provider_call_count=1)


def search_hotels_web(
    *,
    destination: str,
    check_in_date: date,
    check_out_date: date,
    adults: int,
    max_price: int | None,
) -> HotelSearchResult:
    nights = max(1, (check_out_date - check_in_date).days)
    prompt = f"""Search the public web for current-ish hotel price evidence.
Destination: {destination}. Check-in: {check_in_date.isoformat()}.
Check-out: {check_out_date.isoformat()}. Adults: {adults}. Nights: {nights}.
Maximum nightly VND price: {max_price or 'not specified'}.
Prefer hotel or reputable travel search pages displaying VND prices.

Return ONLY a JSON array with up to {settings.web_search_max_results} objects:
name, area (nullable), rating 0-5 (nullable), stars 1-5 (nullable),
nightly_price (integer VND), amenities (array), source_url.
Do not invent missing fields or URLs. Prices are estimates, not guaranteed availability."""
    payload, text = _response_payload(prompt)
    if not _has_web_search_call(payload):
        raise InsufficientWebEvidence("Response không chứa Web Search call")
    urls = _collect_urls(payload)
    raw = _parse_json_array(text)
    observed = datetime.now(UTC)
    results: list[HotelOption] = []
    for item in raw:
        source_url = _valid_source_url(item, urls)
        nightly = _vnd_integer(item.get("nightly_price"))
        if not source_url or nightly is None:
            continue
        if max_price and nightly > max_price:
            continue
        try:
            results.append(
                HotelOption(
                    id=f"WH{len(results) + 1}",
                    name=str(item.get("name") or "Unknown hotel"),
                    destination=destination,
                    area=item.get("area"),
                    rating=item.get("rating"),
                    stars=item.get("stars"),
                    nightly_price=nightly,
                    nights=nights,
                    total_price=nightly * nights,
                    amenities=item.get("amenities") or [],
                    source="web",
                    source_url=source_url,
                    observed_at=observed,
                )
            )
        except (TypeError, ValueError):
            continue
    if len(results) < 2:
        raise InsufficientWebEvidence("Không đủ ít nhất 2 khách sạn có giá và nguồn")
    return HotelSearchResult(source="web", results=results, provider_call_count=1)
