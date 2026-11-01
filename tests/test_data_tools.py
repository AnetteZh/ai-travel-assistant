from __future__ import annotations

from datetime import date, timedelta

from travel_data import (
    Cart,
    CartSelection,
    Guests,
    TravelRequest,
    book,
    get_weather,
    price_cart,
    search_hotels,
    search_transfers,
    search_transport,
    suggest_destinations,
)
from travel_data.world import load_world


def test_world_counts_and_synthetic_flags():
    world = load_world()
    assert len(world.destinations) == 20
    assert len(world.hotels) == 200
    assert len(world.transport_templates) == 760
    assert len(world.transfer_templates) == 120
    assert all(h.is_synthetic for h in world.hotels)


def test_search_hotels_returns_only_hotels_matching_hard_pool_requirement():
    start = date.today() + timedelta(days=30)
    request = TravelRequest(
        destination_id="dest-sochi",
        date_from=start,
        date_to=start + timedelta(days=4),
        hard_requirements=["has_pool"],
        soft_preferences=["тихо", "завтрак"],
        guests=Guests(adults=2),
    )
    result = search_hotels(request)
    assert result.options
    assert all(item.hotel.has_pool for item in result.options)
    assert all(not item.violated_requirements for item in result.options)
    assert all(item.evidence for item in result.options)


def test_unmatched_hard_requirements_return_nearby_alternatives_with_explanations():
    start = date.today() + timedelta(days=30)
    request = TravelRequest(
        destination_id="dest-sochi",
        date_from=start,
        date_to=start + timedelta(days=3),
        hard_requirements=["5 stars", "pet-friendly", "has_pool", "spa", "free cancellation"],
        guests=Guests(adults=2, pets=1),
    )
    result = search_hotels(request)
    assert result.options == []
    assert result.alternatives
    assert any(item.violated_requirements for item in result.alternatives)
    assert all(item.cons for item in result.alternatives)
    assert all(item.why_it_fits for item in result.alternatives)


def test_transport_transfer_cart_and_demo_booking_work_end_to_end():
    start = date.today() + timedelta(days=5)
    end = start + timedelta(days=4)
    request = TravelRequest(origin="Москва", destination_id="dest-sochi", date_from=start, date_to=end)
    hotel_result = search_hotels(request, "dest-sochi")
    transport_result = search_transport(request, "dest-sochi")
    assert hotel_result.options
    assert transport_result.outbound
    assert transport_result.return_options

    hotel = hotel_result.options[0].hotel
    outbound = transport_result.outbound[0]
    inbound = transport_result.return_options[0]
    transfer_result = search_transfers("dest-sochi", hotel.id, outbound.arrival_at, "airport", "to_hotel")
    assert transfer_result.options
    taxi = next(item for item in transfer_result.options if item.transfer_type == "taxi")
    departure_transfer_result = search_transfers("dest-sochi", hotel.id, inbound.departure_at, "airport", "to_terminal")
    assert departure_transfer_result.options
    departure_taxi = next(item for item in departure_transfer_result.options if item.transfer_type == "taxi")
    assert departure_taxi.terminal_departure_at == inbound.departure_at
    assert departure_taxi.pickup_at < departure_taxi.terminal_departure_at

    # Simulate a resumed LangGraph process: reprice from selected objects in TripState,
    # not only the in-memory lookup cache populated by the search tools.
    from travel_data import tools as data_tools
    data_tools._TRANSPORT_CACHE.clear()
    data_tools._TRANSFER_CACHE.clear()
    cart = price_cart(CartSelection(
        destination_id="dest-sochi",
        hotel_id=hotel.id,
        date_from=start,
        date_to=end,
        transport_ids=[outbound.id, inbound.id],
        transfer_ids=[taxi.id, departure_taxi.id],
        transport_options=[outbound, inbound],
        transfer_options=[taxi, departure_taxi],
        guests=Guests(adults=2),
        budget_total=100000,
    ))
    assert isinstance(cart, Cart)
    assert cart.total == sum(item.amount for item in cart.items)
    assert cart.total > 0
    booking = book(cart)
    assert getattr(booking, "status", None) == "confirmed_demo"
    assert "Реальная оплата" in booking.message


def test_weather_falls_back_to_explicitly_synthetic_climate_outside_forecast_window():
    target = date.today() + timedelta(days=40)
    weather = get_weather("dest-kazan", target)
    assert weather.is_synthetic is True
    assert weather.source == "synthetic monthly climate estimate"
    assert weather.temperature_min_c < weather.temperature_max_c


def test_empty_destination_query_is_safe():
    result = suggest_destinations(TravelRequest(date_from=date.today() + timedelta(days=30), date_to=date.today() + timedelta(days=35)))
    assert len(result.options) in range(3, 6)
    assert result.assumptions
    assert all(item.evidence for item in result.options)


def test_met_norway_forecast_is_used_before_open_meteo(monkeypatch):
    from datetime import datetime, timezone
    from travel_data import tools as data_tools
    from travel_data.world import get_destination

    target = date.today() + timedelta(days=2)
    destination = get_destination("dest-sochi")
    assert destination is not None
    payload = {
        "properties": {
            "timeseries": [
                {
                    "time": datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9).isoformat().replace("+00:00", "Z"),
                    "data": {
                        "instant": {"details": {"air_temperature": 19.0}},
                        "next_6_hours": {"summary": {"symbol_code": "partlycloudy_day"}},
                    },
                },
                {
                    "time": datetime.combine(target, datetime.min.time(), tzinfo=timezone.utc).replace(hour=15).isoformat().replace("+00:00", "Z"),
                    "data": {
                        "instant": {"details": {"air_temperature": 24.0}},
                        "next_6_hours": {"summary": {"symbol_code": "clearsky_day"}},
                    },
                },
            ]
        }
    }
    monkeypatch.setattr(data_tools, "_get_met_payload", lambda _destination: payload)
    monkeypatch.setattr(data_tools, "_fetch_open_meteo", lambda *_args: None)
    data_tools._WEATHER_CACHE.pop((destination.id, target), None)

    result = data_tools.get_weather(destination.id, target)

    assert result.is_synthetic is False
    assert result.source == "MET Norway Locationforecast API"
    assert result.temperature_min_c == 19.0
    assert result.temperature_max_c == 24.0
    assert result.precipitation_probability is None
