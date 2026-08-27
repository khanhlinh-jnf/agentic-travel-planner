"""Planner Agent: evidence selection, itinerary generation, and deterministic budget."""

from __future__ import annotations

from app.schemas import FlightOption, HotelOption, TripPlan, TripRequest
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


def build_plan(
    request: TripRequest,
    flights: list[FlightOption],
    hotels: list[HotelOption],
    *,
    current_plan: TripPlan | None = None,
    revision_instruction: str = "",
) -> TripPlan:
    flight, hotel = _require_options(flights, hotels)
    narrative = draft_narrative(
        request,
        flight,
        hotel,
        current_plan=current_plan,
        revision_instruction=revision_instruction,
    )
    budget = calculate_budget(request, flight, hotel, narrative.itinerary)
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
        alternative_flights=flights[1:4],
        recommended_hotel=hotel,
        alternative_hotels=hotels[1:4],
        itinerary=narrative.itinerary,
        budget=budget,
        rationale=narrative.rationale,
        warnings=warnings,
    )

