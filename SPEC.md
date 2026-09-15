# Agentic Travel Planner — Swarm-first Capstone Specification

## 1. Goal

Build one medium-scope travel-planner demo that shows:

- direct multi-agent handoffs through a LangGraph Swarm;
- clarification and plan-review Human-in-the-Loop interrupts;
- remote MCP tool calls for live flight, hotel, and Google Maps place evidence;
- local MCP mock fallback for reliable offline demos;
- selective replanning, thread state, and lightweight preference memory;
- FastAPI backend and a thin Streamlit UI.

The product recommends only. It never books, pays, logs in, submits a booking form, or
claims that a fare/room is held.

### LLMOps implementation addendum (2026-09-12)

- Git YAML prompt artifacts for intake, revision intent and itinerary, with immutable versions,
  deployment aliases and optional Langfuse resolution. Remote failure uses local evidence-marked fallback.
- Planner v1 remains production; v2 is a staging candidate, not a proven improvement.
- Per-run prompt pinning and optional sticky planner A/B routing (off by default).
- Request/node/tool/generation observations; session = trip thread; separate run per resume/lookup.
- Response usage-based token/cost estimates, cached input accounting, unknown cost represented as null.
  Costs are LLM-only, exclude travel providers, and are separate from the travel budget.
- SSE POST start/resume routes emit live node/tool progress, then result/error; existing JSON APIs remain.
- Cloud and content capture are optional. Offline tests use mocks and an in-memory SDK exporter.
- One frozen-evidence comparison script supports at most two explicitly requested paid generations;
  automatic quality scoring, production experiment analysis and auto-promotion are out of scope.
- No durable tracing/checkpoint store, distributed execution lock or authentication added in this scope.
  See [the learning guide](docs/LEARN_THE_REPO.html) for design decisions and setup.

## 2. Confirmed decisions

- Repository: `agentic-travel-planner`.
- Orchestration: Swarm-first; no Hierarchical Supervisor.
- Backend: FastAPI; Swagger at `/docs`.
- Frontend: Streamlit; it never imports or executes the graph directly.
- LLM: configurable OpenAI model, default `gpt-5.4-mini`.
- Flight live source: Google Flights engine through SerpApi's official hosted MCP server.
- Hotel live source: Flightpowers Booking.com MCP connector listed on Glama.
- Failure/offline source: deterministic JSON through a local stdio MCP server.
- Currency: VND only in the MVP; no FX service.
- POI/itinerary: Google Maps evidence through the existing SerpApi MCP, with mock fallback.
- Extra lookup: flight booking options and hotel-by-name details, only on demand.

## 3. User flow

```text
request
  -> Intake Agent parses a typed TripRequest
  -> validation
       -> incomplete: clarification HITL -> Intake Agent
       -> ready: Swarm entry builds [flight, hotel, place, planner]
  -> Flight Agent -> SerpApi MCP or local mock MCP
       -> direct handoff
  -> Hotel Agent -> Booking MCP or local mock MCP
       -> direct handoff
  -> Place Agent -> SerpApi Google Maps or local mock MCP
       -> direct handoff
  -> Planner Agent -> itinerary + deterministic budget
       -> direct handoff to plan-review HITL
          -> approve -> save preferences -> END
          -> revise -> classify affected domains -> selective queue
                       -> affected specialist(s) -> Planner -> review
```

`swarm_entry` and `revision_entry` are deterministic queue-building nodes, not manager
agents. There is no central agent deciding after every specialist step.

## 4. Scope

The final plan contains:

1. flight choices with outbound/return segments, dates, times, stops, and honest price scope;
2. hotel choices with nightly/stay totals and an optional different hotel for each night;
3. day-by-day itinerary with named places, addresses, ratings, and Maps links when available;
4. deterministic flight, hotel, food, local transport, activity, and buffer line items;
5. total and remaining/over-budget values;
6. rationale, source warnings, timestamps, and links when available;
7. observable node/tool/handoff trace without hidden reasoning.

On demand, the user can:

- select a flight and fetch current seller/booking options;
- select a hotel and re-check that property by name;
- open a safe direct provider link manually.

