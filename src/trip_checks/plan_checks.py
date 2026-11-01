"""check_plan: проверка состыковок поездки кодом, без LLM.

Вход: собранный план (транспорт туда/обратно, трансферы, отель, даты),
запрос пользователя и, если есть, найденные трансферы и погода.
Выход: список TripIssue с кодами из travel_data.codes.

Правила сравнения времени:
* все datetime в данных timezone-aware (с часовым поясом);
* сравниваем моменты времени напрямую (Python сам приводит к UTC);
* «часы работы» и «время заселения» отеля — местное время направления,
  поэтому момент прибытия переводим в часовой пояс направления.

Серьёзность (severity), согласована с BLOCKING_CODES в agent_core/graph.py:
* "error"   — план невозможен (даты, недоступный трансфер), корзину не считаем,
              граф отправляет план на починку;
* "warning" — план возможен, но пользователь должен увидеть риск;
* "info"    — справочная пометка.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from travel_data.models import Cart, Evidence, TransferSearchResult, WeatherSummary
from travel_data.world import get_destination

if TYPE_CHECKING:  # импорт только для подсказок типов, чтобы не было циклического импорта
    from agent_core.contracts import TravelPlan, TripIssue, TripRequest

# Пороговые значения. Меняются здесь и больше нигде.
MAX_CHECKIN_WAIT_MINUTES = 4 * 60
MIN_TERMINAL_BUFFER_MINUTES = {"flight": 90, "train": 30}
RAIN_PROBABILITY_THRESHOLD = 0.6
WARM_SEA_MIN_TEMPERATURE_C = 20.0
WARM_KEYWORDS = ("мор", "пляж", "тёпл", "тепл", "купа", "загар")


# ---------------------------------------------------------------- helpers

def _issue(code: str, message: str, severity: str = "warning", evidence: Iterable[Evidence] = ()) -> "TripIssue":
    from agent_core.contracts import TripIssue  # ленивый импорт: agent_core импортирует этот модуль

    unique = list({item.id: item for item in evidence}.values())
    return TripIssue(code=code, message=message, severity=severity, evidence=unique)


def _tz(destination_id: str) -> ZoneInfo:
    destination = get_destination(destination_id)
    if destination is None:
        return ZoneInfo("Europe/Moscow")
    try:
        return ZoneInfo(destination.timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def _parse_hhmm(value: str) -> time:
    return datetime.strptime(value[:5], "%H:%M").time()


def _is_within(moment: time, start: time, end: time) -> bool:
    """Попадает ли время в интервал; интервал может переходить через полночь (22:00–06:00)."""
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end


def _hm(minutes: float) -> str:
    minutes = int(round(minutes))
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        return f"{hours} ч {rest} мин"
    if hours:
        return f"{hours} ч"
    return f"{rest} мин"


def _fmt(moment: datetime) -> str:
    return moment.strftime("%d.%m %H:%M")


def _hotel_evidence(plan: "TravelPlan", *fields: str) -> list[Evidence]:
    return [item for item in plan.hotel.evidence if item.field_path in fields]


def _synthetic_evidence(source_type: str, source_id: str, field_path: str, value: object) -> Evidence:
    """Evidence для поля отеля, если Data не положил его в RankedHotel.evidence."""
    return Evidence(
        id=f"ev-{source_type}-{source_id}-{field_path}",
        source_type=source_type,  # type: ignore[arg-type]
        source_id=source_id,
        field_path=field_path,
        value=value,
        is_synthetic=True,
    )


def _hotel_field(plan: "TravelPlan", field: str) -> list[Evidence]:
    found = _hotel_evidence(plan, field)
    if found:
        return found
    hotel = plan.hotel.hotel
    return [_synthetic_evidence("hotel", hotel.id, field, getattr(hotel, field))]


# ---------------------------------------------------------------- rules

def _check_dates(plan: "TravelPlan") -> list["TripIssue"]:
    issues: list[TripIssue] = []
    tz = _tz(plan.destination_id)
    if plan.outbound and plan.outbound.departure_at <= datetime.now(timezone.utc):
        issues.append(_issue(
            "DATE_MISMATCH",
            f"Транспорт туда отправляется {plan.outbound.departure_at.astimezone(tz):%d.%m.%Y в %H:%M}, "
            "это время уже прошло.",
            severity="error",
            evidence=plan.outbound.evidence,
        ))
    if plan.outbound:
        arrival_day = plan.outbound.arrival_at.astimezone(tz).date()
        if arrival_day > plan.date_from:
            lost = (arrival_day - plan.date_from).days
            issues.append(_issue(
                "DATE_MISMATCH",
                f"Прибытие {arrival_day:%d.%m}, а заселение оплачено с {plan.date_from:%d.%m}: "
                f"пропадёт ночей — {lost}.",
                severity="error",
                evidence=plan.outbound.evidence,
            ))
        elif arrival_day < plan.date_from:
            issues.append(_issue(
                "DATE_MISMATCH",
                f"Прибытие {arrival_day:%d.%m}, а отель только с {plan.date_from:%d.%m}: "
                "на первую ночь нет жилья.",
                severity="error",
                evidence=plan.outbound.evidence,
            ))
    if plan.inbound:
        departure_day = plan.inbound.departure_at.astimezone(tz).date()
        if departure_day < plan.date_to:
            issues.append(_issue(
                "DATE_MISMATCH",
                f"Обратный рейс {departure_day:%d.%m}, а отель оплачен до {plan.date_to:%d.%m}: "
                "часть ночей не будет использована.",
                severity="error",
                evidence=plan.inbound.evidence,
            ))
        elif departure_day > plan.date_to:
            issues.append(_issue(
                "DATE_MISMATCH",
                f"Выезд из отеля {plan.date_to:%d.%m}, а обратный рейс только {departure_day:%d.%m}: "
                "между ними нет жилья.",
                severity="error",
                evidence=plan.inbound.evidence,
            ))
    if plan.outbound and plan.inbound and plan.inbound.departure_at <= plan.outbound.arrival_at:
        issues.append(_issue(
            "DATE_MISMATCH",
            "Обратный транспорт отправляется раньше, чем вы прибудете: план невозможен.",
            severity="error",
            evidence=[*plan.outbound.evidence, *plan.inbound.evidence],
        ))
    return issues


def _check_arrival(plan: "TravelPlan", transfers: TransferSearchResult | None) -> list["TripIssue"]:
    """Прилёт → трансфер → ресепшен → заселение."""
    if plan.outbound is None:
        return []
    issues: list[TripIssue] = []
    tz = _tz(plan.destination_id)
    hotel = plan.hotel.hotel
    transfer = plan.arrival_transfer

    if transfer is not None and not transfer.is_available:
        issues.append(_issue(
            "LATE_ARRIVAL",
            "Выбранный трансфер не работает во время прибытия.",
            severity="error",
            evidence=transfer.evidence,
        ))
        transfer = None
    elif transfer is None and transfers is not None and not transfers.options:
        issues.append(_issue(
            "LATE_ARRIVAL",
            f"Прибытие в {plan.outbound.arrival_at.astimezone(tz):%H:%M}: "
            "ни один трансфер в это время не работает, остаётся только такси по факту.",
            severity="error",
            evidence=[item for option in transfers.unavailable_options for item in option.evidence],
        ))

    # Когда человек реально окажется у отеля (местное время направления).
    at_hotel = plan.outbound.arrival_at
    if transfer is not None:
        at_hotel = max(at_hotel, transfer.pickup_at) + timedelta(minutes=transfer.duration_minutes)
    at_hotel_local = at_hotel.astimezone(tz)

    reception_from = _parse_hhmm(hotel.reception_open_from)
    reception_until = _parse_hhmm(hotel.reception_open_until)
    if not _is_within(at_hotel_local.time().replace(tzinfo=None), reception_from, reception_until):
        issues.append(_issue(
            "LATE_ARRIVAL",
            f"У отеля вы будете около {at_hotel_local:%H:%M}, а ресепшен работает "
            f"{hotel.reception_open_from}–{hotel.reception_open_until}. Нужно предупредить отель "
            "или выбрать другой рейс.",
            evidence=[
                *plan.outbound.evidence,
                *_hotel_field(plan, "reception_open_from"),
                *_hotel_field(plan, "reception_open_until"),
            ],
        ))
        return issues

    checkin_at = datetime.combine(at_hotel_local.date(), _parse_hhmm(hotel.checkin_from), tzinfo=tz)
    wait_minutes = (checkin_at - at_hotel_local).total_seconds() / 60
    if wait_minutes > MAX_CHECKIN_WAIT_MINUTES:
        issues.append(_issue(
            "CHECKIN_GAP",
            f"У отеля вы будете в {at_hotel_local:%H:%M}, заселение с {hotel.checkin_from}: "
            f"ждать {_hm(wait_minutes)}. Можно запросить ранний заезд.",
            evidence=[*plan.outbound.evidence, *_hotel_field(plan, "checkin_from")],
        ))
    return issues


def _check_departure(plan: "TravelPlan", departure_transfers: TransferSearchResult | None) -> list["TripIssue"]:
    """Выезд из отеля → трансфер → вылет: успевает ли человек на регистрацию."""
    if plan.inbound is None:
        return []
    issues: list[TripIssue] = []
    tz = _tz(plan.destination_id)
    transfer = plan.departure_transfer

    if transfer is not None and not transfer.is_available:
        issues.append(_issue(
            "TRANSFER_OUTSIDE_HOURS",
            "Трансфер к вокзалу или аэропорту не работает в нужное время.",
            severity="error",
            evidence=transfer.evidence,
        ))
        return issues
    if transfer is None:
        if departure_transfers is not None and not departure_transfers.options:
            issues.append(_issue(
                "TRANSFER_OUTSIDE_HOURS",
                "Не найден трансфер к терминалу на время отправления.",
                severity="error",
                evidence=[item for option in departure_transfers.unavailable_options for item in option.evidence],
            ))
        return issues

    at_terminal = transfer.pickup_at + timedelta(minutes=transfer.duration_minutes)
    buffer_minutes = (plan.inbound.departure_at - at_terminal).total_seconds() / 60
    needed = MIN_TERMINAL_BUFFER_MINUTES[plan.inbound.transport_type]
    if buffer_minutes < needed:
        what = "вылета" if plan.inbound.transport_type == "flight" else "отправления поезда"
        issues.append(_issue(
            "TIGHT_CONNECTION",
            f"Трансфер привезёт к терминалу в {at_terminal.astimezone(tz):%H:%M}, "
            f"до {what} останется {_hm(max(buffer_minutes, 0))}, а нужно хотя бы {_hm(needed)}.",
            evidence=[*transfer.evidence, *plan.inbound.evidence],
        ))
    return issues


def _check_hotel(plan: "TravelPlan", request: "TripRequest") -> list["TripIssue"]:
    issues: list[TripIssue] = []
    hotel = plan.hotel.hotel
    if hotel.cancellation_policy == "non_refundable":
        issues.append(_issue(
            "NON_REFUNDABLE",
            "Тариф отеля невозвратный: при отмене деньги не вернут.",
            evidence=_hotel_field(plan, "cancellation_policy"),
        ))
    if request.guests.pets > 0 and not hotel.has_pets_allowed:
        issues.append(_issue(
            "PETS_NOT_ALLOWED",
            "С вами едет питомец, а отель не принимает животных.",
            severity="error",
            evidence=_hotel_field(plan, "has_pets_allowed"),
        ))
    return issues


def _check_weather(request: "TripRequest", weather: WeatherSummary | None) -> list["TripIssue"]:
    if weather is None:
        return []
    evidence = [
        _synthetic_evidence("weather", weather.destination_id, "temperature_max_c", weather.temperature_max_c)
    ]
    if weather.precipitation_probability is not None and weather.precipitation_probability >= RAIN_PROBABILITY_THRESHOLD:
        return [_issue(
            "WEATHER_RISK",
            f"Высокая вероятность осадков: {round(weather.precipitation_probability * 100)}%.",
            severity="info",
            evidence=evidence,
        )]
    wishes = " ".join([*request.soft, *request.hard, request.mood or ""]).casefold()
    if any(word in wishes for word in WARM_KEYWORDS) and weather.temperature_max_c < WARM_SEA_MIN_TEMPERATURE_C:
        return [_issue(
            "WEATHER_RISK",
            f"Вы хотите тепла, а днём ожидается до {round(weather.temperature_max_c)}°C.",
            severity="info",
            evidence=evidence,
        )]
    return []


# ---------------------------------------------------------------- public API

def check_plan(
    plan: "TravelPlan",
    request: "TripRequest",
    *,
    transfers: TransferSearchResult | None = None,
    departure_transfers: TransferSearchResult | None = None,
    weather: WeatherSummary | None = None,
) -> list["TripIssue"]:
    """Все проверки плана, кроме бюджета (бюджет — check_cart, после расчёта корзины)."""
    return [
        *_check_dates(plan),
        *_check_arrival(plan, transfers),
        *_check_departure(plan, departure_transfers),
        *_check_hotel(plan, request),
        *_check_weather(request, weather),
    ]


def check_cart(cart: Cart, budget_total: int | None) -> list["TripIssue"]:
    """Бюджет считает код: итог корзины против бюджета пользователя."""
    if not budget_total or cart.total <= budget_total:
        return []
    over = cart.total - budget_total
    percent = over / budget_total * 100
    return [_issue(
        "OVER_BUDGET",
        f"Стоимость превышает бюджет на {percent:.1f}%.",
        evidence=[item for cart_item in cart.items for item in cart_item.evidence],
    )]


def has_blocking_issues(issues: Iterable["TripIssue"]) -> bool:
    return any(issue.severity == "error" for issue in issues)
