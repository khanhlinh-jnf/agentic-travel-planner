"""FastAPI contracts and helpers for starting/resuming durable LangGraph threads."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.graph import travel_graph
from app.schemas import WorkflowMetrics

router = APIRouter(prefix="/api/trips", tags=["travel-planner"])


class StartTripRequest(BaseModel):
    message: str = Field(min_length=3, max_length=4000)
    user_id: str = Field(default="demo-user", min_length=1, max_length=100)


class ResumeTripRequest(BaseModel):
    action: str | None = None
    message: str = ""


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _interrupt_payload(result: dict[str, Any]) -> dict[str, Any] | None:
    interrupts = result.get("__interrupt__", ())
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return _jsonable(value)


def _response(thread_id: str, result: dict[str, Any]) -> dict[str, Any]:
    state = {key: _jsonable(value) for key, value in result.items() if key != "__interrupt__"}
    pending = _interrupt_payload(result)
    if pending:
        status = "awaiting_input"
    else:
        status = state.get("status", "running")
    return {
        "thread_id": thread_id,
        "status": status,
        "phase": state.get("phase", "unknown"),
        "interrupt": pending,
        "state": state,
    }


@router.post("/start")
async def start_trip(payload: StartTripRequest) -> dict[str, Any]:
    thread_id = str(uuid4())
    initial_state = {
        "thread_id": thread_id,
        "user_id": payload.user_id,
        "raw_request": payload.message,
        "errors": [],
        "warnings": [],
        "trace": [],
        "metrics": WorkflowMetrics(),
    }
    try:
        result = await travel_graph.ainvoke(initial_state, config=_config(thread_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Workflow failed: {exc}") from exc
    return _response(thread_id, result)


@router.post("/{thread_id}/resume")
async def resume_trip(thread_id: str, payload: ResumeTripRequest) -> dict[str, Any]:
    snapshot = await travel_graph.aget_state(_config(thread_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Trip thread not found")

    resume_value: dict[str, str] | str
    if payload.action:
        resume_value = {"action": payload.action, "feedback": payload.message}
    else:
        resume_value = payload.message
    try:
        result = await travel_graph.ainvoke(Command(resume=resume_value), config=_config(thread_id))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Workflow resume failed: {exc}") from exc
    return _response(thread_id, result)


@router.get("/{thread_id}")
async def get_trip(thread_id: str) -> dict[str, Any]:
    snapshot = await travel_graph.aget_state(_config(thread_id))
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Trip thread not found")
    result = dict(snapshot.values)
    if snapshot.interrupts:
        result["__interrupt__"] = snapshot.interrupts
    return _response(thread_id, result)
