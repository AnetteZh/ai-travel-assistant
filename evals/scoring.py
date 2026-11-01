"""Сравнение разбора запроса с эталоном (golden.jsonl), по полям."""

from __future__ import annotations

from typing import Any

from agent_core.contracts import RequestExtraction, TripRequest
from travel_data.world import get_destination

# Порядок колонок в отчёте.
FIELDS = [
    "intent", "origin", "destination", "date_from", "date_to", "budget_total",
    "adults", "children_ages", "pets", "hard_contains", "soft_contains", "mood_contains",
]


def merged_request(extraction: RequestExtraction) -> TripRequest:
    """Патч поверх пустого запроса: так же, как это делает merge_node в графе."""
    updates = extraction.patch.model_dump(exclude_none=True)
    return TripRequest.model_validate({**TripRequest().model_dump(), **updates})


def _city_id(value: str | None) -> str | None:
    if not value:
        return None
    destination = get_destination(value)
    return destination.id if destination else f"unknown:{value}"


def _contains_all(haystack: list[str] | str | None, needles: list[str] | str) -> bool:
    if isinstance(needles, str):
        needles = [needles]
    text = " ".join(haystack) if isinstance(haystack, list) else (haystack or "")
    text = text.casefold()
    return all(needle.casefold() in text for needle in needles)


def actual_value(field: str, extraction: RequestExtraction, request: TripRequest) -> Any:
    if field == "intent":
        return extraction.intent
    if field == "origin":
        return _city_id(request.origin)
    if field == "destination":
        return _city_id(request.destination)
    if field in ("date_from", "date_to"):
        value = getattr(request, field)
        return value.isoformat() if value else None
    if field == "budget_total":
        return request.budget_total
    if field == "adults":
        return request.guests.adults
    if field == "children_ages":
        return sorted(request.guests.children_ages)
    if field == "pets":
        return request.guests.pets
    if field == "hard_contains":
        return request.hard
    if field == "soft_contains":
        return request.soft
    if field == "mood_contains":
        return request.mood
    raise KeyError(field)


def expected_value(field: str, expected: dict[str, Any]) -> Any:
    value = expected[field]
    if field == "origin":
        return _city_id(value)
    if field == "children_ages":
        return sorted(value)
    return value


def score_case(case: dict[str, Any], extraction: RequestExtraction) -> dict[str, bool]:
    """{поле: угадано?} только для полей, которые есть в эталоне этого запроса."""
    request = merged_request(extraction)
    result: dict[str, bool] = {}
    for field in FIELDS:
        if field not in case["expected"]:
            continue
        actual = actual_value(field, extraction, request)
        if field.endswith("_contains"):
            result[field] = _contains_all(actual, case["expected"][field])
        else:
            result[field] = actual == expected_value(field, case["expected"])
    return result
