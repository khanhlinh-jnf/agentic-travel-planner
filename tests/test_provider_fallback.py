from datetime import date

from app.mcp.providers import service
from app.mcp.providers.openai_web import _valid_source_url, _vnd_integer


def test_web_normalizers_accept_common_provider_formats() -> None:
    allowed = {"https://www.booking.com/searchresults.html?dest=da-nang"}
    item = {"source_url": "https://booking.com/hotel/vn/example.html"}

    assert _valid_source_url(item, allowed) == item["source_url"]
    assert _vnd_integer("1,450,000 VND") == 1_450_000


def test_provider_falls_back_as_one_mock_response(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "use_mock_travel_data", False)

    def fail_web(**kwargs):
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(service, "search_flights_web", fail_web)
    result = service.search_flights(
        origin="TP.HCM",
        destination="Đà Nẵng",
        departure_date=date(2099, 10, 15),
        return_date=date(2099, 10, 18),
        adults=2,
    )

    assert result.source == "mock"
    assert result.results
    assert all(option.source == "mock" for option in result.results)
    assert "TimeoutError" in (result.warning or "")
