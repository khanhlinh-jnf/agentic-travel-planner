# Developer Guide — Agentic Travel Planner

Tài liệu này giúp bạn đọc toàn bộ backend, hiểu agent handoff, MCP, provider fallback,
FastAPI endpoints, Streamlit và cách debug log. Design chuẩn hiện tại là **Swarm-first**;
project không còn Supervisor/Hierarchical layer.

## 1. Mental model

Có bốn lớp chính:

```text
Streamlit
  -> FastAPI
      -> LangGraph state + HITL
          -> Flight / Hotel / Place / Planner agents
              -> MCP client
                  -> SerpApi MCP (flight + Google Maps places)
                  -> Flightpowers Booking MCP (hotel)
                  -> local stdio MCP (mock fallback)
```

Nguyên tắc quan trọng:

- UI không import graph, agent hay provider; UI chỉ gọi HTTP.
- Flight, Hotel và Place Agent không gọi HTTP provider trực tiếp; chúng đi qua MCP adapter.
- Planner không được tự tạo giá flight/hotel.
- Budget là phép tính Python deterministic.
- Agent handoff bằng `Command(goto=...)`, không quay về một Supervisor.
- App không booking, thanh toán, giữ giá hoặc tự submit form.

## 2. Luồng hoàn chỉnh

### Plan đầu tiên

```text
START
 -> load_memory
 -> parse_request
 -> validate_request
    -> clarify --interrupt--> parse_request       (nếu thiếu/sai)
    -> swarm_entry                                (nếu đủ)
 -> flight_agent --Command--> hotel_agent
 -> hotel_agent  --Command--> place_agent
 -> place_agent  --Command--> planner_agent
 -> planner_agent --Command--> review
 -> review --interrupt-->
    -> save -> END                                (approve)
    -> revision_entry                             (revise)
```

### Revision

`revision_entry` phân loại domain bị ảnh hưởng và tạo `agent_queue`:

| Yêu cầu | Queue |
|---|---|
| Chỉ đổi hotel | `hotel, planner` |
| Chỉ đổi flight | `flight, planner` |
| Đổi cả hai | `flight, hotel, planner` |
| Chỉ đổi lịch trình | `place, planner` |

Revision làm thay đổi destination/interests chạy `place, planner`; thay đổi lịch trình thuần túy
cũng có thể refresh Place Agent để lấy lại evidence địa điểm.

Agent hiện tại lấy chính nó khỏi đầu queue rồi handoff thẳng sang phần tử tiếp theo.

## 3. Cây thư mục

```text
agentic-travel-planner/
├── app/
│   ├── agents/
│   │   ├── planner.py
│   │   └── specialists.py
│   ├── mcp/
│   │   ├── client.py
│   │   ├── server.py
│   │   └── providers/
│   │       ├── booking.py
│   │       ├── mock.py
│   │       ├── serpapi.py
│   │       └── service.py
│   ├── memory/store.py
│   ├── services/
│   │   ├── budget.py
│   │   ├── constraint_patch.py
│   │   ├── llm.py
│   │   ├── quota.py
│   │   └── validation.py
│   ├── api.py
│   ├── config.py
│   ├── graph.py
│   ├── main.py
│   ├── schemas.py
│   └── state.py
├── data/
├── scripts/
├── tests/
├── ui/streamlit_app.py
├── .env.example
├── README.md
└── SPEC.md
```

Các file `__init__.py` chỉ đánh dấu Python package và không chứa business logic.

## 4. `app/config.py`

Đây là file duy nhất đọc `.env`. `Settings` dùng `pydantic-settings`; `settings` là singleton
được cache bởi `get_settings()`.

### Biến môi trường

