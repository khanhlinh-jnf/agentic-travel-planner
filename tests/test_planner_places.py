from datetime import date

from app.agents.planner import _attach_place_evidence
from app.schemas import ItineraryActivity, ItineraryDay, PlaceOption, PlanNarrative


def test_transport_activity_does_not_duplicate_a_meal_place() -> None:
    place = PlaceOption(
        id="seafood",
        name="Mộc quán Seafood Đà Nẵng",
        address="26 Tô Hiến Thành, Đà Nẵng",
        rating=4.8,
        review_count=100,
        source="mock",
    )
    narrative = PlanNarrative(
        itinerary=[
            ItineraryDay(
                day=1,
                date=date(2099, 10, 15),
                title="Đến Đà Nẵng",
                activities=[
                    ItineraryActivity(
                        time_of_day="evening",
                        title="Bay từ TP.HCM đến Đà Nẵng",
                        description="Nhận phòng và nghỉ ngơi.",
                        place_name=place.name,
                        estimated_cost_per_person=200_000,
                    ),
                    ItineraryActivity(
                        time_of_day="evening",
                        title="Ăn tối hải sản",
                        description="Ăn tại quán ven biển.",
                        place_name=place.name,
                        estimated_cost_per_person=350_000,
                    ),
                ],
            )
        ],
        rationale="Demo",
    )

    result = _attach_place_evidence(narrative, [place])
    flight, dinner = result.itinerary[0].activities

    assert flight.time_of_day == "flexible"
    assert flight.place_name is None
    assert flight.estimated_cost_per_person == 0
    assert dinner.place_name == place.name
    assert dinner.estimated_cost_per_person == 350_000
