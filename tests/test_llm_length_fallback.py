from datetime import date
from types import SimpleNamespace

from app.schemas import FlightOption, HotelOption, PlaceOption, TripRequest
from app.services import llm


class SimulatedLengthError(Exception):
    pass


def test_narrative_length_limit_falls_back_without_failing_workflow(monkeypatch) -> None:
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
        price_scope="round_trip",
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
    place = PlaceOption(
        id="place",
        name="Demo Place",
        address="1 Demo Street",
        rating=4.5,
        source="mock",
    )

    def raise_length_error(**kwargs):
        del kwargs
        raise SimulatedLengthError

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=raise_length_error))
    )
    monkeypatch.setattr(llm.settings, "use_mock_llm", False)
    monkeypatch.setattr(llm.settings, "openai_api_keys", "test-key")
    monkeypatch.setattr(llm, "LengthFinishReasonError", SimulatedLengthError)
    monkeypatch.setattr(llm, "_client", lambda: fake_client)
    monkeypatch.setattr(llm, "_record_live_call", lambda: None)

    narrative = llm.draft_narrative(request, flight, hotel, [place])

    assert len(narrative.itinerary) == 4
    assert any("vượt giới hạn độ dài" in warning for warning in narrative.warnings)
