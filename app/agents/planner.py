"""Planner Agent: evidence selection, itinerary generation, and deterministic budget."""

from __future__ import annotations

import re
from datetime import timedelta

from app.schemas import (
    FlightOption,
    HotelOption,
    HotelStay,
    ItineraryDay,
    PlaceOption,
    PlanNarrative,
    TripPlan,
    TripRequest,
)
from app.services.budget import calculate_budget
from app.services.llm import draft_narrative


def _require_options(
    flights: list[FlightOption], hotels: list[HotelOption]
) -> tuple[FlightOption, HotelOption]:
    if not flights:
        raise ValueError("Không có lựa chọn chuyến bay khả dụng")
    if not hotels:
        raise ValueError("Không có lựa chọn khách sạn khả dụng")
    return flights[0], hotels[0]


def _hotel_stays(
    request: TripRequest,
    hotels: list[HotelOption],
    instruction: str = "",
) -> list[HotelStay]:
    assert request.departure_date and request.return_date
    nights = max(1, (request.return_date - request.departure_date).days)
    split = bool(
        re.search(
            r"(?:mỗi|moi)\s*(?:ngày|ngay|đêm|dem).*?(?:hotel|khách sạn|khach san)|"
            r"(?:hotel|khách sạn|khach san).*?(?:khác nhau|khac nhau|mỗi đêm|moi dem)",
            instruction,
            re.I,
        )
    )
    if split and len(hotels) > 1:
        return [
            HotelStay(
                check_in_date=request.departure_date + timedelta(days=index),
                check_out_date=request.departure_date + timedelta(days=index + 1),
                hotel=hotels[index % len(hotels)],
                nights=1,
                total_price=hotels[index % len(hotels)].nightly_price,
            )
            for index in range(nights)
        ]
    hotel = hotels[0]
    return [
        HotelStay(
            check_in_date=request.departure_date,
            check_out_date=request.return_date,
            hotel=hotel,
            nights=nights,
            total_price=hotel.total_price,
        )
    ]


def _attach_place_evidence(
    narrative: PlanNarrative, places: list[PlaceOption]
) -> PlanNarrative:
    if not places:
        return narrative
    cursor = 0
    eligible_words = (
        "ăn",
        "ẩm thực",
        "tham quan",
        "khám phá",
        "bảo tàng",
        "chợ",
        "biển",
        "cà phê",
        "cafe",
    )
    transport_words = (
        "bay",
        "chuyến bay",
        "sân bay",
        "di chuyển",
        "nhận phòng",
        "check-in",
        "trả phòng",
        "check-out",
    )
    meal_words = ("ăn", "ẩm thực", "bữa", "hải sản", "nhà hàng", "quán")
    days: list[ItineraryDay] = []
    for day in narrative.itinerary:
        activities = []
        used_meal_places: set[str] = set()
        for activity in day.activities:
            activity_text = f"{activity.title} {activity.description}".casefold()
            if any(word in activity_text for word in transport_words):
                activities.append(
                    activity.model_copy(
                        update={
                            "time_of_day": "flexible",
                            "estimated_cost_per_person": 0,
                            "place_name": None,
                            "address": None,
                            "rating": None,
                            "review_count": None,
                            "maps_url": None,
                            "place_source": None,
                        }
                    )
                )
                continue
            matched_place = next(
                (
                    place
                    for place in places
                    if activity.place_name
                    and place.name.casefold() == activity.place_name.casefold()
                ),
                None,
            )
            eligible = any(word in activity_text for word in eligible_words)
            if matched_place:
                place = matched_place
            elif eligible:
                place = places[cursor % len(places)]
                cursor += 1
            else:
                if activity.place_name:
                    activity = activity.model_copy(
                        update={
                            "place_name": None,
                            "address": None,
                            "rating": None,
                            "review_count": None,
                            "maps_url": None,
                            "place_source": None,
                        }
                    )
                activities.append(activity)
                continue
            place_key = place.name.casefold()
            is_meal = any(word in activity_text for word in meal_words)
            if is_meal and place_key in used_meal_places:
                continue
            if is_meal:
                used_meal_places.add(place_key)
            activities.append(
                activity.model_copy(
                    update={
                        "place_name": place.name,
                        "address": place.address,
                        "rating": place.rating,
                        "review_count": place.review_count,
                        "maps_url": place.source_url,
                        "place_source": place.source,
                        "image_url": place.image_url,
                    }
                )
            )
        days.append(day.model_copy(update={"activities": activities}))
    return narrative.model_copy(update={"itinerary": days})


def build_plan(
    request: TripRequest,
    flights: list[FlightOption],
    hotels: list[HotelOption],
    places: list[PlaceOption] | None = None,
    *,
    current_plan: TripPlan | None = None,
    revision_instruction: str = "",
) -> TripPlan:
    flight, hotel = _require_options(flights, hotels)
    place_options = places or []
    stays = _hotel_stays(request, hotels, revision_instruction)
    narrative = draft_narrative(
        request,
        flight,
        hotel,
        place_options,
        current_plan=current_plan,
        revision_instruction=revision_instruction,
    )
    narrative = _attach_place_evidence(narrative, place_options)
    budget = calculate_budget(request, flight, hotel, narrative.itinerary, stays)
    warnings = list(narrative.warnings)
    if flight.source == "mock" or hotel.source == "mock":
        warnings.append("Một phần dữ liệu giá đang dùng mock; không dùng để đặt chỗ.")
    if budget.is_over_budget:
        warnings.append(
            f"Kế hoạch đang vượt ngân sách {abs(budget.remaining_budget):,} VND."
        )
    return TripPlan(
        selected_flight_id=flight.id,
        selected_hotel_id=hotel.id,
        recommended_flight=flight,
        alternative_flights=flights[1:],
        recommended_hotel=hotel,
        alternative_hotels=hotels[1:],
        hotel_stays=stays,
        places=place_options,
        itinerary=narrative.itinerary,
        budget=budget,
        rationale=narrative.rationale,
        warnings=warnings,
    )


def apply_plan_selections(
    plan: TripPlan,
    request: TripRequest,
    flights: list[FlightOption],
    hotels: list[HotelOption],
    *,
    selected_flight_id: str | None = None,
    hotel_selection_ids: list[str] | None = None,
) -> TripPlan:
    flight = next(
        (item for item in flights if item.id == selected_flight_id),
        plan.recommended_flight,
    )
    stays = plan.hotel_stays
    if hotel_selection_ids and request.departure_date and request.return_date:
        by_id = {item.id: item for item in hotels}
        nights = (request.return_date - request.departure_date).days
        selected_hotels = [by_id[item_id] for item_id in hotel_selection_ids if item_id in by_id]
        if len(selected_hotels) == nights:
            stays = [
                HotelStay(
                    check_in_date=request.departure_date + timedelta(days=index),
                    check_out_date=request.departure_date + timedelta(days=index + 1),
                    hotel=selected_hotel,
                    nights=1,
                    total_price=selected_hotel.nightly_price,
                )
                for index, selected_hotel in enumerate(selected_hotels)
            ]
    recommended_hotel = stays[0].hotel if stays else plan.recommended_hotel
    budget = calculate_budget(request, flight, recommended_hotel, plan.itinerary, stays)
    return plan.model_copy(
        update={
            "selected_flight_id": flight.id,
            "selected_hotel_id": recommended_hotel.id,
            "recommended_flight": flight,
            "recommended_hotel": recommended_hotel,
            "hotel_stays": stays,
            "budget": budget,
        }
    )
