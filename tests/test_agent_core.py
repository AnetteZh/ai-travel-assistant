from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from travel_data.models import WeatherSummary

from agent_core.api import create_app
from agent_core.config import Settings, get_settings
from agent_core.contracts import RequestExtraction, TripRequest, TripRequestPatch, TripState
from agent_core.cli import format_summary, run_cli
from agent_core.graph import (
    CoreError,
    TravelAgent,
    _build_structured_model,
    _checked_call,
    _parse_with_model,
    _safe_model_error,
)
from travel_data.world import get_destination


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


def test_missing_destination_returns_destination_cards_and_synthetic_markers():
    agent = TravelAgent(parser_for(TripRequestPatch(), "suggest_destinations"))
    state = agent.run("Предложи направления")

    assert state.destinations is not None
    destination_block = next(block for block in state.blocks if block.type == "destination_cards")
    assert len(destination_block.data["items"]) <= 5
    assert state.blocks[0].type == "message"
    assert any(block.type == "destination_cards" for block in state.blocks)
    assert state.destinations.is_synthetic
    assert all(
        evidence.is_synthetic
        for item in state.destinations.options
        for evidence in item.evidence
    )


def test_destination_search_uses_data_and_creates_programmatic_cart():
    start = date.today() + timedelta(days=30)
    patch = TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=4),
    )
    state = TravelAgent(parser_for(patch)).run("Хочу в Сочи")

    assert state.hotels and state.hotels.options
    assert state.transport and state.transport.outbound
    assert state.plan is not None
    assert state.cart is not None
    assert state.cart.total == sum(item.amount for item in state.cart.items)
    assert state.version == 1
    assert all(evidence.is_synthetic for evidence in state.plan.hotel.evidence)
    assert state.tool_calls["search_transfers"] <= 3
    assert all(item.evidence for item in state.cart.items)
    timeline = next(block for block in state.blocks if block.type == "trip_timeline")
    assert timeline.data["events"][0]["event"] == "outbound"


def test_follow_up_about_destination_returns_details_instead_of_repeating_options():
    first_state = TravelAgent(parser_for(TripRequestPatch(), "suggest_destinations")).run(
        "Посоветуй направления"
    )
    assert first_state.destinations is not None
    selected = first_state.destinations.options[1]
    followup_agent = TravelAgent(
        lambda _message: RequestExtraction(
            intent="details",
            detail_target=selected.destination.id,
        )
    )
    detailed = followup_agent.run(
        "Расскажи подробнее про второй вариант",
        first_state,
    )

    assert detailed.response_mode == "details"
    assert detailed.response_text is not None
    assert selected.destination.name in detailed.response_text
    assert f"{selected.estimated_trip_price:,}".replace(",", " ") in detailed.response_text
    assert detailed.response_text.startswith("Подробнее:")
    assert detailed.response_text.count("Направления:") == 0
    assert "синтет" not in detailed.response_text.casefold()
    assert format_summary(detailed) == detailed.response_text


def test_follow_up_about_hotel_reports_grounded_hotel_facts():
    start = date.today() + timedelta(days=30)
    trip = TravelAgent(parser_for(TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=4),
    ))).run("Составь поездку в Сочи")
    assert trip.hotels is not None and trip.hotels.options
    selected = trip.hotels.options[0]

    detailed = TravelAgent(
        lambda _message: RequestExtraction(
            intent="details",
            detail_target=selected.hotel.id,
        )
    ).run("Что известно об этом отеле?", trip)

    assert detailed.response_text is not None
    assert selected.hotel.name in detailed.response_text
    assert f"{selected.hotel.price_per_night:,}".replace(",", " ") in detailed.response_text
    assert "Условия отмены:" in detailed.response_text
    assert "синтет" not in detailed.response_text.casefold()


