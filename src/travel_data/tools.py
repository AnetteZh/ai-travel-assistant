from __future__ import annotations

import json
import logging
import os
import re
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .codes import HARD_REQUIREMENT_VIOLATIONS
from .models import (
    BookingResult,
    Cart,
    CartItem,
    CartSelection,
    DestinationSearchResult,
    DestinationSuggestion,
    Evidence,
    Guests,
    HotelSearchResult,
    RankedHotel,
    TravelRequest,
    TransferOption,
    TransferSearchResult,
    TransportOption,
    TransportSearchResult,
    WeatherSummary,
    Weights,
)
from .world import get_destination, get_hotel, load_world

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
MET_NO_URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
logger = logging.getLogger(__name__)

# These in-memory indexes make a demo run simple. For a multi-worker deployment,
# persist option/cart records in the shared TripState store or a database instead.
_TRANSPORT_CACHE: dict[str, TransportOption] = {}
_TRANSFER_CACHE: dict[str, TransferOption] = {}
_CART_CACHE: dict[str, Cart] = {}
_WEATHER_CACHE: dict[tuple[str, date], WeatherSummary] = {}
_MET_RESPONSE_CACHE: dict[tuple[float, float], dict[str, Any]] = {}
_BOOKING_CACHE: dict[str, BookingResult] = {}


def _as_request(value: TravelRequest | dict[str, Any]) -> TravelRequest:
    return value if isinstance(value, TravelRequest) else TravelRequest.model_validate(value)


def _as_selection(value: CartSelection | dict[str, Any]) -> CartSelection:
    return value if isinstance(value, CartSelection) else CartSelection.model_validate(value)


def _evidence(source_type: str, source_id: str, field_path: str, value: Any, is_synthetic: bool) -> Evidence:
    safe_source = re.sub(r"[^A-Za-z0-9_-]", "-", source_id)
    safe_field = re.sub(r"[^A-Za-z0-9_-]", "-", field_path)
    return Evidence(
        id=f"ev-{source_type}-{safe_source}-{safe_field}",
        source_type=source_type,
        source_id=source_id,
        field_path=field_path,
        value=value,
        is_synthetic=is_synthetic,
    )


def _resolve_dates(request: TravelRequest) -> tuple[date, date, list[str]]:
    assumptions: list[str] = []
    today = date.today()
    if request.date_from and request.date_to:
        start, end = request.date_from, request.date_to
    elif request.date_from:
        start, end = request.date_from, request.date_from + timedelta(days=7)
        assumptions.append("Дата выезда указана, продолжительность принята равной 7 ночам.")
    elif request.date_to:
        start, end = request.date_to - timedelta(days=7), request.date_to
        assumptions.append("Дата возвращения указана, продолжительность принята равной 7 ночам.")
    else:
        start, end = today + timedelta(days=14), today + timedelta(days=21)
        assumptions.append("Даты не указаны: для предварительного расчёта взят период через 14 дней на 7 ночей.")
    if end <= start:
        # This branch protects direct tool calls; Pydantic already validates request dates.
        end = start + timedelta(days=7)
        assumptions.append("Период поездки был некорректен, продолжительность заменена на 7 ночей.")
    return start, end, assumptions


def _nights(start: date, end: date) -> int:
    return max(1, (end - start).days)


def _destinations_by_id() -> dict[str, Any]:
    return {destination.id: destination for destination in load_world().destinations}


def _lowered(*values: str | None) -> str:
    return " ".join(value for value in values if value).casefold().replace("ё", "е")


def _is_pet_requirement(requirement: str) -> bool:
    text = _lowered(requirement)
    return any(token in text for token in ("питом", "с собак", "с кош", "с живот", "pet", "pets", "dog", "cat"))


def _is_pool_requirement(requirement: str) -> bool:
    text = _lowered(requirement)
    return "бассейн" in text or "pool" in text


def _is_spa_requirement(requirement: str) -> bool:
    text = _lowered(requirement)
    return "спа" in text or " spa" in f" {text}"


def _is_cancellation_requirement(requirement: str) -> bool:
    text = _lowered(requirement)
    return any(token in text for token in ("бесплатн" , "возврат", "отмен", "refundable", "free cancellation"))


def _is_family_requirement(requirement: str) -> bool:
    text = _lowered(requirement)
    return any(token in text for token in ("ребен", "дет", "семейн", "family", "kid"))


def _minimum_stars(requirement: str) -> int | None:
    text = _lowered(requirement)
    patterns = [r"(?:не менее|не меньше|от|min(?:imum)?\s*)\s*(\d)\s*(?:звезд|stars?)?", r"(\d)\s*(?:звезд|stars?)"]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            value = int(match.group(1))
            if 1 <= value <= 5:
                return value
    return None


def _hotel_violations(hotel: Any, request: TravelRequest) -> list[str]:
    violations: list[str] = []
    for requirement in request.hard_requirements:
        if _is_pet_requirement(requirement) and not hotel.has_pets_allowed:
            violations.append("PETS_NOT_ALLOWED")
        if _is_pool_requirement(requirement) and not hotel.has_pool:
            violations.append("POOL_REQUIRED")
        if _is_spa_requirement(requirement) and not hotel.has_spa:
            violations.append("SPA_REQUIRED")
        if _is_cancellation_requirement(requirement) and hotel.cancellation_policy not in ("free_until_7_days", "free_until_3_days"):
            violations.append("FREE_CANCELLATION_REQUIRED")
        if _is_family_requirement(requirement) and not hotel.family_friendly:
            violations.append("FAMILY_FRIENDLY_REQUIRED")
        min_stars = _minimum_stars(requirement)
        if min_stars and hotel.stars < min_stars:
            violations.append(f"MIN_STARS_{min_stars}")
    return sorted(set(violations))


def _preference_score(hotel: Any, prefs: list[str]) -> tuple[float, list[str]]:
    text = _lowered(*prefs)
    matches: list[str] = []
    checks = [
        (any(t in text for t in ("тихо", "спокой", "quiet")), hotel.noise_level == "quiet", "тихий район"),
        (any(t in text for t in ("море", "пляж", "sea", "beach")), hotel.distance_to_beach_km is not None and hotel.distance_to_beach_km <= 2, "недалеко от пляжа"),
        (any(t in text for t in ("центр", "central", "center")), hotel.distance_to_center_km <= 1.5, "рядом с центром"),
        (any(t in text for t in ("завтрак", "breakfast")), hotel.breakfast_included, "завтрак включён"),
        (any(t in text for t in ("бассейн", "pool")), hotel.has_pool, "есть бассейн"),
        (any(t in text for t in ("спа", "spa")), hotel.has_spa, "есть спа"),
        (any(t in text for t in ("дет", "ребен", "family", "семь")), hotel.family_friendly, "подходит для семьи"),
        (any(t in text for t in ("питом", "с собак", "с кош", "pet")), hotel.has_pets_allowed, "размещение с питомцами"),
    ]
    applicable = [passed for requested, passed, _ in checks if requested]
    for requested, passed, label in checks:
        if requested and passed:
            matches.append(label)
    score = (sum(applicable) / len(applicable)) if applicable else 0.5
    # Rating is a grounded general quality signal, blended into preference fit.
    return min(1.0, 0.75 * score + 0.25 * (hotel.rating / 5.0)), matches


def _cancellation_score(policy: str) -> float:
    return {
        "free_until_7_days": 1.0,
        "free_until_3_days": 0.8,
        "partial": 0.45,
        "non_refundable": 0.0,
    }[policy]


