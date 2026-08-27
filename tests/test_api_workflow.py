from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.services.llm import live_call_count


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
    assert first["state"]["metrics"]["mcp_calls"] == 2
    original_flight_id = first["state"]["trip_plan"]["selected_flight_id"]

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
    assert second["state"]["metrics"]["mcp_calls"] == 3
    assert second["state"]["trip_plan"]["selected_flight_id"] == original_flight_id

    approved = client.post(
        f"/api/trips/{first['thread_id']}/resume",
        json={"action": "approve"},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "completed"
    assert live_call_count() == 0
    memory_path.unlink(missing_ok=True)


def test_clarification_interrupt() -> None:
    client = TestClient(app)
    started = client.post("/api/trips/start", json={"message": "Tôi muốn đi Đà Nẵng."})
    assert started.status_code == 200
    body = started.json()
    assert body["interrupt"]["kind"] == "clarification"
    assert body["interrupt"]["questions"]
