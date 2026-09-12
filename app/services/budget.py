"""Deterministic VND budget calculation with auditable line items."""

from __future__ import annotations

from app.schemas import (
    BudgetLineItem,
    BudgetSummary,
    FlightOption,
    HotelOption,
    HotelStay,
    ItineraryDay,
    TripRequest,
)

FOOD_PER_PERSON_PER_DAY = 350_000
LOCAL_TRANSPORT_PER_DAY = 250_000
BUFFER_RATIO = 0.05


def _flight_items(flight: FlightOption, travelers: int) -> list[BudgetLineItem]:
    legs = [flight.outbound_leg, flight.return_leg]
    priced_legs = [leg for leg in legs if leg and leg.total_price is not None]
    if priced_legs and sum(leg.total_price or 0 for leg in priced_legs) == flight.total_price:
        labels = {"outbound": "Vé chiều đi", "return": "Vé chiều về"}
        return [
            BudgetLineItem(
                category="flight",
                label=labels[leg.direction],
                quantity=travelers,
                unit_price=leg.price_per_person or 0,
                total=leg.total_price or 0,
            )
            for leg in priced_legs
        ]
    scope = "Vé máy bay khứ hồi" if flight.price_scope == "round_trip" else "Vé máy bay một chiều"
    return [
        BudgetLineItem(
            category="flight",
            label=scope,
            quantity=travelers,
            unit_price=flight.price_per_person,
            total=flight.total_price,
            note=flight.price_note,
        )
    ]


def _hotel_items(hotel: HotelOption, stays: list[HotelStay]) -> list[BudgetLineItem]:
    if stays:
        return [
            BudgetLineItem(
                category="hotel",
                label=(
                    f"{stay.hotel.name} · {stay.check_in_date.isoformat()}"
                    if stay.nights == 1
                    else f"{stay.hotel.name} · {stay.nights} đêm"
                ),
                quantity=stay.nights,
                unit_price=stay.hotel.nightly_price,
                total=stay.total_price,
                note=stay.hotel.room_type,
            )
            for stay in stays
        ]
    return [
        BudgetLineItem(
            category="hotel",
            label=f"{hotel.name} · {hotel.nights} đêm",
            quantity=hotel.nights,
            unit_price=hotel.nightly_price,
            total=hotel.total_price,
            note=hotel.room_type,
        )
    ]


def calculate_budget(
    request: TripRequest,
    flight: FlightOption,
    hotel: HotelOption,
    itinerary: list[ItineraryDay],
    hotel_stays: list[HotelStay] | None = None,
) -> BudgetSummary:
    if request.travelers is None or request.total_budget is None:
        raise ValueError("travelers and total_budget are required")

    trip_days = request.duration_days or len(itinerary)
    if trip_days < 1:
        raise ValueError("trip_days must be positive")

    stays = hotel_stays or []
    hotel_total = sum(stay.total_price for stay in stays) if stays else hotel.total_price
    line_items = [*_flight_items(flight, request.travelers), *_hotel_items(hotel, stays)]

    activities = 0
    for day in itinerary:
        for activity in day.activities:
            total = activity.estimated_cost_per_person * request.travelers
            activities += total
            line_items.append(
                BudgetLineItem(
                    category="activities",
                    label=f"{day.date.isoformat()} · {activity.title}",
                    quantity=request.travelers,
                    unit_price=activity.estimated_cost_per_person,
                    total=total,
                    note=activity.place_name,
                )
            )

    food = FOOD_PER_PERSON_PER_DAY * request.travelers * trip_days
    local_transport = LOCAL_TRANSPORT_PER_DAY * trip_days
    for day_index in range(trip_days):
        line_items.append(
            BudgetLineItem(
                category="food",
                label=f"Ăn uống ngày {day_index + 1}",
                quantity=request.travelers,
                unit_price=FOOD_PER_PERSON_PER_DAY,
                total=FOOD_PER_PERSON_PER_DAY * request.travelers,
            )
        )
        line_items.append(
            BudgetLineItem(
                category="local_transport",
                label=f"Di chuyển nội thành ngày {day_index + 1}",
                unit_price=LOCAL_TRANSPORT_PER_DAY,
                total=LOCAL_TRANSPORT_PER_DAY,
            )
        )

    subtotal = flight.total_price + hotel_total + activities + food + local_transport
    buffer = round(subtotal * BUFFER_RATIO)
    line_items.append(
        BudgetLineItem(
            category="buffer",
            label="Dự phòng biến động giá và phát sinh",
            unit_price=buffer,
            total=buffer,
            note=f"{BUFFER_RATIO:.0%} trên tạm tính",
        )
    )
    estimated_total = subtotal + buffer
    remaining = request.total_budget - estimated_total

    return BudgetSummary(
        flight=flight.total_price,
        hotel=hotel_total,
        activities=activities,
        food=food,
        local_transport=local_transport,
        buffer=buffer,
        estimated_total=estimated_total,
        total_budget=request.total_budget,
        remaining_budget=remaining,
        is_over_budget=remaining < 0,
        buffer_rate=BUFFER_RATIO,
        line_items=line_items,
    )