def _score_hotel(hotel: Any, request: TravelRequest, nights: int, min_price: int, max_price: int) -> tuple[float, list[str], list[str], list[str], list[str], str, list[Evidence], int]:
    lodging_total = hotel.price_per_night * nights
    if request.budget_total:
        # Budget is for the whole trip; use a transparent 55% hotel allocation only for ranking.
        target_lodging = max(1, int(request.budget_total * 0.55))
        price_score = max(0.0, min(1.0, 1 - lodging_total / target_lodging))
    elif max_price > min_price:
        price_score = 1.0 - ((hotel.price_per_night - min_price) / (max_price - min_price))
    else:
        price_score = 1.0

    cancellation_score = _cancellation_score(hotel.cancellation_policy)
    text_prefs = _lowered(*request.soft_preferences, request.mood)
    if any(token in text_prefs for token in ("пляж", "море", "beach", "sea")) and hotel.distance_to_beach_km is not None:
        location_distance = hotel.distance_to_beach_km
    else:
        location_distance = hotel.distance_to_center_km
    location_score = max(0.0, 1.0 - min(location_distance / 10.0, 1.0))
    preference_score, matched_prefs = _preference_score(hotel, request.soft_preferences + ([request.mood] if request.mood else []))

    raw_weights = {
        "price": request.weights.price,
        "cancellation": request.weights.cancellation,
        "location": request.weights.location,
        "preferences": request.weights.preferences,
    }
    feature_scores = {
        "price": price_score,
        "cancellation": cancellation_score,
        "location": location_score,
        "preferences": preference_score,
    }
    total_weight = sum(raw_weights.values())
    if total_weight <= 0:
        score = hotel.rating / 5
    else:
        score = sum(raw_weights[key] * feature_scores[key] for key in raw_weights) / total_weight

    violations = _hotel_violations(hotel, request)
    reason_codes: list[str] = []
    hotel_specific_hard = any(
        _is_pet_requirement(req) or _is_pool_requirement(req) or _is_spa_requirement(req)
        or _is_cancellation_requirement(req) or _is_family_requirement(req) or _minimum_stars(req) is not None
        for req in request.hard_requirements
    )
    if hotel_specific_hard and not violations:
        reason_codes.append("MATCHES_HARD")
    if hotel.family_friendly and (request.guests.children_ages or _is_family_requirement(_lowered(*request.hard_requirements))):
        reason_codes.append("KID_FRIENDLY")
    if hotel.cancellation_policy in ("free_until_7_days", "free_until_3_days"):
        reason_codes.append("FREE_CANCELLATION")
    if hotel.distance_to_center_km < 1 or (hotel.distance_to_beach_km is not None and hotel.distance_to_beach_km < 1):
        reason_codes.append("NEAR_POI")

    risk_codes: list[str] = []
    if hotel.cancellation_policy == "non_refundable":
        risk_codes.append("NON_REFUNDABLE")

    pros = [f"Рейтинг {hotel.rating:.1f}/5", f"{hotel.stars} звезды", f"{hotel.price_per_night:,} ₽ за ночь".replace(",", " ")]
    if hotel.has_pool:
        pros.append("Есть бассейн")
    if hotel.has_spa:
        pros.append("Есть спа")
    if hotel.has_pets_allowed:
        pros.append("Можно с питомцами")
    if hotel.breakfast_included:
        pros.append("Завтрак включён")
    if matched_prefs:
        pros.extend([f"Совпадает с пожеланием: {item}" for item in matched_prefs[:2]])

    cons: list[str] = []
    if hotel.noise_level == "noisy":
        cons.append("По данным мок-модели, уровень шума высокий")
    if hotel.distance_to_center_km > 3:
        cons.append(f"До центра {hotel.distance_to_center_km:.1f} км")
    if hotel.distance_to_beach_km is not None and hotel.distance_to_beach_km > 3 and "море" in text_prefs:
        cons.append(f"До пляжа {hotel.distance_to_beach_km:.1f} км")
    if hotel.cancellation_policy == "non_refundable":
        cons.append("Тариф невозвратный")
    if request.guests.pets and not hotel.has_pets_allowed:
        cons.append("Размещение с питомцами не разрешено")

    why_parts = []
    reason_labels = {
        "MATCHES_HARD": "выполнены жёсткие требования",
        "KID_FRIENDLY": "есть семейные удобства",
        "FREE_CANCELLATION": "доступна бесплатная отмена по условиям тарифа",
        "NEAR_POI": "отель недалеко от центра или пляжа",
    }
    human_reasons = [reason_labels[code] for code in reason_codes if code in reason_labels]
    if human_reasons:
        why_parts.append("Подходит, потому что: " + ", ".join(human_reasons))
    if matched_prefs:
        why_parts.append("Учтены пожелания: " + ", ".join(matched_prefs[:3]))
    if not why_parts:
        why_parts.append(f"Рейтинг {hotel.rating:.1f}/5 и цена {hotel.price_per_night} ₽ за ночь учтены при ранжировании")
    if cons:
        why_parts.append("Компромиссы: " + "; ".join(cons[:2]))

    fields = [
        "name", "price_per_night", "stars", "rating", "has_pool", "has_pets_allowed",
        "has_spa", "cancellation_policy", "breakfast_included", "family_friendly",
        "distance_to_center_km", "distance_to_beach_km", "noise_level",
    ]
    evidence = [_evidence("hotel", hotel.id, field, getattr(hotel, field), hotel.is_synthetic) for field in fields]
    ranked = RankedHotel(
        hotel=hotel,
        estimated_lodging_total=lodging_total,
        score=round(score, 4),
        reason_codes=sorted(set(reason_codes)),
        risk_codes=sorted(set(risk_codes)),
        pros=pros,
        cons=cons,
        why_it_fits=". ".join(why_parts) + ".",
        violated_requirements=violations,
        evidence=evidence,
    )
    return score, reason_codes, risk_codes, pros, cons, ranked.why_it_fits, evidence, lodging_total


# Пожелания пишут в любой форме слова («у моря», «на природе», «в горах»), а теги
# направлений — в начальной. Для основных категорий сверяем по корню слова.
_TAG_PATTERNS = {
    "море": re.compile(r"\bмор(е|я|ю|ем|ской|ские|ское)\b|побереж|купа", re.IGNORECASE),
    "пляж": re.compile(r"пляж", re.IGNORECASE),
    "горы": re.compile(r"\bгор(?!од)\w*|горнолыж", re.IGNORECASE),
    "город": re.compile(r"город|мегапол|урбан", re.IGNORECASE),
    "природа": re.compile(r"природ|\bлес|поход|заповед", re.IGNORECASE),
    "озёра": re.compile(r"оз[её]р", re.IGNORECASE),
    "север": re.compile(r"север|сияни", re.IGNORECASE),
    "семейный отдых": re.compile(r"сем[ье]|дет|ребён|ребен", re.IGNORECASE),
    "активный отдых": re.compile(r"актив", re.IGNORECASE),
    "история": re.compile(r"истор", re.IGNORECASE),
    "музеи": re.compile(r"музе", re.IGNORECASE),
    "гастрономия": re.compile(r"гастроном|еда|кухн|ресторан", re.IGNORECASE),
}
_WARM_WEATHER = re.compile(r"тепл|солнц|\bмор(е|я|ю|ем|ской)\b|пляж|купа|warm|beach|sea", re.IGNORECASE)


