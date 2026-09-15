import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openai import LengthFinishReasonError
from openai.types.chat import ChatCompletion

from app.config import settings
from app.main import app
from app.schemas import TripRequestPatch
from app.services import llm
from app.services import observability as obs


def fake_response():
    return SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=100)),
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=TripRequestPatch(travelers=2)))],
    )


@pytest.mark.asyncio
async def test_concurrent_llm_usage_isolated_and_cached_cost_not_double_counted(monkeypatch):
    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(parse=lambda **kwargs: fake_response())
    ))
    monkeypatch.setattr(llm, "_client", lambda: client)
    monkeypatch.setattr(settings, "llm_model", "test-model")
    monkeypatch.setattr(settings, "llm_prices_json", json.dumps({
        "test-model": {"input": 2, "cached_input": 0.5, "output": 8}
    }))

    async def request(name, n):
        with obs.run_scope(name, name, "test") as run:
            for _ in range(n):
                await asyncio.to_thread(
                    llm._managed_parse, "trip_intake", TripRequestPatch,
                    {"today": "2099-01-01", "current_request": "{}", "user_message": name},
                )
            return run.summary()
    one, two = await asyncio.gather(request("one", 1), request("two", 2))
    assert one["llm_calls"] == 1 and two["llm_calls"] == 2
    assert one["input_tokens"] == 1000 and two["input_tokens"] == 2000
    assert one["cost_usd"] == pytest.approx((900 * 2 + 100 * 0.5 + 200 * 8) / 1e6)
    assert obs.current_run() is None


@pytest.mark.asyncio
async def test_length_error_records_billed_tokens_and_unknown_price(monkeypatch):
    completion = ChatCompletion(
        id="test", model="unknown-model", object="chat.completion", created=0,
        choices=[], usage={"prompt_tokens": 100, "completion_tokens": 1800, "total_tokens": 1900},
    )

    def fail(**kwargs):
        raise LengthFinishReasonError(completion=completion)

    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=fail))
    ))
    monkeypatch.setattr(settings, "llm_model", "unknown-model")
    with obs.run_scope("length", "user", "test") as run:
        with pytest.raises(LengthFinishReasonError):
            llm._managed_parse("trip_intake", TripRequestPatch,
                               {"today": "today", "current_request": "{}", "user_message": "hi"})
        metrics = run.summary()
    assert metrics["output_tokens"] == 1800
    assert metrics["usage_incomplete"] is False
    assert metrics["cost_usd"] is None
    assert metrics["llm_calls"] == 1


def stream_events(client, url, body):
    with client.stream("POST", url, json=body) as response:
        assert response.status_code == 200
        return [json.loads(line[6:]) for line in response.iter_lines()
                if line.startswith("data: ")]


def test_stream_start_revision_and_snapshot_have_metrics():
    with TestClient(app) as client:
        events = stream_events(client, "/api/trips/start/stream", {
            "user_id": "stream-test",
            "message": "Từ TP.HCM đi Đà Nẵng từ 2099-10-15 đến 2099-10-18, "
                       "2 người, ngân sách 20 triệu, thích biển.",
        })
        assert events[0]["type"] == "progress"
        assert events[-1]["type"] == "result"
        result = events[-1]["data"]
        assert result["interrupt"]["kind"] == "plan_review"
        metrics = result["observability"]["latest"]
        assert metrics["llm_calls"] == 0 and metrics["cost_usd"] == 0
        assert metrics["mcp_tool_calls"] == 3
        assert any(e["status"] == "awaiting_input" for e in metrics["events"])
        assert not any(e["status"] == "error" for e in metrics["events"])
        thread = result["thread_id"]
        revised = stream_events(client, f"/api/trips/{thread}/resume/stream", {
            "action": "revise",
            "message": "Đổi khách sạn dưới 1,5 triệu/đêm, giữ nguyên chuyến bay.",
        })[-1]["data"]
        actors = {e["actor"] for e in revised["observability"]["latest"]["events"]}
        assert "hotel_agent" in actors and "planner_agent" in actors
        assert "flight_agent" not in actors
        assert len(client.get(f"/api/trips/{thread}").json()["observability"]["runs"]) == 2


@pytest.mark.asyncio
async def test_stream_progress_arrives_while_graph_is_still_blocked(monkeypatch):
    # A real async consumer proves progress is live; TestClient alone buffers the response.
    from app.api import _stream

    started = asyncio.Event()
    release = asyncio.Event()
    async def work():
        from app.api import _stream_queue
        with obs.run_scope("blocked", "user", "test", queue=_stream_queue.get()):
            obs.emit("flight_agent", "running")
            started.set()
            await release.wait()
        return {"ok": True}
    response = _stream(work)
    iterator = response.body_iterator
    first = await anext(iterator)
    await started.wait()
    assert "progress" in first
    assert not release.is_set()
    release.set()
    remaining = [chunk async for chunk in iterator]
    assert any('"type": "result"' in chunk for chunk in remaining)


@pytest.mark.asyncio
async def test_general_notice_is_not_mock_but_explicit_fallback_is():
    async def warned_node(state):
        return {"warnings": ["Prices may change before checkout"]}
    async def fallback_node(state):
        obs.emit("planner_agent", "fallback", detail="LengthFinishReasonError")
        return {"warnings": ["Fallback itinerary"]}
    with obs.run_scope("notice-test", "user", "test") as run:
        await obs.instrument_node("planner_agent", warned_node)({})
        assert run.events[-1]["status"] == "success"
        await obs.instrument_node("planner_agent", fallback_node)({})
        assert run.events[-1]["status"] == "fallback"


@pytest.mark.parametrize("value", ["[]", "bad json", '{"test":{"input":-1}}'])
def test_invalid_price_configuration_is_unknown_not_a_workflow_error(monkeypatch, value):
    from app.services.pricing import model_price
    monkeypatch.setattr(settings, "llm_prices_json", value)
    assert model_price("test") is None
