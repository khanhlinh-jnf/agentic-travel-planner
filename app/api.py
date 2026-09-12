"""FastAPI contracts and helpers for starting/resuming durable LangGraph threads."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.config import settings
from app.graph import travel_graph
from app.mcp.providers.service import get_flight_booking_options, get_hotel_detail
from app.schemas import FlightOption, HotelOption, TripRequest, WorkflowMetrics
from app.services import quota

router = APIRouter(prefix="/api/trips", tags=["travel-planner"])


class StartTripRequest(BaseModel):
    message: str = Field(min_length=3, max_length=4000)
    user_id: str = Field(default="demo-user", min_length=1, max_length=100)


class ResumeTripRequest(BaseModel):
    action: str | None = None
    message: str = ""
    selected_flight_id: str | None = None
    hotel_selection_ids: list[str] = Field(default_factory=list)


class OptionDetailRequest(BaseModel):
    option_id: str = Field(min_length=1, max_length=100)


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

    resume_value: dict[str, Any] | str
    if payload.action:
        resume_value = {
            "action": payload.action,
            "feedback": payload.message,
            "selected_flight_id": payload.selected_flight_id,
            "hotel_selection_ids": payload.hotel_selection_ids,
        }
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


def _trip_context(snapshot: Any) -> tuple[dict[str, Any], TripRequest]:
    if not snapshot.values:
        raise HTTPException(status_code=404, detail="Trip thread not found")
    values = dict(snapshot.values)
    request = values.get("trip_request")
    if not isinstance(request, TripRequest):
        request = TripRequest.model_validate(request)
    return values, request


def _lookup_response(thread_id: str, result: Any) -> dict[str, Any]:
    quota.record(thread_id, result.provider_call_count)
    response = result.model_dump(mode="json")
    response["external_search_calls_in_thread"] = quota.total(thread_id)
    return response


@router.post("/{thread_id}/flight-booking-options")
async def flight_booking_options(
    thread_id: str, payload: OptionDetailRequest
) -> dict[str, Any]:
    snapshot = await travel_graph.aget_state(_config(thread_id))
    values, request = _trip_context(snapshot)
    flights = [
        option if isinstance(option, FlightOption) else FlightOption.model_validate(option)
        for option in values.get("flight_options", [])
    ]
    flight = next((option for option in flights if option.id == payload.option_id), None)
    if not flight:
        raise HTTPException(status_code=404, detail="Flight option not found")
    if not all((request.origin, request.destination, request.departure_date, request.travelers)):
        raise HTTPException(status_code=409, detail="Trip request is incomplete")
    calls = quota.total(thread_id)
    minimum_calls = 1 if flight.booking_token else 2
    allow_live = calls + minimum_calls <= settings.max_external_search_calls_per_plan
    result = await get_flight_booking_options(
        flight=flight,
        origin=request.origin,
        destination=request.destination,
        departure_date=request.departure_date,
        return_date=request.return_date,
        adults=request.travelers,
        allow_live=allow_live,
    )
    return _lookup_response(thread_id, result)


@router.post("/{thread_id}/hotel-details")
async def hotel_details(thread_id: str, payload: OptionDetailRequest) -> dict[str, Any]:
    snapshot = await travel_graph.aget_state(_config(thread_id))
    values, request = _trip_context(snapshot)
    hotels = [
        option if isinstance(option, HotelOption) else HotelOption.model_validate(option)
        for option in values.get("hotel_options", [])
    ]
    hotel = next((option for option in hotels if option.id == payload.option_id), None)
    if not hotel:
        raise HTTPException(status_code=404, detail="Hotel option not found")
    if not all(
        (request.destination, request.departure_date, request.return_date, request.travelers)
    ):
        raise HTTPException(status_code=409, detail="Trip request is incomplete")
    calls = quota.total(thread_id)
    result = await get_hotel_detail(
        hotel=hotel,
        destination=request.destination,
        check_in_date=request.departure_date,
        check_out_date=request.return_date,
        adults=request.travelers,
        allow_live=calls < settings.max_external_search_calls_per_plan,
    )
    return _lookup_response(thread_id, result)
