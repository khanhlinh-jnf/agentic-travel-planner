"""Exercise the installed SDK with an in-memory exporter, never Cloud or OpenAI."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.config import settings
from app.prompts.registry import PromptRegistry
from app.schemas import TripRequestPatch
from app.services import llm
from app.services import observability as obs


@pytest.mark.asyncio
async def test_sdk_parentage_usage_and_content_privacy(monkeypatch):
    exporter = InMemorySpanExporter()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = Langfuse(
        public_key="pk-lf-offline-integration", secret_key="sk-lf-test-not-real",
        httpx_client=httpx.Client(transport=transport), span_exporter=exporter,
    )
    monkeypatch.setattr(client, "get_trace_url", lambda **kwargs: "https://example.test/trace")
    monkeypatch.setattr(obs, "langfuse_client", lambda **kwargs: client)
    monkeypatch.setattr(settings, "telemetry_capture_content", False)
    response = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=200,
                              prompt_tokens_details=SimpleNamespace(cached_tokens=100)),
        choices=[SimpleNamespace(message=SimpleNamespace(parsed=TripRequestPatch(travelers=2)))],
    )
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=lambda **kwargs: response))
    ))

    async def node(state):
        await asyncio.to_thread(llm._managed_parse, "trip_intake", TripRequestPatch, {
            "today": "2099-01-01", "current_request": "{}",
            "user_message": "private-content-never-export-this",
        })
        with obs.tool_span("mock", "search_flights"):
            pass
        return {}

    with obs.run_scope("sdk-test-thread", "sdk-test-user", "start") as run:
        await obs.instrument_node("parse_request", node)({})
        run.status = "completed"
    await asyncio.to_thread(client.flush)
    spans = {span.name: span for span in exporter.get_finished_spans()}
    root = spans["trip.start"]
    node_span = spans["parse_request"]
    generation = spans["trip_intake"]
    assert node_span.parent.span_id == root.context.span_id
    assert generation.parent.span_id == node_span.context.span_id
    assert spans["mock.search_flights"].parent.span_id == node_span.context.span_id
    assert len(spans) == 4  # Manual instrumentation does not duplicate generations.
    attributes = dict(generation.attributes)
    usage = json.loads(attributes["langfuse.observation.usage_details"])
    assert usage == {"input": 900, "cached_input": 100, "output": 200}
    assert "private-content-never-export-this" not in str(attributes)
    assert root.attributes["session.id"] == "sdk-test-thread"


@pytest.mark.asyncio
async def test_remote_prompt_success_and_incompatible_template_fallback(monkeypatch):
    local = PromptRegistry().local("trip_intake")
    remote = SimpleNamespace(
        version=8, prompt=local.messages,
        config={**local.config, "variables": local.metadata["variables"]},
    )
    monkeypatch.setattr(settings, "prompt_backend", "langfuse")
    monkeypatch.setattr(obs, "langfuse_client", lambda **kwargs: SimpleNamespace(
        get_prompt=lambda *a, **k: remote
    ) if kwargs.get("for_prompts") else None)
    with obs.run_scope("remote-ok", "user", "test"):
        prompt = PromptRegistry().get("trip_intake")
        assert prompt.source == "langfuse" and prompt.version == 8
        assert prompt.reference()["artifact_version"] == 1
        assert prompt.remote is remote
    remote.prompt = [{"role": "system", "content": "Wrong contract {{secret}}"}]
    with obs.run_scope("remote-bad", "user", "test"):
        prompt = PromptRegistry().get("trip_intake")
        assert prompt.source == "local" and prompt.fallback_reason == "ValueError"


@pytest.mark.asyncio
async def test_root_failure_and_missing_usage_are_not_free_success(monkeypatch):
    def fail(**kwargs):
        raise TimeoutError("upstream not responding")
    monkeypatch.setattr(llm, "_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=fail))
    ))
    with pytest.raises(TimeoutError), obs.run_scope("failed-request", "user", "test"):
        llm._managed_parse("trip_intake", TripRequestPatch, {
            "today": "2099-01-01", "current_request": "{}", "user_message": "hi",
        })
    latest = obs.history("failed-request")[-1]
    assert latest["status"] == "error"
    assert latest["usage_incomplete"] and latest["cost_usd"] is None
    assert latest["llm_calls"] == 1
