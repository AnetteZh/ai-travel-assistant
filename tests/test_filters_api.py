"""Поиск по фильтрам интерфейса: POST /trips создаёт поездку из патча без разбора текста."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from travel_data.models import WeatherSummary

from agent_core.api import create_app


@pytest.fixture(autouse=True)
def no_live_weather(monkeypatch):
    from travel_data import tools

    monkeypatch.setattr(tools, "get_weather", lambda destination_id, target_date: WeatherSummary(
        destination_id=destination_id, target_date=target_date, temperature_min_c=10, temperature_max_c=20,
        description="x", source="test", is_synthetic=True,
    ))


def test_filters_without_destination_return_destination_cards():
    start = date.today() + timedelta(days=20)
    client = TestClient(create_app())
    response = client.post("/trips", json={"patch": {
        "origin": "Москва", "date_from": start.isoformat(), "date_to": (start + timedelta(days=5)).isoformat(),
        "guests": {"adults": 2, "children_ages": [5], "pets": 0}, "budget_total": 90000,
    }})
    assert response.status_code == 200
    body = response.json()
    assert "destination_cards" in [block["type"] for block in body["blocks"]]
    request = body["state"]["request"]
    assert request["guests"]["children_ages"] == [5] and request["budget_total"] == 90000

    chosen = client.patch(f"/trips/{body['conversation_id']}", json={"patch": {"destination": "Сочи"}})
    assert chosen.status_code == 200
    assert "cart" in [block["type"] for block in chosen.json()["blocks"]]
    assert chosen.json()["state"]["request"]["date_from"] == start.isoformat()


def test_filters_with_destination_build_a_trip_right_away():
    client = TestClient(create_app())
    response = client.post("/trips", json={"patch": {"origin": "Москва", "destination": "Казань"}})
    assert response.status_code == 200
    assert "option_cards" in [block["type"] for block in response.json()["blocks"]]
