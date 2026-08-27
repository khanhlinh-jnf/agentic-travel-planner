# Agentic Travel Planner — Capstone Specification

## 1. Goal

Build one focused travel-planner experience that demonstrates:

- a Hierarchical LangGraph flow for the initial plan;
- clarification and plan-review Human-in-the-Loop interrupts;
- a direct-handoff Swarm for revisions;
- a custom Travel Search MCP server;
- selective replanning and deterministic budget calculation;
- thread state plus lightweight cross-session user preferences;
- FastAPI as the backend and a thin Streamlit chat UI.

The product plans and recommends. It never books, pays, logs into travel sites, or
claims guaranteed availability.

## 2. Confirmed product decisions

- Repository: `agentic-travel-planner` (public GitHub repository).
- Backend: FastAPI, including Swagger at `/docs`.
- Frontend: Streamlit; it calls FastAPI over HTTP and never runs the graph directly.
- Model: configurable, default `gpt-5.4-mini`.
- Live search: OpenAI Responses API Web Search, called only inside the MCP provider.
- Offline/failure path: deterministic mock flight and hotel JSON.
- Currency: VND only for the MVP; no FX integration.
- Destination knowledge: model knowledge, no RAG or POI vector database.

## 3. User workflow

```text
request
  -> structured intake
  -> validate critical fields
  -> clarification HITL when incomplete
  -> Hierarchical Supervisor
       -> Flight Agent -> MCP search_flights
       -> Hotel Agent  -> MCP search_hotels
       -> Planner Agent -> itinerary + deterministic budget
  -> plan-review HITL
       -> approve -> save stable preferences -> END
       -> revise  -> Revision Swarm
                       Planner -> Flight/Hotel -> Planner
                    -> review HITL again
```

Critical fields are origin, destination, departure date, return date or duration,
traveler count, and total budget.

## 4. Scope

The final plan contains:

1. one recommended and up to three alternative flights;
2. one recommended and up to three alternative hotels;
3. a day-by-day itinerary;
4. deterministic flight, hotel, food, local transport, activity, and buffer costs;
5. total and remaining/over-budget values;
6. rationale, data-source warnings, and source links where available;
7. an observable node/tool/handoff trace without chain-of-thought.

Explicit non-goals: real booking, payment, scraping, account automation, email,
calendar integration, auth, production deployment, fine-tuning, RAG, route optimization,
and multi-currency conversion.

## 5. Cost-aware orchestration

The Supervisor is deterministic and state-driven. This keeps the hierarchy visible while
avoiding repeated LLM routing calls. Ranking and all money arithmetic are deterministic.

A normal live initial plan targets four OpenAI requests:

1. structured intake parse;
2. flight Web Search inside MCP;
3. hotel Web Search inside MCP;
4. structured itinerary draft.

Mock-mode tests make zero OpenAI requests. Web Search is capped and any provider exception
falls back to mock instead of escaping into the graph.

## 6. Agent boundaries

### Supervisor

Routes only. It sees state but has no external tools and never invents travel data.

### Flight Agent

Owns only `search_flights`, filters hard constraints, and ranks directness, schedule,
duration, then price.

### Hotel Agent

Owns only `search_hotels`, filters nightly budget/area/star preferences, then ranks price
and rating.

### Planner Agent

Selects from normalized evidence, drafts the itinerary, explains trade-offs, and invokes
the deterministic budget service. It never invents live flight/hotel prices.

## 7. Travel MCP contract

The stdio MCP server exposes exactly two tools:

- `search_flights(origin, destination, departure_date, return_date, adults, currency)`
- `search_hotels(destination, check_in_date, check_out_date, adults, currency, max_price)`

The provider order is:

```text
USE_MOCK_TRAVEL_DATA=true -> mock
otherwise -> OpenAI Web Search -> validate evidence -> mock on failure
```

`source="web"` is allowed only when the response contains an actual Web Search call and
source URLs. Live fields not present in public pages remain `null`; they are never inferred.
Live and mock options are never mixed in one tool response.

## 8. HITL and memory

LangGraph `interrupt()` pauses for:

- missing-field clarification;
- approval or natural-language revision of a completed draft.

FastAPI resumes the same graph using `thread_id` and `Command(resume=...)`.
`MemorySaver` holds workflow state locally. `data/user_memory.json` stores only stable
preferences such as avoiding early flights; current-trip instructions always win.

## 9. Revision Swarm

The revision parser produces a structured patch and affected domains. Agents hand off
directly with LangGraph `Command` and no second Supervisor.

- hotel-only revision: Planner -> Hotel -> Planner; no flight search;
- flight-only revision: Planner -> Flight -> Planner; no hotel search;
- mixed revision: Planner -> Flight -> Hotel -> Planner;
- itinerary-only revision: Planner only.

Search-staleness flags protect unaffected state. A budget-only revision recalculates first
and searches again only when the current plan violates the new budget.

## 10. Date and evidence rules

- Internally use ISO `YYYY-MM-DD` dates.
- Reject impossible and past dates.
- Clarify an omitted year when multiple interpretations are reasonable.
- Derive return date from an unambiguous departure date and duration.
- Treat attraction knowledge as non-realtime.
- Do not claim current hours, closures, ticket prices, flight/hotel availability, or final
  checkout prices without tool evidence.

## 11. Safety limits

- `MAX_SUPERVISOR_STEPS = 8`
- `MAX_REVISIONS = 3`
- `MAX_EXTERNAL_SEARCH_CALLS_PER_PLAN = 6`
- provider timeouts and structured fallback warnings;
- no provider exception may crash the graph;
- no raw provider payload or hidden reasoning in UI/logs.

## 12. Definition of done

- FastAPI, Swagger, and Streamlit run from documented commands.
- Complete and incomplete requests both reach the correct HITL state.
- Initial flow shows Supervisor -> Flight -> Hotel -> Planner -> Review.
- Both flight and hotel calls cross the custom MCP boundary.
- Web results include source evidence; mock fallback works without a key.
- Review approval completes the thread.
- Natural-language revisions use direct Swarm handoffs.
- Hotel-only and flight-only revisions do not rerun the unaffected search.
- Budget calculations are deterministic and covered by tests.
- Stable preference memory survives a new thread.
- Tests make zero real OpenAI requests.

