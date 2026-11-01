"""Подборка направлений: 5 карточек и «покажи ещё» без повторов."""

from __future__ import annotations

import pytest
from travel_data import suggest_destinations
from travel_data.models import TravelRequest, WeatherSummary

from agent_core.contracts import RequestExtraction, TripRequestPatch
from agent_core.graph import MAX_DESTINATION_CARDS, TravelAgent


@pytest.fixture(autouse=True)
def no_live_weather(monkeypatch):
    from travel_data import tools

    monkeypatch.setattr(
        tools,
        "get_weather",
        lambda destination_id, target_date: WeatherSummary(
            destination_id=destination_id, target_date=target_date, temperature_min_c=10,
            temperature_max_c=20, description="x", source="test", is_synthetic=True,
        ),
    )


def ask(message: str, trip=None, intent: str = "suggest_destinations"):
    agent = TravelAgent(lambda _m: RequestExtraction(patch=TripRequestPatch(), intent=intent))
    return agent.run(message, trip)


def card_ids(state) -> list[str]:
    block = next(block for block in state.blocks if block.type == "destination_cards")
    return [item["destination"]["id"] for item in block.data["items"]]


def test_data_excludes_already_shown_destinations():
    first = suggest_destinations(TravelRequest())
    shown = [item.destination.id for item in first.options]
    second = suggest_destinations(TravelRequest(exclude_destination_ids=shown))
    assert second.options
    assert not {item.destination.id for item in second.options} & set(shown)


def test_five_cards_are_shown():
    state = ask("Предложи направления")
    assert len(card_ids(state)) == MAX_DESTINATION_CARDS == 5


def test_show_more_returns_new_destinations_each_time():
    first = ask("Предложи направления")
    second = ask("Покажи другие направления", first)
    third = ask("А ещё варианты?", second)
    ids = [card_ids(first), card_ids(second), card_ids(third)]
    assert not set(ids[0]) & set(ids[1])
    assert not set(ids[2]) & (set(ids[0]) | set(ids[1]))


def test_new_request_starts_fresh_list():
    first = ask("Предложи направления")
    again = ask("Предложи направления", first)
    assert card_ids(again) == card_ids(first)


def test_catalog_runs_out_and_starts_over():
    state = ask("Предложи направления")
    for _ in range(6):  # 19 направлений без Москвы, по 5 за раз
        state = ask("Покажи ещё", state)
    assert card_ids(state)


@pytest.mark.parametrize("wish, expected", [
    ("у моря", "Сочи"),
    ("в горах", "Минеральные Воды"),
    ("северное сияние", "Мурманск"),
    ("городской отдых", "Екатеринбург"),
])
def test_category_wishes_change_the_selection(wish, expected):
    """Пожелание в любой форме слова («у моря», «в горах») меняет подборку."""
    default = {item.destination.name for item in suggest_destinations(TravelRequest()).options}
    chosen = [item.destination.name for item in suggest_destinations(TravelRequest(soft_preferences=[wish])).options]
    assert expected in chosen
    assert set(chosen) != default


def test_nothing_in_budget_is_reported_and_cheapest_are_shown():
    agent = TravelAgent(lambda _m: RequestExtraction(patch=TripRequestPatch(budget_total=5000), intent="suggest_destinations"))
    state = agent.run("Куда поехать за 5 тысяч?")
    block = next(block for block in state.blocks if block.type == "destination_cards")
    assert block.data["nothing_in_budget"] is True
    assert state.response_text.startswith("Под ваш бюджет ничего не найдено")
    prices = [item["estimated_trip_price"] for item in block.data["items"]]
    assert prices == sorted(prices)  # ближайшие к бюджету — самые недорогие


def test_budget_options_that_fit_come_first():
    cheapest = min(item.estimated_trip_price for item in suggest_destinations(TravelRequest()).options)
    agent = TravelAgent(lambda _m: RequestExtraction(patch=TripRequestPatch(budget_total=cheapest + 1), intent="suggest_destinations"))
    state = agent.run("подбери")
    block = next(block for block in state.blocks if block.type == "destination_cards")
    assert block.data["nothing_in_budget"] is False
    assert block.data["items"][0]["estimated_trip_price"] <= cheapest + 1


def test_trip_over_budget_says_nothing_found():
    from datetime import date, timedelta
    start = date.today() + timedelta(days=30)
    patch = TripRequestPatch(origin="Москва", destination="Казань", date_from=start, date_to=start + timedelta(days=5), budget_total=5000)
    state = TravelAgent(lambda _m: RequestExtraction(patch=patch)).run("Казань за 5 тысяч")
    assert state.cart is not None and state.cart.over_budget
    assert state.response_text.startswith("Под ваш бюджет ничего не найдено")