Explicit non-goals: booking/payment, submitting provider POST forms, account automation,
email/calendar, production auth/deployment, RAG, scraping owned by this app, route
optimization, and multi-currency conversion.

## 5. Agent contracts

### Intake Agent

Parses only explicit constraints into `TripRequestPatch`, merges them with existing state,
then relies on deterministic normalization and validation. It never starts provider search.

### Flight Agent

Owns flight search and ranking. It converts city names to known IATA codes where possible,
calls the SerpApi MCP `search` tool with `engine=google_flights`, normalizes results, applies
hard constraints, then ranks directness, price, and duration.

### Hotel Agent

Owns hotel search and ranking. It calls Booking MCP `search_hotels`, validates its dynamic
payload, applies nightly-price/star/rating/area constraints, then ranks price and rating.

### Place Agent

Calls the same SerpApi MCP `search` tool with `engine=google_maps`, normalizes named places,
addresses, ratings, review counts, price level, and a safe Google Maps search URL. It performs
one broad destination lookup per plan instead of one paid request per itinerary activity.

### Planner Agent

Selects only from normalized flight/hotel/place evidence, drafts the itinerary, and calls the
deterministic budget service. It does not own an external travel-search tool.
The prompt is bounded to concise activities. A structured-output length failure falls back to
the deterministic narrative without retrying or failing the workflow.

## 6. Swarm handoff rules

Every agent node returns a LangGraph `Command(goto=...)`:

| Situation | Queue |
|---|---|
| Initial plan | `flight -> hotel -> place -> planner -> human` |
| Hotel-only revision | `hotel -> planner -> human` |
| Flight-only revision | `flight -> planner -> human` |
| Mixed flight/hotel revision | `flight -> hotel -> planner -> human` |
| Itinerary-only revision | `place -> planner -> human` |

Staleness flags preserve unaffected evidence. A budget-only revision reruns live evidence
only when the current plan violates the new budget and the user did not explicitly preserve
that domain.

## 7. MCP and provider contracts

### 7.1 SerpApi flight MCP

Connection:

```text
transport: streamable_http
URL: https://mcp.serpapi.com/mcp
Authorization: Bearer ${SERPAPI_API_KEY}
tool: search
```

Search arguments include `engine=google_flights`, departure/arrival IATA codes, outbound
date, optional return date, adults, VND, locale, stops, time window, and max price where
the user supplied those constraints.

Normalized fields include airline(s), flight number(s), times, duration, stops, price,
ephemeral `departure_token`/`booking_token`, carbon emissions, source URL, and observed time.
Tokens are stored in graph state but excluded from API/UI serialization.

For round trip, the initial result commonly has `departure_token`. Planning performs one
bounded follow-up for the top outbound flight to obtain return candidates and their
`booking_token`; each normalized option is therefore a complete outbound/return combination.
The on-demand booking endpoint then uses the selected combination's token to fetch sellers.

Google Flights normally reports one round-trip price. Per-leg prices remain null unless the
provider explicitly supplies them; the app never fabricates a 50/50 split for live fares.

Booking payload handling is deliberately read-only:

- seller, displayed price, baggage, flight numbers, and safe direct links may be returned;
- `post_data` is never returned to the UI and is never submitted;
- a seller requiring a form is marked `requires_provider_form=true`.

### 7.2 Flightpowers Booking hotel MCP

Default direct connection:

```text
transport: streamable_http
URL: https://hotels.flightpowers.com/mcp
x-rapidapi-key: ${RAPIDAPI_KEY}
tools: search_hotels, find_hotel_by_name
```

`BOOKING_MCP_URL` can instead point to the URL issued after adding the connector through
Glama Gateway. When the gateway manages credentials, a local RapidAPI key may be omitted.

The connector does not publish a fixed output schema, so normalization accepts a list or
common wrapper keys and keeps only records with a name and parseable price. Search inputs
use exact provider field names: `checkin_date`, `checkout_date`, `budget_per_night`.

### 7.3 SerpApi Google Maps MCP

The Place Agent reuses the SerpApi MCP connection and `search` tool with
`engine=google_maps`, `type=search`, and a destination query. It normalizes only evidence
returned by `local_results`/`place_results`: place name, category, address, rating, review
count, price level, and Maps URL. One search supplies a bounded candidate pool for the LLM.

