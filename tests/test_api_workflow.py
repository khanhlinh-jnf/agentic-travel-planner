from fastapi.testclient import TestClient

from app.config import settings
from app.graph import _merge_request_patch
from app.main import app
from app.schemas import TripRequest, TripRequestPatch
from app.services.llm import heuristic_trip_patch, live_call_count
from app.services.validation import normalize_request, validate_request


def test_full_hitl_and_selective_hotel_revision(monkeypatch) -> None:
    memory_path = settings.project_root / "data" / "user_memory.pytest.json"
    memory_path.unlink(missing_ok=True)
    monkeypatch.setattr(settings, "user_memory_path", str(memory_path))
    client = TestClient(app)
    prompt = (
        "Tôi muốn đi từ TP.HCM đến Đà Nẵng từ 2099-10-15 đến 2099-10-18, "
        "2 người, ngân sách 20 triệu VND, thích biển và ăn uống."
    )

    started = client.post("/api/trips/start", json={"message": prompt})
    assert started.status_code == 200, started.text
    first = started.json()
    assert first["status"] == "awaiting_input"
    assert first["interrupt"]["kind"] == "plan_review"
    assert first["state"]["metrics"]["mcp_calls"] == 3
    assert "supervisor_steps" not in first["state"]["metrics"]
    assert all(event["actor"] != "supervisor" for event in first["state"]["trace"])
    assert any(
        activity.get("place_name")
        for day in first["state"]["trip_plan"]["itinerary"]
        for activity in day["activities"]
    )
    assert first["state"]["trip_plan"]["budget"]["line_items"]
    original_flight_id = first["state"]["trip_plan"]["selected_flight_id"]

    booking = client.post(
        f"/api/trips/{first['thread_id']}/flight-booking-options",
        json={"option_id": original_flight_id},
    )
    assert booking.status_code == 200, booking.text
    assert booking.json()["source"] == "mock"

    hotel = client.post(
        f"/api/trips/{first['thread_id']}/hotel-details",
        json={"option_id": first["state"]["trip_plan"]["selected_hotel_id"]},
    )
    assert hotel.status_code == 200, hotel.text
    assert hotel.json()["hotel"]

    revised = client.post(
        f"/api/trips/{first['thread_id']}/resume",
        json={
            "action": "revise",
            "message": "Đổi khách sạn dưới 1,5 triệu/đêm, giữ nguyên chuyến bay.",
        },
    )
    assert revised.status_code == 200, revised.text
    second = revised.json()
    assert second["interrupt"]["kind"] == "plan_review"
    assert second["state"]["metrics"]["mcp_calls"] == 4
    assert second["state"]["trip_plan"]["selected_flight_id"] == original_flight_id

    hotel_ids = [item["id"] for item in second["state"]["hotel_options"][:3]]

    approved = client.post(
        f"/api/trips/{first['thread_id']}/resume",
        json={
            "action": "approve",
            "selected_flight_id": original_flight_id,
            "hotel_selection_ids": hotel_ids,
        },
    )
    assert approved.status_code == 200, approved.text
    approved_body = approved.json()
    assert approved_body["status"] == "completed"
    assert len(approved_body["state"]["trip_plan"]["hotel_stays"]) == 3
    assert [
        stay["hotel"]["id"] for stay in approved_body["state"]["trip_plan"]["hotel_stays"]
    ] == hotel_ids
    assert live_call_count() == 0
    memory_path.unlink(missing_ok=True)


def test_clarification_interrupt() -> None:
    client = TestClient(app)
    started = client.post("/api/trips/start", json={"message": "Tôi muốn đi Đà Nẵng."})
    assert started.status_code == 200
    body = started.json()
    assert body["interrupt"]["kind"] == "clarification"
    assert body["interrupt"]["questions"]


def test_clarification_date_patch_replaces_conflicting_duration() -> None:
    current = TripRequest(
        departure_date="2099-10-15",
        return_date="2099-10-18",
        duration_days=2,
    )

    updated = _merge_request_patch(
        current,
        TripRequestPatch(return_date="2099-10-18"),
    )

    assert updated.return_date.isoformat() == "2099-10-18"
    assert updated.duration_days == 4


def test_clarification_duration_patch_replaces_conflicting_return_date() -> None:
    current = TripRequest(
        departure_date="2099-10-15",
        return_date="2099-10-18",
        duration_days=2,
    )

    updated = _merge_request_patch(
        current,
        TripRequestPatch(duration_days=4),
    )

    assert updated.return_date.isoformat() == "2099-10-18"
    assert updated.duration_days == 4


def test_clarification_reply_parses_single_return_date() -> None:
    current = TripRequest(departure_date="2099-10-15", duration_days=2)

    patch = heuristic_trip_patch("Ngày về là 2099-10-18", current)

    assert patch.departure_date is None
    assert patch.return_date.isoformat() == "2099-10-18"


def test_explicit_dates_auto_correct_conflicting_duration() -> None:
    normalized = normalize_request(
        TripRequest(
            departure_date="2099-10-15",
            return_date="2099-10-18",
            duration_days=2,
        )
    )

    _, errors = validate_request(normalized, today=normalized.departure_date)

    assert normalized.duration_days == 4
    assert all("mâu thuẫn" not in error for error in errors)
