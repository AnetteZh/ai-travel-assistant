"""Ответы GigaChat из прогона scripts/eval_agent.py (27.10): Core не должен терять условия поездки.

GigaChat в корректном ответе явно присылает null для всех полей, о которых не было речи,
путает день недели для «на выходные», пропускает названный город и номер выбранного варианта.
Модель здесь подменена: ответы повторяют то, что она реально вернула.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from travel_data.models import Guests, WeatherSummary

from agent_core.config import Settings
from agent_core.contracts import RequestExtraction, TripRequestPatch
from agent_core.graph import TravelAgent, _mentioned_cities, _nearest_weekend


@pytest.fixture(autouse=True)
def no_live_weather(monkeypatch):
    from travel_data import tools

    monkeypatch.setattr(
        tools,
        "get_weather",
        lambda destination_id, target_date: WeatherSummary(
            destination_id=destination_id,
            target_date=target_date,
            temperature_min_c=10,
            temperature_max_c=20,
            description="Тестовая погода",
            source="synthetic test fixture",
            is_synthetic=True,
        ),
    )


def gigachat_settings():
    return Settings(
        llm_provider="gigachat", openai_api_key=None, openai_model="gpt-4o-mini", openai_base_url=None,
        gigachat_credentials="placeholder", gigachat_model="GigaChat", gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5, max_search_calls_per_tool=3, max_repairs=1,
    )


def nulls(**fields) -> TripRequestPatch:
    """Патч «как у GigaChat»: все поля присланы явно, неизвестные — null или пустые."""
    base = {
        "origin": None, "destination": None, "date_from": None, "date_to": None,
        "budget_total": None, "hard": [], "soft": [], "mood": None,
    }
    return TripRequestPatch(**{**base, **fields})


class ScriptedModel:
    """Вместо GigaChat: ответ по последнему сообщению пользователя, в формате include_raw=True."""

    def __init__(self, answers: dict[str, RequestExtraction]):
        self.answers = answers

    def invoke(self, messages):
        text = messages[-1].content.rsplit("Новое сообщение пользователя:\n", 1)[-1]
        return {"raw": None, "parsed": self.answers[text], "parsing_error": None}


@pytest.fixture
def agent_with(monkeypatch):
    def make(answers: dict[str, RequestExtraction]) -> TravelAgent:
        monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: ScriptedModel(answers))
        monkeypatch.setattr("agent_core.graph.get_settings", gigachat_settings)
        return TravelAgent()
    return make


def run(agent: TravelAgent, *turns: str):
    state = None
    for turn in turns:
        state = agent.run(turn, state)
    return state


def block_types(state) -> set[str]:
    return {block.type for block in state.blocks}


SOCHI_100K = RequestExtraction(intent="plan_trip", patch=nulls(origin="Москва", destination="Сочи", budget_total=100000))


def test_model_nulls_on_update_keep_origin_and_destination(agent_with):
    agent = agent_with({
        "Сочи из Москвы, бюджет 100 тысяч": SOCHI_100K,
        "Давай бюджет поменьше, тысяч 60": RequestExtraction(
            intent="update", patch=nulls(budget_total=60000, guests=Guests(adults=2))
        ),
    })
    state = run(agent, "Сочи из Москвы, бюджет 100 тысяч", "Давай бюджет поменьше, тысяч 60")
    assert (state.request.origin, state.request.destination, state.request.budget_total) == ("Москва", "Сочи", 60000)


def test_shifted_dates_keep_destination(agent_with):
    start = date.today() + timedelta(days=30)
    first = f"Сочи из Москвы с {start:%d.%m} по {start + timedelta(days=7):%d.%m}"
    agent = agent_with({
        first: RequestExtraction(patch=nulls(origin="Москва", destination="Сочи", date_from=start, date_to=start + timedelta(days=7))),
        "А если на два дня позже?": RequestExtraction(
            intent="update", patch=nulls(date_from=start + timedelta(days=2), date_to=start + timedelta(days=9))
        ),
    })
    state = run(agent, first, "А если на два дня позже?")
    assert state.request.destination == "Сочи"
    assert state.request.date_from == start + timedelta(days=2)
    assert "plan_diff" in block_types(state)


def test_other_destinations_keep_origin_and_budget(agent_with):
    agent = agent_with({
        "Сочи из Москвы, бюджет 100 тысяч": SOCHI_100K,
        "Покажи другие направления": RequestExtraction(intent="suggest_destinations", patch=nulls()),
    })
    state = run(agent, "Сочи из Москвы, бюджет 100 тысяч", "Покажи другие направления")
    assert state.request.origin == "Москва" and state.request.budget_total == 100000
    assert state.request.destination is None
    assert "destination_cards" in block_types(state)


def test_guests_are_not_reset_by_model_defaults(agent_with):
    agent = agent_with({
        "Казань из Москвы, с нами собака": RequestExtraction(
            patch=nulls(origin="Москва", destination="Казань", guests=Guests(adults=2, pets=1))
        ),
        "Бюджет 50 тысяч": RequestExtraction(intent="update", patch=nulls(budget_total=50000, guests=Guests())),
    })
    state = run(agent, "Казань из Москвы, с нами собака", "Бюджет 50 тысяч")
    assert state.request.guests.pets == 1


def test_weekend_is_nearest_saturday_to_monday(agent_with):
    wrong_sunday = date.today() + timedelta(days=(6 - date.today().weekday()) % 7 or 7)
    agent = agent_with({
        "Из Москвы в Ярославль на выходные": RequestExtraction(
            patch=nulls(origin="Москва", destination="Ярославль", date_from=wrong_sunday, date_to=wrong_sunday + timedelta(days=2))
        ),
    })
    state = run(agent, "Из Москвы в Ярославль на выходные")
    assert state.request.date_from.weekday() == 5
    assert state.request.date_to == state.request.date_from + timedelta(days=2)


@pytest.mark.parametrize(("today", "saturday"), [
    (date(2026, 10, 26), date(2026, 10, 31)),  # понедельник
    (date(2026, 10, 30), date(2026, 10, 31)),  # пятница
    (date(2026, 10, 31), date(2026, 11, 7)),   # суббота — следующие выходные
    (date(2026, 11, 1), date(2026, 11, 7)),    # воскресенье
])
def test_nearest_weekend(today, saturday):
    assert _nearest_weekend(today) == (saturday, saturday + timedelta(days=2))


def test_city_named_in_message_is_not_lost(agent_with):
    message = "Нужен отель с бассейном обязательно, Сочи, вылет из Москвы"
    agent = agent_with({message: RequestExtraction(intent="plan_trip", patch=nulls(hard=["бассейн"]))})
    state = run(agent, message)
    assert (state.request.destination, state.request.origin) == ("Сочи", "Москва")
    assert "option_cards" in block_types(state)


def test_named_city_turns_ideas_into_a_trip(agent_with):
    message = "Хочу в Калининград из Москвы"
    agent = agent_with({message: RequestExtraction(intent="suggest_destinations", patch=nulls())})
    assert run(agent, message).request.destination == "Калининград"


def test_choose_first_suggested_destination(agent_with):
    agent = agent_with({
        "Куда можно поехать из Москвы отдохнуть?": RequestExtraction(intent="suggest_destinations", patch=nulls(origin="Москва")),
        "Давай первый вариант": RequestExtraction(intent="details", patch=nulls(), detail_target="1"),
    })
    first = run(agent, "Куда можно поехать из Москвы отдохнуть?")
    expected = first.destinations.options[0].destination.name
    state = agent.run("Давай первый вариант", first)
    assert state.request.destination == expected
    assert "option_cards" in block_types(state)


def test_ideas_without_city_skip_extra_question(agent_with):
    message = "Не знаю, куда поехать, хочу просто отдохнуть"
    agent = agent_with({message: RequestExtraction(
        intent="question", patch=nulls(), clarification_question="Какой отдых вы предпочитаете?"
    )})
    state = run(agent, message)
    assert state.clarification_question is None
    assert "destination_cards" in block_types(state)


def test_question_about_children_age_is_kept_for_ideas(agent_with):
    message = "Хочу куда-нибудь к тёплому морю, недорого, с ребёнком"
    agent = agent_with({message: RequestExtraction(intent="suggest_destinations", patch=nulls(soft=["у моря"]))})
    assert "лет" in (run(agent, message).clarification_question or "")


@pytest.mark.parametrize(("text", "expected"), [
    ("еду из питера в москву на неделю", (["Москва"], ["Санкт-Петербург"])),
    ("хочу в питер из мск на недельку вдвоем", (["Санкт-Петербург"], ["Москва"])),
    ("в сочи, вылет из москвы", (["Сочи"], ["Москва"])),
    ("на выходные в нижний новгород", (["Нижний Новгород"], [])),
    ("хочу в париж", ([], [])),
])
def test_mentioned_cities(text, expected):
    assert _mentioned_cities(text) == expected


# --- Ответы из прогона 27.10 22:19: строка «null», даты и суммы словами ----------------------

def as_gigachat(args: dict):
    """Ответ в формате include_raw=True: как вернул бы langchain-gigachat для этих аргументов."""
    from langchain_core.messages import AIMessage
    from pydantic import ValidationError

    raw = AIMessage(content="", tool_calls=[{"name": "RequestExtraction", "id": "call_1", "args": args}])
    try:
        return {"raw": raw, "parsed": RequestExtraction.model_validate(args), "parsing_error": None}
    except ValidationError as error:
        return {"raw": raw, "parsed": None, "parsing_error": error}


class RawModel:
    def __init__(self, answers: dict[str, dict]):
        self.answers = answers
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        text = messages[-1].content.rsplit("Новое сообщение пользователя:\n", 1)[-1]
        return as_gigachat(self.answers[text])


@pytest.fixture
def raw_agent(monkeypatch):
    def make(answers: dict[str, dict]):
        model = RawModel(answers)
        monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: model)
        monkeypatch.setattr("agent_core.graph.get_settings", gigachat_settings)
        return TravelAgent(), model
    return make


def test_null_string_clarification_does_not_block_the_trip(raw_agent):
    message = "Хочу в Казань недорого, вылет из Москвы"
    agent, _ = raw_agent({message: {
        "intent": "plan_trip", "clarification_question": "null",
        "patch": {"origin": "Москва", "destination": "Казань", "budget_total": "null", "soft": ["недорого"]},
    }})
    state = run(agent, message)
    assert state.clarification_question is None
    assert state.response_text != "null"
    assert "option_cards" in block_types(state)


def test_dates_written_as_words_are_understood_without_retry(raw_agent):
    message = "В Сочи из Москвы с 15 по 22 декабря"
    agent, model = raw_agent({message: {
        "intent": "plan_trip",
        "patch": {"origin": "Москва", "destination": "Сочи", "date_from": "15 декабря", "date_to": "22 декабря"},
    }})
    state = run(agent, message)
    assert (state.request.date_from.month, state.request.date_from.day) == (12, 15)
    assert (state.request.date_to.month, state.request.date_to.day) == (12, 22)
    assert model.calls == 1
    assert "option_cards" in block_types(state)


def test_dates_from_text_when_model_lost_them(raw_agent):
    message = "В Сочи из Москвы с 15 по 22 декабря"
    agent, _ = raw_agent({message: {"intent": "plan_trip", "clarification_question": "null", "patch": {"destination": "Сочи"}}})
    state = run(agent, message)
    assert (state.request.date_from.day, state.request.date_to.day) == (15, 22)


def test_shift_two_days_later_from_text(raw_agent):
    first = "Хочу в Сочи из Москвы с 10 по 17 декабря"
    agent, _ = raw_agent({
        first: {"intent": "plan_trip", "patch": {"origin": "Москва", "destination": "Сочи"}},
        "А если на два дня позже?": {"intent": "question", "clarification_question": "null", "patch": {}},
    })
    state = run(agent, first, "А если на два дня позже?")
    assert (state.request.date_from.day, state.request.date_to.day) == (12, 19)
    assert state.clarification_question is None
    assert "plan_diff" in block_types(state)


def test_budget_change_as_words(raw_agent):
    first = "Сочи из Москвы, бюджет 100 тысяч"
    second = "Давай бюджет поменьше, тысяч 60"
    agent, _ = raw_agent({
        first: {"intent": "plan_trip", "patch": {"origin": "Москва", "destination": "Сочи", "budget_total": 100000}},
        second: {"intent": "update", "clarification_question": "null", "patch": {"budget_total": "60 тысяч", "origin": "null"}},
    })
    state = run(agent, first, second)
    assert (state.request.origin, state.request.budget_total) == ("Москва", 60000)
    assert state.clarification_question is None


def test_dog_from_text_when_model_lost_it(raw_agent):
    agent, _ = raw_agent({
        "Казань из Москвы вдвоём": {"intent": "plan_trip", "patch": {"origin": "Москва", "destination": "Казань"}},
        "С нами едет собака": {"intent": "update", "clarification_question": "null", "patch": {}},
    })
    state = run(agent, "Казань из Москвы вдвоём", "С нами едет собака")
    assert state.request.guests.pets == 1 and state.request.guests.adults == 2
    assert any("питом" in item for item in state.request.hard)


def test_other_destinations_keep_budget_when_model_sends_null_strings(raw_agent):
    first = "Сочи из Москвы, бюджет 80 тысяч"
    agent, _ = raw_agent({
        first: {"intent": "plan_trip", "patch": {"origin": "Москва", "destination": "Сочи", "budget_total": 80000}},
        "Покажи другие направления": {"intent": "suggest_destinations", "patch": {"budget_total": "null", "origin": "null", "destination": "null"}},
    })
    state = run(agent, first, "Покажи другие направления")
    assert (state.request.origin, state.request.budget_total, state.request.destination) == ("Москва", 80000, None)


def test_required_pool_from_text(raw_agent):
    message = "Нужен отель с бассейном обязательно, Сочи, вылет из Москвы"
    agent, _ = raw_agent({message: {"intent": "plan_trip", "clarification_question": "null", "patch": {"destination": "Сочи"}}})
    state = run(agent, message)
    assert "бассейн" in state.request.hard
    assert "option_cards" in block_types(state)


def test_child_age_from_text(raw_agent):
    message = "Мы семья: двое взрослых и ребёнок 3 года, хотим на море, бюджет 120 тысяч, вылет из Москвы, нужна бесплатная отмена"
    agent, _ = raw_agent({message: {
        "intent": "suggest_destinations",
        "patch": {"origin": "Москва", "budget_total": 120000, "guests": {"adults": 2, "children_ages": []}, "hard": ["бесплатная отмена"]},
    }})
    state = run(agent, message)
    assert state.request.guests.children_ages == [3]
    assert state.clarification_question is None


def test_per_night_price_is_not_a_budget(raw_agent):
    message = "Сочи из Москвы, отель до 5 тысяч за ночь"
    agent, _ = raw_agent({message: {"intent": "plan_trip", "patch": {"origin": "Москва", "destination": "Сочи"}}})
    assert run(agent, message).request.budget_total is None
