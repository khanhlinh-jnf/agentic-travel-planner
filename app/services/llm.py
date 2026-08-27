"""Cost-aware OpenAI calls with deterministic offline fallbacks."""

from __future__ import annotations

import re
from datetime import date, time, timedelta
from functools import lru_cache

from openai import OpenAI

from app.config import settings
from app.schemas import (
    FlightOption,
    HotelOption,
    ItineraryActivity,
    ItineraryDay,
    PlanNarrative,
    RevisionIntent,
    TripPlan,
    TripRequest,
    TripRequestPatch,
)

_LIVE_CALLS = 0


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    if not settings.primary_api_key:
        raise RuntimeError("OPENAI_API_KEYS chưa được cấu hình")
    return OpenAI(api_key=settings.primary_api_key)


def live_call_count() -> int:
    return _LIVE_CALLS


def _record_live_call() -> None:
    global _LIVE_CALLS
    _LIVE_CALLS += 1


def _extract_budget(text: str) -> int | None:
    match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:triệu|trieu|tr)\b", text, re.I)
    if match:
        return round(float(match.group(1).replace(",", ".")) * 1_000_000)
    match = re.search(r"(?:budget|ngân sách)\D{0,20}([\d.]{6,})", text, re.I)
    if match:
        return int(match.group(1).replace(".", ""))
    return None


def _extract_city(text: str, choices: list[str]) -> str | None:
    lowered = text.lower()
    return next((choice for choice in choices if choice.lower() in lowered), None)


def heuristic_trip_patch(text: str) -> TripRequestPatch:
    cities = ["TP.HCM", "Hồ Chí Minh", "Đà Nẵng", "Phú Quốc", "Tokyo", "Hà Nội"]
    # Do not treat the dot in "TP.HCM" as a sentence delimiter.
    origin_match = re.search(r"\btừ\s+([^,;]+)", text, re.I)
    origin = _extract_city(origin_match.group(1), cities) if origin_match else None

    destination = None
    for city in cities:
        if city.lower() in text.lower() and (not origin or city.lower() != origin.lower()):
            destination = city
            if city.lower() not in ("tp.hcm", "hồ chí minh"):
                break

    iso_dates = [date.fromisoformat(value) for value in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)]
    duration_match = re.search(r"\b(\d+)\s*(?:ngày|ngay|n)(?:\d+đ)?\b", text, re.I)
    travelers_match = re.search(r"\b(\d+)\s*(?:người|nguoi|khách|khach)\b", text, re.I)
    after_match = re.search(r"(?:sau|không.*trước)\s*(\d{1,2})(?:[:h](\d{2}))?", text, re.I)
    nightly_match = re.search(
        r"(?:dưới|tối đa|không quá)\s*(\d+(?:[.,]\d+)?)\s*(?:triệu|trieu|tr)\s*/?\s*(?:đêm|dem)",
        text,
        re.I,
    )

    interests = []
    interest_words = {
        "biển": "biển",
        "ăn uống": "ẩm thực",
        "đồ ăn": "ẩm thực",
        "mua sắm": "mua sắm",
        "văn hóa": "văn hóa",
        "phố đi bộ": "đi bộ",
    }
    lowered = text.lower()
    for needle, value in interest_words.items():
        if needle in lowered and value not in interests:
            interests.append(value)

    flight_preferences = None
    if after_match:
        flight_preferences = {
            "departure_after": time(
                hour=min(23, int(after_match.group(1))),
                minute=int(after_match.group(2) or 0),
            )
        }
    hotel_preferences = None
    if nightly_match:
        hotel_preferences = {
            "max_nightly_price": round(
                float(nightly_match.group(1).replace(",", ".")) * 1_000_000
            )
        }

    pace = None
    if any(word in lowered for word in ("chill", "thư giãn", "nhẹ nhàng")):
        pace = "relaxed"
    elif any(word in lowered for word in ("dày", "nhiều điểm", "tận dụng")):
        pace = "packed"

    return TripRequestPatch.model_validate(
        {
            "origin": origin,
            "destination": destination,
            "departure_date": iso_dates[0] if iso_dates else None,
            "return_date": iso_dates[1] if len(iso_dates) > 1 else None,
            "duration_days": int(duration_match.group(1)) if duration_match else None,
            "travelers": int(travelers_match.group(1)) if travelers_match else None,
            "total_budget": _extract_budget(text),
            "interests": interests or None,
            "travel_pace": pace,
            "flight_preferences": flight_preferences,
            "hotel_preferences": hotel_preferences,
            "free_text_constraints": [text] if text.strip() else None,
            "date_is_ambiguous": bool(re.search(r"\b\d{1,2}/\d{1,2}\b", text)),
        }
    )


def parse_trip_request(text: str, current: TripRequest | None = None) -> TripRequestPatch:
    if settings.use_mock_llm or not settings.primary_api_key:
        return heuristic_trip_patch(text)

    today = date.today().isoformat()
    current_json = current.model_dump_json() if current else "{}"
    messages = [
        {
            "role": "system",
            "content": (
                "Extract only explicitly stated travel constraints into the schema. "
                "Do not guess missing critical fields. Currency is VND. "
                "Dates must be ISO; set date_is_ambiguous=true when a date lacks a year. "
                f"Today is {today}. Existing request values: {current_json}"
            ),
        },
        {"role": "user", "content": text},
    ]
    _record_live_call()
    result = _client().chat.completions.parse(
        model=settings.llm_model,
        messages=messages,
        response_format=TripRequestPatch,
        max_completion_tokens=900,
    )
    parsed = result.choices[0].message.parsed
    return parsed or heuristic_trip_patch(text)


