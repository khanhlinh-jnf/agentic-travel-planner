"""LangGraph state for the complete planning and revision workflow."""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from app.schemas import (
    FlightOption,
    HotelOption,
    PlaceOption,
    RevisionIntent,
    TraceEvent,
    TripPlan,
    TripRequest,
    UserPreferences,
    WorkflowMetrics,
)


class TravelState(TypedDict, total=False):
    thread_id: str
    user_id: str
    raw_request: str
    trip_request: TripRequest
    user_preferences: UserPreferences
    missing_fields: list[str]
    validation_errors: list[str]
    clarification_response: str
    date_is_ambiguous: bool

    flight_options: list[FlightOption]
    hotel_options: list[HotelOption]
    place_options: list[PlaceOption]
    trip_plan: TripPlan | None

    flight_search_stale: bool
    hotel_search_stale: bool
    place_search_stale: bool
    itinerary_stale: bool
    review_action: Literal["approve", "revise"] | None
    revision_request: str
    revision_intent: RevisionIntent | None
    agent_queue: list[Literal["flight", "hotel", "place", "planner"]]
    active_agent: str | None
    revision_count: int

    phase: str
    status: str
    errors: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]
    trace: Annotated[list[TraceEvent], operator.add]
    metrics: WorkflowMetrics