def test_follow_up_about_selected_plan_includes_schedule_hotel_and_cost_breakdown():
    start = date.today() + timedelta(days=30)
    first_state = TravelAgent(parser_for(TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=4),
    ))).run("Составь поездку в Сочи")
    assert first_state.plan is not None
    assert first_state.cart is not None
    assert first_state.plan.outbound is not None

    detailed = TravelAgent(
        lambda _message: RequestExtraction(
            intent="details",
            detail_target="current_plan",
        )
    ).run("Расскажи подробно про выбранную поездку", first_state)

    assert detailed.response_text is not None
    assert first_state.plan.hotel.hotel.name in detailed.response_text
    assert first_state.plan.hotel.hotel.checkin_from in detailed.response_text
    assert first_state.plan.hotel.hotel.checkout_time in detailed.response_text
    assert first_state.plan.outbound.departure_at.strftime(
        "%d.%m.%Y %H:%M %z"
    ) in detailed.response_text
    assert first_state.plan.outbound.arrival_at.strftime(
        "%d.%m.%Y %H:%M %z"
    ) in detailed.response_text
    if first_state.plan.outbound.transport_type == "flight":
        assert "авиакомпании в данных этого варианта не указано" in detailed.response_text
    assert "Расчёт корзины:" in detailed.response_text
    assert "Подытог позиций:" in detailed.response_text
    assert f"{first_state.cart.total:,}".replace(",", " ") in detailed.response_text
    assert all(item.title in detailed.response_text for item in first_state.cart.items)
    assert "синтет" not in detailed.response_text.casefold()
    assert detailed.blocks[0].data["text"] == detailed.response_text


def test_follow_up_about_price_explains_saved_cart_components():
    start = date.today() + timedelta(days=30)
    first_state = TravelAgent(parser_for(TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=3),
    ))).run("Составь поездку")
    assert first_state.cart is not None

    detailed = TravelAgent(
        lambda _message: RequestExtraction(
            intent="details",
            detail_target="cart",
        )
    ).run("Из чего складывается цена?", first_state)

    assert detailed.response_text is not None
    assert "Подробный расчёт:" in detailed.response_text
    assert all(item.title in detailed.response_text for item in first_state.cart.items)
    assert f"{first_state.cart.total:,}".replace(",", " ") in detailed.response_text


def test_follow_up_about_flight_discloses_missing_carrier_without_inventing_one():
    start = date.today() + timedelta(days=30)
    trip = TravelAgent(parser_for(TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=3),
    ))).run("Составь поездку")
    assert trip.transport is not None
    flight = next(item for item in trip.transport.outbound if item.transport_type == "flight")

    detailed = TravelAgent(
        lambda _message: RequestExtraction(
            intent="details",
            detail_target=flight.id,
        )
    ).run("Какая авиакомпания и во сколько вылет?", trip)

    assert detailed.response_text is not None
    assert flight.origin_point in detailed.response_text
    assert flight.destination_point in detailed.response_text
    assert flight.departure_at.strftime("%d.%m.%Y %H:%M %z") in detailed.response_text
    assert flight.arrival_at.strftime("%d.%m.%Y %H:%M %z") in detailed.response_text
    assert "Название авиакомпании в данных этого варианта не указано." in detailed.response_text


def test_patch_preserves_unmentioned_request_fields_and_increments_plan_version():
    start = date.today() + timedelta(days=30)
    initial = TripRequest(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=4),
        soft=["тихо"],
    )
    first = TravelAgent(parser_for(TripRequestPatch())).run("", TripState(request=initial))
    patched = TravelAgent(parser_for(TripRequestPatch())).run(
        "",
        first,
        TripRequestPatch(budget_total=80000),
    )

    assert patched.request.budget_total == 80000
    assert patched.request.destination == "Сочи"
    assert patched.request.soft == ["тихо"]
    assert patched.version == first.version + 1
    assert patched.plan_diff is not None