| Biến | Ý nghĩa |
|---|---|
| `APP_NAME` | Tên FastAPI app. |
| `LOG_LEVEL` | Mức log dự kiến của app. Uvicorn vẫn cần flag `--log-level`. |
| `API_BASE_URL` | URL backend mà UI dùng. |
| `OPENAI_API_KEYS` | Một hoặc nhiều OpenAI key, phân cách dấu phẩy. |
| `OPENAI_API_KEY` | Alias một key, dùng khi `OPENAI_API_KEYS` rỗng. |
| `LLM_MODEL` | Model parse request/revision và draft itinerary. |
| `LLM_MAX_COMPLETION_TOKENS` | Output cap nền cho itinerary, mặc định `3200`. |
| `USE_MOCK_LLM` | `true`: heuristic local, không gọi OpenAI. |
| `USE_MOCK_TRAVEL_DATA` | `true`: tất cả travel data đi qua local mock MCP. |
| `TRAVEL_SEARCH_MAX_RESULTS` | Số option tối đa giữ sau normalize. |
| `SERPAPI_API_KEY` | Key cho hosted SerpApi MCP. |
| `SERPAPI_MCP_URL` | Mặc định `https://mcp.serpapi.com/mcp`. |
| `BOOKING_MCP_URL` | Flightpowers hotel MCP hoặc URL do Glama Gateway cấp. |
| `RAPIDAPI_KEY` | Credential cho `hotels.flightpowers.com`. |
| `MAX_REVISIONS` | Số vòng revise tối đa. |
| `MAX_EXTERNAL_SEARCH_CALLS_PER_PLAN` | Cap live provider call cho mỗi plan/revision. |
| `USER_MEMORY_PATH` | File JSON lưu stable preference. |

Properties:

- `api_keys`: tách chuỗi key thành list và bỏ khoảng trắng.
- `primary_api_key`: key OpenAI đầu tiên hoặc chuỗi rỗng.
- `project_root`: root repo suy ra từ vị trí file.
- `memory_file`: resolve đường dẫn memory tuyệt đối.

Không log `settings`, vì object này chứa credential.

## 5. `app/schemas.py`

Mọi domain model kế thừa `StrictModel(extra="forbid")`. Payload có field lạ sẽ fail sớm,
tránh âm thầm dùng nhầm contract.

### Input models

- `FlightPreferences`: khung giờ, hãng, ưu tiên direct, max budget.
- `HotelPreferences`: max price/đêm, khu vực, số sao, review score 0–10.
- `TripRequest`: request đầy đủ đã merge và normalize.
- `TripRequestPatch`: phần thông tin vừa trích từ một câu; các field có thể `None`.
- `RevisionIntent`: domain bị ảnh hưởng, patch và cờ preserve flight/hotel.

### Evidence models

`FlightOption` chứa giá chuẩn hóa VND, hành trình, source và thời điểm quan sát.

`FlightSegment` giữ hãng/số hiệu/sân bay/ngày giờ của một segment. `FlightLeg` gom các segment
vào chiều đi hoặc chiều về. Giá từng leg là nullable vì Google Flights thường chỉ trả giá khứ
hồi gộp; code không tự chia giá live.

- `source="serpapi"`: kết quả Google Flights qua SerpApi MCP.
- `source="mock"`: dữ liệu local.
- `departure_token`, `booking_token`: token tạm thời dùng cho lookup tiếp theo. Hai field đặt
  `exclude=True`, nên không xuất hiện trong API, UI hoặc prompt itinerary.

`HotelOption` tương tự, với source `booking` hoặc `mock`; có thêm room type, review count,
rating 0–10 và Booking link.

`HotelStay` gắn một hotel với check-in/check-out và số đêm, cho phép plan dùng hotel khác nhau
theo từng đêm. `PlaceOption` chứa tên, loại, địa chỉ, rating 0–5, review count và Maps URL.

`FlightSearchResult`, `HotelSearchResult` và `PlaceSearchResult` là whole-provider responses. `provider_call_count`
tăng theo remote MCP attempt; local mock trực tiếp bằng 0.

### On-demand models

- `FlightBookingOffer`: seller, giá, hành lý, flight numbers, safe URL và cờ
  `requires_provider_form`.
- `FlightBookingOptionsResult`: danh sách offer + warning + provider call count.
- `HotelDetailResult`: một hotel vừa được lookup lại theo tên.

### Plan và observability

- `ItineraryActivity`, `ItineraryDay`, `PlanNarrative`: output LLM đã validate; activity có thể
  chứa place evidence cụ thể.
