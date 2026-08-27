"""Deterministic Hierarchical Supervisor routing."""

from __future__ import annotations

from app.config import settings
from app.state import TravelState


def decide_next(state: TravelState) -> str:
    if state.get("status") == "needs_human":
        return "fail"
    if state.get("supervisor_steps", 0) >= settings.max_supervisor_steps:
        return "fail"
    if state.get("flight_search_stale", True) or not state.get("flight_options"):
        return "flight"
    if state.get("hotel_search_stale", True) or not state.get("hotel_options"):
        return "hotel"
    if state.get("itinerary_stale", True) or not state.get("trip_plan"):
        return "planner"
    return "review"
