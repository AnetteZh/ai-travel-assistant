"""«Почему не другой вариант»: объясняем, чем плохи более дешёвые варианты.

Для выбранного плана берём отели и рейсы дешевле выбранных, подставляем их в план
и прогоняем тот же check_plan. Если у дешёвого варианта появляется проблема, которой
нет у выбранного (ресепшен закрыт к приезду, пропадает ночь, невозвратный тариф),
это и есть причина отказа. Всё считает код по данным, модель ничего не придумывает.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Literal

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from travel_data.codes import HARD_REQUIREMENT_VIOLATIONS
from travel_data.world import get_destination

from .plan_checks import check_plan

if TYPE_CHECKING:
    from agent_core.contracts import TripState

_HARD_REASONS = {
    "PETS_NOT_ALLOWED": "туда нельзя с питомцем",
    "POOL_REQUIRED": "там нет бассейна",
    "SPA_REQUIRED": "там нет спа",
    "FREE_CANCELLATION_REQUIRED": "там нет бесплатной отмены",
    "FAMILY_FRIENDLY_REQUIRED": "там нет семейных удобств",
}

MAX_HOTEL_LINES = 2
# Сначала самые убедительные причины: нарушенные условия и проблемы состыковок, потом условия тарифа, потом остальное.
_STRONG = {"LATE_ARRIVAL", "DATE_MISMATCH", "CHECKIN_GAP", "TIGHT_CONNECTION", "TRANSFER_OUTSIDE_HOURS", *_HARD_REASONS, *HARD_REQUIREMENT_VIOLATIONS}


def _priority(code: str) -> int:
    if code in _STRONG:
        return 0
    if code in ("NON_REFUNDABLE", "NO_FREE_CANCELLATION"):
        return 1
    return 2


MAX_TRANSPORT_LINES = 1
SEA_WORDS = ("мор", "пляж", "купа")
QUIET_WORDS = ("тих", "спокой")


@dataclass(frozen=True)
class RejectedOption:
    kind: Literal["hotel", "outbound"]
    name: str
    saving: int
    reason: str
    code: str

    def sentence(self) -> str:
        what = f"«{self.name}»" if self.kind == "hotel" else self.name
        saving = f"{self.saving:,}".replace(",", " ")
        return f"{what} дешевле на {saving} ₽, но {self.reason}."


def _first_sentence(message: str) -> str:
    text = message.split(". ")[0].rstrip(".")
    return text[:1].lower() + text[1:]


def _new_issues(trip: "TripState", candidate_plan: Any) -> list[Any]:
    """Проблемы, которые есть у варианта-кандидата и которых нет у выбранного плана."""
    current = {issue.code for issue in check_plan(trip.plan, trip.request)}
    found = check_plan(candidate_plan, trip.request)
    return [issue for issue in found if issue.code not in current and issue.code != "WEATHER_RISK"]


def _wishes(trip: "TripState") -> str:
    request = trip.request
    return " ".join([*request.soft, *request.hard, request.mood or ""]).casefold()


def _hotel_reason(trip: "TripState", option: Any) -> tuple[str, str] | None:
    chosen = trip.plan.hotel.hotel
    hotel = option.hotel
    for code in option.violated_requirements:
        if code in _HARD_REASONS:
            return _HARD_REASONS[code], code
        if code in HARD_REQUIREMENT_VIOLATIONS:
            return _first_sentence(HARD_REQUIREMENT_VIOLATIONS[code]), code

    issues = _new_issues(trip, trip.plan.model_copy(update={"hotel": option}))
    if issues:
        issue = issues[0]
        return _first_sentence(issue.message), issue.code

    wishes = _wishes(trip)
    if (
        any(word in wishes for word in SEA_WORDS)
        and hotel.distance_to_beach_km is not None
        and (chosen.distance_to_beach_km is None or hotel.distance_to_beach_km - chosen.distance_to_beach_km >= 0.5)
    ):
        return f"до пляжа {hotel.distance_to_beach_km:g} км", "FAR_FROM_BEACH"
    if any(word in wishes for word in QUIET_WORDS) and hotel.noise_level == "noisy" and chosen.noise_level != "noisy":
        return "там шумно, а вы просили тишину", "NOISY"
    if trip.request.guests.children_ages and not hotel.family_friendly and chosen.family_friendly:
        return "там нет удобств для детей", "NOT_FAMILY"
    if chosen.cancellation_policy.startswith("free") and not hotel.cancellation_policy.startswith("free"):
        return "там нельзя бесплатно отменить бронь", "NO_FREE_CANCELLATION"
    if chosen.rating - hotel.rating >= 0.4:
        return f"рейтинг ниже: {hotel.rating:g} против {chosen.rating:g}", "LOW_RATING"
    return None


def _outbound_reason(trip: "TripState", option: Any) -> tuple[str, str] | None:
    chosen = trip.plan.outbound
    issues = _new_issues(trip, trip.plan.model_copy(update={"outbound": option, "arrival_transfer": None}))
    if issues:
        return _first_sentence(issues[0].message), issues[0].code
    if option.stops > chosen.stops:
        return f"с пересадкой ({option.stops})", "MORE_STOPS"
    if option.duration_minutes - chosen.duration_minutes >= 60:
        hours = round((option.duration_minutes - chosen.duration_minutes) / 60)
        return f"в пути на {hours} ч дольше", "LONGER"
    return None


def _origin_tz(trip: "TripState") -> ZoneInfo:
    origin = get_destination(trip.request.origin or "Москва")
    try:
        return ZoneInfo(origin.timezone) if origin else ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def why_not(trip: "TripState") -> list[RejectedOption]:
    """До трёх объяснений, почему не выбран более дешёвый вариант."""
    plan = trip.plan
    if plan is None:
        return []
    result: list[RejectedOption] = []

    if trip.hotels:
        chosen_total = plan.hotel.estimated_lodging_total
        cheaper = [
            option for option in [*trip.hotels.options, *trip.hotels.alternatives]
            if option.hotel.id != plan.hotel.hotel.id and option.estimated_lodging_total < chosen_total
        ]
        hotel_lines: list[RejectedOption] = []
        for option in cheaper:
            found = _hotel_reason(trip, option)
            if found:
                reason, code = found
                hotel_lines.append(RejectedOption("hotel", option.hotel.name, chosen_total - option.estimated_lodging_total, reason, code))
        hotel_lines.sort(key=lambda item: (_priority(item.code), -item.saving))
        result.extend(hotel_lines[:MAX_HOTEL_LINES])

    if trip.transport and plan.outbound:
        cheaper_transport = sorted(
            (option for option in trip.transport.outbound if option.id != plan.outbound.id and option.price < plan.outbound.price),
            key=lambda option: option.price,
        )
        added = 0
        for option in cheaper_transport:
            found = _outbound_reason(trip, option)
            if found:
                reason, code = found
                kind = "Рейс" if option.transport_type == "flight" else "Поезд"
                name = f"{kind} в {option.departure_at.astimezone(_origin_tz(trip)):%H:%M}"
                result.append(RejectedOption("outbound", name, plan.outbound.price - option.price, reason, code))
                added += 1
            if added >= MAX_TRANSPORT_LINES:
                break
    return sorted(result, key=lambda item: (_priority(item.code), -item.saving))


def why_not_payload(trip: "TripState") -> list[dict[str, Any]]:
    return [{**asdict(item), "text": item.sentence()} for item in why_not(trip)]
