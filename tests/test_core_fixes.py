"""Регрессионные тесты для исправлений Core по итогам прогона с фронтендом.

Все тесты работают без GigaChat и без сети: парсер подменён, погода — фикстура.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from travel_data.models import WeatherSummary
from travel_data.world import get_destination

from agent_core.api import create_app
from agent_core.config import Settings
from agent_core.contracts import RequestExtraction, TripIssue, TripRequestPatch, TripState
from agent_core.graph import TravelAgent, _check_plan, _parse_with_model, _repair_plan, _update_trip


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
            description="Тестовая синтетическая погода",
            source="synthetic test fixture",
            is_synthetic=True,
        ),
    )


def parser_for(patch: TripRequestPatch, intent: str = "plan_trip"):
    return lambda _: RequestExtraction(patch=patch, intent=intent)


def plan_trip(**fields) -> TripState:
    return TravelAgent(parser_for(TripRequestPatch(**fields))).run("Составь поездку")


def arrival_day(state: TripState):
    timezone_name = get_destination(state.plan.destination_id).timezone
    return state.plan.outbound.arrival_at.astimezone(ZoneInfo(timezone_name)).date()


# --- Даты ---------------------------------------------------------------------------------

def test_parser_receives_current_date(monkeypatch):
    settings = Settings(
        llm_provider="gigachat", openai_api_key=None, openai_model="gpt-4o-mini", openai_base_url=None,
        gigachat_credentials="placeholder", gigachat_model="GigaChat", gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5, max_search_calls_per_tool=3, max_repairs=1,
    )

    class FakeModel:
        messages = None

        def invoke(self, messages):
            self.messages = messages
            return RequestExtraction()

    fake = FakeModel()
    monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: fake)
    monkeypatch.setattr("agent_core.graph.get_settings", lambda: settings)

    _parse_with_model("Хочу на неделю в Сочи", TripState())

    assert f"Сегодня {date.today().isoformat()}" in fake.messages[1].content


def test_past_dates_from_model_are_not_applied():
    state = plan_trip(origin="Москва", destination="Сочи", date_from=date(2025, 5, 15), date_to=date(2025, 5, 22))

    assert state.request.date_from is None and state.request.date_to is None
    assert state.plan is not None and state.plan.date_from >= date.today()
    assert any("уже прошли" in item for item in state.assumptions)


# --- Прибытие и заселение -----------------------------------------------------------------

def test_plan_prefers_transport_arriving_on_checkin_day():
    state = plan_trip(origin="Москва", destination="Сочи")

    assert arrival_day(state) == state.plan.date_from
    assert not any(issue.code == "DATE_MISMATCH" for issue in state.issues)
    assert state.cart is not None


def test_arrival_after_checkin_day_is_a_blocking_error():
    state = plan_trip(origin="Москва", destination="Сочи")
    timezone_name = get_destination(state.plan.destination_id).timezone
    late = next(
        option for option in state.transport.outbound
        if option.arrival_at.astimezone(ZoneInfo(timezone_name)).date() > state.plan.date_from
    )
    broken = _update_trip(state, plan=state.plan.model_copy(update={"outbound": late}), cart=None, issues=[], tool_calls={})

    checked = _check_plan(broken)

    mismatch = [issue for issue in checked.issues if issue.code == "DATE_MISMATCH"]
    assert mismatch and mismatch[0].severity == "error"
    assert checked.cart is None  # корзина для сломанного плана не считается


def test_blocking_arrival_conflict_is_repaired_with_existing_options():
    state = plan_trip(origin="Москва", destination="Сочи")
    timezone_name = get_destination(state.plan.destination_id).timezone
    late = next(
        option for option in state.transport.outbound
        if option.arrival_at.astimezone(ZoneInfo(timezone_name)).date() > state.plan.date_from
    )
    checked = _check_plan(_update_trip(state, plan=state.plan.model_copy(update={"outbound": late}), cart=None, issues=[], tool_calls={}))

    repaired = _repair_plan(checked)
    rechecked = _check_plan(repaired)

    assert repaired.repair_attempts == 1
    assert arrival_day(rechecked) == rechecked.plan.date_from
    assert not any(issue.severity == "error" for issue in rechecked.issues)
    assert rechecked.cart is not None
    assert rechecked.tool_calls["search_transfers"] <= 3


def test_when_nothing_arrives_on_first_day_checkin_moves_to_arrival_day():
    state = plan_trip(origin="Москва", destination="Владивосток")

    assert state.plan is not None
    assert arrival_day(state) == state.plan.date_from
    assert not any(issue.code == "DATE_MISMATCH" for issue in state.issues)
    assert state.cart is not None and state.cart.nights == (state.plan.date_to - state.plan.date_from).days
    assert "перенесла заселение" in state.response_text


def test_unresolved_blocking_conflict_is_shown_honestly(monkeypatch):
    """Если исправить не удалось, ошибка остаётся видимой, а план не выдаётся за готовый."""
    from agent_core import graph

    monkeypatch.setattr(graph, "_repair_plan", lambda trip: _update_trip(trip, stage="repair_failed"))
    original = graph._check_plan

    def always_broken(trip):
        checked = original(trip)
        issue = TripIssue(code="LATE_ARRIVAL", severity="error", message="Не найден доступный трансфер на время прибытия.")
        return _update_trip(checked, issues=[*checked.issues, issue], cart=None)

    monkeypatch.setattr(graph, "_check_plan", always_broken)
    state = TravelAgent(parser_for(TripRequestPatch(origin="Москва", destination="Сочи"))).run("x")

    assert state.cart is None
    assert any(issue.severity == "error" for issue in state.issues)
    assert "несостыковки" in state.response_text
    assert "рассчитала стоимость" not in state.response_text


# --- Выбор направления ---------------------------------------------------------------------

def test_suggest_destinations_after_plan_clears_city_but_keeps_other_conditions():
    first = plan_trip(origin="Москва", destination="Сочи", budget_total=80000)

    again = TravelAgent(parser_for(TripRequestPatch(), "suggest_destinations")).run("Покажи другие направления", first)

    assert again.request.destination is None
    assert again.request.origin == "Москва"
    assert again.request.budget_total == 80000
    assert any(block.type == "destination_cards" for block in again.blocks)
    assert not any(block.type == "cart" for block in again.blocks)


def test_destination_identifier_is_stored_as_city_name():
    state = plan_trip(origin="Москва", destination="dest-kaliningrad")

    assert state.request.destination == "Калининград"
    assert state.plan is not None


def test_destination_cards_do_not_show_internal_source_notes():
    state = TravelAgent(parser_for(TripRequestPatch(), "suggest_destinations")).run("Куда поехать?")
    cards = next(block for block in state.blocks if block.type == "destination_cards")

    for item in cards.data["items"]:
        texts = [item.get("why_it_fits") or "", *item.get("pros", []), *item.get("cons", [])]
        assert all("synthetic" not in text.casefold() for text in texts)


# --- Допущения -----------------------------------------------------------------------------

def test_missing_origin_produces_exactly_one_moscow_assumption():
    state = plan_trip(destination="Сочи")
    moscow = [item for item in state.assumptions if "Москв" in item]

    assert len(moscow) == 1


def test_explicit_origin_produces_no_moscow_assumption():
    state = plan_trip(origin="Москва", destination="Сочи")

    assert not any("не указан" in item and "Москв" in item for item in state.assumptions)


# --- Бронирование --------------------------------------------------------------------------

def test_book_endpoint_creates_demo_booking_for_checked_cart():
    agent = TravelAgent(parser_for(TripRequestPatch(origin="Москва", destination="Сочи")))
    client = TestClient(create_app(agent))
    response = client.post("/chat", json={"message": "Составь поездку"})
    conversation_id = response.text.split('"conversation_id": "', 1)[1].split('"', 1)[0]

    booked = client.post(f"/trips/{conversation_id}/book")

    assert booked.status_code == 200
    body = booked.json()
    assert body["booking_id"].startswith("DEMO-")
    assert body["status"] == "confirmed_demo"
    assert "демонстрационная" in body["message"].casefold()


def test_book_endpoint_reports_unknown_trip_and_missing_cart():
    agent = TravelAgent(parser_for(TripRequestPatch(), "suggest_destinations"))
    client = TestClient(create_app(agent))

    unknown = client.post("/trips/unknown/book")
    assert unknown.status_code == 404
    assert unknown.json()["detail"] == "Поездка не найдена."

    response = client.post("/chat", json={"message": "Куда поехать?"})
    conversation_id = response.text.split('"conversation_id": "', 1)[1].split('"', 1)[0]
    no_cart = client.post(f"/trips/{conversation_id}/book")
    assert no_cart.status_code == 409


def test_book_endpoint_refuses_plan_with_blocking_issue():
    good = plan_trip(origin="Москва", destination="Сочи")
    broken = _update_trip(good, issues=[TripIssue(code="DATE_MISMATCH", severity="error", message="Конфликт дат.")])

    class FixedAgent:
        async def astream(self, message, trip=None, patch=None):
            yield {"check_plan": {"trip": broken}}

    client = TestClient(create_app(FixedAgent()))
    response = client.post("/chat", json={"message": "x"})
    conversation_id = response.text.split('"conversation_id": "', 1)[1].split('"', 1)[0]

    refused = client.post(f"/trips/{conversation_id}/book")
    assert refused.status_code == 409
    assert "несостыковки" in refused.json()["detail"]


def test_search_tool_limit_still_holds_with_repairs():
    state = plan_trip(origin="Москва", destination="Сочи", budget_total=1)

    assert all(count <= 3 for count in state.tool_calls.values())
    assert state.repair_attempts <= 1


def test_option_cards_list_the_planned_transport_first():
    state = plan_trip(origin="Москва", destination="Владивосток")
    cards = next(block for block in state.blocks if block.type == "option_cards")

    assert cards.data["transport_outbound"][0]["id"] == state.plan.outbound.id
    assert cards.data["transport_return"][0]["id"] == state.plan.inbound.id
    assert len(cards.data["transport_outbound"]) <= 3


# --- Сверка ответа модели с текстом пользователя --------------------------------------------

from agent_core.graph import _ground_extraction  # noqa: E402
from travel_data import Guests  # noqa: E402


def extraction(**patch) -> RequestExtraction:
    return RequestExtraction(patch=TripRequestPatch(**patch))


def test_budget_without_amount_in_message_is_not_applied():
    grounded = _ground_extraction(extraction(destination="Сочи", budget_total=10000), "Хочу в Сочи недорого", None)
    assert grounded.patch.budget_total is None
    assert grounded.patch.destination == "Сочи"

    kept = _ground_extraction(extraction(budget_total=80000), "бюджет 80 тысяч", None)
    assert kept.patch.budget_total == 80000


def test_dates_without_any_date_mention_are_not_applied():
    today = date.today()
    grounded = _ground_extraction(
        extraction(destination="Сочи", date_from=today, date_to=today + timedelta(days=7)),
        "Хочу на неделю в Сочи",
        None,
    )
    assert grounded.patch.date_from is None and grounded.patch.date_to is None

    explicit = _ground_extraction(
        extraction(date_from=date(2030, 11, 15), date_to=date(2030, 11, 22)), "с 15 по 22 ноября", None,
    )
    assert explicit.patch.date_from == date(2030, 11, 15)


def test_children_without_ages_trigger_one_question():
    grounded = _ground_extraction(
        extraction(destination="Казань", guests=Guests(adults=1, children_ages=[7])), "в Казань с детьми", None,
    )
    assert grounded.clarification_question and "лет" in grounded.clarification_question
    assert "guests" not in grounded.patch.model_dump(exclude_unset=True)

    with_ages = _ground_extraction(
        extraction(guests=Guests(adults=2, children_ages=[5, 8])), "с детьми 5 и 8 лет", None,
    )
    assert with_ages.clarification_question is None
    assert with_ages.patch.guests.children_ages == [5, 8]


def test_moscow_destination_without_origin_asks_where_from():
    state = plan_trip(destination="Москва")

    assert state.clarification_question and "Из какого города" in state.clarification_question
    assert state.plan is None


def test_destination_cards_drop_latin_source_notes():
    from agent_core.graph import _clean_customer_text

    text = "Подходит по тегам направления и погодным данным (MET Norway Locationforecast API); стоимость — 100 ₽."
    assert _clean_customer_text(text) == "Подходит по тегам направления и погодным данным; стоимость — 100 ₽."
    assert _clean_customer_text("Отель (у моря) рядом") == "Отель (у моря) рядом"


def test_transport_that_already_departed_is_not_used():
    today = date.today()
    state = plan_trip(origin="Москва", destination="Сочи", date_from=today, date_to=today + timedelta(days=3))

    departed = state.plan.outbound.departure_at <= datetime.now(timezone.utc)
    flagged = any(issue.code == "DATE_MISMATCH" and issue.severity == "error" for issue in state.issues)
    assert not departed or flagged  # либо выбран ещё не ушедший рейс, либо ошибка видна


def test_budget_number_is_not_mistaken_for_a_date():
    grounded = _ground_extraction(
        extraction(budget_total=80000, date_from=date(2030, 5, 1), date_to=date(2030, 5, 8)),
        "Хочу на неделю в Сочи, бюджет 80 тысяч",
        None,
    )
    assert grounded.patch.budget_total == 80000
    assert grounded.patch.date_from is None

    for message in ("с 1 по 8 мая", "1.05–8.05", "в следующем месяце"):
        kept = _ground_extraction(extraction(date_from=date(2030, 5, 1), date_to=date(2030, 5, 8)), message, None)
        assert kept.patch.date_from == date(2030, 5, 1), message


# --- Пустые значения от модели ---------------------------------------------------------------

def gigachat_settings():
    return Settings(
        llm_provider="gigachat", openai_api_key=None, openai_model="gpt-4o-mini", openai_base_url=None,
        gigachat_credentials="placeholder", gigachat_model="GigaChat", gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5, max_search_calls_per_tool=3, max_repairs=1,
    )


def test_zero_budget_from_model_is_treated_as_unknown(monkeypatch):
    """GigaChat присылает budget_total: 0, если бюджет не назван. Это не должно ронять ответ."""
    from langchain_core.messages import AIMessage
    from pydantic import ValidationError

    raw = AIMessage(content="", tool_calls=[{
        "name": "RequestExtraction",
        "id": "call_1",
        "args": {"patch": {"destination": "Сочи", "budget_total": 0, "origin": "", "guests": {"adults": 0}}, "intent": "plan_trip"},
    }])
    try:
        RequestExtraction.model_validate(raw.tool_calls[0]["args"])
    except ValidationError as error:
        parsing_error = error

    class FakeModel:
        calls = 0

        def invoke(self, _messages):
            self.calls += 1
            return {"raw": raw, "parsed": None, "parsing_error": parsing_error}

    fake = FakeModel()
    monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: fake)
    monkeypatch.setattr("agent_core.graph.get_settings", gigachat_settings)

    result = _parse_with_model("Хочу на неделю в Сочи", TripState())

    assert fake.calls == 1
    assert result.patch.destination == "Сочи"
    assert result.patch.budget_total is None
    assert "origin" not in result.patch.model_dump(exclude_unset=True)


def test_parsed_structured_output_with_raw_is_accepted(monkeypatch):
    class FakeModel:
        def invoke(self, _messages):
            return {"raw": None, "parsed": RequestExtraction(patch=TripRequestPatch(destination="Казань")), "parsing_error": None}

    monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: FakeModel())
    monkeypatch.setattr("agent_core.graph.get_settings", gigachat_settings)

    assert _parse_with_model("В Казань", TripState()).patch.destination == "Казань"


def test_parser_receives_current_trip_conditions(monkeypatch):
    class FakeModel:
        messages = None

        def invoke(self, messages):
            self.messages = messages
            return RequestExtraction()

    fake = FakeModel()
    monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: fake)
    monkeypatch.setattr("agent_core.graph.get_settings", gigachat_settings)
    trip = plan_trip(origin="Москва", destination="Сочи", budget_total=80000)

    _parse_with_model("На два дня позже", trip)

    content = fake.messages[1].content
    assert "Текущие условия поездки" in content
    assert '"destination": "Сочи"' in content and '"budget_total": 80000' in content


def test_prompt_has_rules_and_examples_for_key_fields():
    from agent_core.config import prompt_path

    prompt = prompt_path().read_text(encoding="utf-8")
    for marker in ("`origin`", "`budget_total`", "`children_ages`", "Недорого", "suggest_destinations", "Примеры"):
        assert marker in prompt


def test_hotel_cards_show_up_to_six_options():
    state = plan_trip(origin="Москва", destination="Сочи")
    cards = next(block for block in state.blocks if block.type == "option_cards")

    assert 3 < len(cards.data["hotels"]) <= 6


def test_travelling_with_pet_makes_pet_friendly_hotels_mandatory():
    state = plan_trip(origin="Москва", destination="Сочи", guests=Guests(adults=2, pets=1))

    assert "с питомцем" in state.request.hard
    cards = next(block for block in state.blocks if block.type == "option_cards")
    assert all(item["hotel"]["has_pets_allowed"] for item in cards.data["hotels"])


def test_health_checks_are_not_logged():
    import logging

    import agent_core.api  # noqa: F401 — фильтр ставится при импорте

    logger = logging.getLogger("uvicorn.access")
    health = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "GET", "/health", "1.1", 200), None)
    chat = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:1", "POST", "/chat", "1.1", 200), None)
    assert not logger.filter(health)
    assert logger.filter(chat)
