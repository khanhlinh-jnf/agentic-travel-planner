"""Thin Streamlit client. All planning work stays behind FastAPI."""

from __future__ import annotations

import os
from typing import Any

import httpx
import streamlit as st

API_BASE_URL = os.getenv("API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

st.set_page_config(page_title="Agentic Travel Planner", page_icon="✈️", layout="wide")
st.title("✈️ Agentic Travel Planner")
st.caption("FastAPI + LangGraph + MCP + OpenAI Web Search, với mock fallback an toàn")


def api_request(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=180) as client:
        response = client.request(method, f"{API_BASE_URL}{path}", json=payload)
        response.raise_for_status()
        return response.json()


def set_result(result: dict[str, Any]) -> None:
    st.session_state.result = result
    st.session_state.thread_id = result["thread_id"]


def money(value: int | float | None) -> str:
    return f"{int(value or 0):,} VND"


def render_plan(plan: dict[str, Any]) -> None:
    st.subheader("Kế hoạch đề xuất")
    flight = plan["recommended_flight"]
    hotel = plan["recommended_hotel"]
    left, middle, right = st.columns(3)
    left.metric("Chuyến bay", f"{flight['airline']} {flight.get('flight_number') or ''}")
    left.caption(f"{flight['origin']} → {flight['destination']} · {money(flight['total_price'])}")
    middle.metric("Khách sạn", hotel["name"])
    middle.caption(f"{hotel.get('area') or hotel['destination']} · {money(hotel['total_price'])}")
    budget = plan["budget"]
    right.metric(
        "Tổng ước tính",
        money(budget["estimated_total"]),
        money(budget["remaining_budget"]),
    )

    for day in plan["itinerary"]:
        label = f"Ngày {day['day']} · {day['date']} · {day['title']}"
        with st.expander(label, expanded=day["day"] == 1):
            for activity in day["activities"]:
                verify = " · cần xác minh" if activity.get("needs_verification") else ""
                st.markdown(f"**{activity['time_of_day'].title()} — {activity['title']}**{verify}")
                st.write(activity["description"])

    if plan.get("warnings"):
        for warning in plan["warnings"]:
            st.warning(warning)


if "result" not in st.session_state:
    st.session_state.result = None
if "thread_id" not in st.session_state:
    st.session_state.thread_id = None

with st.sidebar:
    st.header("Kết nối")
    st.code(API_BASE_URL)
    try:
        api_request("GET", "/health")
        st.success("FastAPI đang chạy")
    except Exception:
        st.error("Chưa kết nối được FastAPI")
    if st.button("Chuyến mới", use_container_width=True):
        st.session_state.result = None
        st.session_state.thread_id = None
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
                set_result(api_request("POST", "/api/trips/start", {"message": prompt}))
            st.rerun()
        except Exception as exc:
            st.error(f"Không thể bắt đầu: {exc}")
else:
    result = st.session_state.result
    state = result.get("state", {})
    pending = result.get("interrupt")

    st.caption(
        f"Thread: {result['thread_id']} · Trạng thái: {result['status']} "
        f"· Phase: {result['phase']}"
    )
    if state.get("trip_plan"):
        render_plan(state["trip_plan"])

    if pending and pending.get("kind") == "clarification":
        st.info(pending.get("message"))
        for question in pending.get("questions", []):
            st.write(f"• {question}")
        with st.form("clarify"):
            answer = st.text_area("Bổ sung thông tin")
            sent = st.form_submit_button("Gửi và tiếp tục", type="primary")
        if sent:
            try:
                with st.spinner("Đang tiếp tục..."):
                    set_result(
                        api_request(
                            "POST",
                            f"/api/trips/{result['thread_id']}/resume",
                            {"message": answer},
                        )
                    )
                st.rerun()
            except Exception as exc:
                st.error(f"Không thể tiếp tục: {exc}")

    if pending and pending.get("kind") == "plan_review":
        st.divider()
        approve_col, revision_col = st.columns([1, 2])
        if approve_col.button("Duyệt kế hoạch", type="primary", use_container_width=True):
            try:
                set_result(
                    api_request(
                        "POST",
                        f"/api/trips/{result['thread_id']}/resume",
                        {"action": "approve"},
                    )
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Không thể duyệt: {exc}")
        with revision_col.form("revision"):
            revision = st.text_input(
                "Yêu cầu sửa",
                placeholder=(
                    "Ví dụ: đổi khách sạn dưới 1,5 triệu/đêm, "
                    "giữ nguyên chuyến bay"
                ),
            )
            revise = st.form_submit_button("Yêu cầu chỉnh sửa", use_container_width=True)
        if revise:
            try:
                with st.spinner("Agent đang chỉnh đúng phần bị ảnh hưởng..."):
                    set_result(
                        api_request(
                            "POST",
                            f"/api/trips/{result['thread_id']}/resume",
                            {"action": "revise", "message": revision},
                        )
                    )
                st.rerun()
            except Exception as exc:
                st.error(f"Không thể chỉnh sửa: {exc}")

    with st.expander("Agent trace & metrics"):
        st.json(state.get("metrics", {}))
        for event in state.get("trace", []):
            st.write(
                f"`{event['kind']}` **{event['actor']}** — "
                f"{event['action']} {event.get('detail', '')}"
            )