def test_partial_date_patch_preserves_trip_duration_and_other_fields():
    start = date.today() + timedelta(days=30)
    initial = TripState(request=TripRequest(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=4),
        soft=["тихо"],
    ))
    updated_start = start + timedelta(days=10)
    state = TravelAgent(parser_for(TripRequestPatch())).run(
        "",
        initial,
        TripRequestPatch(date_from=updated_start),
    )

    assert state.request.date_from == updated_start
    assert state.request.date_to == updated_start + timedelta(days=4)
    assert state.request.soft == ["тихо"]
    assert any("сохранена прежняя длительность" in item for item in state.assumptions)


def test_request_validation_rejects_invalid_dates_and_extra_fields():
    with pytest.raises(ValueError):
        TripRequest(date_from=date(2026, 10, 26), date_to=date(2026, 10, 25))
    with pytest.raises(ValueError):
        TripRequest.model_validate({"destination": "Сочи", "invented": True})


def test_llm_provider_settings_select_openai_compatible_endpoint(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://llm.example/v1")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.llm_provider == "openai_compatible"
    assert settings.openai_api_key == "test-key"
    assert settings.openai_model == "test-model"
    assert settings.openai_base_url == "https://llm.example/v1"
    get_settings.cache_clear()


def test_llm_provider_settings_select_gigachat(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gigachat")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "test-credentials")
    monkeypatch.setenv("GIGACHAT_MODEL", "GigaChat-Pro")
    monkeypatch.setenv("GIGACHAT_SCOPE", "GIGACHAT_API_B2B")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.llm_provider == "gigachat"
    assert settings.gigachat_credentials == "test-credentials"
    assert settings.gigachat_model == "GigaChat-Pro"
    assert settings.gigachat_scope == "GIGACHAT_API_B2B"
    get_settings.cache_clear()


def test_selected_llm_provider_requires_its_own_secret(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gigachat")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "your-gigachat-credentials")
    get_settings.cache_clear()

    with pytest.raises(CoreError, match="GIGACHAT_CREDENTIALS"):
        _parse_with_model("test")
    get_settings.cache_clear()


def test_model_messages_have_exactly_one_first_system_message(monkeypatch):
    settings = Settings(
        llm_provider="gigachat",
        openai_api_key=None,
        openai_model="gpt-4o-mini",
        openai_base_url=None,
        gigachat_credentials="placeholder",
        gigachat_model="GigaChat",
        gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5,
        max_search_calls_per_tool=3,
        max_repairs=1,
    )

    class FakeModel:
        messages = None

        def invoke(self, messages):
            self.messages = messages
            return RequestExtraction(intent="details", detail_target="dest-sochi")

    fake_model = FakeModel()
    monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: fake_model)
    monkeypatch.setattr("agent_core.graph.get_settings", lambda: settings)

    _parse_with_model("Расскажи подробнее", TripState())

    assert fake_model.messages is not None
    assert fake_model.messages[0].type == "system"
    assert sum(message.type == "system" for message in fake_model.messages) == 1
    assert all(message.type == "human" for message in fake_model.messages[1:])
    assert "Текущие варианты из состояния поездки" in fake_model.messages[1].content
    assert "Расскажи подробнее" in fake_model.messages[1].content


def test_model_error_reports_provider_detail_without_leaking_credentials(monkeypatch):
    secret = "private-test-credential"
    monkeypatch.setenv("LLM_PROVIDER", "gigachat")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", secret)
    get_settings.cache_clear()
    monkeypatch.setattr(
        "agent_core.graph._build_structured_model",
        lambda _settings: type(
            "FailingModel",
            (),
            {"invoke": lambda _self, _messages: (_ for _ in ()).throw(
                ValueError(f"HTTP 401 Authorization: Bearer {secret}")
            )},
        )(),
    )

    with pytest.raises(CoreError, match="GigaChat.*ValueError") as error:
        _parse_with_model("Тестовое сообщение")

    assert "HTTP 401" in str(error.value)
    assert secret not in str(error.value)
    get_settings.cache_clear()


def test_model_error_details_are_limited():
    settings = Settings(
        llm_provider="gigachat",
        openai_api_key=None,
        openai_model="gpt-4o-mini",
        openai_base_url=None,
        gigachat_credentials=None,
        gigachat_model="GigaChat",
        gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5,
        max_search_calls_per_tool=3,
        max_repairs=1,
    )

    detail = _safe_model_error(ValueError("x" * 1000), settings)

    assert len(detail) <= 420


def test_invalid_model_date_retries_once_and_accepts_corrected_structured_output(monkeypatch):
    settings = Settings(
        llm_provider="gigachat",
        openai_api_key=None,
        openai_model="gpt-4o-mini",
        openai_base_url=None,
        gigachat_credentials="placeholder",
        gigachat_model="GigaChat",
        gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5,
        max_search_calls_per_tool=3,
        max_repairs=1,
    )
    invalid = ValidationError.from_exception_data(
        "RequestExtraction",
        [{
            "type": "date_from_datetime_parsing",
            "loc": ("patch", "date_from"),
            "input": "2025-2026",
            "ctx": {"error": "invalid date"},
        }],
    )

    class FakeModel:
        calls = 0

        def invoke(self, _messages):
            self.calls += 1
            if self.calls == 1:
                raise invalid
            return RequestExtraction(
                clarification_question="Уточните даты поездки.",
            )

    fake_model = FakeModel()
    monkeypatch.setenv("LLM_PROVIDER", "gigachat")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "test-credentials")
    from agent_core.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: fake_model)
    monkeypatch.setattr("agent_core.graph.get_settings", lambda: settings)

    result = _parse_with_model("Поездка в 2025-2026")

    assert fake_model.calls == 2
    assert result.clarification_question == "Уточните даты поездки."
    assert result.patch.date_from is None


