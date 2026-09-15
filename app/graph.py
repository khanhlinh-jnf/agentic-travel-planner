"""One Swarm-first LangGraph workflow for initial planning and selective revision."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.agents.planner import apply_plan_selections, build_plan
from app.agents.specialists import search_flights, search_hotels, search_places
from app.config import settings
from app.memory.store import (
    apply_preferences,
    load_preferences,
    preferences_from_request,
    save_preferences,
)
from app.schemas import TraceEvent, TripRequest, TripRequestPatch, WorkflowMetrics
from app.services import quota
from app.services.constraint_patch import apply_revision
from app.services.llm import live_call_count, parse_revision, parse_trip_request
from app.services.observability import instrument_node
from app.services.validation import clarification_questions, normalize_request, validate_request
from app.state import TravelState

AgentName = Literal["flight", "hotel", "place", "planner"]


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
    has_return_date = "return_date" in updates
    has_duration = "duration_days" in updates
    for nested in ("flight_preferences", "hotel_preferences"):
        if nested in updates:
            merged_nested = values[nested]
            merged_nested.update(updates.pop(nested))
            values[nested] = merged_nested
    values.update(updates)
    if has_return_date and not has_duration:
        values["duration_days"] = None
    elif has_duration and not has_return_date:
        values["return_date"] = None
    return normalize_request(TripRequest.model_validate(values))


def _agent_node(agent: AgentName) -> str:
    return {
        "flight": "flight_agent",
        "hotel": "hotel_agent",
        "place": "place_agent",
        "planner": "planner_agent",
    }[agent]


def _next_handoff(queue: list[AgentName], completed: AgentName) -> tuple[list[AgentName], str]:
    remaining = list(queue)
    if remaining and remaining[0] == completed:
        remaining.pop(0)
    if not remaining:
        remaining = ["planner"] if completed != "planner" else []
    return remaining, _agent_node(remaining[0]) if remaining else "review"


def _live_search_allowed(state: TravelState) -> bool:
    return quota.total(state.get("thread_id", "")) < settings.max_external_search_calls_per_plan


async def load_memory_node(state: TravelState) -> dict[str, Any]:
    user_id = state.get("user_id", "demo-user")
    quota.reset(state.get("thread_id", ""))
    return {
        "user_preferences": load_preferences(user_id),
        "metrics": state.get("metrics") or WorkflowMetrics(),
        "status": "running",
        "phase": "intake",
        "flight_search_stale": True,
        "hotel_search_stale": True,
        "place_search_stale": True,
        "itinerary_stale": True,
        "agent_queue": [],
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


def route_after_validation(state: TravelState) -> Literal["clarify", "swarm_entry"]:
    needs_input = state.get("missing_fields") or state.get("validation_errors")
    return "clarify" if needs_input else "swarm_entry"


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


async def swarm_entry_node(state: TravelState) -> Command:
    queue: list[AgentName] = []
    if state.get("flight_search_stale", True) or not state.get("flight_options"):
        queue.append("flight")
    if state.get("hotel_search_stale", True) or not state.get("hotel_options"):
        queue.append("hotel")
    if state.get("place_search_stale", True) or not state.get("place_options"):
        queue.append("place")
    queue.append("planner")
    target = _agent_node(queue[0])
    return Command(
        goto=target,
        update={
            "agent_queue": queue,
            "active_agent": queue[0],
            "metrics": _metrics(state, handoffs=1),
            "trace": [
                _event("handoff", "swarm", f"handoff_to_{queue[0]}", ", ".join(queue))
            ],
        },
    )


async def flight_agent_node(state: TravelState) -> Command:
    try:
        result = await search_flights(
            state["trip_request"], allow_live=_live_search_allowed(state)
        )
        if not result.results:
            raise ValueError("No flight options returned")
    except Exception as exc:
        return Command(
            goto="fail",
            update={
                "status": "needs_human",
                "errors": [f"Flight Agent failed: {exc}"],
                "trace": [_event("warning", "flight_agent", "failed", str(exc))],
            },
        )
    remaining, target = _next_handoff(state.get("agent_queue", []), "flight")
    quota.record(state.get("thread_id", ""), result.provider_call_count)
    return Command(
        goto=target,
        update={
            "flight_options": result.results,
            "flight_search_stale": False,
            "agent_queue": remaining,
            "active_agent": remaining[0] if remaining else None,
            "warnings": [result.warning] if result.warning else [],
            "metrics": _metrics(
                state,
                mcp_calls=1,
                external_search_calls=result.provider_call_count,
                handoffs=1,
            ),
            "trace": [
                _event(
                    "tool",
                    "flight_agent",
                    "search_flights",
                    f"Received {len(result.results)} options from {result.source}",
                ),
                _event("handoff", "flight_agent", f"handoff_to_{remaining[0]}"),
            ],
        },
    )


async def hotel_agent_node(state: TravelState) -> Command:
    try:
        result = await search_hotels(
            state["trip_request"], allow_live=_live_search_allowed(state)
        )
        if not result.results:
            raise ValueError("No hotel options returned")
    except Exception as exc:
        return Command(
            goto="fail",
            update={
                "status": "needs_human",
                "errors": [f"Hotel Agent failed: {exc}"],
                "trace": [_event("warning", "hotel_agent", "failed", str(exc))],
            },
        )
    remaining, target = _next_handoff(state.get("agent_queue", []), "hotel")
    quota.record(state.get("thread_id", ""), result.provider_call_count)
    return Command(
        goto=target,
        update={
            "hotel_options": result.results,
            "hotel_search_stale": False,
            "agent_queue": remaining,
            "active_agent": remaining[0] if remaining else None,
            "warnings": [result.warning] if result.warning else [],
            "metrics": _metrics(
                state,
                mcp_calls=1,
                external_search_calls=result.provider_call_count,
                handoffs=1,
            ),
            "trace": [
                _event(
                    "tool",
                    "hotel_agent",
                    "search_hotels",
                    f"Received {len(result.results)} options from {result.source}",
                ),
                _event("handoff", "hotel_agent", f"handoff_to_{remaining[0]}"),
            ],
        },
    )


async def place_agent_node(state: TravelState) -> Command:
    try:
        result = await search_places(
            state["trip_request"], allow_live=_live_search_allowed(state)
        )
        if not result.results:
            raise ValueError("No place options returned")
    except Exception as exc:
        return Command(
            goto="fail",
            update={
                "status": "needs_human",
                "errors": [f"Place Agent failed: {exc}"],
                "trace": [_event("warning", "place_agent", "failed", str(exc))],
            },
        )
    remaining, target = _next_handoff(state.get("agent_queue", []), "place")
    quota.record(state.get("thread_id", ""), result.provider_call_count)
    return Command(
        goto=target,
        update={
            "place_options": result.results,
            "place_search_stale": False,
            "agent_queue": remaining,
            "active_agent": remaining[0] if remaining else None,
            "warnings": [result.warning] if result.warning else [],
            "metrics": _metrics(
                state,
                mcp_calls=1,
                external_search_calls=result.provider_call_count,
                handoffs=1,
            ),
            "trace": [
                _event(
                    "tool",
                    "place_agent",
                    "search_places",
                    f"Received {len(result.results)} options from {result.source}",
                ),
                _event("handoff", "place_agent", f"handoff_to_{remaining[0]}"),
            ],
        },
    )


async def planner_agent_node(state: TravelState) -> Command:
    before = live_call_count()
    try:
        plan = await asyncio.to_thread(
            build_plan,
            state["trip_request"],
            state["flight_options"],
            state["hotel_options"],
            state.get("place_options", []),
            current_plan=state.get("trip_plan"),
            revision_instruction=state.get("revision_request", ""),
        )
        plan = plan.model_copy(
            update={"warnings": list(dict.fromkeys([*state.get("warnings", []), *plan.warnings]))}
        )
    except Exception as exc:
        return Command(
            goto="fail",
            update={
                "status": "needs_human",
                "errors": [f"Planner Agent failed: {exc}"],
                "trace": [_event("warning", "planner_agent", "failed", str(exc))],
            },
        )
    return Command(
        goto="review",
        update={
            "trip_plan": plan,
            "itinerary_stale": False,
            "agent_queue": [],
            "active_agent": None,
            "phase": "review",
            "metrics": _metrics(
                state,
                llm_calls=live_call_count() - before,
                handoffs=1,
            ),
            "trace": [
                _event(
                    "handoff",
                    "planner_agent",
                    "handoff_to_human",
                    "Plan ready for review",
                )
            ],
        },
    )


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
    update: dict[str, Any] = {
        "review_action": action,
        "revision_request": feedback,
        "trace": [_event("hitl", "human", action, feedback or "Plan approved")],
    }
    if action == "approve" and isinstance(answer, dict):
        update["trip_plan"] = apply_plan_selections(
            state["trip_plan"],
            state["trip_request"],
            state.get("flight_options", []),
            state.get("hotel_options", []),
            selected_flight_id=answer.get("selected_flight_id"),
            hotel_selection_ids=answer.get("hotel_selection_ids"),
        )
    return update


def route_after_review(state: TravelState) -> Literal["save", "revision_entry"]:
    return "save" if state.get("review_action") == "approve" else "revision_entry"


def _revision_queue(stale: dict[str, bool]) -> list[AgentName]:
    queue: list[AgentName] = []
    if stale["flight_search_stale"]:
        queue.append("flight")
    if stale["hotel_search_stale"]:
        queue.append("hotel")
    if stale["place_search_stale"]:
        queue.append("place")
    queue.append("planner")
    return queue


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
    revision_text = state.get("revision_request", "").strip()
    intent = await asyncio.to_thread(
        parse_revision,
        revision_text,
        state["trip_request"],
    )
    # Free-form review feedback must always reach the Planner. The specialist queue
    # remains narrow (for example hotel-only), while the Planner still receives the
    # exact instruction and regenerates the visible plan from the current evidence.
    if revision_text and not intent.itinerary_instruction:
        intent = intent.model_copy(
            update={
                "itinerary_instruction": revision_text,
            }
        )
    request, stale = apply_revision(
        state["trip_request"],
        intent,
        current_plan_over_budget=state["trip_plan"].budget.is_over_budget,
    )
    queue = _revision_queue(stale)
    quota.reset(state.get("thread_id", ""))
    return Command(
        goto=_agent_node(queue[0]),
        update={
            "trip_request": normalize_request(request),
            "revision_intent": intent,
            "agent_queue": queue,
            "active_agent": queue[0],
            "flight_search_stale": stale["flight_search_stale"],
            "hotel_search_stale": stale["hotel_search_stale"],
            "place_search_stale": stale["place_search_stale"],
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
                    "review",
                    f"handoff_to_{queue[0]}",
                    ", ".join(queue),
                ),
                _event(
                    "node",
                    "revision_parser",
                    "classified_feedback",
                    f"domains={','.join(intent.affected_domains)}; queue={','.join(queue)}",
                ),
            ],
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
        "trace": [_event("warning", "workflow", "stop", "Workflow stopped safely")],
    }


def build_graph():
    builder = StateGraph(TravelState)
    for name, node in [
        ("load_memory", load_memory_node),
        ("parse_request", parse_request_node),
        ("validate_request", validate_request_node),
        ("clarify", clarification_node),
        ("swarm_entry", swarm_entry_node),
        ("flight_agent", flight_agent_node),
        ("hotel_agent", hotel_agent_node),
        ("place_agent", place_agent_node),
        ("planner_agent", planner_agent_node),
        ("review", review_node),
        ("revision_entry", revision_entry_node),
        ("save", save_node),
        ("fail", fail_node),
    ]:
        builder.add_node(name, instrument_node(name, node))

    builder.add_edge(START, "load_memory")
    builder.add_edge("load_memory", "parse_request")
    builder.add_edge("parse_request", "validate_request")
    builder.add_conditional_edges("validate_request", route_after_validation)
    builder.add_edge("clarify", "parse_request")
    builder.add_conditional_edges("review", route_after_review)
    builder.add_edge("save", END)
    builder.add_edge("fail", END)
    return builder.compile(checkpointer=MemorySaver())


travel_graph = build_graph()
