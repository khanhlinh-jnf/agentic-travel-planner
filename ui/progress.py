"""Live backend events and run metrics. No workflow execution in the frontend."""

import json
from html import escape

import httpx
import streamlit as st

LABELS = {
    "load_memory": "Đọc sở thích", "parse_request": "Hiểu yêu cầu",
    "validate_request": "Kiểm tra thông tin", "clarify": "Bổ sung thông tin",
    "swarm_entry": "Chuẩn bị tìm kiếm", "flight_agent": "Tìm chuyến bay",
    "hotel_agent": "Tìm khách sạn", "place_agent": "Tìm địa điểm",
    "planner_agent": "Lập lịch trình", "review": "Chờ bạn duyệt",
    "revision_entry": "Hiểu yêu cầu chỉnh sửa", "save": "Lưu kế hoạch",
    "fail": "Cần hỗ trợ",
}
STATUS = {
    "queued": ("○", "Đang chờ", "#64748b"),
    "skipped": ("↪", "Giữ kết quả cũ", "#64748b"),
    "running": ("●", "Đang thực hiện", "#4f46e5"),
    "success": ("✓", "Hoàn tất", "#047857"),
    "fallback": ("⚠", "Dùng dữ liệu dự phòng", "#b45309"),
    "error": ("✕", "Có lỗi", "#be123c"),
    "awaiting_input": ("◉", "Chờ bạn", "#0369a1"),
}


def progress_html(events):
    latest = {}
    for event in events:
        if event["kind"] == "queue" and event["actor"] in {"swarm_entry", "revision_entry"}:
            names = [name + "_agent" for name in event["agents"]]
            for name in ("flight_agent", "hotel_agent", "place_agent", "planner_agent"):
                latest[name] = {
                    "status": "queued" if name in names else "skipped",
                }
        if event["kind"] == "node" and event["status"] in STATUS:
            latest[event["actor"]] = event
    cards = []
    for name, event in latest.items():
        icon, label, color = STATUS[event["status"]]
        seconds = event.get("duration_ms")
        elapsed = f" · {seconds / 1000:.1f}s" if seconds is not None else ""
        cards.append(
            f'<div style="padding:12px 16px;background:#f8fafc;border:1px solid #e2e8f0;'
            f'border-left:4px solid {color};border-radius:12px;margin:6px 0">'
            f'<b>{icon} {escape(LABELS.get(name, name))}</b>'
            f'<span style="color:{color};float:right">{label}{elapsed}</span></div>'
        )
    tools = [e for e in events if e["kind"] == "tool"]
    if tools:
        last = tools[-1]
        cards.append(
            '<p style="font-size:13px;color:#64748b">Tra cứu: '
            + escape(last["actor"]) + " · " + escape(last["status"]) + "</p>"
        )
    return "".join(cards)


def stream_trip(base_url, path, payload):
    """SSE POST stream; terminal errors and missing result are explicit."""
    events = []
    result = None
    if payload and payload.get("message"):
        with st.chat_message("user"):
            st.info(payload["message"], icon="🧳")
    with st.status("Các agent đang xử lý yêu cầu của bạn…", expanded=True) as status:
        panel = st.empty()
        with httpx.Client(timeout=httpx.Timeout(180, connect=10)) as client:
            with client.stream("POST", base_url + path, json=payload) as response:
                if response.is_error:
                    response.read()
                    response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    if event["type"] == "progress":
                        if event["data"].get("thread_id"):
                            st.session_state.thread_id = event["data"]["thread_id"]
                        events.append(event["data"])
                        panel.markdown(progress_html(events), unsafe_allow_html=True)
                    elif event["type"] == "result":
                        result = event["data"]
                    elif event["type"] == "error":
                        status.update(label="Chưa hoàn thành", state="error")
                        raise RuntimeError(event["data"]["message"])
        if result is None:
            raise RuntimeError(
                "Kết nối bị ngắt trước khi có kết quả; kiểm tra thread trước khi gửi lại."
            )
        failed = result.get("status") == "needs_human"
        status.update(
            label="Cần kiểm tra lỗi" if failed else "Đã xử lý xong",
            state="error" if failed else "complete", expanded=failed,
        )
    return result


def render_observability(result, graph_state=None):
    data = result.get("observability", {})
    runs = data.get("runs") or []
    if not runs:
        return
    latest = runs[-1]
    with st.container(border=True):
        st.markdown("#### Hoạt động của trợ lý · lượt vừa rồi")
        timing, tokens, price, calls = st.columns(4)
        timing.metric("Thời gian xử lý", f'{latest["elapsed_ms"] / 1000:.1f}s')
        tokens.metric("Token LLM", f'{latest["input_tokens"] + latest["output_tokens"]:,}')
        value = latest["cost_usd"]
        price.metric(
            "Phí LLM ước tính", f"${value:.6f}" if value is not None else "Chưa xác định"
        )
        calls.metric("LLM / MCP calls", f'{latest["llm_calls"]} / {latest["mcp_tool_calls"]}')
        st.caption("Phí vận hành AI (USD), riêng với ngân sách du lịch (VND). "
                   "Chưa gồm SerpApi, RapidAPI và hosting.")
        if latest["usage_incomplete"]:
            st.warning("Một số call lỗi không trả token usage; số token hiện tại chưa đầy đủ.")
        if latest.get("trace_url"):
            st.link_button("Mở trace Langfuse", latest["trace_url"])
        with st.expander("Agent, prompt version và lịch sử các lượt"):
            st.markdown(progress_html(latest["events"]), unsafe_allow_html=True)
            if latest["prompts"]:
                st.dataframe(latest["prompts"], hide_index=True, width="stretch")
            st.dataframe([
                {"Lượt": r["action"], "Thời gian (s)": r["elapsed_ms"] / 1000,
                 "Token": r["input_tokens"] + r["output_tokens"],
                 "Phí LLM (USD)": r["cost_usd"], "Run ID": r["run_id"]}
                for r in runs
            ], hide_index=True, width="stretch")
            st.json(latest["generations"])
        if graph_state:
            with st.expander("Chi tiết kỹ thuật LangGraph"):
                st.json(graph_state.get("metrics", {}))
                for event in graph_state.get("trace", []):
                    st.write(
                        f"`{event['kind']}` **{event['actor']}** — "
                        f"{event['action']} {event.get('detail', '')}"
                    )