### 7.4 Local fallback MCP

The backend spawns `python -m app.mcp.server` over stdio. It exposes:

- `search_flights_mock`;
- `search_hotels_mock`;
- `search_places_mock`;
- `flight_booking_options_mock`;
- `find_hotel_by_name_mock`.

Provider rules:

```text
USE_MOCK_TRAVEL_DATA=true -> local MCP mock
otherwise:
  flight -> SerpApi MCP -> local MCP mock on missing key/error/invalid payload
  hotel  -> Booking MCP -> local MCP mock on missing credential/error/invalid payload
  place  -> SerpApi Google Maps -> local MCP mock on missing key/error/invalid payload
```

A response is entirely live or entirely mock. No live and mock options are mixed.

## 8. HITL and memory

LangGraph `interrupt()` pauses at:

- critical-field clarification;
- final plan review.

FastAPI resumes the same checkpoint using `thread_id` and `Command(resume=...)`.
`MemorySaver` holds current workflow state. `data/user_memory.json` stores only stable
preferences such as travel pace and preferred hotel area. Current-trip input wins.

Critical fields: origin, destination, departure date, return date or duration, traveler
count, and total budget.

Khi có cả departure date và return date, hệ thống luôn tự tính `duration_days` theo số ngày
bao gồm cả hai đầu. Duration do model/user cung cấp không được dùng để tạo clarification nếu
nó lệch với hai ngày rõ ràng.

## 9. API contract

| Method | Path | Side effect |
|---|---|---|
| `POST` | `/api/trips/start` | Creates an in-memory workflow thread. |
| `POST` | `/api/trips/{thread_id}/resume` | Resumes a paused graph. |
| `GET` | `/api/trips/{thread_id}` | Read-only state snapshot. |
| `POST` | `/api/trips/{thread_id}/flight-booking-options` | External read-only lookup. |
| `POST` | `/api/trips/{thread_id}/hotel-details` | External read-only lookup. |

The two lookup endpoints accept only an option ID already present in that thread. Clients
cannot inject arbitrary provider tokens, hotel names, or booking form payloads.

## 10. Cost and safety

- `MAX_REVISIONS=3` prevents open-ended correction loops.
- `MAX_EXTERNAL_SEARCH_CALLS_PER_PLAN=6` is checked before live searches/lookups.
- Initial live planning targets one SerpApi call and one Booking call.
- Booking options may consume one or two additional SerpApi calls only after a click.
- Mock-mode tests consume zero OpenAI/provider calls.
- Provider exceptions fall back rather than crash the graph.
- Keys, ephemeral tokens, raw provider payloads, and hidden reasoning are not shown in UI.
- Prices are timestamps, not guarantees; hotel details are refreshed rather than cached.

## 11. Definition of done

- FastAPI, Swagger, and Streamlit run with documented commands.
- Complete and incomplete requests reach the correct HITL state.
- Initial trace shows Swarm -> Flight -> Hotel -> Planner -> Human, with no Supervisor.
- Both travel specialists cross an MCP boundary.
- Live responses identify `serpapi` or `booking`; fallback identifies `mock` with warning.
- Approval completes the thread and saves stable preferences.
- Selective revisions do not rerun unaffected searches.
- Read-only flight booking options and hotel detail work in mock mode and live when configured.
- No endpoint books, pays, submits a provider form, or accepts raw provider tokens.
- Budget calculations are deterministic and tested.
- Test suite makes no real OpenAI, SerpApi, or Booking calls.

## 12. Provider references

- [SerpApi Google Flights API](https://serpapi.com/google-flights-api)
- [SerpApi Google Flights results](https://serpapi.com/google-flights-results)
- [SerpApi booking options](https://serpapi.com/google-flights-booking-options)
- [Official SerpApi MCP server](https://github.com/serpapi/serpapi-mcp)
- [Flightpowers Booking connector on Glama](https://glama.ai/mcp/connectors/com.flightpowers/booking)
- [Flightpowers travel-agent-skills repository](https://github.com/mtnrabi/travel-agent-skills)
