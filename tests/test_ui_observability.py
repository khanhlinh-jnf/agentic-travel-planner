"""Frontend smoke and learning-document checks without a running server or paid APIs."""

from html.parser import HTMLParser

import httpx
from fastapi.testclient import TestClient
from streamlit.testing.v1 import AppTest

from app.config import settings
from app.main import app


def test_streamlit_renders_plan_with_observability(monkeypatch):
    with TestClient(app) as client:
        result = client.post("/api/trips/start", json={
            "user_id": "ui-test",
            "message": "Từ TP.HCM đi Đà Nẵng từ 2099-10-15 đến 2099-10-18, "
                       "2 người, ngân sách 20 triệu, thích biển.",
        }).json()
    monkeypatch.syspath_prepend(str(settings.project_root / "ui"))

    def health_only(self, method, url, **kwargs):
        assert method == "GET" and url.endswith("/health")
        return httpx.Response(200, json={"status": "ok"}, request=httpx.Request(method, url))

    monkeypatch.setattr(httpx.Client, "request", health_only)
    ui = AppTest.from_file(str(settings.project_root / "ui/streamlit_app.py"))
    ui.session_state["result"] = result
    ui.run(timeout=20)
    assert not ui.exception
    assert any(item.label == "🤖 Hoạt động trợ lý" for item in ui.tabs)
    labels = {item.label for item in ui.metric}
    assert {"Thời gian xử lý", "Token LLM", "Phí LLM ước tính", "LLM / MCP calls"} <= labels
    assert any(item.label == "Agent, prompt version và lịch sử các lượt" for item in ui.expander)


def test_learning_html_has_valid_local_links_and_sections():
    class Document(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []
            self.links = []

        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if "id" in values:
                self.ids.append(values["id"])
            if tag == "a":
                self.links.append(values["href"])

    path = settings.project_root / "docs/LEARN_THE_REPO.html"
    document = Document()
    document.feed(path.read_text("utf-8"))
    assert len(document.ids) == len(set(document.ids))
    anchors = [link[1:] for link in document.links if link.startswith("#")]
    assert len(anchors) == 11 and all(anchor in document.ids for anchor in anchors)
    for link in document.links:
        if link.startswith("../"):
            assert (path.parent / link).is_file(), link