- `BudgetLineItem`, `BudgetSummary`: breakdown deterministic từ nhóm lớn đến từng dòng.
- `TripPlan`: evidence được chọn, alternatives, itinerary và budget.
- `TraceEvent`: `node`, `route`, `tool`, `handoff`, `hitl`, hoặc `warning`.
- `WorkflowMetrics`: số LLM/MCP/external/handoff/revision. Không còn supervisor metric.
- `UserPreferences`: chỉ những preference ổn định được nhớ qua thread.

## 6. `app/state.py`

`TravelState` là `TypedDict` của LangGraph. Các nhóm field:

- Identity/input: `thread_id`, `user_id`, `raw_request`, `trip_request`.
- Validation/HITL: missing fields, errors, clarification response.
- Evidence: `flight_options`, `hotel_options`, `place_options`, `trip_plan`.
- Replanning: bốn cờ `*_stale`, `revision_intent`, `agent_queue`, `revision_count`.
- Runtime: phase, status, metrics, trace, warnings, errors.

`trace`, `warnings`, `errors` dùng reducer `operator.add`, nghĩa là update mới được nối vào
list cũ. `metrics` được clone rồi thay thế bởi helper `_metrics()`.

## 7. `app/services/validation.py`

- `normalize_request()`: suy return date từ departure + duration. Nếu có đủ hai ngày thì
  luôn tự tính lại duration và xem hai ngày là source of truth.
- `validate_request()`: kiểm tra critical field, ngày quá khứ, thứ tự ngày, giới hạn 30 ngày
  và budget; không bắt user tự sửa duration lệch.
- `clarification_questions()`: map lỗi kỹ thuật thành câu hỏi tiếng Việt.

Quy ước `duration_days` tính cả ngày đi và ngày về. Ví dụ 15–18 là 4 ngày, hotel có 3 đêm.

## 8. `app/services/llm.py`

File này có ba nhóm tác vụ LLM và fallback local:

### Parse request

- `heuristic_trip_patch(text, current)`: regex local cho city, ISO date, người, budget,
  sở thích và một số preference.
- `parse_trip_request(text, current)`: dùng structured output `TripRequestPatch` khi có key;
  nếu mock/no key thì gọi heuristic.

Khi câu clarification chỉ có một ngày và chứa ý “ngày về”, heuristic hiểu đó là return date
thay vì ghi đè departure date.

### Itinerary

- `_fallback_narrative()`: lịch local deterministic đủ để demo offline.
- `draft_narrative()`: structured output `PlanNarrative`; chỉ gửi evidence cần thiết, giới hạn
  tối đa hai activity/ngày và tự chọn token cap theo số ngày (tối đa 5000).
- Nếu SDK raise `LengthFinishReasonError`, hàm trả `_fallback_narrative()` kèm warning thay vì
  làm Planner Agent và cả workflow thất bại. Nhánh này không retry nên không tốn thêm LLM call.

### Revision

- `heuristic_revision()`: xác định flight/hotel/itinerary/budget và preserve instruction.
- `parse_revision()`: structured `RevisionIntent` hoặc fallback heuristic.

`live_call_count()` chỉ là counter trong process cho các call ở file này; nó không phải
OpenAI billing dashboard và không đếm MCP provider calls.

## 9. `app/services/constraint_patch.py`

Các tập `FLIGHT_FIELDS`, `HOTEL_FIELDS`, `ITINERARY_FIELDS` mô tả thay đổi nào làm evidence
nào stale.

`apply_revision()`:

1. merge patch vào current `TripRequest`;
2. tính changed fields và affected domains;
3. áp dụng `preserve_flight`/`preserve_hotel`;
4. với budget-only, chỉ search lại khi plan hiện tại đang over budget;
5. trả request mới và ba stale flags.

## 10. `app/services/budget.py`

`calculate_budget()` cộng các phần bằng integer VND:

- flight và hotel lấy đúng evidence đã chọn;
- activities lấy từ itinerary;
- food/local transport dùng rule cố định theo ngày/người;
- buffer là tỷ lệ trên subtotal;
- `remaining_budget = total_budget - estimated_total`.

Không để model cộng tiền vì số học cần lặp lại được và dễ test.

