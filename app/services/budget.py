"""Deterministic VND budget calculation."""

from __future__ import annotations

from app.schemas import BudgetSummary, FlightOption, HotelOption, ItineraryDay, TripRequest

FOOD_PER_PERSON_PER_DAY = 350_000
LOCAL_TRANSPORT_PER_DAY = 250_000
BUFFER_RATIO = 0.05


def calculate_budget(
    request: TripRequest,
    flight: FlightOption,
    hotel: HotelOption,
    itinerary: list[ItineraryDay],
) -> BudgetSummary:
    if request.travelers is None or request.total_budget is None:
        raise ValueError("travelers and total_budget are required")

    trip_days = request.duration_days or len(itinerary)
    if trip_days < 1:
        raise ValueError("trip_days must be positive")

    activities = sum(
        activity.estimated_cost_per_person * request.travelers
        for day in itinerary
        for activity in day.activities
    )
    food = FOOD_PER_PERSON_PER_DAY * request.travelers * trip_days
    local_transport = LOCAL_TRANSPORT_PER_DAY * trip_days
    subtotal = flight.total_price + hotel.total_price + activities + food + local_transport
    buffer = round(subtotal * BUFFER_RATIO)
    estimated_total = subtotal + buffer
    remaining = request.total_budget - estimated_total

    return BudgetSummary(
        flight=flight.total_price,
        hotel=hotel.total_price,
        activities=activities,
        food=food,
        local_transport=local_transport,
        buffer=buffer,
        estimated_total=estimated_total,
        total_budget=request.total_budget,
        remaining_budget=remaining,
        is_over_budget=remaining < 0,
    )