def test_repeated_invalid_model_date_returns_safe_validation_summary(monkeypatch):
    settings = Settings(
        llm_provider="gigachat",
        openai_api_key=None,
        openai_model="gpt-4o-mini",
        openai_base_url=None,
        gigachat_credentials="placeholder",
        gigachat_model="GigaChat",
        gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5,
        max_search_calls_per_tool=3,
        max_repairs=1,
    )
    invalid = ValidationError.from_exception_data(
        "RequestExtraction",
        [{
            "type": "date_from_datetime_parsing",
            "loc": ("patch", "date_from"),
            "input": "2025-2026",
            "ctx": {"error": "invalid date"},
        }],
    )

    class FakeModel:
        calls = 0

        def invoke(self, _messages):
            self.calls += 1
            raise invalid

    fake_model = FakeModel()
    monkeypatch.setattr("agent_core.graph._build_structured_model", lambda _settings: fake_model)
    monkeypatch.setattr("agent_core.graph.get_settings", lambda: settings)

    with pytest.raises(CoreError, match="GigaChat дважды вернул некорректные данные поездки") as error:
        _parse_with_model("Поездка в 2025-2026")

    assert fake_model.calls == 2
    assert "patch.date_from" in str(error.value)
    assert "2025-2026" not in str(error.value)


def test_both_llm_provider_wrappers_build_structured_parsers():
    openai_parser = _build_structured_model(Settings(
        llm_provider="openai_compatible",
        openai_api_key="placeholder",
        openai_model="test-model",
        openai_base_url="https://llm.example/v1",
        gigachat_credentials=None,
        gigachat_model="GigaChat",
        gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5,
        max_search_calls_per_tool=3,
        max_repairs=1,
    ))
    gigachat_parser = _build_structured_model(Settings(
        llm_provider="gigachat",
        openai_api_key=None,
        openai_model="gpt-4o-mini",
        openai_base_url=None,
        gigachat_credentials="placeholder",
        gigachat_model="GigaChat",
        gigachat_scope="GIGACHAT_API_PERS",
        openai_timeout_seconds=5,
        max_search_calls_per_tool=3,
        max_repairs=1,
    ))

    assert callable(openai_parser.invoke)
    assert callable(gigachat_parser.invoke)