`app/services/quota.py` giữ counter remote-call theo `thread_id` trong process. Graph search
và hai endpoint on-demand cùng ghi vào counter này, nên detail lookup và revision dùng chung
cap. Đây là demo in-memory; restart backend sẽ reset counter.

## 11. `app/mcp/client.py`

`_connection(provider)` tạo ba loại connection:

| Provider | Transport | Auth |
|---|---|---|
| `serpapi` | Streamable HTTP | `Authorization: Bearer ...` |
| `booking` | Streamable HTTP | `x-rapidapi-key` nếu có |
| `mock` | stdio subprocess | Không auth |

`get_provider_tools()` tạo `MultiServerMCPClient` riêng cho từng provider và cache tool theo
provider. Việc tách cache tránh collision vì Booking và local fallback có tool gần giống tên.

`_parse_json_text()` và `_coerce_payload()` xử lý các dạng MCP adapter có thể trả:

- structured dict;
- `structuredContent`/`structured_content`;
- text content block chứa JSON;
- JSON list;
- Pydantic-like object có `model_dump()`.

`call_provider_tool(provider, name, arguments)` lấy đúng tool rồi `ainvoke`. `reset_cache()`
dùng cho test hoặc reload cấu hình trong cùng process.

Không đưa key vào URL SerpApi; dùng header để key ít xuất hiện trong access log hơn.

## 12. `app/mcp/server.py`

Đây là MCP server local, chạy qua stdio và **chỉ phục vụ fallback**. Backend tự spawn nó;
không cần mở port riêng.

Tools:

| Tool | Chức năng |
|---|---|
| `search_flights_mock` | Flight fixtures theo route/ngày/người. |
| `search_hotels_mock` | Hotel fixtures theo destination và nightly budget. |
| `search_places_mock` | Địa điểm, địa chỉ và rating mô phỏng. |
| `flight_booking_options_mock` | Seller option mô phỏng, read-only. |
| `find_hotel_by_name_mock` | Hotel detail mô phỏng. |

Resource `travel://capabilities` mô tả server không booking/payment.

Rất quan trọng: với stdio MCP, không `print()` dữ liệu debug ra stdout vì stdout dành cho
JSON-RPC. Dùng `logging`/stderr nếu cần.

## 13. `app/mcp/providers/serpapi.py`

### Flight search

- `flight_search_params()`: tạo params cho `engine=google_flights`; map city bằng
  `city_code()`, chọn round trip/one-way, thêm VND/locale/passengers và preference filters.
- `search_flights_live()`: gọi một lần lấy outbound; với round trip gọi thêm một lần bằng
  `departure_token` để lấy return candidates.
- `normalize_flight_search()`: đọc `best_flights` + `other_flights`, giữ từng segment và tạo
  tối đa `TRAVEL_SEARCH_MAX_RESULTS` `FlightOption`.

Các helper `_integer()`, `_clock()`, `_payload_dict()` chịu trách nhiệm parse dữ liệu biên.
Nếu không có option với giá hợp lệ, module raise `InsufficientSerpApiEvidence` để facade
fallback toàn response.

### Google Maps places

`search_places_live()` dùng cùng MCP tool `search` với `engine=google_maps`. `normalize_places()`
đọc `local_results`/`place_results` và chỉ giữ place có tên; địa chỉ/rating/review count thiếu thì
để null. Một plan chỉ dùng một place search, không gọi riêng cho từng activity.

### Booking options

Google Flights round trip là flow nhiều bước:

```text
initial search -> departure_token
departure_token search -> return candidates + booking_token
booking_token search -> booking_options
```

`flight_booking_options_live()` chỉ chạy khi user gọi endpoint on-demand. Flight round-trip đã
có `booking_token` từ planning nên lookup thường chỉ cần bước cuối lấy seller.

`normalize_booking_options()` đọc seller `together`, `departing`, `returning`. `_booking_offer()`
chỉ cho phép direct URL khi `booking_request` không có `post_data`. Nếu có form POST, app chỉ
set `requires_provider_form=true`; raw form data không rời backend.

## 14. `app/mcp/providers/booking.py`

