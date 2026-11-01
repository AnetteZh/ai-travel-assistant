"""Защита ответа: последний фильтр перед тем, как текст увидит пользователь.

Три правила:
1. Внутренние id (hotel-sochi-03, ev-..., option-...) и имена полей (has_pool)
   убираются из текста. Коды рисков (OVER_BUDGET) заменяются на понятные слова.
2. UNSUPPORTED_NUMBER: каждое «значимое» число в тексте (деньги, проценты,
   длительности, расстояния) должно найтись в данных поездки (TripState,
   Evidence, корзина). Предложение с выдуманным числом вырезается.
3. Починка вместо отказа: плохой кусок убираем, ответ целиком не роняем.
   Всё, что вырезали, возвращаем в отчёте, чтобы записать в trace.

Даты и время здесь не проверяются: состыковки по времени проверяет check_plan.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from pydantic import BaseModel

from travel_data import models as data_models
from travel_data.codes import HARD_REQUIREMENT_VIOLATIONS, REASON_CODE_DESCRIPTIONS, RISK_CODE_DESCRIPTIONS

# ---------------------------------------------------------------- ids & codes

_ID_PREFIXES = ("ev", "dest", "hotel", "route", "transfer", "option", "cart", "booking", "fee")
_ID_RE = re.compile(r"\b(?:%s)-[A-Za-z0-9][A-Za-z0-9_-]*" % "|".join(_ID_PREFIXES))

CODE_LABELS = {
    "OVER_BUDGET": "дороже бюджета",
    "LATE_ARRIVAL": "поздний приезд",
    "TRANSFER_OUTSIDE_HOURS": "трансфер не работает в нужное время",
    "CHECKIN_GAP": "долгое ожидание заселения",
    "TIGHT_CONNECTION": "мало времени на пересадку или регистрацию",
    "NON_REFUNDABLE": "невозвратный тариф",
    "WEATHER_RISK": "погодный риск",
    "DATE_MISMATCH": "даты не совпадают",
    "MATCHES_HARD": "выполнены обязательные условия",
    "KID_FRIENDLY": "подходит для детей",
    "FREE_CANCELLATION": "бесплатная отмена",
    "SHORT_TRAVEL": "недолгая дорога",
    "NEAR_POI": "рядом с центром или пляжем",
    "PETS_NOT_ALLOWED": "нельзя с животными",
    "POOL_REQUIRED": "нет бассейна",
    "SPA_REQUIRED": "нет спа",
    "FREE_CANCELLATION_REQUIRED": "нет бесплатной отмены",
    "FAMILY_FRIENDLY_REQUIRED": "нет семейных удобств",
    "UNSUPPORTED_NUMBER": "неподтверждённое число",
}
_ALL_CODES = sorted(
    {*CODE_LABELS, *RISK_CODE_DESCRIPTIONS, *REASON_CODE_DESCRIPTIONS, *HARD_REQUIREMENT_VIOLATIONS},
    key=len,
    reverse=True,
)
_CODE_RE = re.compile(r"\b(%s)\b" % "|".join(_ALL_CODES))


def _field_names() -> set[str]:
    names: set[str] = set()
    for value in vars(data_models).values():
        if isinstance(value, type) and issubclass(value, BaseModel):
            names.update(name for name in value.model_fields if "_" in name)
    return names


_FIELD_RE = re.compile(r"\b(%s)\b" % "|".join(sorted(_field_names(), key=len, reverse=True)))

# ---------------------------------------------------------------- numbers

_NUM = r"\d{1,3}(?:[   ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_HOURS_MINUTES_RE = re.compile(rf"(?P<h>\d+)\s*ч(?:ас\w*)?\.?\s*(?P<m>\d+)\s*мин\w*")
_UNIT_RE = re.compile(
    rf"(?P<num>{_NUM})\s*(?P<unit>₽|руб\w*|RUB|%|°C|°|ч\b|час\w*|мин\w*|км\b|м\b)",
)
_BARE_NUMBER_RE = re.compile(rf"(?<![\d:.\-/])(?P<num>{_NUM})(?![\d:\-/])(?!\.\d)")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_STRING_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
MONEY_LIKE_MIN = 1000  # «голое» число от 1000 без единиц считаем деньгами и тоже проверяем


def _to_float(raw: str) -> float:
    return float(re.sub(r"[   ]", "", raw).replace(",", "."))


def _walk_numbers(value: Any, out: set[float]) -> None:
    if isinstance(value, BaseModel):
        _walk_numbers(value.model_dump(mode="json"), out)
    elif isinstance(value, dict):
        for item in value.values():
            _walk_numbers(item, out)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            _walk_numbers(item, out)
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        out.add(float(value))
    elif isinstance(value, str):
        for match in _STRING_NUMBER_RE.findall(value):
            out.add(_to_float(match))


def collect_known_numbers(*sources: Any, extra: Iterable[float] = ()) -> set[float]:
    """Все числа из данных поездки плюс производные (часы из минут, проценты из долей)."""
    raw: set[float] = set(extra)
    for source in sources:
        _walk_numbers(source, raw)
    known = set(raw)
    for value in raw:
        known.add(round(value))
        known.add(round(value, 1))
        known.add(round(value * 100))  # 0.6 → 60%
        if value >= 60:  # минуты → часы
            known.add(round(value / 60, 1))
            known.add(float(int(value // 60)))
    return known


def _supported(value: float, known: set[float]) -> bool:
    tolerance = max(1.0, abs(value) * 0.005)
    return any(abs(value - item) <= tolerance for item in known)


def _unsupported_in(sentence: str, known: set[float]) -> list[str]:
    bad: list[str] = []
    consumed: list[tuple[int, int]] = []
    for match in _HOURS_MINUTES_RE.finditer(sentence):
        total = int(match["h"]) * 60 + int(match["m"])
        consumed.append(match.span())
        if not _supported(total, known):
            bad.append(match.group(0))
    for match in _UNIT_RE.finditer(sentence):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        consumed.append(match.span())
        if not _supported(_to_float(match["num"]), known):
            bad.append(match.group(0).strip())
    for match in _BARE_NUMBER_RE.finditer(sentence):
        if any(start <= match.start() < end for start, end in consumed):
            continue
        value = _to_float(match["num"])
        if value >= MONEY_LIKE_MIN and not _supported(value, known):
            bad.append(match["num"])
    return bad


# ---------------------------------------------------------------- public API

@dataclass(frozen=True)
class GuardrailReport:
    text: str
    removed_ids: list[str] = field(default_factory=list)
    replaced_codes: list[str] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    removed_sentences: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.removed_ids or self.replaced_codes or self.removed_sentences)


def _clean_spacing(text: str) -> str:
    text = re.sub(r"\(\s*[,;]?\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" +([,.;:!?])", r"\1", text)
    return text.strip()


def guard_response_text(text: str, *sources: Any, extra_known: Iterable[float] = ()) -> GuardrailReport:
    """Очищает текст ответа.

    sources — всё, на что агенту разрешено ссылаться: TripState, Evidence, корзина.
    """
    if not text:
        return GuardrailReport(text=text)

    removed_ids = _ID_RE.findall(text)
    text = _ID_RE.sub("", text)
    text = _FIELD_RE.sub(lambda m: (removed_ids.append(m.group(0)), "")[1], text)

    replaced_codes = _CODE_RE.findall(text)
    text = _CODE_RE.sub(lambda m: CODE_LABELS.get(m.group(0), m.group(0).replace("_", " ").lower()), text)

    known = collect_known_numbers(*sources, extra=extra_known)
    unsupported: list[str] = []
    removed_sentences: list[str] = []
    lines_out: list[str] = []
    for line in text.split("\n"):
        kept: list[str] = []
        for sentence in _SENTENCE_SPLIT_RE.split(line):
            bad = _unsupported_in(sentence, known)
            if bad:
                unsupported.extend(bad)
                removed_sentences.append(sentence.strip())
            else:
                kept.append(sentence)
        cleaned = _clean_spacing(" ".join(kept))
        if cleaned or not line.strip():
            lines_out.append(cleaned)
    return GuardrailReport(
        text="\n".join(lines_out).strip(),
        removed_ids=removed_ids,
        replaced_codes=replaced_codes,
        unsupported_numbers=unsupported,
        removed_sentences=removed_sentences,
    )
