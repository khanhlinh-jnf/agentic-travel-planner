from datetime import date

import pytest

from app.mcp.providers import booking as booking_provider
from app.mcp.providers import serpapi as serpapi_provider
from app.mcp.providers import service
from app.mcp.providers.booking import _number, normalize_hotels
from app.mcp.providers.serpapi import _integer, normalize_flight_search, normalize_places


def test_provider_normalizers_accept_realistic_payloads() -> None:
    assert _integer("1,450,000 VND") == 1_450_000
    assert _number("1,250,000 VND") == 1_250_000

    flights = normalize_flight_search(
        {
            "best_flights": [
                {
                    "flights": [
                        {
                            "airline": "Demo Air",
                            "flight_number": "DA 101",
                            "departure_airport": {"time": "2099-10-15 09:00"},
                            "arrival_airport": {"time": "2099-10-15 10:30"},
                        }
                    ],
                    "total_duration": 90,
                    "price": 1_450_000,
                    "booking_token": "ephemeral-token",
                }
            ]
        },
        origin="SGN",
        destination="DAD",
        departure_date=date(2099, 10, 15),
        return_date=None,
        adults=2,
    )
    assert flights.source == "serpapi"
    assert flights.results[0].total_price == 2_900_000
    assert "booking_token" not in flights.results[0].model_dump()

    hotels = normalize_hotels(
        [
            {
                "name": "Demo Hotel",
                "price": 3_750_000,
                "review_score": 8.7,
                "room_type": "Deluxe",
                "location": "Sơn Trà",
                "link": "https://booking.example/hotel",
            }
        ],
        destination="Đà Nẵng",
        check_in_date=date(2099, 10, 15),
        check_out_date=date(2099, 10, 18),
    )
    assert hotels[0].source == "booking"
    assert hotels[0].nightly_price == 1_250_000
    assert hotels[0].total_price == 3_750_000

    places = normalize_places(
        {
            "local_results": [
                {
                    "title": "Demo Restaurant",
                    "type": "Vietnamese restaurant",
                    "address": "1 Demo Street, Da Nang",
                    "rating": 4.6,
                    "reviews": 1234,
                    "price": "₫₫",
                }
            ]
        }
    )
    assert places[0].source == "serpapi"
    assert places[0].rating == 4.6
    assert places[0].review_count == 1234
    assert places[0].source_url.startswith("https://www.google.com/maps/search/")


@pytest.mark.asyncio
async def test_provider_falls_back_as_one_mock_response(monkeypatch) -> None:
    monkeypatch.setattr(service.settings, "use_mock_travel_data", False)
    monkeypatch.setattr(service.settings, "serpapi_api_key", "test-key")

    async def fail_live(**kwargs):
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(service, "search_flights_live", fail_live)
    result = await service.search_flights(
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


@pytest.mark.asyncio
async def test_live_mcp_tool_contracts_are_normalized(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    async def fake_call(provider: str, name: str, arguments: dict):
        calls.append((provider, name, arguments))
        if provider == "serpapi":
            is_return = bool(arguments.get("params", {}).get("departure_token"))
            return {
                "best_flights": [
                    {
                        "flights": [
                            {
                                "airline": "Demo Air",
                                "departure_airport": {"time": "2099-10-15 09:00"},
                                "arrival_airport": {"time": "2099-10-15 10:30"},
                            }
                        ],
                        "price": 1_000_000,
                        "departure_token": None if is_return else "departure-token",
                        "booking_token": "booking-token" if is_return else None,
                    }
                ]
            }
        return [
            {
                "name": "Demo Hotel",
                "price": 900_000,
                "review_score": 8.5,
                "link": "https://booking.example/demo",
            }
        ]

    monkeypatch.setattr(serpapi_provider.mcp_client, "call_provider_tool", fake_call)
    monkeypatch.setattr(booking_provider.mcp_client, "call_provider_tool", fake_call)

    flight_result = await serpapi_provider.search_flights_live(
        origin="TP.HCM",
        destination="Đà Nẵng",
        departure_date=date(2099, 10, 15),
        return_date=date(2099, 10, 18),
        adults=2,
    )
    hotel_result = await booking_provider.search_hotels_live(
        destination="Đà Nẵng",
        check_in_date=date(2099, 10, 15),
        check_out_date=date(2099, 10, 18),
        adults=2,
        max_price=1_000_000,
    )

    assert flight_result.source == "serpapi"
    assert flight_result.results[0].outbound_leg
    assert flight_result.results[0].return_leg
    assert flight_result.provider_call_count == 2
    assert hotel_result.source == "booking"
    assert calls[0][0:2] == ("serpapi", "search")
    assert calls[0][2]["params"]["engine"] == "google_flights"
    assert calls[1][2]["params"]["departure_token"] == "departure-token"
    assert calls[2][0:2] == ("booking", "search_hotels")
    assert calls[2][2]["checkin_date"] == "2099-10-15"


@pytest.mark.asyncio
async def test_round_trip_recovers_with_two_one_way_searches(monkeypatch) -> None:
    async def fake_call(provider: str, name: str, arguments: dict):
        assert (provider, name) == ("serpapi", "search")
        params = arguments["params"]
        if params["type"] == "1":
            return {
                "best_flights": [
                    {
                        "flights": [
                            {
                                "airline": "Demo Air",
                                "departure_airport": {"time": "2099-10-15 09:00"},
                                "arrival_airport": {"time": "2099-10-15 10:30"},
                            }
                        ],
                        "price": 1_000_000,
                    }
                ]
            }
        price = 1_000_000 if params["departure_id"] == "SGN" else 1_500_000
        return {
            "best_flights": [
                {
                    "flights": [
                        {
                            "airline": "Demo Air",
                            "departure_airport": {"time": "2099-10-15 09:00"},
                            "arrival_airport": {"time": "2099-10-15 10:30"},
                        }
                    ],
                    "price": price,
                }
            ]
        }

    monkeypatch.setattr(serpapi_provider.mcp_client, "call_provider_tool", fake_call)

    result = await serpapi_provider.search_flights_live(
        origin="TP.HCM",
        destination="Đà Nẵng",
        departure_date=date(2099, 10, 15),
        return_date=date(2099, 10, 18),
        adults=2,
    )

    assert result.source == "serpapi"
    assert result.provider_call_count == 4
    assert result.results[0].return_leg
    assert result.results[0].price_per_person == 2_500_000
    assert "hai kết quả một chiều" in (result.warning or "")
