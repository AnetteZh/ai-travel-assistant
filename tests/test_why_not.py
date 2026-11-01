"""«Почему не другой вариант»: причины отказа от более дешёвых вариантов считает код."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from travel_data.models import WeatherSummary

from agent_core.contracts import RequestExtraction, TripRequestPatch
from agent_core.graph import TravelAgent
from trip_checks import why_not


@pytest.fixture(autouse=True)
def no_live_weather(monkeypatch):
    from travel_data import tools

    monkeypatch.setattr(tools, "get_weather", lambda destination_id, target_date: WeatherSummary(
        destination_id=destination_id, target_date=target_date, temperature_min_c=10, temperature_max_c=20,
        description="x", source="test", is_synthetic=True,
    ))


def plan(destination: str, **extra):
    start = date.today() + timedelta(days=30)
    patch = TripRequestPatch(origin="Москва", destination=destination, date_from=start, date_to=start + timedelta(days=5), **extra)
    return TravelAgent(lambda _m: RequestExtraction(patch=patch)).run("тест")


@pytest.mark.parametrize("destination", ["Сочи", "Казань", "Мурманск"])
def test_rejected_options_are_cheaper_and_have_a_reason(destination):
    state = plan(destination)
    rejected = why_not(state)
    assert rejected
    for item in rejected:
        assert item.saving > 0
        assert item.reason
        assert item.sentence().startswith(("«", "Поезд", "Рейс"))


def test_strong_reasons_come_first():
    state = plan("Сочи")
    codes = [item.code for item in why_not(state)]
    assert codes[0] in {"DATE_MISMATCH", "LATE_ARRIVAL", "CHECKIN_GAP"}


def test_why_not_reaches_ui_and_chat_without_being_cut_by_guardrails():
    state = plan("Казань")
    comparison = next(block for block in state.blocks if block.type == "comparison")
    assert comparison.data["why_not"]
    first = comparison.data["why_not"][0]["text"]
    assert "Почему не дешевле:" in state.response_text
    assert first in state.response_text  # цифры экономии не вырезаны защитой ответа


def test_no_plan_no_rejections():
    state = TravelAgent(lambda _m: RequestExtraction(intent="suggest_destinations")).run("куда поехать")
    assert why_not(state) == []