def _tag_matches(tag: str, text_prefs: str) -> bool:
    pattern = _TAG_PATTERNS.get(tag.casefold())
    if pattern is not None:
        return bool(pattern.search(text_prefs))
    tag_text = tag.casefold()
    return tag_text in text_prefs or any(word in text_prefs for word in tag_text.split())


def suggest_destinations(request: TravelRequest | dict[str, Any] | None = None) -> DestinationSearchResult:
    """Recommend 3–5 directions; the world is synthetic, weather may be live Open-Meteo."""
    request = _as_request(request or {})
    world = load_world()
    start, end, assumptions = _resolve_dates(request)
    nights = _nights(start, end)
    origin_city = request.origin or "Москва"
    origin_destination = get_destination(origin_city)
    text_prefs = _lowered(*request.soft_preferences, request.mood)

    candidates = []
    candidate_destinations = [d for d in world.destinations if not origin_destination or d.id != origin_destination.id]
    excluded = set(request.exclude_destination_ids)
    if excluded and len([d for d in candidate_destinations if d.id not in excluded]) >= 3:
        candidate_destinations = [d for d in candidate_destinations if d.id not in excluded]
    elif excluded:
        assumptions.append("Показаны все направления каталога, поэтому подборка начинается сначала.")
    for destination in candidate_destinations:
        destination_hotels = [hotel for hotel in world.hotels if hotel.destination_id == destination.id]
        cheapest_hotel = min(hotel.price_per_night for hotel in destination_hotels)
        normalized_origin = origin_destination.name if origin_destination else origin_city
        routes = [route for route in world.transport_templates if route.origin_city.casefold() == normalized_origin.casefold() and route.destination_id == destination.id]
        # If no route is available from an unrecognized origin, don't invent a fare.
        cheapest_route = min((route.base_price for route in routes), default=None)
        fastest_route = min((route.duration_minutes for route in routes), default=None)
        transfer_price = min((t.base_price for t in world.transfer_templates if t.destination_id == destination.id and t.arrival_point == "airport"), default=0)
        transport_estimate = 2 * cheapest_route if cheapest_route is not None else 0
        estimate = cheapest_hotel * nights + transport_estimate + 2 * transfer_price

        matched_tags = [tag for tag in destination.tags if _tag_matches(tag, text_prefs)]
        requested_preference_count = len(request.soft_preferences) + (1 if request.mood else 0)
        preference_score = (len(matched_tags) / max(1, min(4, requested_preference_count))) if requested_preference_count else 0.5
        climate = destination.climate_monthly[str(start.month)]
        weather_score = 0.5
        wants_warm_weather = bool(_WARM_WEATHER.search(text_prefs))
        if wants_warm_weather:
            weather_score = max(0.0, min(1.0, (climate["temperature_max_c"] + 5) / 32))
        price_score = 0.5
        if request.budget_total:
            price_score = max(0.0, min(1.0, 1 - estimate / request.budget_total))
        elif estimate > 0:
            price_score = max(0.0, min(1.0, 1 - cheapest_hotel / max(1, destination.typical_price_per_night * 2)))
        short_score = 1.0 if fastest_route and fastest_route <= 240 else (0.5 if fastest_route and fastest_route <= 420 else (0.25 if fastest_route else 0.0))
        score_components = {
            "price": (request.weights.price, price_score),
            "travel_time": (request.weights.travel_time, short_score),
            "location": (request.weights.location, weather_score),
            "preferences": (request.weights.preferences, preference_score),
        }
        active_weight_sum = sum(weight for weight, _ in score_components.values())
        score = (
            sum(weight * component for weight, component in score_components.values()) / active_weight_sum
            if active_weight_sum > 0 else 0.5
        )
        candidates.append((score, destination, estimate, fastest_route, cheapest_hotel, cheapest_route, matched_tags, climate))

    # С бюджетом сначала идут варианты, которые в него укладываются, внутри — по баллу.
    budget = request.budget_total
    # Если в бюджет не укладывается ничего, показываем самые недорогие варианты.
    def _budget_rank(score: float, estimate: int) -> tuple[bool, float]:
        over = bool(budget) and estimate > budget
        return (over, estimate if over else -score)

    candidates.sort(key=lambda item: _budget_rank(item[0], item[2]))
    # Only top candidates trigger external weather calls; this keeps the tool responsive.
    chosen = candidates[:5]
    suggestions: list[DestinationSuggestion] = []
    for score, destination, estimate, fastest_route, cheapest_hotel, cheapest_route, matched_tags, climate in chosen:
        weather = get_weather(destination.id, start)
        weather_score_actual = 0.5
        if wants_warm_weather:
            weather_score_actual = max(0.0, min(1.0, (weather.temperature_max_c + 5) / 32))
        score_components_actual = {
            "price": (request.weights.price, max(0.0, min(1.0, 1 - estimate / request.budget_total)) if request.budget_total else max(0.0, min(1.0, 1 - cheapest_hotel / max(1, destination.typical_price_per_night * 2)))),
            "travel_time": (request.weights.travel_time, 1.0 if fastest_route and fastest_route <= 240 else (0.5 if fastest_route and fastest_route <= 420 else (0.25 if fastest_route else 0.0))),
            "location": (request.weights.location, weather_score_actual),
            "preferences": (request.weights.preferences, (len(matched_tags) / max(1, min(4, requested_preference_count))) if requested_preference_count else 0.5),
        }
        weight_sum_actual = sum(weight for weight, _ in score_components_actual.values())
        score = sum(weight * component for weight, component in score_components_actual.values()) / weight_sum_actual if weight_sum_actual > 0 else 0.5
        reason_codes: list[str] = []
        if fastest_route is not None and fastest_route <= 240:
            reason_codes.append("SHORT_TRAVEL")
        if weather.precipitation_probability is not None and weather.precipitation_probability >= 65:
            reason_codes.append("WEATHER_RISK")
        if any(token in text_prefs for token in ("тепл", "солнц", "море", "пляж", "warm", "beach", "sea")) and weather.temperature_max_c < 18:
            if "WEATHER_RISK" not in reason_codes:
                reason_codes.append("WEATHER_RISK")
        pros = [f"Направление: {destination.name}, {destination.region}", f"Ориентир на поездку: {estimate:,} ₽".replace(",", " ")]
        if matched_tags:
            pros.append("Совпадает по темам: " + ", ".join(matched_tags))
        if weather.temperature_max_c >= 22:
            weather_label = "прогноз" if not weather.is_synthetic else "сезонная оценка"
            pros.append(f"Тёплая погода по данным ({weather_label}): до {weather.temperature_max_c:.0f} °C")
        cons = []
        if request.budget_total and estimate > request.budget_total:
            cons.append("Оценочная стоимость выше заданного бюджета")
        if weather.precipitation_probability is not None and weather.precipitation_probability >= 65:
            weather_source_label = "прогнозу" if not weather.is_synthetic else "синтетической сезонной оценке"
            cons.append(f"Повышенная вероятность осадков по {weather_source_label}")
        if any(token in text_prefs for token in ("тепл", "солнц", "море", "пляж", "warm", "beach", "sea")) and weather.temperature_max_c < 18:
            cons.append("Для ожидания тёплой погоды прогноз прохладный")
        if fastest_route is None:
            cons.append(f"Нет смоделированного маршрута из города «{origin_city}»")
        why = f"Подходит по тегам направления и погодным данным ({weather.source}); расчётная стоимость для {nights} ночей — {estimate} ₽."
        if matched_tags:
            why = f"Совпадает с пожеланиями ({', '.join(matched_tags)}); источник погоды — {weather.source}; расчётная стоимость для {nights} ночей — {estimate} ₽."
        cheapest_hotel_record = min(
            (h for h in world.hotels if h.destination_id == destination.id),
            key=lambda h: h.price_per_night,
        )
        cheapest_route_record = min(
            (r for r in world.transport_templates if r.origin_city.casefold() == (origin_destination.name if origin_destination else origin_city).casefold() and r.destination_id == destination.id),
            key=lambda r: r.base_price,
            default=None,
        )
        cheapest_transfer_record = min(
            (t for t in world.transfer_templates if t.destination_id == destination.id and t.arrival_point == "airport"),
            key=lambda t: t.base_price,
            default=None,
        )
        evidence = [
            _evidence("destination", destination.id, "name", destination.name, destination.is_synthetic),
            _evidence("destination", destination.id, "tags", destination.tags, destination.is_synthetic),
            _evidence("hotel", cheapest_hotel_record.id, "price_per_night", cheapest_hotel_record.price_per_night, cheapest_hotel_record.is_synthetic),
            _evidence("transport", cheapest_route_record.id, "base_price", cheapest_route_record.base_price, True) if cheapest_route_record else _evidence("destination", destination.id, "transport_estimate_unavailable", None, True),
            _evidence("transfer", cheapest_transfer_record.id, "base_price", cheapest_transfer_record.base_price, True) if cheapest_transfer_record else _evidence("destination", destination.id, "transfer_estimate_unavailable", None, True),
            _evidence("weather", destination.id, "temperature_max_c", weather.temperature_max_c, weather.is_synthetic),
            _evidence("weather", destination.id, "precipitation_probability", weather.precipitation_probability, weather.is_synthetic),
            _evidence("destination", destination.id, "estimated_trip_price", estimate, True),
        ]
        suggestions.append(DestinationSuggestion(
            destination=destination,
            estimated_trip_price=estimate,
            fastest_travel_minutes=fastest_route,
            weather=weather,
            score=round(score, 4),
            reason_codes=reason_codes,
            pros=pros,
            cons=cons,
            why_it_fits=why,
            evidence=evidence,
        ))
    suggestions.sort(key=lambda item: _budget_rank(item.score, item.estimated_trip_price))
    return DestinationSearchResult(
        options=suggestions[:5],
        assumptions=assumptions + ([f"Город вылета не указан: для предварительной оценки использована Москва."] if not request.origin else []),
        message="Найдено несколько направлений для сравнения. Все цены на отели и транспорт синтетические; погода помечена источником отдельно.",
    )


