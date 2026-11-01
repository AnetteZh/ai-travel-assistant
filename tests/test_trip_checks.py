"""Тесты check_plan: на каждое правило — «конфликт есть → код есть», и чистый план без замечаний."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from travel_data.models import WeatherSummary

from agent_core.contracts import RequestExtraction, TripRequest, TripRequestPatch
from agent_core.graph import TravelAgent
from trip_checks import check_cart, check_plan, has_blocking_issues
from trip_checks.plan_checks import _is_within


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


def run_trip(destination: str, **patch_fields):
    start = date.today() + timedelta(days=30)
    patch = TripRequestPatch(
        origin="Москва",
        destination=destination,
        date_from=start,
        date_to=start + timedelta(days=5),
        **patch_fields,
    )
    return TravelAgent(lambda _message: RequestExtraction(patch=patch)).run("тест")


@pytest.fixture()
def kazan():
    state = run_trip("Казань")
    assert state.plan is not None and state.plan.outbound and state.plan.inbound
    return state


def codes(issues):
    return [issue.code for issue in issues]


def local(state, day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm), tzinfo=ZoneInfo("Europe/Moscow"))


def with_hotel(plan, **hotel_fields):
    hotel = plan.hotel.hotel.model_copy(update=hotel_fields)
    return plan.model_copy(update={"hotel": plan.hotel.model_copy(update={"hotel": hotel})})


def shift_outbound(plan, arrival_at: datetime, drop_transfer: bool = True):
    outbound = plan.outbound.model_copy(update={
        "arrival_at": arrival_at,
        "departure_at": arrival_at - timedelta(minutes=plan.outbound.duration_minutes),
    })
    update = {"outbound": outbound}
    if drop_transfer:
        update["arrival_transfer"] = None
    return plan.model_copy(update=update)


# ------------------------------------------------------------ clean plan

def test_clean_plan_has_no_issues(kazan):
    issues = check_plan(
        kazan.plan, kazan.request,
        transfers=kazan.transfers, departure_transfers=kazan.departure_transfers,
    )
    assert issues == []


# ------------------------------------------------------------ dates

def test_arrival_next_day_is_date_mismatch(kazan):
    plan = shift_outbound(kazan.plan, local(kazan, kazan.plan.date_from + timedelta(days=1), 12))
    issues = check_plan(plan, kazan.request)
    assert "DATE_MISMATCH" in codes(issues)
    assert "пропадёт ночей — 1" in issues[0].message


def test_return_before_hotel_checkout_is_date_mismatch(kazan):
    inbound = kazan.plan.inbound.model_copy(update={
        "departure_at": local(kazan, kazan.plan.date_to - timedelta(days=2), 18),
        "arrival_at": local(kazan, kazan.plan.date_to - timedelta(days=2), 20),
    })
    plan = kazan.plan.model_copy(update={"inbound": inbound, "departure_transfer": None})
    assert "DATE_MISMATCH" in codes(check_plan(plan, kazan.request))


def test_return_before_arrival_blocks_the_plan(kazan):
    inbound = kazan.plan.inbound.model_copy(update={
        "departure_at": kazan.plan.outbound.arrival_at - timedelta(hours=1),
    })
    plan = kazan.plan.model_copy(update={"inbound": inbound, "departure_transfer": None})
    issues = check_plan(plan, kazan.request)
    assert has_blocking_issues(issues)


# ------------------------------------------------------------ arrival → transfer → reception → check-in

def test_arrival_after_reception_closes_is_late_arrival(kazan):
    plan = with_hotel(kazan.plan, reception_open_from="07:00", reception_open_until="22:00")
    plan = shift_outbound(plan, local(kazan, plan.date_from, 23, 40))
    issues = check_plan(plan, kazan.request)
    assert codes(issues) == ["LATE_ARRIVAL"]
    assert "23:40" in issues[0].message


def test_early_arrival_is_checkin_gap(kazan):
    plan = with_hotel(kazan.plan, reception_open_from="00:00", reception_open_until="23:59", checkin_from="14:00")
    plan = shift_outbound(plan, local(kazan, plan.date_from, 7))
    issues = check_plan(plan, kazan.request)
    assert codes(issues) == ["CHECKIN_GAP"]
    assert "7 ч" in issues[0].message


def test_wait_under_four_hours_is_fine(kazan):
    plan = with_hotel(kazan.plan, reception_open_from="00:00", reception_open_until="23:59", checkin_from="14:00")
    plan = shift_outbound(plan, local(kazan, plan.date_from, 11))
    assert check_plan(plan, kazan.request) == []


def test_transfer_duration_counts_towards_arrival_at_hotel(kazan):
    """Прилёт в 21:30 — ещё рано, но с трансфером на час у отеля будем после 22:00."""
    plan = with_hotel(kazan.plan, reception_open_from="07:00", reception_open_until="22:00")
    arrival = local(kazan, plan.date_from, 21, 30)
    plan = shift_outbound(plan, arrival, drop_transfer=False)
    transfer = plan.arrival_transfer.model_copy(update={"pickup_at": arrival, "duration_minutes": 60})
    plan = plan.model_copy(update={"arrival_transfer": transfer})
    assert "LATE_ARRIVAL" in codes(check_plan(plan, kazan.request))


def test_time_zones_are_respected(kazan):
    """15:30 UTC — это 18:30 в Москве, но 23:30 в Иркутске (UTC+8)."""
    plan = with_hotel(kazan.plan, reception_open_from="07:00", reception_open_until="22:00")
    arrival_utc = datetime.combine(plan.date_from, time(15, 30), tzinfo=timezone.utc)
    moscow_plan = shift_outbound(plan, arrival_utc)
    irkutsk_plan = moscow_plan.model_copy(update={"destination_id": "dest-irkutsk"})
    assert "LATE_ARRIVAL" not in codes(check_plan(moscow_plan, kazan.request))
    assert "LATE_ARRIVAL" in codes(check_plan(irkutsk_plan, kazan.request))


def test_unavailable_arrival_transfer_is_late_arrival(kazan):
    transfer = kazan.plan.arrival_transfer.model_copy(update={"is_available": False})
    plan = kazan.plan.model_copy(update={"arrival_transfer": transfer})
    assert "LATE_ARRIVAL" in codes(check_plan(plan, kazan.request))


def test_reception_window_across_midnight():
    assert _is_within(time(23, 30), time(22, 0), time(6, 0))
    assert _is_within(time(5, 0), time(22, 0), time(6, 0))
    assert not _is_within(time(12, 0), time(22, 0), time(6, 0))


# ------------------------------------------------------------ hotel → transfer → departure

def test_transfer_arriving_right_before_departure_is_tight(kazan):
    departure = kazan.plan.inbound.departure_at
    transfer = kazan.plan.departure_transfer.model_copy(update={
        "pickup_at": departure - timedelta(minutes=40),
        "duration_minutes": 30,
    })
    plan = kazan.plan.model_copy(update={"departure_transfer": transfer})
    issues = check_plan(plan, kazan.request)
    assert codes(issues) == ["TIGHT_CONNECTION"]


def test_unavailable_departure_transfer(kazan):
    transfer = kazan.plan.departure_transfer.model_copy(update={"is_available": False})
    plan = kazan.plan.model_copy(update={"departure_transfer": transfer})
    assert "TRANSFER_OUTSIDE_HOURS" in codes(check_plan(plan, kazan.request))


# ------------------------------------------------------------ hotel rules, weather, budget

def test_non_refundable_rate_is_flagged(kazan):
    plan = with_hotel(kazan.plan, cancellation_policy="non_refundable")
    issues = check_plan(plan, kazan.request)
    assert codes(issues) == ["NON_REFUNDABLE"]
    assert issues[0].evidence and issues[0].evidence[0].field_path == "cancellation_policy"


def test_pet_in_hotel_without_pets_blocks_the_plan(kazan):
    plan = with_hotel(kazan.plan, has_pets_allowed=False)
    request = kazan.request.model_copy(update={"guests": kazan.request.guests.model_copy(update={"pets": 1})})
    issues = check_plan(plan, request)
    assert "PETS_NOT_ALLOWED" in codes(issues)
    assert has_blocking_issues(issues)


def make_weather(t_max: float, rain: float | None = None) -> WeatherSummary:
    return WeatherSummary(
        destination_id="dest-sochi", target_date=date.today(), temperature_min_c=t_max - 8,
        temperature_max_c=t_max, precipitation_probability=rain, description="x", source="x", is_synthetic=True,
    )


def test_cold_weather_for_sea_trip_is_weather_risk(kazan):
    request = TripRequest(soft=["у моря"])
    assert "WEATHER_RISK" in codes(check_plan(kazan.plan, request, weather=make_weather(14)))
    assert "WEATHER_RISK" not in codes(check_plan(kazan.plan, request, weather=make_weather(27)))


def test_rain_is_weather_risk(kazan):
    assert "WEATHER_RISK" in codes(check_plan(kazan.plan, TripRequest(), weather=make_weather(25, rain=0.8)))


def test_over_budget_reports_percent(kazan):
    cart = kazan.cart
    issues = check_cart(cart, budget_total=int(cart.total / 1.25))
    assert codes(issues) == ["OVER_BUDGET"]
    assert "25.0%" in issues[0].message
    assert check_cart(cart, budget_total=cart.total) == []
    assert check_cart(cart, budget_total=None) == []


# ------------------------------------------------------------ integration with the graph

def test_graph_avoids_overnight_train_date_mismatch():
    """Раньше агент брал ночной поезд с прибытием на следующий день и терял ночь в отеле."""
    state = run_trip("Сочи")
    assert state.cart is not None
    assert "DATE_MISMATCH" not in codes(state.issues)
    arrival_day = state.plan.outbound.arrival_at.astimezone(ZoneInfo("Europe/Moscow")).date()
    assert arrival_day == state.plan.date_from


@pytest.mark.parametrize("destination", ["Казань", "Сочи", "Калининград", "Мурманск", "Иркутск"])
def test_final_plans_have_no_blocking_issues(destination):
    state = run_trip(destination)
    assert state.cart is not None
    assert not has_blocking_issues(state.issues)
    assert not {"DATE_MISMATCH", "LATE_ARRIVAL", "TIGHT_CONNECTION"} & set(codes(state.issues))