Connector có hai tool read-only:

- `search_hotels(destination, checkin_date, checkout_date, ...)`;
- `find_hotel_by_name(hotel_name, checkin_date, checkout_date, ...)`.

`search_hotels_live()` và `find_hotel_by_name_live()` gọi đúng tool. `_items()` hỗ trợ response
là list hoặc wrapper `results/hotels/properties/data` vì connector không publish output
schema cứng. `normalize_hotels()` chỉ giữ item có name và parseable price, rồi chuẩn hóa
location, review, room type và link. Payload thực tế của Flightpowers dùng `price` cho tổng
kỳ lưu trú; code chia theo số đêm để tạo `nightly_price`. Nếu provider trả field per-night
rõ ràng thì field đó được ưu tiên.

Hotel price rất dễ stale. Search-by-name là request mới mỗi lần user bấm, không lấy một cache
giá cũ để trình bày như realtime.

## 15. `app/mcp/providers/mock.py`

- `city_code()`: map một số tên Việt Nam/city phổ biến sang IATA.
- `_flight_templates()`, `_hotel_templates()`, `_place_templates()`: đọc JSON một lần bằng `lru_cache`.
- `search_flights_mock()`, `search_hotels_mock()`, `search_places_mock()`: tạo normalized response deterministic.
- `flight_booking_options_mock()`, `find_hotel_by_name_mock()`: giữ hai endpoint on-demand
  hoạt động trong demo offline.

Mock luôn có `source="mock"`, warning, và `provider_call_count=0`.

## 16. `app/mcp/providers/service.py`

Đây là facade duy nhất agent/API dùng cho travel data.

### Search

`search_flights()`:

```text
mock mode / live cap -> local MCP
không có SERPAPI_API_KEY -> local MCP + config warning
có key -> SerpApi MCP
          lỗi/invalid evidence -> local MCP + error-type warning
```

`search_hotels()` tương tự. Booking live được xem là configured khi có `RAPIDAPI_KEY` hoặc
`BOOKING_MCP_URL` đã đổi khỏi direct default (ví dụ URL do Glama cấp).

`search_places()` dùng SerpApi Google Maps và fallback toàn response sang local place mock.

### Detail

- `get_flight_booking_options()`: live chỉ khi option ban đầu là SerpApi và còn call budget.
- `get_hotel_detail()`: live chỉ khi option ban đầu là Booking và connector configured.

Fallback warning chỉ ghi loại exception, không ghi raw message để giảm nguy cơ lộ credential
từ URL/header/provider error.

## 17. `app/agents/specialists.py`

- `rank_flights()`: lọc khung giờ/hãng/budget; nếu không option nào hard-match thì giữ toàn
  bộ để tránh empty plan; sort direct, price, duration.
- `rank_hotels()`: lọc nightly price/star/rating/area; sort price rồi rating.
- `search_flights(request, allow_live)`: assert critical fields, gọi provider facade và rank.
- `search_hotels(request, allow_live)`: tương tự.
- `search_places(request, allow_live)`: lấy place evidence đã chuẩn hóa cho Planner.

File này không biết endpoint MCP hay credential. Đó là trách nhiệm của provider/client layer.

## 18. `app/agents/planner.py`

- `_require_options()`: fail rõ ràng nếu thiếu flight/hotel evidence.
- `_hotel_stays()`: mặc định một hotel cho cả kỳ; khi revision yêu cầu hotel khác nhau theo
  từng ngày/đêm thì chia thành các stay một đêm.
- `build_plan()`: chọn option đầu sau ranking, gọi `draft_narrative()`, tính budget và gộp
  warnings. Nếu một trong hai source là mock, plan cảnh báo không dùng để đặt chỗ.

`apply_plan_selections()` áp lựa chọn flight và hotel từng đêm từ UI trước khi approve, sau đó
tính lại budget. Planner không sửa giá source.

## 19. `app/graph.py`

### Helpers

- `_event()`: tạo trace có timestamp.
- `_metrics()`: clone và tăng counter, tránh mutate state cũ.
- `_merge_request_patch()`: deep-merge nested preference; nếu clarification chỉ sửa return
  date hoặc duration thì xóa giá trị còn lại trước normalize để tránh loop mâu thuẫn.