def search_hotels(request: TravelRequest | dict[str, Any], destination_id: str | None = None) -> HotelSearchResult:
    """Filter by hard requirements first, then rank matching hotels by user's weights."""
    request = _as_request(request)
    selected_destination_id = destination_id or request.destination_id
    start, end, assumptions = _resolve_dates(request)
    nights = _nights(start, end)
    destination = get_destination(selected_destination_id) if selected_destination_id else None
    if destination is None:
        return HotelSearchResult(
            destination_id=selected_destination_id or "",
            date_from=start, date_to=end, nights=nights, options=[], alternatives=[],
            message="Не удалось определить направление. Передай destination_id из списка направлений.",
            assumptions=assumptions,
        )

    hotels = [hotel for hotel in load_world().hotels if hotel.destination_id == destination.id]
    if not hotels:
        return HotelSearchResult(
            destination_id=destination.id, date_from=start, date_to=end, nights=nights, options=[], alternatives=[],
            message="В мок-мире пока нет отелей для этого направления.", assumptions=assumptions,
        )

    min_price = min(hotel.price_per_night for hotel in hotels)
    max_price = max(hotel.price_per_night for hotel in hotels)
    results = []
    for hotel in hotels:
        score, _, _, _, _, _, _, lodging_total = _score_hotel(hotel, request, nights, min_price, max_price)
        violations = _hotel_violations(hotel, request)
        # Keep the reason/risk details created by the same deterministic scorer.
        details = _ranked_hotel_for(hotel, request, nights, min_price, max_price, violations)
        results.append((len(violations), score, details))

    valid = [item for item in results if item[0] == 0]
    valid.sort(key=lambda item: item[1], reverse=True)
    if valid:
        return HotelSearchResult(
            destination_id=destination.id, date_from=start, date_to=end, nights=nights,
            options=[item[2] for item in valid[:10]], alternatives=[],
            message=f"Найдено {len(valid)} отелей, выполнивших жёсткие требования; показаны лучшие варианты по весам пользователя.",
            assumptions=assumptions,
        )

    results.sort(key=lambda item: (item[0], -item[1]))
    alternatives = [item[2] for item in results[:5]]
    return HotelSearchResult(
        destination_id=destination.id, date_from=start, date_to=end, nights=nights,
        options=[], alternatives=alternatives,
        message="Ни один отель не выполнил все жёсткие требования. В alternatives показаны ближайшие варианты и конкретные нарушения.",
        assumptions=assumptions,
    )


def _ranked_hotel_for(hotel: Any, request: TravelRequest, nights: int, min_price: int, max_price: int, violations: list[str]) -> RankedHotel:
    score, reason_codes, risk_codes, pros, cons, why, evidence, lodging_total = _score_hotel(hotel, request, nights, min_price, max_price)
    explained_violations = []
    for code in violations:
        if code in HARD_REQUIREMENT_VIOLATIONS:
            explained_violations.append(HARD_REQUIREMENT_VIOLATIONS[code])
        elif code.startswith("MIN_STARS_"):
            explained_violations.append(f"Не выполнено требование минимум {code.removeprefix('MIN_STARS_')} звёзд.")
        else:
            explained_violations.append(code)
    cons = list(cons) + [item for item in explained_violations if item not in cons]
    if explained_violations:
        why = why.rstrip(".") + ". Не выполнены требования: " + "; ".join(explained_violations) + "."
    return RankedHotel(
        hotel=hotel,
        estimated_lodging_total=lodging_total,
        score=round(score, 4),
        reason_codes=sorted(set(reason_codes)),
        risk_codes=sorted(set(risk_codes)),
        pros=pros,
        cons=cons,
        why_it_fits=why,
        violated_requirements=violations,
        evidence=evidence,
    )


def _safe_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("Europe/Moscow")