def _fallback_narrative(request: TripRequest, revision_instruction: str = "") -> PlanNarrative:
    assert request.departure_date and request.duration_days and request.destination
    interests = request.interests or ["điểm nổi bật", "ẩm thực địa phương"]
    days = []
    for index in range(request.duration_days):
        current_date = request.departure_date + timedelta(days=index)
        if index == 0:
            activities = [
                ItineraryActivity(
                    time_of_day="afternoon",
                    title="Nhận phòng và làm quen khu vực",
                    description=f"Di chuyển nhẹ quanh khu lưu trú tại {request.destination}.",
                ),
                ItineraryActivity(
                    time_of_day="evening",
                    title="Ẩm thực địa phương",
                    description="Khám phá món ăn nổi bật; kiểm tra giờ mở cửa trước khi đi.",
                    estimated_cost_per_person=180_000,
                    needs_verification=True,
                ),
            ]
        elif index == request.duration_days - 1:
            activities = [
                ItineraryActivity(
                    time_of_day="morning",
                    title="Buổi sáng tự do",
                    description="Mua quà hoặc thư giãn trước giờ trả phòng.",
                )
            ]
        else:
            interest = interests[(index - 1) % len(interests)]
            activities = [
                ItineraryActivity(
                    time_of_day="morning",
                    title=f"Khám phá {interest}",
                    description=f"Gợi ý trải nghiệm {interest} nổi bật tại {request.destination}.",
                    estimated_cost_per_person=150_000,
                    needs_verification=True,
                ),
                ItineraryActivity(
                    time_of_day="afternoon",
                    title="Khám phá khu trung tâm",
                    description="Đi bộ, nghỉ ngơi và linh hoạt theo thời tiết thực tế.",
                ),
            ]
        days.append(
            ItineraryDay(
                day=index + 1,
                date=current_date,
                title=f"Ngày {index + 1}: {request.destination}",
                activities=activities,
            )
        )
    rationale = "Ưu tiên lịch trình cân bằng, ngân sách rõ ràng và thời gian nghỉ hợp lý."
    if revision_instruction:
        rationale += f" Đã áp dụng yêu cầu sửa: {revision_instruction}"
    return PlanNarrative(
        itinerary=days,
        rationale=rationale,
        warnings=["Lịch trình offline dùng kiến thức chung; hãy xác minh thông tin realtime."],
    )


def draft_narrative(
    request: TripRequest,
    flight: FlightOption,
    hotel: HotelOption,
    *,
    current_plan: TripPlan | None = None,
    revision_instruction: str = "",
) -> PlanNarrative:
    if settings.use_mock_llm or not settings.primary_api_key:
        return _fallback_narrative(request, revision_instruction)

    current = current_plan.model_dump_json() if current_plan else "null"
    prompt = f"""Draft a Vietnamese day-by-day itinerary for this request:
{request.model_dump_json()}

Selected flight: {flight.model_dump_json()}
Selected hotel: {hotel.model_dump_json()}
Existing plan for selective revision: {current}
Revision instruction: {revision_instruction or 'none'}

Use general destination knowledge only. Never claim exact current opening hours,
temporary closures, live ticket prices, or availability. Mark time-sensitive activities
needs_verification=true. Keep estimated activity costs conservative VND integers.
Return exactly {request.duration_days} itinerary days."""
    _record_live_call()
    result = _client().chat.completions.parse(
        model=settings.llm_model,
        messages=[
            {"role": "system", "content": "You are a careful Vietnamese travel planner."},
            {"role": "user", "content": prompt},
        ],
        response_format=PlanNarrative,
        max_completion_tokens=settings.llm_max_completion_tokens,
    )
    return result.choices[0].message.parsed or _fallback_narrative(
        request, revision_instruction
    )


def heuristic_revision(text: str) -> RevisionIntent:
    lowered = text.lower()
    domains = []
    preserve_flight = bool(re.search(r"giữ[^,.;]*(?:flight|chuyến bay)", lowered))
    preserve_hotel = bool(re.search(r"giữ[^,.;]*(?:hotel|khách sạn)", lowered))
    if any(word in lowered for word in ("flight", "chuyến bay", "bay sau", "bay trước")):
        domains.append("flight")
    if any(word in lowered for word in ("hotel", "khách sạn", "resort", "/đêm")):
        domains.append("hotel")
    if any(word in lowered for word in ("ngày", "lịch", "chill", "điểm đến")):
        domains.append("itinerary")
    if any(word in lowered for word in ("budget", "ngân sách", "triệu")):
        domains.append("budget")
    if not domains:
        domains = ["itinerary"]
    patch = heuristic_trip_patch(text)
    return RevisionIntent(
        affected_domains=list(dict.fromkeys(domains)),
        patch=patch,
        preserve_flight=preserve_flight,
        preserve_hotel=preserve_hotel,
        itinerary_instruction=text if "itinerary" in domains else None,
    )


def parse_revision(text: str, current: TripRequest) -> RevisionIntent:
    if settings.use_mock_llm or not settings.primary_api_key:
        return heuristic_revision(text)
    _record_live_call()
    result = _client().chat.completions.parse(
        model=settings.llm_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "Classify a travel-plan revision. Patch only explicitly changed fields. "
                    "Set preserve_flight/preserve_hotel from explicit keep instructions. "
                    f"Current request: {current.model_dump_json()}"
                ),
            },
            {"role": "user", "content": text},
        ],
        response_format=RevisionIntent,
        max_completion_tokens=900,
    )
    return result.choices[0].message.parsed or heuristic_revision(text)