- `_agent_node()`: map logical agent sang graph node.
- `_next_handoff()`: pop agent vừa xong và chọn target kế tiếp.
- `_live_search_allowed()`: so metrics với external call cap.

### Nodes

| Node | Việc làm | Đi tiếp |
|---|---|---|
| `load_memory` | Load preference, init flags/metrics. | `parse_request` |
| `parse_request` | Parse + merge + áp preference. | `validate_request` |
| `validate_request` | Missing/error check. | `clarify` hoặc `swarm_entry` |
| `clarify` | `interrupt()` hỏi thêm. | `parse_request` |
| `swarm_entry` | Tạo initial queue. | first agent bằng `Command` |
| `flight_agent` | Search/rank flight. | next agent bằng `Command` |
| `hotel_agent` | Search/rank hotel. | next agent bằng `Command` |
| `place_agent` | Search/normalize Google Maps places. | next agent bằng `Command` |
| `planner_agent` | Compose itinerary/budget. | `review` bằng `Command` |
| `review` | `interrupt()` approve/revise. | `save` hoặc `revision_entry` |
| `revision_entry` | Parse revision, stale flags, selective queue. | first affected agent |
| `save` | Persist stable preferences. | `END` |
| `fail` | Dừng an toàn và cần human. | `END` |

`build_graph()` chỉ có static edge cho flow bắt buộc và conditional edge quanh HITL. Các
agent transition là dynamic `Command`, thể hiện direct handoff thực sự.

## 20. `app/memory/store.py`

- `load_preferences(user_id)`: đọc JSON; trả default nếu file chưa có hoặc lỗi.
- `save_preferences(user_id, preferences)`: merge theo user ID.
- `apply_preferences(request, preferences)`: chỉ điền field user chưa nêu.
- `preferences_from_request()`: lấy subset ổn định sau approve.

Đây là local demo memory, không có locking/auth/encryption và không phù hợp production.

## 21. `app/api.py`

### Request helpers

- `StartTripRequest`: message + user ID.
- `ResumeTripRequest`: action + message; khi approve có thể mang `selected_flight_id` và danh
  sách `hotel_selection_ids` theo từng đêm.
- `OptionDetailRequest`: chỉ nhận option ID.
- `_config()`: LangGraph thread config.
- `_jsonable()`: Pydantic/dict/list sang JSON-safe value.
- `_interrupt_payload()`: lấy interrupt đầu tiên.
- `_response()`: response envelope nhất quán.
- `_trip_context()`: load snapshot và validate request.
- `quota.total()`: đọc tổng remote-call của cả graph và on-demand lookup.

### Endpoints

#### `POST /api/trips/start`

Tạo UUID thread, init state rồi `travel_graph.ainvoke()`. Thường trả `awaiting_input` vì graph
dừng ở clarification hoặc plan review.

#### `POST /api/trips/{thread_id}/resume`

Kiểm tra thread, biến body thành resume value rồi gọi `Command(resume=...)`.

- clarification: gửi `{"message": "..."}`;
- approve: gửi `{"action": "approve", "selected_flight_id": "...", "hotel_selection_ids": [...]}`;
- revise: gửi `{"action": "revise", "message": "..."}`.

#### `GET /api/trips/{thread_id}`

Trả snapshot/checkpoint hiện tại và pending interrupt nếu có.

#### `POST /api/trips/{thread_id}/flight-booking-options`

Chỉ chấp nhận `option_id` nằm trong `flight_options` của thread. Backend tự lấy token ẩn,
kiểm tra external call budget rồi gọi provider facade. Client không thể gửi token tùy ý.

#### `POST /api/trips/{thread_id}/hotel-details`

Chỉ chấp nhận hotel option có trong thread, rồi gọi `find_hotel_by_name` với chính dates và
traveler count đã validate.

Hai response lookup có thêm `external_search_calls_in_thread`. Graph metrics giữ các call
trong planning/revision; counter response này cộng thêm lookup calls trong FastAPI process.

