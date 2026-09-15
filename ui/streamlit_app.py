"""Thin Streamlit chat client. All planning work stays behind FastAPI."""

from __future__ import annotations

import os
from copy import deepcopy
from datetime import date, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import urlparse

import httpx
import streamlit as st
from progress import render_observability, stream_trip

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
TIME_LABELS = {
    "morning": "Buổi sáng",
    "afternoon": "Buổi chiều",
    "evening": "Buổi tối",
    "flexible": "Linh hoạt",
}
CATEGORY_LABELS = {
    "flight": "Vé máy bay",
    "hotel": "Khách sạn",
    "activities": "Tham quan và hoạt động",
    "food": "Ăn uống",
    "local_transport": "Di chuyển nội thành",
    "buffer": "Dự phòng",
}

st.set_page_config(page_title="Agentic Travel Planner", page_icon="✈️", layout="wide")
st.title("✈️ Agentic Travel Planner")
st.caption("Trợ ly du lịch thông minh")
st.markdown(
    """
    <style>
    .activity-cost-badge {
        display: inline-block;
        margin-top: 0.35rem;
        padding: 0.32rem 0.7rem;
        border: 1px solid #b9e4c2;
        border-radius: 999px;
        background: #eaf8ed;
        color: #247a37;
        font-weight: 700;
    }
    .stApp {
        background:
            radial-gradient(circle at 6% -8%, #dce8ff 0, transparent 28rem),
            radial-gradient(circle at 96% 2%, #e6dcff 0, transparent 25rem),
            #f7f8fc;
    }
    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, #171a36, #252557);
    }
    [data-testid="stSidebar"] * { color: #f4f6ff; }
    [data-testid="stSidebar"] code { color: #1a1d3b; }
    .plan-hero {
        margin: 0.75rem 0 1.15rem;
        padding: 1.7rem;
        border: 1px solid rgba(255, 255, 255, .5);
        border-radius: 1.25rem;
        color: #fff;
        background: linear-gradient(120deg, #29245d, #5450ad 58%, #856cc8);
        box-shadow: 0 16px 36px rgba(53, 46, 124, .20);
    }
    .plan-hero__eyebrow {
        font-size: .78rem; font-weight: 750; letter-spacing: .11em; opacity: .78;
    }
    .plan-hero h2 { margin: .35rem 0 .25rem; color: #fff; font-size: 1.8rem; }
    .plan-hero p { margin: 0; opacity: .88; }
    .budget-hero {
        margin: 0.5rem 0 1rem;
        padding: 1.2rem 1.35rem;
        border: 1px solid #b9e4c2;
        border-radius: 1rem;
        background: linear-gradient(135deg, #edf9f3, #e9edff);
        box-shadow: 0 10px 22px rgba(37, 79, 112, .08);
    }
    .budget-hero__eyebrow { color: #28783a; font-weight: 700; }
    .budget-hero__amount { color: #173b27; font-size: 2rem; font-weight: 800; }
    .budget-hero__detail { color: #50615a; margin-top: 0.25rem; }
    [data-testid="stMetric"] {
        padding: .9rem;
        border: 1px solid #e2e5f1;
        border-radius: 1rem;
        background: rgba(255, 255, 255, .78);
        box-shadow: 0 6px 16px rgba(31, 37, 86, .05);
    }
    [data-testid="stMetricLabel"] { font-weight: 650; }
    [data-testid="stVerticalBlockBorderWrapper"] {
        border-color: #e2e5f1;
        border-radius: 1rem;
        background: rgba(255, 255, 255, .72);
        box-shadow: 0 7px 18px rgba(31, 37, 86, .04);
    }
    [data-baseweb="tab-list"] { gap: .5rem; border-bottom: 0; }
    [data-baseweb="tab"] {
        height: 2.5rem;
        padding: 0 .9rem;
        border: 1px solid #e1e4f0;
        border-radius: .7rem;
        background: rgba(255,255,255,.72);
    }
    [aria-selected="true"][data-baseweb="tab"] {
        border-color: #4e4aa5;
        background: #ecebff;
        color: #332f82;
        font-weight: 750;
    }
    .stButton > button, .stLinkButton > a { border-radius: .7rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    if method == "POST" and (path.endswith("/start") or path.endswith("/resume")):
        return stream_trip(API_BASE_URL, path + "/stream", payload)
    with httpx.Client(timeout=180) as client:
        response = client.request(method, f"{API_BASE_URL}{path}", json=payload)
        response.raise_for_status()
        return response.json()


def money(value: int | float | None) -> str:
    return f"{int(value or 0):,} VND"


def activity_cost_badge(value: int | float) -> None:
    st.markdown(
        f'<span class="activity-cost-badge">💸 Ước tính: {money(value)}/người</span>',
        unsafe_allow_html=True,
    )


@st.cache_data(ttl=3600, show_spinner=False)
def provider_image_bytes(image_url: str) -> bytes | None:
    """Download a provider thumbnail once so a broken hotlink never reaches the UI."""
    parsed = urlparse(image_url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    try:
        response = httpx.get(
            image_url,
            follow_redirects=True,
            timeout=5,
            headers={"User-Agent": "AgenticTravelPlanner/1.0"},
        )
        content_type = response.headers.get("content-type", "").lower()
        if (
            response.status_code != 200
            or not content_type.startswith("image/")
            or len(response.content) > 5_000_000
        ):
            return None
        return response.content
    except httpx.HTTPError:
        return None


def render_provider_image(image_url: str | None, caption: str) -> bool:
    if not image_url:
        return False
    image = provider_image_bytes(image_url)
    if not image:
        return False
    st.image(image, caption=caption, width="stretch")
    return True


def render_budget_hero(summary: dict[str, int | float | bool]) -> None:
    remaining = int(summary["remaining_budget"])
    state = "Còn trong ngân sách" if remaining >= 0 else "Đang vượt ngân sách"
    detail = (
        f"{state}: {money(abs(remaining))} · Ngân sách: "
        f"{money(summary['total_budget'])}"
    )
    st.markdown(
        "<div class='budget-hero'>"
        "<div class='budget-hero__eyebrow'>💰 TỔNG CHI PHÍ DỰ KIẾN</div>"
        f"<div class='budget-hero__amount'>{money(summary['estimated_total'])}</div>"
        f"<div class='budget-hero__detail'>{detail}</div>"
        "</div>",
        unsafe_allow_html=True,
    )


def short_date(value: str | date | None) -> str:
    if value is None:
        return "?"
    parsed = date.fromisoformat(value) if isinstance(value, str) else value
    return parsed.strftime("%d/%m/%Y")


def date_time(value: str | None) -> str:
    if not value:
        return "Chưa có giờ"
    return datetime.fromisoformat(value).strftime("%d/%m/%Y %H:%M")


def duration(value: int | None) -> str:
    if value is None:
        return "Chưa rõ thời lượng"
    hours, minutes = divmod(value, 60)
    return f"{hours} giờ {minutes} phút" if hours else f"{minutes} phút"


def append_event(role: str, kind: str, **content: Any) -> None:
    st.session_state.timeline.append({"role": role, "kind": kind, **deepcopy(content)})


def set_result(result: dict[str, Any]) -> None:
    st.session_state.result = result
    st.session_state.thread_id = result["thread_id"]


def append_backend_event(result: dict[str, Any], *, revised: bool = False) -> None:
    pending = result.get("interrupt") or {}
    state = result.get("state", {})
    if pending.get("kind") == "clarification":
        append_event(
            "assistant",
            "clarification",
            message=pending.get("message", "Mình cần thêm thông tin."),
            questions=pending.get("questions", []),
        )
        return
    if pending.get("kind") == "plan_review" and state.get("trip_plan"):
        version = 1 + sum(event["kind"] == "plan" for event in st.session_state.timeline)
        rationale = state["trip_plan"].get("rationale", "").strip()
        append_event(
            "assistant",
            "plan",
            message=(
                (
                    "Mình đã cập nhật kế hoạch theo yêu cầu của bạn.\n\n"
                    f"{rationale}"
                )
                if revised
                else f"Mình đã chuẩn bị xong kế hoạch.\n\n{rationale}"
            ),
            plan=state["trip_plan"],
            version=version,
        )
        return
    if result.get("status") == "completed":
        append_event("assistant", "text", message="Đã duyệt và lưu kế hoạch này.")
        return
    errors = state.get("errors") or []
    append_event(
        "assistant",
        "text",
        message="Không thể tiếp tục workflow." + (f" {'; '.join(errors)}" if errors else ""),
    )


def update_latest_plan(result: dict[str, Any]) -> None:
    plan = result.get("state", {}).get("trip_plan")
    if not plan:
        return
    for event in reversed(st.session_state.timeline):
        if event["kind"] == "plan":
            event["plan"] = deepcopy(plan)
            return


def reset_trip() -> None:
    st.session_state.result = None
    st.session_state.thread_id = None
    st.session_state.timeline = []
    st.session_state.hotel_details = {}
    st.session_state.selected_flights = {}
    st.session_state.hotel_night_selections = {}


def flight_label(option: dict[str, Any]) -> str:
    outbound = option.get("outbound_leg") or {}
    returning = option.get("return_leg") or {}
    out_segments = outbound.get("segments") or []
    return_segments = returning.get("segments") or []
    out_time = date_time(out_segments[0].get("departure_at")) if out_segments else "?"
    return_time = date_time(return_segments[0].get("departure_at")) if return_segments else ""
    route = f"Đi {out_time}"
    if return_time:
        route += f" · Về {return_time}"
    return f"{option['airline']} · {route} · {money(option['total_price'])}"


def render_flight_leg(leg: dict[str, Any] | None, title: str, price_note: str | None) -> None:
    with st.container(border=True):
        st.markdown(f"#### {title}")
        if not leg or not leg.get("segments"):
            st.caption("Provider chưa trả đủ chi tiết chặng bay.")
            return
        segments = leg["segments"]
        for index, segment in enumerate(segments, start=1):
            number = segment.get("flight_number") or "chưa có số hiệu"
            st.markdown(f"**Chặng {index}: {segment['airline']} · {number}**")
            st.write(
                f"{segment['origin_airport']} → {segment['destination_airport']}  \n"
                f"Cất cánh: **{date_time(segment['departure_at'])}**  \n"
                f"Hạ cánh: **{date_time(segment['arrival_at'])}**"
            )
            details = [duration(segment.get("duration_minutes"))]
            if segment.get("aircraft"):
                details.append(segment["aircraft"])
            if segment.get("travel_class"):
                details.append(segment["travel_class"])
            st.caption(" · ".join(details))
        stops = leg.get("stops")
        st.caption(
            f"Tổng: {duration(leg.get('duration_minutes'))} · "
            f"{'Bay thẳng' if stops == 0 else f'{stops} điểm dừng'}"
        )
        if leg.get("total_price") is not None:
            st.metric("Giá chặng", money(leg["total_price"]))
        else:
            st.metric("Giá chặng", "Provider không tách riêng")
            if price_note:
                st.caption(price_note)


def render_flight_section(
    plan: dict[str, Any], version: int, *, interactive: bool
) -> dict[str, Any]:
    options = [plan["recommended_flight"], *plan.get("alternative_flights", [])]
    by_id = {option["id"]: option for option in options}
    default_id = plan.get("selected_flight_id") or options[0]["id"]
    current_id = st.session_state.selected_flights.get(version, default_id)
    if current_id not in by_id:
        current_id = default_id

    st.markdown("### ✈️ Chuyến bay")
    if interactive:
        selected = st.selectbox(
            "Chọn combo chuyến bay đi – về",
            options,
            index=[item["id"] for item in options].index(current_id),
            format_func=flight_label,
            key=f"flight-option-select-{version}",
        )
        st.session_state.selected_flights[version] = selected["id"]
    else:
        selected = by_id.get(current_id, options[0])
        st.write(f"**{flight_label(selected)}**")

    outbound_column, return_column = st.columns(2)
    with outbound_column:
        render_flight_leg(selected.get("outbound_leg"), "Chiều đi", selected.get("price_note"))
    with return_column:
        if selected.get("return_leg"):
            render_flight_leg(
                selected.get("return_leg"), "Chiều về", selected.get("price_note")
            )
        else:
            render_flight_leg(None, "Chiều về", selected.get("price_note"))
    total_price = selected["total_price"]
    price_per_person = selected["price_per_person"]
    travelers = max(1, round(total_price / price_per_person)) if price_per_person else 1
    total_column, per_person_column = st.columns(2)
    total_column.metric(f"Tổng vé cho {travelers} người", money(total_price))
    per_person_column.metric("Giá vé mỗi người", money(price_per_person))
    st.caption(f"Nguồn giá: {selected['source']}. Giá có thể thay đổi khi đặt vé.")
    if selected.get("source_url"):
        st.link_button(
            "Mở chuyến bay trên Google Flights",
            selected["source_url"],
            key=f"flight-link-{version}",
        )
    return selected


def hotel_label(hotel: dict[str, Any]) -> str:
    rating = f" · ⭐ {hotel['rating']}" if hotel.get("rating") is not None else ""
    return f"{hotel['name']} · {money(hotel['nightly_price'])}/đêm{rating} · {hotel['source']}"


def plan_night_dates(plan: dict[str, Any]) -> list[str]:
    dates: list[str] = []
    for stay in plan.get("hotel_stays", []):
        start = date.fromisoformat(stay["check_in_date"])
        for offset in range(stay["nights"]):
            dates.append((start + timedelta(days=offset)).isoformat())
    if dates:
        return dates
    flight = plan["recommended_flight"]
    start = date.fromisoformat(flight["departure_date"])
    nights = plan["recommended_hotel"].get("nights", 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(nights)]


def default_hotel_ids(plan: dict[str, Any], night_dates: list[str]) -> list[str]:
    ids: list[str] = []
    for night in night_dates:
        night_date = date.fromisoformat(night)
        match = next(
            (
                stay
                for stay in plan.get("hotel_stays", [])
                if date.fromisoformat(stay["check_in_date"])
                <= night_date
                < date.fromisoformat(stay["check_out_date"])
            ),
            None,
        )
        ids.append(match["hotel"]["id"] if match else plan["selected_hotel_id"])
    return ids


def render_hotel_preview(hotel: dict[str, Any], nights: int, version: int) -> None:
    details_column, image_column = st.columns([3, 2])
    with details_column:
        st.markdown(f"#### {hotel['name']}")
        facts = []
        if hotel.get("area"):
            facts.append(f"📍 {hotel['area']}")
        if hotel.get("rating") is not None:
            facts.append(f"⭐ {hotel['rating']}/10")
        if hotel.get("stars"):
            facts.append(f"{'★' * hotel['stars']}")
        if facts:
            st.caption(" · ".join(facts))
        st.write(
            f"**{money(hotel['nightly_price'])}/đêm** · "
            f"ước tính **{money(hotel['nightly_price'] * nights)}** cho {nights} đêm"
        )
        if hotel.get("amenities"):
            st.caption("Tiện nghi: " + " · ".join(hotel["amenities"][:4]))
        if hotel.get("source_url"):
            st.link_button(
                "Xem khách sạn",
                hotel["source_url"],
                key=f"hotel-preview-link-{version}",
            )
    with image_column:
        if not render_provider_image(hotel.get("image_url"), "Ảnh từ nhà cung cấp"):
            st.info("Ảnh sẽ hiển thị khi Booking MCP trả URL ảnh.", icon="🏨")


def render_hotel_section(
    plan: dict[str, Any], version: int, *, interactive: bool
) -> list[dict[str, Any]]:
    options = [plan["recommended_hotel"], *plan.get("alternative_hotels", [])]
    by_id = {option["id"]: option for option in options}
    nights = plan_night_dates(plan)
    defaults = default_hotel_ids(plan, nights)
    selected_ids = st.session_state.hotel_night_selections.get(version, defaults)
    if len(selected_ids) != len(nights):
        selected_ids = defaults

    st.markdown("### 🏨 Khách sạn theo từng đêm")
    chosen: list[dict[str, Any]] = []
    if interactive:
        st.caption("Có thể chọn cùng một khách sạn cho cả chuyến hoặc đổi theo từng đêm.")
        for index, night in enumerate(nights):
            current_id = selected_ids[index] if selected_ids[index] in by_id else defaults[index]
            selected = st.selectbox(
                f"Đêm {index + 1} · nhận phòng {short_date(night)}",
                options,
                index=[item["id"] for item in options].index(current_id),
                format_func=hotel_label,
                key=f"hotel-night-{version}-{index}",
            )
            chosen.append(selected)
        st.session_state.hotel_night_selections[version] = [hotel["id"] for hotel in chosen]
    else:
        for index, night in enumerate(nights):
            hotel = by_id.get(selected_ids[index], plan["recommended_hotel"])
            chosen.append(hotel)
            st.write(
                f"**Đêm {index + 1} · {short_date(night)}:** {hotel['name']} · "
                f"{money(hotel['nightly_price'])}"
            )

    total = sum(hotel["nightly_price"] for hotel in chosen)
    st.metric("Tổng tiền khách sạn", money(total), f"{len(chosen)} đêm")
    with st.container(border=True):
        render_hotel_preview(chosen[0], len(chosen), version)

    if interactive:
        lookup = st.selectbox(
            "Khách sạn cần tra lại availability",
            options,
            format_func=hotel_label,
            key=f"hotel-detail-select-{version}",
        )
        if st.button(
            "Tra lại giá và chi tiết khách sạn",
            key=f"hotel-detail-button-{version}",
            width="stretch",
        ):
            try:
                with st.spinner("Đang tra lại availability và giá..."):
                    st.session_state.hotel_details[version] = api_request(
                        "POST",
                        f"/api/trips/{st.session_state.thread_id}/hotel-details",
                        {"option_id": lookup["id"]},
                    )
            except Exception as exc:
                st.error(f"Không thể tra hotel: {exc}")
    detail = st.session_state.hotel_details.get(version)
    if detail:
        if detail.get("warning"):
            with st.expander("Lưu ý từ kết quả khách sạn", expanded=False):
                st.info(detail["warning"])
        current = detail.get("hotel")
        if current:
            with st.container(border=True):
                st.write(f"**{current['name']}**")
                st.write(
                    f"Một đêm: **{money(current['nightly_price'])}** · "
                    f"Cả kỳ {current['nights']} đêm: **{money(current['total_price'])}**"
                )
                details = []
                if current.get("room_type"):
                    details.append(f"Phòng: {current['room_type']}")
                if current.get("rating") is not None:
                    review_count = current.get("review_count") or 0
                    details.append(f"⭐ {current['rating']} ({review_count} review)")
                if details:
                    st.caption(" · ".join(details))
                if current.get("source_url"):
                    st.link_button(
                        "Mở Booking.com",
                        current["source_url"],
                        key=f"hotel-link-{version}",
                    )
    return chosen


def effective_budget(
    plan: dict[str, Any], flight: dict[str, Any], hotels: list[dict[str, Any]]
) -> dict[str, int | float | bool]:
    budget = plan["budget"]
    hotel_total = sum(hotel["nightly_price"] for hotel in hotels)
    subtotal = (
        flight["total_price"]
        + hotel_total
        + budget["activities"]
        + budget["food"]
        + budget["local_transport"]
    )
    buffer_rate = budget.get("buffer_rate", 0.05)
    buffer = round(subtotal * buffer_rate)
    total = subtotal + buffer
    return {
        "flight": flight["total_price"],
        "hotel": hotel_total,
        "activities": budget["activities"],
        "food": budget["food"],
        "local_transport": budget["local_transport"],
        "buffer": buffer,
        "estimated_total": total,
        "total_budget": budget["total_budget"],
        "remaining_budget": budget["total_budget"] - total,
        "is_over_budget": total > budget["total_budget"],
        "buffer_rate": buffer_rate,
    }


def budget_items(
    plan: dict[str, Any],
    flight: dict[str, Any],
    hotels: list[dict[str, Any]],
    summary: dict[str, int | float | bool],
) -> list[dict[str, Any]]:
    items = [
        item
        for item in plan["budget"].get("line_items", [])
        if item["category"] not in {"flight", "hotel", "buffer"}
    ]
    legs = [flight.get("outbound_leg"), flight.get("return_leg")]
    priced_legs = [leg for leg in legs if leg and leg.get("total_price") is not None]
    if priced_legs and sum(leg["total_price"] for leg in priced_legs) == flight["total_price"]:
        labels = {"outbound": "Vé chiều đi", "return": "Vé chiều về"}
        items.extend(
            {
                "category": "flight",
                "label": labels[leg["direction"]],
                "quantity": 1,
                "unit_price": leg["total_price"],
                "total": leg["total_price"],
                "note": "Giá cho toàn bộ hành khách",
            }
            for leg in priced_legs
        )
    else:
        items.append(
            {
                "category": "flight",
                "label": "Vé khứ hồi" if flight.get("return_leg") else "Vé một chiều",
                "quantity": 1,
                "unit_price": flight["total_price"],
                "total": flight["total_price"],
                "note": flight.get("price_note"),
            }
        )
    night_dates = plan_night_dates(plan)
    for index, hotel in enumerate(hotels):
        items.append(
            {
                "category": "hotel",
                "label": f"{short_date(night_dates[index])} · {hotel['name']}",
                "quantity": 1,
                "unit_price": hotel["nightly_price"],
                "total": hotel["nightly_price"],
                "note": hotel.get("room_type") or hotel.get("area"),
            }
        )
    items.append(
        {
            "category": "buffer",
            "label": "Dự phòng biến động giá và phát sinh",
            "quantity": 1,
            "unit_price": summary["buffer"],
            "total": summary["buffer"],
            "note": f"{summary['buffer_rate']:.0%} trên tạm tính",
        }
    )
    if not plan["budget"].get("line_items"):
        existing = {item["category"] for item in items}
        for category in ("activities", "food", "local_transport"):
            if category not in existing:
                items.append(
                    {
                        "category": category,
                        "label": CATEGORY_LABELS[category],
                        "quantity": 1,
                        "unit_price": summary[category],
                        "total": summary[category],
                        "note": "Chi phí ước tính gộp",
                    }
                )
    return items


def render_budget(
    plan: dict[str, Any], flight: dict[str, Any], hotels: list[dict[str, Any]]
) -> None:
    summary = effective_budget(plan, flight, hotels)
    st.markdown("### 💰 Bảng chi phí chi tiết")
    render_budget_hero(summary)
    total_column, budget_column, remaining_column = st.columns(3)
    total_column.metric("Tổng ước tính", money(summary["estimated_total"]))
    budget_column.metric("Ngân sách", money(summary["total_budget"]))
    remaining_column.metric(
        "Còn lại" if not summary["is_over_budget"] else "Vượt ngân sách",
        money(abs(summary["remaining_budget"])),
    )

    overview = [
        {"Nhóm chi phí": label, "Thành tiền": money(summary[key])}
        for key, label in CATEGORY_LABELS.items()
    ]
    overview.append({"Nhóm chi phí": "TỔNG CỘNG", "Thành tiền": money(summary["estimated_total"])})
    st.dataframe(overview, hide_index=True, width="stretch")

    items = budget_items(plan, flight, hotels, summary)
    for category, label in CATEGORY_LABELS.items():
        category_items = [item for item in items if item["category"] == category]
        with st.expander(f"{label} · {money(summary[category])}", expanded=False):
            rows = [
                {
                    "Mục": item["label"],
                    "SL": item.get("quantity", 1),
                    "Đơn giá": money(item["unit_price"]),
                    "Thành tiền": money(item["total"]),
                    "Ghi chú": item.get("note") or "",
                }
                for item in category_items
            ]
            st.dataframe(rows, hide_index=True, width="stretch")


def render_itinerary(plan: dict[str, Any], version: int) -> None:
    st.markdown("### 🗓️ Lịch trình")
    for day in plan["itinerary"]:
        label = (
            f"**Ngày {day['day']} · {short_date(day['date'])}** "
            f"— **{day['title']}**"
        )
        with st.expander(label, expanded=day["day"] == 1):
            for activity_index, activity in enumerate(day["activities"]):
                with st.container(border=True):
                    detail_column, image_column = st.columns([3, 2])
                    with detail_column:
                        verify = " · cần xác minh" if activity.get("needs_verification") else ""
                        time_label = TIME_LABELS.get(activity["time_of_day"], "Linh hoạt")
                        st.markdown(f"**{time_label} — {activity['title']}**{verify}")
                        st.write(activity["description"])
                        if activity.get("place_name"):
                            place = f"📍 **{activity['place_name']}**"
                            if activity.get("address"):
                                place += f" · {activity['address']}"
                            st.write(place)
                            if activity.get("rating") is not None:
                                st.caption(
                                    f"⭐ {activity['rating']} · "
                                    f"{activity.get('review_count') or 0:,} lượt đánh giá · "
                                    f"source: {activity.get('place_source') or 'unknown'}"
                                )
                            if activity.get("maps_url"):
                                st.link_button(
                                    "Mở trên Google Maps",
                                    activity["maps_url"],
                                    key=(
                                        f"maps-link-{version}-{day['day']}-{activity_index}"
                                    ),
                                )
                        estimated_cost = activity.get("estimated_cost_per_person") or 0
                        if estimated_cost > 0:
                            activity_cost_badge(estimated_cost)
                    with image_column:
                        render_provider_image(
                            activity.get("image_url"), "Ảnh từ Google Maps"
                        )
                if activity_index < len(day["activities"]) - 1:
                    st.divider()


def render_itinerary_sheet(plan: dict[str, Any]) -> None:
    st.markdown("### 📋 Lịch trình dạng sheet")
    st.caption(
        "Mỗi dòng là một hoạt động. Dùng tab này để nhìn nhanh toàn bộ chuyến đi; "
        "tab Lịch trình vẫn giữ chi tiết và ảnh địa điểm."
    )
    rows: list[dict[str, str]] = []
    for day in plan["itinerary"]:
        day_label = f"Ngày {day['day']} · {short_date(day['date'])}"
        for activity in day["activities"]:
            rows.append(
                {
                    "Ngày": day_label,
                    "Buổi": TIME_LABELS.get(activity["time_of_day"], "Linh hoạt"),
                    "Kế hoạch": activity["title"],
                    "Địa điểm": activity.get("place_name") or "—",
                    "Chi phí/người": money(activity.get("estimated_cost_per_person")),
                    "Trạng thái": (
                        "Cần xác minh" if activity.get("needs_verification") else "Đề xuất"
                    ),
                    "Bản đồ": activity.get("maps_url") or "",
                }
            )
    st.dataframe(
        rows,
        hide_index=True,
        width="stretch",
        height=min(560, max(220, 44 * len(rows) + 48)),
        column_config={
            "Ngày": st.column_config.TextColumn(width="medium"),
            "Buổi": st.column_config.TextColumn(width="small"),
            "Kế hoạch": st.column_config.TextColumn(width="large"),
            "Địa điểm": st.column_config.TextColumn(width="large"),
            "Chi phí/người": st.column_config.TextColumn(width="medium"),
            "Trạng thái": st.column_config.TextColumn(width="medium"),
            "Bản đồ": st.column_config.LinkColumn("Bản đồ", display_text="Mở Maps"),
        },
    )


def render_warnings(warnings: list[str]) -> None:
    clean = list(dict.fromkeys(warning for warning in warnings if warning))
    if not clean:
        return
    with st.expander(f"⚠️ Lưu ý và giới hạn dữ liệu ({len(clean)})", expanded=False):
        st.markdown("\n".join(f"- {warning}" for warning in clean))


def render_plan(plan: dict[str, Any], version: int, *, interactive: bool) -> None:
    flight = plan["recommended_flight"]
    destination = escape(str(flight.get("destination") or "điểm đến"))
    price_per_person = flight.get("price_per_person") or 0
    travelers = (
        max(1, round(flight["total_price"] / price_per_person))
        if price_per_person
        else "—"
    )
    st.markdown(
        f"""
        <div class="plan-hero">
          <div class="plan-hero__eyebrow">HÀNH TRÌNH #{version:02d}</div>
          <h2>Chuyến đi đến {destination}</h2>
          <p>Chọn phương án phù hợp, xem lịch trình và kiểm soát ngân sách trong một nơi.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    overview_tab, travel_tab, itinerary_tab, sheet_tab, budget_tab = st.tabs(
        [
            "✨ Tổng quan",
            "✈️ Di chuyển & ở",
            "🗓️ Lịch trình",
            "📋 Sheet lịch trình",
            "💰 Ngân sách",
        ]
    )
    with overview_tab:
        st.markdown("#### Tóm tắt đề xuất")
        overview_one, overview_two, overview_three = st.columns(3)
        overview_one.metric("Điểm đến", flight.get("destination") or "—")
        overview_two.metric("Số khách", travelers)
        overview_three.metric("Số ngày", len(plan.get("itinerary", [])))
        st.info(plan.get("rationale") or "Đang tổng hợp đề xuất cho chuyến đi này.", icon="✨")

    with travel_tab:
        selected_flight = render_flight_section(plan, version, interactive=interactive)
        selected_hotels = render_hotel_section(plan, version, interactive=interactive)

    with itinerary_tab:
        render_itinerary(plan, version)

    with sheet_tab:
        render_itinerary_sheet(plan)

    with budget_tab:
        render_budget(plan, selected_flight, selected_hotels)
    render_warnings(plan.get("warnings", []))
    if not interactive:
        st.caption("Đây là phiên bản cũ — được giữ lại để đối chiếu.")


def render_timeline(current_result: dict[str, Any] | None) -> None:
    latest_plan_index = next(
        (
            index
            for index in range(len(st.session_state.timeline) - 1, -1, -1)
            if st.session_state.timeline[index]["kind"] == "plan"
        ),
        None,
    )
    latest_is_reviewable = bool(
        current_result and (current_result.get("interrupt") or {}).get("kind") == "plan_review"
    )
    for index, event in enumerate(st.session_state.timeline):
        with st.chat_message(event["role"]):
            if event["kind"] == "text":
                if event["role"] == "user":
                    st.markdown("##### Yêu cầu của bạn")
                    st.info(event["message"], icon="🧳")
                else:
                    st.markdown(event["message"])
            elif event["kind"] == "clarification":
                st.markdown(event["message"])
                for question in event.get("questions", []):
                    st.write(f"• {question}")
            elif event["kind"] == "plan":
                st.info(event["message"], icon="🤖")
                render_plan(
                    event["plan"],
                    event["version"],
                    interactive=latest_is_reviewable and index == latest_plan_index,
                )


if "result" not in st.session_state:
    st.session_state.result = None
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None
if "timeline" not in st.session_state:
    st.session_state.timeline = []
if "hotel_details" not in st.session_state:
    st.session_state.hotel_details = {}
if "selected_flights" not in st.session_state:
    st.session_state.selected_flights = {}
if "hotel_night_selections" not in st.session_state:
    st.session_state.hotel_night_selections = {}

if st.session_state.result and not st.session_state.timeline:
    old_state = st.session_state.result.get("state", {})
    if old_state.get("raw_request"):
        append_event("user", "text", message=old_state["raw_request"])
    append_backend_event(st.session_state.result)

with st.sidebar:
    st.header("Kết nối")
    st.code(API_BASE_URL)
    try:
        api_request("GET", "/health")
        st.success("FastAPI đang chạy")
    except Exception:
        st.error("Chưa kết nối được FastAPI")
    if st.button("Chuyến mới", width="stretch"):
        reset_trip()
        st.rerun()

if not st.session_state.result:
    with st.form("start-trip"):
        prompt = st.text_area(
            "Mô tả chuyến đi",
            value=(
                "Tôi muốn đi từ TP.HCM đến Đà Nẵng từ 2026-10-15 đến 2026-10-18, "
                "2 người, ngân sách 20 triệu VND, thích ẩm thực và biển."
            ),
            height=140,
        )
        submitted = st.form_submit_button("Lập kế hoạch", type="primary")
    if submitted:
        try:
            with st.spinner("Các agent đang lập kế hoạch..."):
                new_result = api_request("POST", "/api/trips/start", {"message": prompt})
            append_event("user", "text", message=prompt)
            set_result(new_result)
            append_backend_event(new_result)
            st.rerun()
        except Exception as exc:
            st.error(f"Không thể bắt đầu: {exc}")
else:
    result = st.session_state.result
    state = result.get("state", {})
    pending = result.get("interrupt") or {}

    st.caption(
        f"Thread: {result['thread_id']} · Trạng thái: {result['status']} "
        f"· Phase: {result['phase']}"
    )
    render_timeline(result)
    render_observability(result)

    with st.expander("Agent trace & metrics"):
        st.json(state.get("metrics", {}))
        for event in state.get("trace", []):
            st.write(
                f"`{event['kind']}` **{event['actor']}** — "
                f"{event['action']} {event.get('detail', '')}"
            )

    if pending.get("kind") == "clarification":
        answer = st.chat_input("Bổ sung thông tin còn thiếu rồi nhấn Enter...")
        if answer:
            try:
                with st.spinner("Đang tiếp tục..."):
                    new_result = api_request(
                        "POST",
                        f"/api/trips/{result['thread_id']}/resume",
                        {"message": answer},
                    )
                append_event("user", "text", message=answer)
                set_result(new_result)
                append_backend_event(new_result)
                st.rerun()
            except Exception as exc:
                st.error(f"Không thể tiếp tục: {exc}")

    if pending.get("kind") == "plan_review":
        if st.button(
            "Duyệt kế hoạch mới nhất",
            type="primary",
            key="approve-current-plan",
            width="stretch",
        ):
            latest_version = max(
                event["version"]
                for event in st.session_state.timeline
                if event["kind"] == "plan"
            )
            selected_flight_id = st.session_state.selected_flights.get(latest_version)
            hotel_selection_ids = st.session_state.hotel_night_selections.get(
                latest_version, []
            )
            try:
                with st.spinner("Đang lưu kế hoạch..."):
                    new_result = api_request(
                        "POST",
                        f"/api/trips/{result['thread_id']}/resume",
                        {
                            "action": "approve",
                            "selected_flight_id": selected_flight_id,
                            "hotel_selection_ids": hotel_selection_ids,
                        },
                    )
                append_event("user", "text", message="Duyệt kế hoạch mới nhất.")
                set_result(new_result)
                update_latest_plan(new_result)
                append_backend_event(new_result)
                st.rerun()
            except Exception as exc:
                st.error(f"Không thể duyệt: {exc}")

        revision = st.chat_input("Nhập yêu cầu chỉnh sửa kế hoạch rồi nhấn Enter...")
        if revision:
            try:
                with st.spinner("Agent đang chỉnh đúng phần bị ảnh hưởng..."):
                    new_result = api_request(
                        "POST",
                        f"/api/trips/{result['thread_id']}/resume",
                        {"action": "revise", "message": revision},
                    )
                append_event("user", "text", message=revision)
                set_result(new_result)
                append_backend_event(new_result, revised=True)
                st.rerun()
            except Exception as exc:
                st.error(f"Không thể chỉnh sửa: {exc}")
