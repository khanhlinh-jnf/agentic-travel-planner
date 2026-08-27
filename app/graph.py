"""One LangGraph workflow: hierarchical planning, HITL, then selective Swarm revision."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents.planner import build_plan
from app.agents.specialists import search_flights, search_hotels
from app.agents.supervisor import decide_next
from app.config import settings
from app.memory.store import (
    apply_preferences,
    load_preferences,
    preferences_from_request,
    save_preferences,
)
from app.schemas import TraceEvent, TripRequest, TripRequestPatch, WorkflowMetrics
from app.services.constraint_patch import apply_revision
from app.services.llm import live_call_count, parse_revision, parse_trip_request
from app.services.validation import clarification_questions, normalize_request, validate_request
from app.state import TravelState


def _event(kind: str, actor: str, action: str, detail: str = "") -> TraceEvent:
    return TraceEvent(kind=kind, actor=actor, action=action, detail=detail)


def _metrics(state: TravelState, **increments: int) -> WorkflowMetrics:
    current = deepcopy(state.get("metrics") or WorkflowMetrics())
    for field, amount in increments.items():
        setattr(current, field, getattr(current, field) + amount)
    return current


def _merge_request_patch(current: TripRequest | None, patch: TripRequestPatch) -> TripRequest:
    values = current.model_dump() if current else TripRequest().model_dump()
    updates = patch.model_dump(exclude_none=True, exclude={"date_is_ambiguous"})
    for nested in ("flight_preferences", "hotel_preferences"):
        if nested in updates:
            merged_nested = values[nested]
            merged_nested.update(updates.pop(nested))
            values[nested] = merged_nested
    values.update(updates)
    return normalize_request(TripRequest.model_validate(values))


async def load_memory_node(state: TravelState) -> dict[str, Any]:
    user_id = state.get("user_id", "demo-user")
    return {
        "user_preferences": load_preferences(user_id),
        "metrics": state.get("metrics") or WorkflowMetrics(),
        "status": "running",
        "phase": "intake",
        "flight_search_stale": True,
        "hotel_search_stale": True,
        "itinerary_stale": True,
        "supervisor_steps": 0,
        "revision_count": 0,
        "trace": [_event("node", "memory", "load", f"Loaded preferences for {user_id}")],
    }


async def parse_request_node(state: TravelState) -> dict[str, Any]:
    text = state.get("clarification_response") or state.get("raw_request", "")
    before = live_call_count()
    patch = await asyncio.to_thread(parse_trip_request, text, state.get("trip_request"))
    trip_request = _merge_request_patch(state.get("trip_request"), patch)
    trip_request = apply_preferences(trip_request, state["user_preferences"])
    return {
        "trip_request": trip_request,
        "clarification_response": "",
        "date_is_ambiguous": patch.date_is_ambiguous,
        "metrics": _metrics(state, llm_calls=live_call_count() - before),
        "trace": [_event("node", "intake_agent", "parse", "Created a typed trip request")],
    }


async def validate_request_node(state: TravelState) -> dict[str, Any]:
    missing, errors = validate_request(state["trip_request"])
    if state.get("date_is_ambiguous"):
        errors.append("Ngày chưa có năm; vui lòng nhập ngày đầy đủ theo YYYY-MM-DD.")
    needs_input = bool(missing or errors)
    return {
        "missing_fields": missing,
        "validation_errors": errors,
        "phase": "clarification" if needs_input else "research",
        "trace": [
            _event(
                "route",
                "validator",
                "request_needs_clarification" if needs_input else "request_ready",
                "; ".join(errors) if errors else ", ".join(missing),
            )
        ],
    }


def route_after_validation(state: TravelState) -> Literal["clarify", "supervisor"]:
    needs_input = state.get("missing_fields") or state.get("validation_errors")
    return "clarify" if needs_input else "supervisor"


async def clarification_node(state: TravelState) -> dict[str, Any]:
    questions = clarification_questions(
        state.get("missing_fields", []),
        state.get("validation_errors", []),
    )
    answer = interrupt(
        {
            "kind": "clarification",
            "message": "Mình cần thêm thông tin trước khi tìm chuyến đi.",
            "questions": questions,
        }
    )
    if isinstance(answer, dict):
        answer_text = str(answer.get("message") or answer.get("answer") or "")
    else:
        answer_text = str(answer)
    return {
        "clarification_response": answer_text,
        "trace": [_event("hitl", "human", "clarify", "Supplied missing constraints")],
    }


async def supervisor_node(state: TravelState) -> dict[str, Any]:
    decision = decide_next(state)
    steps = state.get("supervisor_steps", 0) + 1
    return {
        "supervisor_decision": decision,
        "supervisor_steps": steps,
        "metrics": _metrics(state, supervisor_steps=1),
        "trace": [_event("route", "supervisor", f"route_to_{decision}")],
    }


def route_supervisor(state: TravelState) -> str:
    return {
        "flight": "flight_agent",
        "hotel": "hotel_agent",
        "planner": "planner_agent",
        "review": "review",
        "fail": "fail",
    }[state["supervisor_decision"]]


async def flight_agent_node(state: TravelState) -> dict[str, Any]:
    try:
        result = await search_flights(state["trip_request"])
    except Exception as exc:
        return {
            "status": "needs_human",
            "errors": [f"Flight Agent failed: {exc}"],
            "trace": [_event("warning", "flight_agent", "failed", str(exc))],
        }
    return {
        "flight_options": result.results,
        "flight_search_stale": False,
        "warnings": [result.warning] if result.warning else [],
        "metrics": _metrics(
            state,
            mcp_calls=1,
            external_search_calls=result.provider_call_count,
        ),
        "trace": [
            _event(
                "tool",
                "flight_agent",
                "search_flights",
                f"Received {len(result.results)} options from {result.source}",
            )
        ],
    }


async def hotel_agent_node(state: TravelState) -> dict[str, Any]:
    try:
        result = await search_hotels(state["trip_request"])
    except Exception as exc:
        return {
            "status": "needs_human",
            "errors": [f"Hotel Agent failed: {exc}"],
            "trace": [_event("warning", "hotel_agent", "failed", str(exc))],
        }
    return {
        "hotel_options": result.results,
        "hotel_search_stale": False,
        "warnings": [result.warning] if result.warning else [],
        "metrics": _metrics(
            state,
            mcp_calls=1,
            external_search_calls=result.provider_call_count,
        ),
        "trace": [
            _event(
                "tool",
                "hotel_agent",
                "search_hotels",
                f"Received {len(result.results)} options from {result.source}",
            )
        ],
    }


async def planner_agent_node(state: TravelState) -> dict[str, Any]:
    before = live_call_count()
    plan = await asyncio.to_thread(
        build_plan,
        state["trip_request"],
        state["flight_options"],
        state["hotel_options"],
    )
    return {
        "trip_plan": plan,
        "itinerary_stale": False,
        "phase": "review",
        "metrics": _metrics(state, llm_calls=live_call_count() - before),
        "trace": [_event("node", "planner_agent", "compose_plan", "Itinerary and budget ready")],
    }


async def review_node(state: TravelState) -> dict[str, Any]:
    answer = interrupt(
        {
            "kind": "plan_review",
            "message": "Lịch trình đã sẵn sàng. Bạn muốn duyệt hay yêu cầu chỉnh sửa?",
            "plan": state["trip_plan"].model_dump(mode="json"),
        }
    )
    if isinstance(answer, dict):
        action = str(answer.get("action", "approve")).strip().lower()
        feedback = str(answer.get("feedback") or answer.get("message") or "")
    else:
        raw = str(answer).strip()
        action = "approve" if raw.lower() in {"approve", "ok", "duyệt"} else "revise"
        feedback = "" if action == "approve" else raw
    if action not in {"approve", "revise"}:
        action = "revise"
    return {
        "review_action": action,
        "revision_request": feedback,
        "trace": [_event("hitl", "human", action, feedback or "Plan approved")],
    }


def route_after_review(state: TravelState) -> Literal["save", "revision_entry"]:
    return "save" if state.get("review_action") == "approve" else "revision_entry"


def _revision_queue(stale: dict[str, bool]) -> list[str]:
    queue: list[str] = []
    if stale["flight_search_stale"]:
        queue.append("flight")
    if stale["hotel_search_stale"]:
        queue.append("hotel")
    queue.append("planner")
    return queue


def _agent_node(agent: str) -> str:
    return {
        "flight": "swarm_flight",
        "hotel": "swarm_hotel",
        "planner": "swarm_planner",
    }[agent]


async def revision_entry_node(state: TravelState) -> Command:
    if state.get("revision_count", 0) >= settings.max_revisions:
        return Command(
            goto="fail",
            update={
                "status": "needs_human",
                "errors": ["Đã đạt số vòng chỉnh sửa tối đa."],
            },
        )
    before = live_call_count()
    intent = await asyncio.to_thread(
        parse_revision,
        state.get("revision_request", ""),
        state["trip_request"],
    )
    request, stale = apply_revision(
        state["trip_request"],
        intent,
        current_plan_over_budget=state["trip_plan"].budget.is_over_budget,
    )
    queue = _revision_queue(stale)
    target = _agent_node(queue[0])
    return Command(
        goto=target,
        update={
            "trip_request": normalize_request(request),
            "revision_intent": intent,
            "revision_queue": queue,
            "flight_search_stale": stale["flight_search_stale"],
            "hotel_search_stale": stale["hotel_search_stale"],
            "itinerary_stale": True,
            "phase": "revision",
            "revision_count": state.get("revision_count", 0) + 1,
            "metrics": _metrics(
                state,
                llm_calls=live_call_count() - before,
                handoffs=1,
                revisions=1,
            ),
            "trace": [
                _event(
                    "handoff",
                    "planner_agent",
                    f"handoff_to_{queue[0]}",
                    ", ".join(queue),
                )
            ],
        },
    )


def _next_handoff(queue: list[str], completed: str) -> tuple[list[str], str]:
    remaining = list(queue)
    if remaining and remaining[0] == completed:
        remaining.pop(0)
    next_agent = remaining[0] if remaining else "planner"
    return remaining, _agent_node(next_agent)


async def swarm_flight_node(state: TravelState) -> Command:
    result = await search_flights(state["trip_request"])
    remaining, target = _next_handoff(state.get("revision_queue", []), "flight")
    return Command(
        goto=target,
        update={
            "flight_options": result.results,
            "flight_search_stale": False,
            "revision_queue": remaining,
            "warnings": [result.warning] if result.warning else [],
            "metrics": _metrics(
                state,
                mcp_calls=1,
                external_search_calls=result.provider_call_count,
                handoffs=1,
            ),
            "trace": [_event("handoff", "flight_agent", f"handoff_to_{target}")],
        },
    )


async def swarm_hotel_node(state: TravelState) -> Command:
    result = await search_hotels(state["trip_request"])
    remaining, target = _next_handoff(state.get("revision_queue", []), "hotel")
    return Command(
        goto=target,
        update={
            "hotel_options": result.results,
            "hotel_search_stale": False,
            "revision_queue": remaining,
            "warnings": [result.warning] if result.warning else [],
            "metrics": _metrics(
                state,
                mcp_calls=1,
                external_search_calls=result.provider_call_count,
                handoffs=1,
            ),
            "trace": [_event("handoff", "hotel_agent", f"handoff_to_{target}")],
        },
    )


async def swarm_planner_node(state: TravelState) -> Command:
    before = live_call_count()
    plan = await asyncio.to_thread(
        build_plan,
        state["trip_request"],
        state["flight_options"],
        state["hotel_options"],
        current_plan=state.get("trip_plan"),
        revision_instruction=state.get("revision_request", ""),
    )
    return Command(
        goto="review",
        update={
            "trip_plan": plan,
            "itinerary_stale": False,
            "revision_queue": [],
            "phase": "review",
            "metrics": _metrics(
                state,
                llm_calls=live_call_count() - before,
                handoffs=1,
            ),
            "trace": [_event("handoff", "planner_agent", "handoff_to_human", "Revised plan ready")],
        },
    )


async def save_node(state: TravelState) -> dict[str, Any]:
    preferences = preferences_from_request(state["trip_request"])
    save_preferences(state.get("user_id", "demo-user"), preferences)
    return {
        "user_preferences": preferences,
        "status": "completed",
        "phase": "completed",
        "trace": [_event("node", "memory", "save", "Saved reusable preferences")],
    }


async def fail_node(state: TravelState) -> dict[str, Any]:
    return {
        "status": "needs_human",
        "phase": "failed",
        "trace": [_event("warning", "supervisor", "stop", "Workflow stopped safely")],
    }


def build_graph():
    builder = StateGraph(TravelState)
    builder.add_node("load_memory", load_memory_node)
    builder.add_node("parse_request", parse_request_node)
    builder.add_node("validate_request", validate_request_node)
    builder.add_node("clarify", clarification_node)
    builder.add_node("supervisor", supervisor_node)
    builder.add_node("flight_agent", flight_agent_node)
    builder.add_node("hotel_agent", hotel_agent_node)
    builder.add_node("planner_agent", planner_agent_node)
    builder.add_node("review", review_node)
    builder.add_node("revision_entry", revision_entry_node)
    builder.add_node("swarm_flight", swarm_flight_node)
    builder.add_node("swarm_hotel", swarm_hotel_node)
    builder.add_node("swarm_planner", swarm_planner_node)
    builder.add_node("save", save_node)
    builder.add_node("fail", fail_node)

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "parse_request")
    builder.add_edge("parse_request", "validate_request")
    builder.add_conditional_edges("validate_request", route_after_validation)
    builder.add_edge("clarify", "parse_request")
    builder.add_conditional_edges("supervisor", route_supervisor)
    builder.add_edge("flight_agent", "supervisor")
    builder.add_edge("hotel_agent", "supervisor")
    builder.add_edge("planner_agent", "supervisor")
    builder.add_conditional_edges("review", route_after_review)
    builder.add_edge("save", END)
    builder.add_edge("fail", END)
    return builder.compile(checkpointer=MemorySaver())


travel_graph = build_graph()
