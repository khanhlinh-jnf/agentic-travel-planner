"""Request-scoped measurements, safe Langfuse spans and live progress events."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache, wraps
from time import perf_counter
from uuid import uuid4

from app.config import settings
from app.services.pricing import model_price

logger = logging.getLogger("travel.telemetry")
_run: ContextVar[RunRecord | None] = ContextVar("travel_run", default=None)
_actor: ContextVar[str] = ContextVar("travel_actor", default="workflow")
_history: dict[str, list[dict]] = {}
_locks: dict[str, asyncio.Lock] = {}


@dataclass
class RunRecord:
    thread_id: str
    user_id: str
    action: str
    run_id: str = field(default_factory=lambda: uuid4().hex)
    started: float = field(default_factory=perf_counter)
    events: list[dict] = field(default_factory=list)
    generations: list[dict] = field(default_factory=list)
    prompts: dict = field(default_factory=dict)
    llm_calls: int = 0
    trace_id: str | None = None
    trace_url: str | None = None
    queue: asyncio.Queue | None = None
    loop: asyncio.AbstractEventLoop | None = None
    status: str = "running"

    def summary(self) -> dict:
        known_cost = sum(g["cost_usd"] or 0 for g in self.generations)
        unknown = sum(g["cost_usd"] is None for g in self.generations)
        return {
            "run_id": self.run_id, "thread_id": self.thread_id, "action": self.action,
            "status": self.status, "elapsed_ms": round((perf_counter() - self.started) * 1000),
            "llm_calls": self.llm_calls,
            "input_tokens": sum(g["input_tokens"] for g in self.generations),
            "output_tokens": sum(g["output_tokens"] for g in self.generations),
            "cached_input_tokens": sum(g["cached_input_tokens"] for g in self.generations),
            "cost_usd": None if unknown else round(known_cost, 8),
            "known_cost_usd": round(known_cost, 8), "unpriced_calls": unknown,
            "cost_scope": "LLM only; excludes SerpApi, RapidAPI and hosting",
            "usage_incomplete": any(not g["usage_available"] for g in self.generations),
            "trace_id": self.trace_id, "trace_url": self.trace_url,
            "events": list(self.events), "generations": list(self.generations),
            "prompts": [p.reference() for p in self.prompts.values()],
            "fallbacks": sum(e["status"] == "fallback" for e in self.events),
            "mcp_tool_calls": sum(
                e["kind"] == "tool" and e["status"] == "running" for e in self.events
            ),
            "first_progress_ms": next(
                (e["elapsed_ms"] for e in self.events if e["status"] == "running"), None
            ),
        }


def current_run() -> RunRecord | None:
    return _run.get()


def thread_lock(thread_id: str) -> asyncio.Lock:
    return _locks.setdefault(thread_id, asyncio.Lock())


def history(thread_id: str) -> list[dict]:
    return _history.get(thread_id, [])


@lru_cache(maxsize=4)
def _make_client(public_key, secret_key, base_url, enabled):
    from langfuse import Langfuse

    return Langfuse(
        public_key=public_key, secret_key=secret_key, base_url=base_url,
        tracing_enabled=enabled, timeout=5,
    )


def langfuse_client(*, for_prompts=False):
    if not for_prompts and not settings.langfuse_enabled:
        return None
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    try:
        return _make_client(
            settings.langfuse_public_key, settings.langfuse_secret_key,
            settings.langfuse_base_url, settings.langfuse_enabled,
        )
    except Exception as exc:
        logger.warning("langfuse_unavailable error_type=%s", type(exc).__name__)
        return None


@contextmanager
def observation(name: str, kind="span", **kwargs):
    """Exporter failures must not change business control flow."""
    stack = ExitStack()
    span = None
    try:
        client = langfuse_client()
        if client is not None:
            span = stack.enter_context(client.start_as_current_observation(
                name=name, as_type=kind, **kwargs
            ))
    except Exception as exc:
        logger.warning("span_start_failed error_type=%s", type(exc).__name__)
    try:
        yield span
    finally:
        try:
            stack.close()
        except Exception as exc:
            logger.warning("span_end_failed error_type=%s", type(exc).__name__)


def update_span(span, **kwargs):
    if span is not None:
        try:
            span.update(**kwargs)
        except Exception as exc:
            logger.warning("span_update_failed error_type=%s", type(exc).__name__)


def emit(actor: str, status: str, *, kind="node", detail="", **data):
    run = current_run()
    if run is None:
        return
    event = {
        "run_id": run.run_id, "seq": len(run.events) + 1, "actor": actor, "status": status,
        "kind": kind, "detail": detail, "created_at": datetime.now(UTC).isoformat(),
        "elapsed_ms": round((perf_counter() - run.started) * 1000), **data,
    }
    run.events.append(event)
    # to_thread() inherits ContextVars, but must not touch asyncio.Queue directly.
    if run.queue is not None and run.loop is not None:
        item = {"type": "progress", "data": event}
        try:
            same_loop = asyncio.get_running_loop() is run.loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            run.queue.put_nowait(item)
        else:
            run.loop.call_soon_threadsafe(run.queue.put_nowait, item)
    logger.info(
        "run=%s actor=%s status=%s elapsed_ms=%s",
        run.run_id, actor, status, event["elapsed_ms"],
    )


@contextmanager
def run_scope(thread_id, user_id, action, queue=None):
    run = RunRecord(thread_id, user_id, action, queue=queue)
    run.loop = asyncio.get_running_loop()
    token = _run.set(run)
    stack = ExitStack()
    try:
        if langfuse_client():
            from langfuse import propagate_attributes

            stack.enter_context(propagate_attributes(
                session_id=thread_id, user_id=user_id,
                tags=["travel-planner", action],
                metadata={"release": settings.app_release, "run_id": run.run_id},
            ))
    except Exception as exc:
        logger.warning("trace_context_failed error_type=%s", type(exc).__name__)
    try:
        with observation(f"trip.{action}") as span:
            emit("workflow", "running", kind="run", thread_id=thread_id, action=action)
            if span is not None:
                try:
                    run.trace_id = span.trace_id
                    run.trace_url = langfuse_client().get_trace_url(trace_id=span.trace_id)
                except Exception:
                    pass
            try:
                yield run
            except BaseException as exc:
                run.status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
                update_span(span, level="ERROR", status_message=type(exc).__name__)
                raise
            finally:
                update_span(span, output={"status": run.status}, metadata={"run_id": run.run_id})
    finally:
        record = run.summary()
        _history.setdefault(thread_id, []).append(record)
        logger.info("run_finished %s", json.dumps({
            k: v for k, v in record.items() if k not in {"events", "generations", "prompts"}
        }, ensure_ascii=False))
        try:
            stack.close()
        except Exception as exc:
            logger.warning("trace_context_close_failed error_type=%s", type(exc).__name__)
        finally:
            _run.reset(token)


def instrument_node(name, function):
    """Start before actual work, not after return; handle HITL without counting it as failure."""
    @wraps(function)
    async def wrapped(state):
        from langgraph.errors import GraphInterrupt
        from langgraph.types import Command

        actor_token = _actor.set(name)
        started = perf_counter()
        emit(name, "running")
        run = current_run()
        event_offset = len(run.events) if run else 0
        try:
            with observation(name) as span:
                try:
                    result = await function(state)
                except GraphInterrupt:
                    emit(name, "awaiting_input", duration_ms=round(
                        (perf_counter() - started) * 1000
                    ))
                    update_span(span, output={"status": "awaiting_input"})
                    raise
                except Exception as exc:
                    emit(name, "error", detail=type(exc).__name__,
                         duration_ms=round((perf_counter() - started) * 1000))
                    update_span(span, level="ERROR", status_message=type(exc).__name__)
                    raise
                update = result.update if isinstance(result, Command) else result
                update = update or {}
                if "agent_queue" in update:
                    emit(name, "queued", kind="queue", agents=update["agent_queue"])
                outcome = "error" if update.get("errors") else "success"
                source = None
                for key in ("flight_options", "hotel_options", "place_options"):
                    options = update.get(key) or []
                    if options:
                        source = options[0].source
                        if source == "mock":
                            outcome = "fallback"
                # Generic itinerary/budget notices are not evidence of a mock fallback.
                if outcome != "error" and run and any(
                    event["actor"] == name and event["status"] == "fallback"
                    for event in run.events[event_offset:]
                ):
                    outcome = "fallback"
                emit(name, outcome, source=source, duration_ms=round(
                    (perf_counter() - started) * 1000
                ))
                if isinstance(result, Command) and result.goto:
                    emit(name, "handoff", kind="handoff", target=str(result.goto))
                update_span(span, output={"status": outcome, "source": source},
                            level="ERROR" if outcome == "error" else (
                                "WARNING" if outcome == "fallback" else "DEFAULT"
                            ))
                return result
        finally:
            _actor.reset(actor_token)
    return wrapped


def record_usage(response, *, model, prompt, error=None) -> dict:
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0
    cost = None
    try:
        price = model_price(model)
        if usage is not None and price:
            cost = (
                (input_tokens - cached) * float(price["input"])
                + cached * float(price["cached_input"])
                + output_tokens * float(price["output"])
            ) / 1_000_000
    except (KeyError, TypeError, ValueError):
        pass
    record = {
        "model": model, "prompt": prompt.reference(), "input_tokens": input_tokens,
        "output_tokens": output_tokens, "cached_input_tokens": cached,
        "cost_usd": cost, "usage_available": usage is not None, "error": error,
    }
    run = current_run()
    if run:
        run.generations.append(record)
    return record


async def lookup_run(thread_id, user_id, action, operation, **kwargs):
    with run_scope(thread_id, user_id, action) as run:
        try:
            result = await operation(**kwargs)
            run.status = "fallback" if result.source == "mock" else "completed"
            if result.source == "mock":
                emit(action, "fallback")
        except Exception:
            run.status = "error"
            raise
    return result


@contextmanager
def tool_span(provider: str, name: str):
    actor = f"{provider}.{name}"
    started = perf_counter()
    emit(actor, "running", kind="tool", parent=_actor.get())
    with observation(actor, "tool", metadata={"provider": provider}) as span:
        try:
            yield span
        except Exception as exc:
            emit(actor, "error", kind="tool", detail=type(exc).__name__,
                 duration_ms=round((perf_counter() - started) * 1000))
            update_span(span, level="ERROR", status_message=type(exc).__name__)
            raise
        else:
            emit(actor, "success", kind="tool",
                 duration_ms=round((perf_counter() - started) * 1000))