def _make_transport_options(origin_city: str, destination_id: str, travel_date: date, weights: Weights) -> list[TransportOption]:
    destination = get_destination(destination_id)
    origin = get_destination(origin_city)
    if destination is None or origin is None or destination.id == origin.id:
        return []
    templates = [
        t for t in load_world().transport_templates
        if t.origin_city.casefold() == origin.name.casefold() and t.destination_id == destination.id
    ]
    raw_options: list[TransportOption] = []
    origin_tz = _safe_timezone(origin.timezone)
    destination_tz = _safe_timezone(destination.timezone)
    for template in templates:
        for index, departure_time in enumerate(template.departure_times):
            hour, minute = (int(part) for part in departure_time.split(":"))
            departure_local = datetime.combine(travel_date, time(hour, minute), tzinfo=origin_tz)
            departure_utc = departure_local.astimezone(timezone.utc)
            arrival_utc = departure_utc + timedelta(minutes=template.duration_minutes)
            arrival_local = arrival_utc.astimezone(destination_tz)
            multiplier = [0.92, 1.0, 1.08][index % 3]
            price = max(500, int(round(template.base_price * multiplier / 10) * 10))
            identifier = f"option-{template.id.removeprefix('route-')}-{travel_date.isoformat()}-{index + 1:02d}"
            reasons: list[str] = []
            risks: list[str] = []
            if template.duration_minutes <= 240:
                reasons.append("SHORT_TRAVEL")
            evidence = [
                _evidence("transport", identifier, "price", price, True),
                _evidence("transport", identifier, "departure_at", departure_local.isoformat(), True),
                _evidence("transport", identifier, "arrival_at", arrival_local.isoformat(), True),
                _evidence("transport", identifier, "duration_minutes", template.duration_minutes, True),
                _evidence("transport", identifier, "stops", template.stops, True),
            ]
            raw_options.append(TransportOption(
                id=identifier,
                origin_city=origin.name,
                destination_city=destination.name,
                origin_point=origin.airport if template.transport_type == "flight" else origin.station,
                destination_point=destination.airport if template.transport_type == "flight" else destination.station,
                transport_type=template.transport_type,
                departure_at=departure_local,
                arrival_at=arrival_local,
                duration_minutes=template.duration_minutes,
                stops=template.stops,
                baggage_included=template.baggage_included,
                price=price,
                reason_codes=reasons,
                risk_codes=risks,
                pros=(
                    [f"В пути {template.duration_minutes // 60} ч {template.duration_minutes % 60} мин"]
                    + (["Багаж включён"] if template.baggage_included else [])
                    + (["Без пересадок"] if template.stops == 0 else [f"Пересадок: {template.stops}"])
                ),
                cons=([] if template.stops == 0 else [f"В маршруте {template.stops} пересадка(и); длительность пересадок отдельно не смоделирована"])
                     + ([] if template.baggage_included else ["Багаж не включён в базовый тариф"]),
                why_it_fits=(
                    f"Вариант ранжирован по цене и длительности пути. Цена {price} ₽; "
                    f"длительность {template.duration_minutes} минут; пересадок: {template.stops}."
                ),
                is_synthetic=True,
                evidence=evidence,
            ))
    if not raw_options:
        return []

    min_price, max_price = min(x.price for x in raw_options), max(x.price for x in raw_options)
    min_duration, max_duration = min(x.duration_minutes for x in raw_options), max(x.duration_minutes for x in raw_options)
    weight_sum = weights.price + weights.travel_time
    if weight_sum <= 0:
        weight_sum = 1
    scored = []
    for item in raw_options:
        price_score = 1 - ((item.price - min_price) / (max_price - min_price)) if max_price > min_price else 1
        time_score = 1 - ((item.duration_minutes - min_duration) / (max_duration - min_duration)) if max_duration > min_duration else 1
        score = (weights.price * price_score + weights.travel_time * time_score) / weight_sum
        if item.baggage_included:
            score = min(1, score + 0.03)
        scored.append(item.model_copy(update={"score": round(score, 4)}))
    return sorted(scored, key=lambda x: (x.score, -x.price), reverse=True)


def search_transport(request: TravelRequest | dict[str, Any], destination_id: str | None = None) -> TransportSearchResult:
    """Search synthetic outbound and return flights/trains, using timezone-aware timestamps."""
    request = _as_request(request)
    target_id = destination_id or request.destination_id
    start, end, assumptions = _resolve_dates(request)
    target = get_destination(target_id) if target_id else None
    origin_text = request.origin or "Москва"
    origin = get_destination(origin_text)
    if not request.origin:
        assumptions.append("Город вылета не указан: для предварительного поиска использована Москва.")
    if target is None:
        return TransportSearchResult(origin=origin_text, destination_id=target_id or "", outbound=[], return_options=[], assumptions=assumptions, message="Не удалось определить направление.")
    if origin is None:
        return TransportSearchResult(origin=origin_text, destination_id=target.id, outbound=[], return_options=[], assumptions=assumptions, message=f"Для города вылета «{origin_text}» нет маршрутов в мок-мире. Выбери один из поддерживаемых городов.")
    if origin.id == target.id:
        return TransportSearchResult(origin=origin.name, destination_id=target.id, outbound=[], return_options=[], assumptions=assumptions, message="Город вылета совпадает с направлением; междугородний транспорт не нужен.")

    outbound = _make_transport_options(origin.name, target.id, start, request.weights)
    return_options = _make_transport_options(target.name, origin.id, end, request.weights)
    hard_text = _lowered(*request.hard_requirements)
    if any(token in hard_text for token in ("без пересад", "no connection", "nonstop", "direct flight")):
        outbound = [item for item in outbound if item.stops == 0]
        return_options = [item for item in return_options if item.stops == 0]
    elif "не более одной пересад" in hard_text or "at most one connection" in hard_text:
        outbound = [item for item in outbound if item.stops <= 1]
        return_options = [item for item in return_options if item.stops <= 1]
    for option in outbound + return_options:
        _TRANSPORT_CACHE[option.id] = option
    message = f"Найдено вариантов: туда — {len(outbound)}, обратно — {len(return_options)}. Расписание и цены транспорта синтетические."
    if not outbound:
        message += " Для маршрута туда не осталось вариантов после фильтрации."
    return TransportSearchResult(
        origin=origin.name,
        destination_id=target.id,
        outbound=outbound[:8],
        return_options=return_options[:8],
        assumptions=assumptions,
        message=message,
    )


def _parse_datetime(value: datetime | str, timezone_name: str) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_safe_timezone(timezone_name))
    return parsed


def _within_operating_hours(arrival: datetime, start: str, end: str, all_day: bool, timezone_name: str) -> bool:
    if all_day:
        return True
    local = arrival.astimezone(_safe_timezone(timezone_name))
    current = local.time().replace(tzinfo=None)
    opening = time.fromisoformat(start)
    closing = time.fromisoformat(end)
    if opening <= closing:
        return opening <= current <= closing
    return current >= opening or current <= closing


