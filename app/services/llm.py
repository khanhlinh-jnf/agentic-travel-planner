"""Cost-aware OpenAI calls with deterministic offline fallbacks."""

from __future__ import annotations

import json
import re
from datetime import date, time, timedelta
from functools import lru_cache
from time import perf_counter

from openai import LengthFinishReasonError, OpenAI

from app.config import settings
from app.prompts import registry
from app.schemas import (
    FlightOption,
    HotelOption,
    ItineraryActivity,
    ItineraryDay,
    PlaceOption,
    PlanNarrative,
    RevisionIntent,
    TripPlan,
    TripRequest,
    TripRequestPatch,
)
from app.services.observability import (
    current_run,
    emit,
    observation,
    record_usage,
    update_span,
)


@lru_cache(maxsize=1)
def _client() -> OpenAI:
    if not settings.primary_api_key:
        raise RuntimeError("OPENAI_API_KEYS chưa được cấu hình")
    return OpenAI(api_key=settings.primary_api_key, max_retries=0, timeout=120)


def live_call_count() -> int:
    run = current_run()
    return run.llm_calls if run else 0


def _record_live_call() -> None:
    run = current_run()
    if run:
        run.llm_calls += 1


def _managed_parse(name, schema, variables, *, token_limit=None):
    prompt = registry().get(name)
    messages = prompt.compile(**variables)
    if prompt.config["response_schema"] != schema.__name__:
        raise ValueError("Prompt and Python response schema mismatch")
    model = prompt.config.get("model") or settings.llm_model
    cap = min(5000, token_limit or int(prompt.config["max_completion_tokens"]))
    metadata = {"prompt": prompt.reference(), "max_completion_tokens": cap}
    if prompt.fallback_reason:
        emit(name, "fallback", kind="prompt", detail=prompt.fallback_reason)
    _record_live_call()
    started = perf_counter()
    with observation(
        name, "generation", model=model, metadata=metadata,
        prompt=prompt.remote,
        input=messages if settings.telemetry_capture_content else {"captured": False},
    ) as span:
        try:
            response = _client().chat.completions.parse(
                model=model, messages=messages, response_format=schema,
                max_completion_tokens=cap,
            )
        except Exception as exc:
            record = record_usage(
                getattr(exc, "completion", None), model=model, prompt=prompt,
                error=type(exc).__name__,
            )
            _update_generation(span, record)
            record["duration_ms"] = round((perf_counter() - started) * 1000)
            update_span(span, level="ERROR", status_message=type(exc).__name__)
            raise
        record = record_usage(response, model=model, prompt=prompt)
        record["duration_ms"] = round((perf_counter() - started) * 1000)
        _update_generation(span, record)
        parsed = response.choices[0].message.parsed
        if settings.telemetry_capture_content and parsed is not None:
            update_span(span, output=parsed.model_dump(mode="json"))
        return parsed


def _update_generation(span, record):
    if not record["usage_available"]:
        return
    update = {"usage_details": {
        "input": record["input_tokens"] - record["cached_input_tokens"],
        "cached_input": record["cached_input_tokens"],
        "output": record["output_tokens"],
    }}
    if record["cost_usd"] is not None:
        update["cost_details"] = {"total": record["cost_usd"]}
    update_span(span, **update)


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


def heuristic_trip_patch(
    text: str,
    current: TripRequest | None = None,
) -> TripRequestPatch:
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
    return_date_match = re.search(
        r"(?:ngày về|ve ngay|về ngày|kết thúc|ket thuc|trả phòng|tra phong)",
        text,
        re.I,
    )
    if len(iso_dates) == 1 and current and current.departure_date and return_date_match:
        departure_date = None
        return_date = iso_dates[0]
    else:
        departure_date = iso_dates[0] if iso_dates else None
        return_date = iso_dates[1] if len(iso_dates) > 1 else None
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
            "departure_date": departure_date,
            "return_date": return_date,
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
        return heuristic_trip_patch(text, current)

    today = date.today().isoformat()
    current_json = current.model_dump_json() if current else "{}"
    parsed = _managed_parse(
        "trip_intake", TripRequestPatch,
        {"today": today, "current_request": current_json, "user_message": text},
    )
    return parsed or heuristic_trip_patch(text, current)


