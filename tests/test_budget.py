from datetime import date

from app.schemas import (
    FlightOption,
    HotelOption,
    ItineraryActivity,
    ItineraryDay,
    TripRequest,
)
from app.services.budget import calculate_budget


def test_budget_is_deterministic_and_reconciles() -> None:
    request = TripRequest(
        origin="TP.HCM",
        destination="Đà Nẵng",
        departure_date=date(2099, 10, 15),
        return_date=date(2099, 10, 18),
        duration_days=4,
        travelers=2,
        total_budget=20_000_000,
    )
    flight = FlightOption(
        id="flight",
        airline="Demo Air",
        origin="TP.HCM",
        destination="Đà Nẵng",
        departure_date=request.departure_date,
        return_date=request.return_date,
        price_per_person=2_000_000,
        total_price=4_000_000,
        source="mock",
    )
    hotel = HotelOption(
        id="hotel",
        name="Demo Hotel",
        destination="Đà Nẵng",
        nightly_price=1_000_000,
        nights=3,
        total_price=3_000_000,
        source="mock",
    )
    itinerary = [
        ItineraryDay(
            day=1,
            date=request.departure_date,
            title="Arrival",
            activities=[
                ItineraryActivity(
                    time_of_day="evening",
                    title="Dinner",
                    description="Local food",
                    estimated_cost_per_person=200_000,
                )
            ],
        )
    ]

    budget = calculate_budget(request, flight, hotel, itinerary)

    components = (
        budget.flight
        + budget.hotel
        + budget.activities
        + budget.food
        + budget.local_transport
        + budget.buffer
    )
    assert budget.estimated_total == components
    assert budget.remaining_budget == budget.total_budget - budget.estimated_total