Hai endpoint detail là external read-only lookup. HTTP `POST` ở API của ta chỉ vì body chứa
selection và thao tác có thể tốn quota; nó không có nghĩa là đặt chỗ.

## 22. `app/main.py`

Tạo FastAPI app, bật CORS cho Streamlit local, mount trip router và có:

- `GET /`: metadata/link;
- `GET /health`: liveness đơn giản.

`/health` không test OpenAI key hay MCP provider. `200` chỉ chứng minh FastAPI process sống.

## 23. `ui/streamlit_app.py`

- `api_request()`: HTTP client duy nhất.
- `money()`: format VND.
- `append_event()`: append một user/assistant event vào timeline, không ghi đè hội thoại cũ.
- `append_backend_event()`: đổi response của backend thành clarification, plan version mới hoặc message.
- `set_result()`: lưu response hiện tại và thread ID vào session state.
- `render_timeline()`: render lại toàn bộ câu chat và các plan theo đúng thứ tự thời gian.
- `render_flight_section()`: dropdown combo đi–về và chi tiết từng segment/ngày giờ/giá.
- `render_hotel_section()`: dropdown hotel cho từng đêm, nightly total và stay total.
- `render_itinerary()`: dùng nhãn buổi sáng/chiều/tối và hiển thị place/rating/Maps link.
- `render_budget()`: bảng nhóm chi phí và expander line items.
- `render_warnings()`: gom notice/warning vào một hộp thu gọn.
- `render_plan()`: ghép các section của từng phiên bản plan.

Mỗi lần revise, UI append câu sửa của user rồi append một plan mới ở cuối trang; plan cũ vẫn giữ
nguyên để đối chiếu. Chỉ plan mới nhất có selector và hai nút lookup. State UI giữ kết quả lookup
trong `flight_bookings` và `hotel_details`, keyed theo số phiên bản plan; lookup không rerun graph.
Nút **Chuyến mới** mới xóa timeline hiện tại. User phải tự bấm link provider. UI không nhận token
và không có code submit booking POST form.

## 24. Chạy project

Terminal backend:

```powershell
cd D:\UNI_STUDY\Year3\Semester3\LLMEngineer\Module2\agentic-travel-planner
.\.venv\Scripts\Activate.ps1
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Terminal frontend:

```powershell
cd D:\UNI_STUDY\Year3\Semester3\LLMEngineer\Module2\agentic-travel-planner
.\.venv\Scripts\Activate.ps1
python -m streamlit run ui/streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

Nếu port 8000 bị chặn/đang dùng, chọn port khác cho cả hai phía:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
$env:API_BASE_URL="http://127.0.0.1:8010"
python -m streamlit run ui/streamlit_app.py --server.port 8501
```

## 25. Cách check log backend

### Log Uvicorn trực tiếp

Terminal đang chạy backend là nguồn log đầu tiên:

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000 --log-level debug
```

Bạn sẽ thấy method, path, status code và traceback nếu request 500. Khi dùng `--reload`, có
reloader process và worker process; đọc traceback ở worker phía sau dòng request lỗi.

### Trace nghiệp vụ