def search_transfers(
    destination_id: str,
    hotel_id: str,
    arrival_at: datetime | str,
    arrival_point: str = "airport",
    direction: str = "to_hotel",
) -> TransferSearchResult:
    """Search a transfer for either direction.

    - direction="to_hotel": arrival_at is the time the traveller reaches the airport/station.
    - direction="to_terminal": arrival_at is the scheduled flight/train departure; pickup_at is calculated backwards.
    The legacy parameter name arrival_at is retained to keep the initial tool contract stable.
    """
    destination = get_destination(destination_id)
    hotel = get_hotel(hotel_id)
    if destination is None or hotel is None or hotel.destination_id != destination.id:
        return TransferSearchResult(options=[], unavailable_options=[], message="Не удалось связать отель с направлением. Проверь destination_id и hotel_id.")
    if arrival_point not in ("airport", "station"):
        return TransferSearchResult(options=[], unavailable_options=[], message="arrival_point должен быть airport или station.")
    if direction not in ("to_hotel", "to_terminal"):
        return TransferSearchResult(options=[], unavailable_options=[], message="direction должен быть to_hotel или to_terminal.")
    try:
        event_at = _parse_datetime(arrival_at, destination.timezone)
    except (ValueError, TypeError):
        return TransferSearchResult(options=[], unavailable_options=[], message="Не удалось прочитать arrival_at. Передай дату и время в ISO 8601.")

    templates = [t for t in load_world().transfer_templates if t.destination_id == destination.id and t.arrival_point == arrival_point]
    distance = max(1.0, hotel.distance_to_center_km * (0.8 if arrival_point == "airport" else 0.5))
    available: list[TransferOption] = []
    unavailable: list[TransferOption] = []
    for template in templates:
        drive_minutes = template.duration_minutes + int(distance * 2)
        # К терминалу нужно приехать заранее: 90 мин до вылета, 30 мин до поезда (см. trip_checks).
        terminal_buffer = 90 if arrival_point == "airport" else 30
        pickup_at = event_at if direction == "to_hotel" else event_at - timedelta(minutes=drive_minutes + terminal_buffer)
        can_operate = _within_operating_hours(pickup_at, template.operating_start, template.operating_end, template.operates_24h, destination.timezone)
        price = int(round((template.base_price + distance * (150 if template.transfer_type == "taxi" else 20)) / 10) * 10)
        identifier = f"{template.id}-{hotel.id}-{direction}-{event_at.strftime('%Y%m%dT%H%M')}"
        risk_codes = [] if can_operate else (["LATE_ARRIVAL"] if direction == "to_hotel" else ["TRANSFER_OUTSIDE_HOURS"])
        evidence = [
            _evidence("transfer", identifier, "operating_start", template.operating_start, True),
            _evidence("transfer", identifier, "operating_end", template.operating_end, True),
            _evidence("transfer", identifier, "price", price, True),
            _evidence("transfer", identifier, "duration_minutes", drive_minutes, True),
            _evidence("transfer", identifier, "pickup_at", pickup_at.isoformat(), True),
        ]
        if direction == "to_hotel":
            pros = (["Работает в момент прибытия", f"Цена {price} ₽", f"В пути около {drive_minutes} минут"] if can_operate else [])
            cons = [] if can_operate else [f"Не работает при прибытии в {event_at.astimezone(_safe_timezone(destination.timezone)).strftime('%H:%M')}: {template.operating_start}–{template.operating_end}"]
            why = f"Трансфер {template.transfer_type} от {arrival_point} к выбранному отелю; " + ("доступен на время прибытия." if can_operate else "недоступен на время прибытия.")
            incoming_at, outgoing_at = event_at, None
        else:
            pros = ([f"Заберёт в {pickup_at.astimezone(_safe_timezone(destination.timezone)).strftime('%H:%M')}", f"Цена {price} ₽", f"До терминала около {drive_minutes} минут"] if can_operate else [])
            cons = [] if can_operate else [f"Не работает в расчётное время выезда {pickup_at.astimezone(_safe_timezone(destination.timezone)).strftime('%H:%M')}: {template.operating_start}–{template.operating_end}"]
            why = f"Трансфер {template.transfer_type} от отеля к {arrival_point}; " + ("позволяет прибыть к терминалу к заданному времени." if can_operate else "закрыт в расчётное время выезда.")
            incoming_at, outgoing_at = None, event_at
        option = TransferOption(
            id=identifier,
            destination_id=destination.id,
            hotel_id=hotel.id,
            arrival_point=arrival_point,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            transfer_type=template.transfer_type,
            arrival_at=incoming_at,
            terminal_departure_at=outgoing_at,
            pickup_at=pickup_at,
            duration_minutes=drive_minutes,
            distance_km=round(distance, 1),
            price=price,
            is_available=can_operate,
            unavailable_reason=None if can_operate else f"Сервис работает {template.operating_start}–{template.operating_end} по местному времени.",
            reason_codes=[],
            risk_codes=risk_codes,
            pros=pros,
            cons=cons,
            why_it_fits=why,
            is_synthetic=True,
            evidence=evidence,
        )
        _TRANSFER_CACHE[option.id] = option
        (available if can_operate else unavailable).append(option)
    available.sort(key=lambda item: item.price)
    unavailable.sort(key=lambda item: item.price)
    direction_label = "от терминала к отелю" if direction == "to_hotel" else "от отеля к терминалу"
    message = f"Доступно трансферов {direction_label}: {len(available)}; закрыто на расчётное время: {len(unavailable)}. Расписание синтетическое."
    if not available:
        message = "Не найдено доступных трансферов на расчётное время. Нужна смена времени транспорта или другое решение по трансферу."
    return TransferSearchResult(options=available, unavailable_options=unavailable, message=message)


def _hotel_lodging_amount(hotel: Any, guests: Guests, nights: int) -> tuple[int, int, int]:
    base = hotel.price_per_night * nights
    extra_guest_fee = max(0, guests.adults - 2) * 650 * nights + len(guests.children_ages) * 300 * nights
    pet_fee = guests.pets * 500
    return base, extra_guest_fee, pet_fee


def price_cart(selection: CartSelection | dict[str, Any]) -> Cart | dict[str, Any]:
    """Calculate the total in code; no model-generated arithmetic is trusted."""
    try:
        selection = _as_selection(selection)
    except Exception as exc:
        return {"error": "INVALID_SELECTION", "message": f"Не удалось разобрать выбор для корзины: {exc}"}
    hotel = get_hotel(selection.hotel_id)
    destination = get_destination(selection.destination_id)
    if not hotel or not destination or hotel.destination_id != destination.id:
        return {"error": "UNKNOWN_HOTEL", "message": "Отель не найден в мок-мире или не соответствует направлению."}
    if selection.guests.pets > 0 and not hotel.has_pets_allowed:
        return {"error": "PETS_NOT_ALLOWED", "message": "Выбранный отель не разрешает размещение с питомцами."}
    nights = _nights(selection.date_from, selection.date_to)
    base_lodging, guest_fee, pet_fee = _hotel_lodging_amount(hotel, selection.guests, nights)
    items: list[CartItem] = [CartItem(
        id=hotel.id,
        item_type="hotel",
        title=f"{hotel.name}, {nights} ночей",
        amount=base_lodging,
        is_synthetic=hotel.is_synthetic,
        evidence=[
            _evidence("hotel", hotel.id, "price_per_night", hotel.price_per_night, hotel.is_synthetic),
            _evidence("hotel", hotel.id, "cancellation_policy", hotel.cancellation_policy, hotel.is_synthetic),
        ],
    )]
    if guest_fee:
        items.append(CartItem(
            id=f"fee-guests-{hotel.id}", item_type="fee", title="Доплата за размещение дополнительных гостей", amount=guest_fee,
            evidence=[_evidence("cart", hotel.id, "extra_guest_fee_per_night", 650, True)],
        ))
    if pet_fee:
        items.append(CartItem(
            id=f"fee-pets-{hotel.id}", item_type="fee", title="Доплата за размещение с питомцами", amount=pet_fee,
            evidence=[_evidence("cart", hotel.id, "pet_fee_per_pet", 500, True)],
        ))

    selected_transport_objects = {option.id: option for option in selection.transport_options}
    selected_transfer_objects = {option.id: option for option in selection.transfer_options}
    transport_ids_to_price = selection.transport_ids or [option.id for option in selection.transport_options]
    transfer_ids_to_price = selection.transfer_ids or [option.id for option in selection.transfer_options]

    for option_id in transport_ids_to_price:
        option = selected_transport_objects.get(option_id) or _TRANSPORT_CACHE.get(option_id)
        if option is None:
            return {"error": "UNKNOWN_TRANSPORT_OPTION", "message": f"Вариант транспорта {option_id} не найден в текущем поиске. Повтори search_transport и выбери актуальный ID."}
        if not option.is_synthetic:
            return {"error": "UNSUPPORTED_REAL_TRANSPORT", "message": "Этот прототип ожидает синтетические варианты транспорта."}
        items.append(CartItem(
            id=option.id, item_type="transport",
            title=f"{option.transport_type}: {option.origin_city} → {option.destination_city}",
            amount=option.price, is_synthetic=option.is_synthetic, evidence=option.evidence,
        ))

    for option_id in transfer_ids_to_price:
        option = selected_transfer_objects.get(option_id) or _TRANSFER_CACHE.get(option_id)
        if option is None:
            return {"error": "UNKNOWN_TRANSFER_OPTION", "message": f"Трансфер {option_id} не найден в текущем поиске. Повтори search_transfers."}
        if not option.is_available:
            return {"error": "TRANSFER_UNAVAILABLE", "message": f"Трансфер {option_id} закрыт во время прибытия."}
        if option.hotel_id != hotel.id:
            return {"error": "TRANSFER_HOTEL_MISMATCH", "message": "Выбранный трансфер относится к другому отелю."}
        items.append(CartItem(
            id=option.id, item_type="transfer", title=f"Трансфер {option.transfer_type} до {hotel.name}",
            amount=option.price, is_synthetic=option.is_synthetic, evidence=option.evidence,
        ))

    subtotal = sum(item.amount for item in items)
    over_budget = bool(selection.budget_total and subtotal > selection.budget_total)
    over_budget_percent = round((subtotal - selection.budget_total) / selection.budget_total * 100, 1) if over_budget and selection.budget_total else 0.0
    risk_codes = []
    if over_budget:
        risk_codes.append("OVER_BUDGET")
    if hotel.cancellation_policy == "non_refundable":
        risk_codes.append("NON_REFUNDABLE")
    cart_id = f"cart-{uuid.uuid4().hex[:10]}"
    cart = Cart(
        id=cart_id,
        destination_id=destination.id,
        hotel_id=hotel.id,
        date_from=selection.date_from,
        date_to=selection.date_to,
        nights=nights,
        guests=selection.guests,
        items=items,
        subtotal=subtotal,
        total=subtotal,
        budget_total=selection.budget_total,
        over_budget=over_budget,
        over_budget_percent=over_budget_percent,
        risk_codes=risk_codes,
        is_synthetic=True,
    )
    _CART_CACHE[cart.id] = cart
    return cart


