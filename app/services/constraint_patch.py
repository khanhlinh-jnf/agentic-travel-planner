"""Merge revision patches and calculate selective-replanning staleness."""

from __future__ import annotations

from app.schemas import RevisionIntent, TripRequest

FLIGHT_FIELDS = {
    "origin",
    "destination",
    "departure_date",
    "return_date",
    "duration_days",
    "travelers",
    "flight_preferences",
}
HOTEL_FIELDS = {
    "destination",
    "departure_date",
    "return_date",
    "duration_days",
    "travelers",
    "hotel_preferences",
}
ITINERARY_FIELDS = {
    "destination",
    "departure_date",
    "return_date",
    "duration_days",
    "interests",
    "travel_pace",
    "free_text_constraints",
}


def apply_revision(
    current: TripRequest, intent: RevisionIntent, *, current_plan_over_budget: bool = False
) -> tuple[TripRequest, dict[str, bool]]:
    patch = intent.patch.model_dump(exclude_none=True, exclude={"date_is_ambiguous"})
    merged = current.model_dump()
    merged.update(patch)
    request = TripRequest.model_validate(merged)
    changed = set(patch)
    domains = set(intent.affected_domains)

    flight_stale = bool(changed & FLIGHT_FIELDS or "flight" in domains)
    hotel_stale = bool(changed & HOTEL_FIELDS or "hotel" in domains)
    itinerary_stale = bool(changed & ITINERARY_FIELDS or "itinerary" in domains)

    if intent.preserve_flight:
        flight_stale = False
    if intent.preserve_hotel:
        hotel_stale = False

    if changed == {"total_budget"} or domains == {"budget"}:
        flight_stale = current_plan_over_budget and not intent.preserve_flight
        hotel_stale = current_plan_over_budget and not intent.preserve_hotel
        itinerary_stale = True

    return request, {
        "flight_search_stale": flight_stale,
        "hotel_search_stale": hotel_stale,
        "itinerary_stale": itinerary_stale or flight_stale or hotel_stale,
    }