def test_terminal_summary_shows_results_assumptions_and_synthetic_notice():
    start = date.today() + timedelta(days=30)
    agent = TravelAgent(parser_for(TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=4),
        budget_total=1,
    )))
    state = agent.run("Составь поездку")

    summary = format_summary(state)

    assert "Подходящие отели" in summary
    assert "Транспорт:" in summary
    assert "Итого по расчёту Travel Data:" in summary
    assert "Проверка бюджета: превышен" in summary
    assert "синтет" not in summary.casefold()
    assert "hotel-sochi" not in summary
    assert "destination_id" not in summary


def test_interactive_cli_keeps_trip_state_and_accepts_follow_up_patch():
    start = date.today() + timedelta(days=30)

    def parser(message: str) -> RequestExtraction:
        if message == "Хочу в Сочи":
            patch = TripRequestPatch(
                origin="Москва",
                destination="Сочи",
                date_from=start,
                date_to=start + timedelta(days=4),
            )
        else:
            patch = TripRequestPatch(budget_total=1)
        return RequestExtraction(patch=patch)

    messages = iter(["Хочу в Сочи", "Снизь бюджет до 1 рубля", "/state", "/quit"])
    output: list[str] = []

    run_cli(
        TravelAgent(parser),
        input_fn=lambda _prompt: next(messages),
        output_fn=output.append,
    )

    rendered = "\n".join(output)
    assert rendered.count("Core AI — терминальная проверка") == 1
    assert "Сочи" in rendered
    assert "Проверка бюджета: превышен" in rendered
    assert "команды" not in rendered


def test_api_streams_status_blocks_and_completion_without_token_streaming():
    start = date.today() + timedelta(days=30)
    agent = TravelAgent(parser_for(TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=3),
    )))
    client = TestClient(create_app(agent))

    response = client.post("/chat", json={"message": "Составь поездку"})

    assert response.status_code == 200
    assert "event: status" in response.text
    assert "checking_plan" in response.text
    assert "event: block" in response.text
    assert "event: complete" in response.text
    assert client.get("/health").json() == {"status": "ok"}
    complete_data = next(
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"state"' in line
    )
    conversation_id = complete_data["conversation_id"]
    patched = client.patch(
        f"/trips/{conversation_id}",
        json={"patch": {"budget_total": 80000}},
    )
    assert patched.status_code == 200
    assert patched.json()["state"]["request"]["budget_total"] == 80000


def test_api_returns_controlled_error_event_when_model_fails():
    def fail(_):
        raise CoreError("Модель не настроена.")

    client = TestClient(create_app(TravelAgent(fail)))
    response = client.post("/chat", json={"message": "Предложи поездку"})

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "Модель не настроена." in response.text


def test_invalid_destination_prompts_for_one_clarification():
    state = TravelAgent(parser_for(TripRequestPatch(destination="Несуществующий город"))).run("Поеду туда")

    assert state.clarification_question
    assert state.stage == "completed"
    assert not state.hotels


def test_search_call_limit_is_enforced():
    assert get_destination("Сочи") is not None
    with pytest.raises(CoreError, match="Превышен лимит"):
        _checked_call(TripState(tool_calls={"search_hotels": 3}), "search_hotels", lambda: None)


def test_budget_conflict_gets_at_most_one_repair_attempt():
    start = date.today() + timedelta(days=30)
    state = TravelAgent(parser_for(TripRequestPatch(
        origin="Москва",
        destination="Сочи",
        date_from=start,
        date_to=start + timedelta(days=4),
        budget_total=1,
    ))).run("Хочу в Сочи")

    assert state.cart is not None
    assert state.cart.over_budget
    assert state.repair_attempts <= 1
    assert state.tool_calls["search_transfers"] <= 3
    assert any(issue.code == "OVER_BUDGET" for issue in state.issues)