def _fallback_narrative(
    request: TripRequest,
    places: list[PlaceOption] | None = None,
    revision_instruction: str = "",
) -> PlanNarrative:
    assert request.departure_date and request.duration_days and request.destination
    interests = request.interests or ["điểm nổi bật", "ẩm thực địa phương"]
    days = []
    place_options = places or []

    def activity_place(index: int) -> dict:
        if not place_options:
            return {}
        place = place_options[index % len(place_options)]
        return {
            "place_name": place.name,
            "address": place.address,
            "rating": place.rating,
            "review_count": place.review_count,
            "maps_url": place.source_url,
            "place_source": place.source,
        }
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
                    title=(place_options[0].name if place_options else "Ẩm thực địa phương"),
                    description="Dùng bữa tại địa điểm cụ thể; kiểm tra giờ mở cửa trước khi đi.",
                    estimated_cost_per_person=180_000,
                    needs_verification=True,
                    **activity_place(0),
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
                    title=(
                        place_options[index % len(place_options)].name
                        if place_options
                        else f"Khám phá {interest}"
                    ),
                    description=(
                        f"Trải nghiệm {interest} tại một địa điểm cụ thể "
                        f"ở {request.destination}."
                    ),
                    estimated_cost_per_person=150_000,
                    needs_verification=True,
                    **activity_place(index),
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
    places: list[PlaceOption] | None = None,
    *,
    current_plan: TripPlan | None = None,
    revision_instruction: str = "",
) -> PlanNarrative:
    if settings.use_mock_llm or not settings.primary_api_key:
        return _fallback_narrative(request, places, revision_instruction)

    request_evidence = request.model_dump(mode="json", exclude_none=True)
    flight_evidence = {
        "outbound_leg": (
            flight.outbound_leg.model_dump(mode="json") if flight.outbound_leg else None
        ),
        "return_leg": flight.return_leg.model_dump(mode="json") if flight.return_leg else None,
    }
    hotel_evidence = {
        "name": hotel.name,
        "area": hotel.area,
        "rating": hotel.rating,
        "source": hotel.source,
    }
    place_evidence = [
        place.model_dump(
            mode="json",
            include={
                "name",
                "category",
                "address",
                "rating",
                "review_count",
                "source",
                "source_url",
            },
        )
        for place in (places or [])[:6]
    ]
    current = (
        current_plan.model_dump(
            mode="json",
            include={"itinerary", "rationale"},
        )
        if current_plan
        else None
    )
    token_limit = min(
        5_000,
        max(settings.llm_max_completion_tokens, 1_200 + (request.duration_days or 1) * 450),
    )
    try:
        parsed = _managed_parse(
            "itinerary_planner", PlanNarrative,
            {
                "trip_request": json.dumps(request_evidence, ensure_ascii=False),
                "flight": json.dumps(flight_evidence, ensure_ascii=False),
                "hotel": json.dumps(hotel_evidence, ensure_ascii=False),
                "places": json.dumps(place_evidence, ensure_ascii=False),
                "current_plan": json.dumps(current, ensure_ascii=False),
                "revision_instruction": revision_instruction or "none",
                "duration_days": request.duration_days,
            },
            token_limit=token_limit,
        )
    except LengthFinishReasonError:
        emit("planner_agent", "fallback", detail="LengthFinishReasonError")
        fallback = _fallback_narrative(request, places, revision_instruction)
        fallback.warnings.append(
            "OpenAI trả lời vượt giới hạn độ dài; Planner đã dùng lịch trình fallback an toàn."
        )
        return fallback
    return parsed or _fallback_narrative(
        request, places, revision_instruction
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
    parsed = _managed_parse(
        "revision_router", RevisionIntent,
        {"current_request": current.model_dump_json(), "user_message": text},
    )
    return parsed or heuristic_revision(text)
