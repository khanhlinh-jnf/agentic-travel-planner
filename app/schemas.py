"""Domain schemas shared by FastAPI, LangGraph, MCP, and Streamlit."""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FlightPreferences(StrictModel):
    departure_after: time | None = None
    departure_before: time | None = None
    airline: str | None = None
    prefer_direct: bool = True
    max_budget: int | None = Field(default=None, ge=0)


class HotelPreferences(StrictModel):
    max_nightly_price: int | None = Field(default=None, ge=0)
    preferred_area: str | None = None
    min_stars: int | None = Field(default=None, ge=1, le=5)
    min_rating: float | None = Field(default=None, ge=0, le=5)


class TripRequest(StrictModel):
    origin: str | None = None
    destination: str | None = None
    departure_date: date | None = None
    return_date: date | None = None
    duration_days: int | None = Field(default=None, ge=1, le=30)
    travelers: int | None = Field(default=None, ge=1, le=12)
    total_budget: int | None = Field(default=None, ge=0)
    currency: Literal["VND"] = "VND"
    interests: list[str] = Field(default_factory=list)
    travel_pace: Literal["relaxed", "balanced", "packed"] | None = None
    flight_preferences: FlightPreferences = Field(default_factory=FlightPreferences)
    hotel_preferences: HotelPreferences = Field(default_factory=HotelPreferences)
    free_text_constraints: list[str] = Field(default_factory=list)


class TripRequestPatch(StrictModel):
    origin: str | None = None
    destination: str | None = None
    departure_date: date | None = None
    return_date: date | None = None
    duration_days: int | None = Field(default=None, ge=1, le=30)
    travelers: int | None = Field(default=None, ge=1, le=12)
    total_budget: int | None = Field(default=None, ge=0)
    interests: list[str] | None = None
    travel_pace: Literal["relaxed", "balanced", "packed"] | None = None
    flight_preferences: FlightPreferences | None = None
    hotel_preferences: HotelPreferences | None = None
    free_text_constraints: list[str] | None = None
    date_is_ambiguous: bool = False


class FlightOption(StrictModel):
    id: str
    airline: str
    flight_number: str | None = None
    origin: str
    destination: str
    departure_date: date
    return_date: date | None = None
    departure_time: time | None = None
    arrival_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=0)
    stops: int | None = Field(default=None, ge=0)
    price_per_person: int = Field(ge=0)
    total_price: int = Field(ge=0)
    currency: Literal["VND"] = "VND"
    source: Literal["web", "mock"]
    source_url: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_estimate: bool = True


class HotelOption(StrictModel):
    id: str
    name: str
    destination: str
    area: str | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    stars: int | None = Field(default=None, ge=1, le=5)
    nightly_price: int = Field(ge=0)
    nights: int = Field(ge=1)
    total_price: int = Field(ge=0)
    currency: Literal["VND"] = "VND"
    amenities: list[str] = Field(default_factory=list)
    source: Literal["web", "mock"]
    source_url: str | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_estimate: bool = True


class FlightSearchResult(StrictModel):
    source: Literal["web", "mock"]
    warning: str | None = None
    results: list[FlightOption] = Field(default_factory=list)
    provider_call_count: int = Field(default=0, ge=0)


class HotelSearchResult(StrictModel):
    source: Literal["web", "mock"]
    warning: str | None = None
    results: list[HotelOption] = Field(default_factory=list)
    provider_call_count: int = Field(default=0, ge=0)


class ItineraryActivity(StrictModel):
    time_of_day: Literal["morning", "afternoon", "evening", "flexible"]
    title: str
    description: str
    estimated_cost_per_person: int = Field(default=0, ge=0)
    needs_verification: bool = False


class ItineraryDay(StrictModel):
    day: int = Field(ge=1)
    date: date
    title: str
    activities: list[ItineraryActivity] = Field(default_factory=list)


class PlanNarrative(StrictModel):
    itinerary: list[ItineraryDay]
    rationale: str
    warnings: list[str] = Field(default_factory=list)


class BudgetSummary(StrictModel):
    flight: int
    hotel: int
    activities: int
    food: int
    local_transport: int
    buffer: int
    estimated_total: int
    total_budget: int
    remaining_budget: int
    is_over_budget: bool
    currency: Literal["VND"] = "VND"


class TripPlan(StrictModel):
    selected_flight_id: str
    selected_hotel_id: str
    recommended_flight: FlightOption
    alternative_flights: list[FlightOption] = Field(default_factory=list)
    recommended_hotel: HotelOption
    alternative_hotels: list[HotelOption] = Field(default_factory=list)
    itinerary: list[ItineraryDay]
    budget: BudgetSummary
    rationale: str
    warnings: list[str] = Field(default_factory=list)


class RevisionIntent(StrictModel):
    affected_domains: list[Literal["flight", "hotel", "itinerary", "budget"]]
    patch: TripRequestPatch = Field(default_factory=TripRequestPatch)
    preserve_flight: bool = False
    preserve_hotel: bool = False
    itinerary_instruction: str | None = None


class TraceEvent(StrictModel):
    kind: Literal["node", "route", "tool", "handoff", "hitl", "warning"]
    actor: str
    action: str
    detail: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorkflowMetrics(StrictModel):
    llm_calls: int = 0
    mcp_calls: int = 0
    external_search_calls: int = 0
    handoffs: int = 0
    supervisor_steps: int = 0
    revisions: int = 0


class UserPreferences(StrictModel):
    avoid_early_flights: bool = False
    preferred_departure_after: time | None = None
    travel_pace: Literal["relaxed", "balanced", "packed"] | None = None
    preferred_hotel_area: str | None = None
    preferred_min_stars: int | None = Field(default=None, ge=1, le=5)