def book(cart: Cart | dict[str, Any] | str) -> BookingResult | dict[str, Any]:
    """Create a demo booking record only; no real supplier or payment is contacted."""
    if isinstance(cart, str):
        resolved_cart = _CART_CACHE.get(cart)
        if resolved_cart is None:
            return {"error": "UNKNOWN_CART", "message": "Корзина не найдена. Сначала вызови price_cart."}
        cart_obj = resolved_cart
    elif isinstance(cart, Cart):
        cart_obj = cart
    else:
        try:
            cart_obj = Cart.model_validate(cart)
        except Exception as exc:
            return {"error": "INVALID_CART", "message": f"Не удалось проверить корзину: {exc}"}
    booking_id = f"DEMO-{uuid.uuid4().hex[:8].upper()}"
    result = BookingResult(
        booking_id=booking_id,
        booked_at=datetime.now(timezone.utc),
        cart=cart_obj,
    )
    _BOOKING_CACHE[booking_id] = result
    return result


def _weather_description(code: int | None, tmax: float, precip: float | None) -> str:
    if code == 0:
        base = "ясно"
    elif code in (1, 2):
        base = "малооблачно"
    elif code == 3:
        base = "облачно"
    elif code in (45, 48):
        base = "туман"
    elif code is not None and 51 <= code <= 67:
        base = "дождь или морось"
    elif code is not None and 71 <= code <= 86:
        base = "снег"
    elif code is not None and code >= 95:
        base = "гроза"
    else:
        base = "оценка по сезонному климату"
    if precip is not None and precip >= 60 and "дожд" not in base:
        base += ", вероятность осадков повышена"
    if tmax < 5:
        base += ", холодно"
    elif tmax >= 25:
        base += ", тепло"
    return base


def _met_weather_description(symbol_code: str | None, tmax: float) -> str:
    """Translate MET Norway's symbol code into a concise Russian description."""
    symbol = (symbol_code or "").casefold().split("_")[0]
    descriptions = {
        "clearsky": "ясно",
        "fair": "малооблачно",
        "partlycloudy": "переменная облачность",
        "cloudy": "облачно",
        "fog": "туман",
        "lightrain": "небольшой дождь",
        "rain": "дождь",
        "heavyrain": "сильный дождь",
        "lightrainshowers": "небольшие ливни",
        "rainshowers": "ливни",
        "heavyrainshowers": "сильные ливни",
        "lightsleet": "небольшой мокрый снег",
        "sleet": "мокрый снег",
        "heavysleet": "сильный мокрый снег",
        "lightsnow": "небольшой снег",
        "snow": "снег",
        "heavysnow": "сильный снег",
        "lightsnowshowers": "небольшие снежные заряды",
        "snowshowers": "снежные заряды",
        "heavysnowshowers": "сильные снежные заряды",
        "thunderstorm": "гроза",
        "lightrainandthunder": "небольшой дождь с грозой",
        "rainandthunder": "дождь с грозой",
        "heavyrainandthunder": "сильный дождь с грозой",
        "lightsnowandthunder": "небольшой снег с грозой",
        "snowandthunder": "снег с грозой",
        "heavysnowandthunder": "сильный снег с грозой",
    }
    base = descriptions.get(symbol, "прогноз погоды MET Norway")
    if tmax < 5:
        base += ", холодно"
    elif tmax >= 25:
        base += ", тепло"
    return base


def _met_cache_path(latitude: float, longitude: float) -> Path:
    """Use a per-user cache outside the repository so API data is not committed."""
    return Path.home() / ".cache" / "travel_data" / "met_norway" / f"{latitude:.4f}_{longitude:.4f}.json"