Không cần đọc chain-of-thought. Mở `Agent trace & metrics` trên UI hoặc xem:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/api/trips/THREAD_ID | ConvertTo-Json -Depth 20
```

Trace bình thường của initial plan:

```text
memory load
intake_agent parse
validator request_ready
swarm handoff_to_flight
flight_agent search_flights
flight_agent handoff_to_hotel
hotel_agent search_hotels
hotel_agent handoff_to_planner
planner_agent handoff_to_human
```

Nếu `source=mock`, xem `warnings` để phân biệt:

- chưa có `SERPAPI_API_KEY`;
- Booking MCP chưa configured;
- MCP/provider exception;
- external call cap đã hết.

### Test nhanh endpoint

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

Swagger <http://127.0.0.1:8000/docs> dễ nhất để test start/resume/details.

### Không log các giá trị này

- OpenAI/SerpApi/RapidAPI key;
- full MCP URL nếu URL có credential query;
- `Authorization` hoặc `x-rapidapi-key` header;
- `departure_token`, `booking_token`;
- `booking_request.post_data`;
- raw provider payload trong production log.

## 26. Test MCP riêng

Kiểm tra local mock MCP qua unit/integration suite:

```powershell
python -m pytest -q -s
```

Kiểm tra tool đăng ký mà không thực hiện external call:

```powershell
python -c "import asyncio; from app.mcp.client import get_provider_tools; print(asyncio.run(get_provider_tools('mock')).keys())"
```

Kỳ vọng năm mock tools. Không chạy command lấy `serpapi`/`booking` nếu bạn không muốn dùng
quota hoặc chưa cấu hình credential.

## 27. Test và quality gate

```powershell
python -m ruff check app ui tests
python -m pytest -q
```

`tests/conftest.py` set:

```text
USE_MOCK_LLM=true
USE_MOCK_TRAVEL_DATA=true
```

Do đó test không gọi OpenAI, SerpApi hay Booking.

Test coverage hiện tại gồm:

- full initial HITL + hotel-only revision + approve;
- không có supervisor field/trace;
- clarification;
- fix date/duration conflict loop;
- mock booking options và hotel detail endpoints;
- SerpApi/Booking payload normalization;
- whole-response provider fallback;
- deterministic budget reconciliation.

## 28. Troubleshooting

| Hiện tượng | Nguyên nhân thường gặp | Cách check |
|---|---|---|
| FastAPI `WinError 10013` | Port bị reserve/chặn | Dùng `8010`, sửa `API_BASE_URL` cho UI. |
| `/health` OK nhưng travel mock | Health không gọi provider | Đọc `warnings` và source trong plan. |
| Flight mock | Thiếu/invalid SerpApi key hoặc MCP lỗi | Kiểm tra `.env`, restart BE, xem warning type. |
| Hotel mock | Thiếu RapidAPI subscription/key hoặc sai gateway URL | Kiểm tra `BOOKING_MCP_URL`, key và subscription. |
| MCP JSON parse lỗi | Server trả text không chứa JSON | Xem provider contract, giữ whole-response fallback. |
| stdio MCP treo/lỗi | Có output thường trên stdout | Bỏ `print()`, log sang stderr. |
| Booking options mock | Flight gốc là mock, hết cap hoặc token stale | Search plan mới rồi thử lại. |
| Seller không có link | Provider yêu cầu POST form | Đây là hành vi an toàn chủ ý; app không submit. |
| Hotel detail khác giá ban đầu | Rate đã đổi | Dùng observed time mới, không coi giá cũ là held. |
| Revision search cả hai | Parser nhận nhiều affected domains | Xem `revision_intent` và `agent_queue` trong state. |
| Date conflict lặp | Reply làm tồn tại return + duration cũ | `_merge_request_patch()` phải clear field đối ứng. |

## 29. Cách xác định data thật hay mock

Xem `source` ở `recommended_flight` và `recommended_hotel`:

- `serpapi`: flight live qua SerpApi MCP;
- `booking`: hotel live qua Flightpowers Booking MCP;
- `mock`: fixture local qua stdio MCP.

`external_search_calls=0` nghĩa là chưa có remote call thành công trong graph. `mcp_calls=2`
vẫn có thể là mock vì Flight và Hotel đều đã vượt qua local MCP boundary.

## 30. Chủ đích giới hạn scope

Project cố ý không có auth, database production, queue worker, caching live fare, return-flight
picker riêng, payment hoặc browser automation. Các phần đó không cần để chứng minh MCP,
multi-agent handoff, HITL, memory và selective replanning, nhưng sẽ làm capstone nặng hơn và
tăng rủi ro demo.

## 31. Tài liệu provider

- [SerpApi Google Flights API](https://serpapi.com/google-flights-api)
- [SerpApi Google Flights results](https://serpapi.com/google-flights-results)
- [SerpApi booking options](https://serpapi.com/google-flights-booking-options)
- [Official SerpApi MCP](https://github.com/serpapi/serpapi-mcp)
- [Flightpowers Booking connector on Glama](https://glama.ai/mcp/connectors/com.flightpowers/booking)
- [Flightpowers repository](https://github.com/mtnrabi/travel-agent-skills)