def _parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def _get_met_payload(destination: Any) -> dict[str, Any] | None:
    """Fetch/cache raw MET Norway forecast and respect its Expires/Last-Modified headers."""
    if os.getenv("MET_NO_ENABLED", "true").strip().casefold() in {"false", "0", "no", "off"}:
        return None
    latitude = round(float(destination.latitude), 4)
    longitude = round(float(destination.longitude), 4)
    key = (latitude, longitude)
    cache_path = _met_cache_path(latitude, longitude)
    now = datetime.now(timezone.utc)
    cached: dict[str, Any] | None = _MET_RESPONSE_CACHE.get(key)

    if cached is None and cache_path.exists():
        try:
            loaded = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("payload"), dict):
                cached = loaded
                _MET_RESPONSE_CACHE[key] = loaded
        except (OSError, ValueError, TypeError):
            logger.debug("Unable to read MET Norway cache at %s", cache_path, exc_info=True)

    if cached is not None:
        expiry = _parse_http_date(cached.get("expires_at"))
        if expiry is not None and expiry > now:
            return cached.get("payload")

    timeout_seconds = float(os.getenv("MET_NO_TIMEOUT_SECONDS", "5"))
    user_agent = os.getenv("MET_NO_USER_AGENT", "").strip()
    if not user_agent:
        # Set a real contact address or public project URL in MET_NO_USER_AGENT.
        user_agent = "travel-data-hackathon/0.1 contact: configure-MET_NO_USER_AGENT"
    headers = {"User-Agent": user_agent}
    if cached and cached.get("last_modified"):
        headers["If-Modified-Since"] = cached["last_modified"]

    try:
        transport = httpx.HTTPTransport(retries=1)
        with httpx.Client(transport=transport, timeout=timeout_seconds, trust_env=False) as client:
            response = client.get(
                MET_NO_URL,
                params={"lat": latitude, "lon": longitude},
                headers=headers,
            )

        if response.status_code == 304 and cached is not None:
            payload = cached["payload"]
            last_modified = response.headers.get("Last-Modified") or cached.get("last_modified")
        else:
            response.raise_for_status()
            payload = response.json()
            last_modified = response.headers.get("Last-Modified")

        expiry = _parse_http_date(response.headers.get("Expires"))
        if expiry is None or expiry <= now:
            # Defensive fallback for gateways that omit/expire Expires; avoids refetching on every run.
            cache_control = response.headers.get("Cache-Control", "")
            max_age = None
            for directive in cache_control.split(","):
                directive = directive.strip()
                if directive.startswith("max-age="):
                    try:
                        max_age = max(60, int(directive.split("=", 1)[1]))
                    except ValueError:
                        pass
            expiry = now + timedelta(seconds=max_age or 900)

        cache_record = {
            "payload": payload,
            "expires_at": expiry.strftime("%a, %d %b %Y %H:%M:%S GMT"),
            "last_modified": last_modified,
            "updated_at": now.isoformat(),
        }
        _MET_RESPONSE_CACHE[key] = cache_record
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache_record, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.debug("Unable to persist MET Norway cache at %s", cache_path, exc_info=True)
        return payload
    except (httpx.HTTPError, ValueError, KeyError, TypeError, OSError) as exc:
        logger.warning(
            "MET Norway unavailable for %s; trying next weather provider. Error: %s: %s",
            destination.name, type(exc).__name__, exc,
        )
        return None


def _fetch_met_norway(destination: Any, target: date) -> WeatherSummary | None:
    """Convert MET Norway's sub-daily point forecast to a daily summary."""
    today = date.today()
    if not today <= target <= today + timedelta(days=9):
        return None
    payload = _get_met_payload(destination)
    if not payload:
        return None
    try:
        timeseries = payload.get("properties", {}).get("timeseries", [])
        tz = ZoneInfo(destination.timezone)
        day_rows: list[tuple[datetime, float, str | None]] = []
        for row in timeseries:
            stamp = row.get("time")
            if not stamp:
                continue
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            local_stamp = parsed.astimezone(tz)
            if local_stamp.date() != target:
                continue
            data = row.get("data", {})
            details = data.get("instant", {}).get("details", {})
            temperature = details.get("air_temperature")
            if temperature is None:
                continue
            next_period = data.get("next_1_hours") or data.get("next_6_hours") or data.get("next_12_hours") or {}
            symbol = next_period.get("summary", {}).get("symbol_code")
            day_rows.append((local_stamp, float(temperature), symbol))

        if not day_rows:
            return None
        temperatures = [row[1] for row in day_rows]
        representative = min(day_rows, key=lambda row: abs(row[0].hour - 12))
        tmin, tmax = min(temperatures), max(temperatures)
        return WeatherSummary(
            destination_id=destination.id,
            target_date=target,
            temperature_min_c=tmin,
            temperature_max_c=tmax,
            # MET Norway compact forecast gives precipitation amount, not probability.
            precipitation_probability=None,
            weather_code=None,
            description=_met_weather_description(representative[2], tmax),
            source="MET Norway Locationforecast API",
            is_synthetic=False,
        )
    except (ValueError, KeyError, TypeError, ZoneInfoNotFoundError) as exc:
        logger.warning(
            "Could not parse MET Norway forecast for %s on %s: %s: %s",
            destination.name, target.isoformat(), type(exc).__name__, exc,
        )
        return None


def _fetch_open_meteo(destination: Any, target: date) -> WeatherSummary | None:
    """Fetch a daily Open-Meteo forecast when the date is inside its forecast window."""
    today = date.today()
    if not today <= target <= today + timedelta(days=15):
        return None
    if os.getenv("OPEN_METEO_ENABLED", "true").strip().casefold() in {"false", "0", "no", "off"}:
        return None
    params = {
        "latitude": destination.latitude,
        "longitude": destination.longitude,
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
        "timezone": destination.timezone,
        "start_date": target.isoformat(),
        "end_date": target.isoformat(),
    }
    try:
        timeout_seconds = float(os.getenv("OPEN_METEO_TIMEOUT_SECONDS", "2.5"))
        transport = httpx.HTTPTransport(retries=1)
        with httpx.Client(transport=transport, timeout=timeout_seconds, trust_env=False) as client:
            response = client.get(OPEN_METEO_URL, params=params)
        response.raise_for_status()
        payload = response.json()
        daily = payload.get("daily", {})
        if not daily.get("time") or daily["time"][0] != target.isoformat():
            return None
        tmax = float(daily["temperature_2m_max"][0])
        tmin = float(daily["temperature_2m_min"][0])
        precip_value = daily.get("precipitation_probability_max", [None])[0]
        precip = float(precip_value) if precip_value is not None else None
        code_value = daily.get("weather_code", [None])[0]
        code = int(code_value) if code_value is not None else None
        return WeatherSummary(
            destination_id=destination.id,
            target_date=target,
            temperature_min_c=tmin,
            temperature_max_c=tmax,
            precipitation_probability=precip,
            weather_code=code,
            description=_weather_description(code, tmax, precip),
            source="Open-Meteo Forecast API",
            is_synthetic=False,
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        logger.warning(
            "Open-Meteo unavailable for %s on %s; using synthetic weather if no other provider works. Error: %s: %s",
            destination.name, target.isoformat(), type(exc).__name__, exc,
        )
        return None


def get_weather(destination_id: str, target_date: date | str | None = None) -> WeatherSummary | dict[str, Any]:
    """Try MET Norway, then Open-Meteo, then return explicitly labelled synthetic climate."""
    destination = get_destination(destination_id)
    if destination is None:
        return {"error": "UNKNOWN_DESTINATION", "message": "Направление не найдено в мок-мире."}
    if target_date is None:
        target = date.today() + timedelta(days=1)
    elif isinstance(target_date, datetime):
        target = target_date.date()
    elif isinstance(target_date, date):
        target = target_date
    else:
        try:
            target = date.fromisoformat(target_date)
        except ValueError:
            return {"error": "INVALID_DATE", "message": "target_date должен быть в формате YYYY-MM-DD."}

    cache_key = (destination.id, target)
    if cache_key in _WEATHER_CACHE:
        return _WEATHER_CACHE[cache_key]

    # MET Norway is primary: it was verified reachable without VPN in the user's network.
    result = _fetch_met_norway(destination, target)
    if result is None:
        result = _fetch_open_meteo(destination, target)
    if result is not None:
        _WEATHER_CACHE[cache_key] = result
        return result

    climate = destination.climate_monthly[str(target.month)]
    tmax = float(climate["temperature_max_c"])
    tmin = float(climate["temperature_min_c"])
    precip = float(climate["precipitation_probability"])
    result = WeatherSummary(
        destination_id=destination.id,
        target_date=target,
        temperature_min_c=tmin,
        temperature_max_c=tmax,
        precipitation_probability=precip,
        weather_code=None,
        description=_weather_description(None, tmax, precip),
        source="synthetic monthly climate estimate",
        is_synthetic=True,
    )
    _WEATHER_CACHE[cache_key] = result
    return result

