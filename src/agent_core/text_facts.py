"""Факты, которые надёжно достаются из текста сообщения без языковой модели.

Используются как страховка: если модель вернула поле словами («15 декабря», «60 тысяч»,
«null») или потеряла его, Core берёт значение отсюда. Правила намеренно узкие — только явные
формулировки, чтобы не придумывать того, чего пользователь не говорил.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

EMPTY_MARKERS = {
    "null", "none", "nil", "undefined", "nan", "n/a", "na", "-", "—", "?",
    "нет", "не указано", "не указан", "не указана", "неизвестно", "нет данных", "н/д",
}

_MONTHS = {
    "январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "мае": 5, "июн": 6,
    "июл": 7, "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12,
}
_MONTH = r"(январ\w*|феврал\w*|март\w*|апрел\w*|ма[йяе]|июн\w*|июл\w*|август\w*|сентябр\w*|октябр\w*|ноябр\w*|декабр\w*)"
_YEAR = r"(?:\s+(20\d{2})(?:\s*г(?:ода|\.)?)?)?"

_NUMBER_WORDS = {
    "один": 1, "одну": 1, "одна": 1, "день": 1, "сутки": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "неделю": 7,
}


def is_empty_marker(value: object) -> bool:
    return isinstance(value, str) and value.strip().casefold() in EMPTY_MARKERS | {""}


def _month(word: str) -> int | None:
    word = word.casefold()
    return next((number for stem, number in _MONTHS.items() if word.startswith(stem)), None)


def _future(day: int, month: int, year: str | None, today: date) -> date | None:
    """Дата без года — ближайшая будущая; с годом — как сказано (прошедшую отсеет Core)."""
    try:
        value = date(int(year) if year else today.year, month, day)
    except ValueError:
        return None
    if not year and value < today:
        try:
            value = value.replace(year=value.year + 1)
        except ValueError:
            return None
    return value


def parse_date_range(text: str, today: date) -> tuple[date, date] | None:
    """«с 15 по 22 декабря», «с 28 декабря по 5 января», «15–22 декабря», «15.12–22.12»."""
    text = text.casefold()
    patterns = (
        rf"с\s+(\d{{1,2}})\s*{_MONTH}?{_YEAR}\s+(?:по|до)\s+(\d{{1,2}})\s*{_MONTH}{_YEAR}",
        rf"(\d{{1,2}})\s*{_MONTH}?{_YEAR}\s*[-–—]\s*(\d{{1,2}})\s*{_MONTH}{_YEAR}",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            d1, m1, y1, d2, m2, y2 = match.groups()
            month_to = _month(m2)
            month_from = _month(m1) if m1 else month_to
            if month_from is None or month_to is None:
                continue
            start = _future(int(d1), month_from, y1 or y2, today)
            end = _future(int(d2), month_to, y2 or y1, today)
            if start and end and end <= start and not (y1 or y2):
                end = _future(int(d2), month_to, str(start.year + 1), today)
            if start and end and end > start:
                return start, end
    match = re.search(
        r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?\s*(?:[-–—]|по|до)\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text
    )
    if match:
        d1, m1, y1, d2, m2, y2 = match.groups()
        y1 = f"20{y1}" if y1 and len(y1) == 2 else y1
        y2 = f"20{y2}" if y2 and len(y2) == 2 else y2
        start = _future(int(d1), int(m1), y1 or y2, today) if 1 <= int(m1) <= 12 else None
        end = _future(int(d2), int(m2), y2 or y1, today) if 1 <= int(m2) <= 12 else None
        if start and end and end > start:
            return start, end
    return None


def parse_single_date(value: str, today: date) -> date | None:
    """Дата, которую модель записала словами: «15 декабря», «15.12.2026», «2026-12-15»."""
    text = value.strip().casefold()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    match = re.fullmatch(rf"(\d{{1,2}})\s*{_MONTH}{_YEAR}", text)
    if match:
        month = _month(match.group(2))
        return _future(int(match.group(1)), month, match.group(3), today) if month else None
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", text)
    if match:
        year = match.group(3)
        year = f"20{year}" if year and len(year) == 2 else year
        month = int(match.group(2))
        return _future(int(match.group(1)), month, year, today) if 1 <= month <= 12 else None
    return None


def parse_shift_days(text: str) -> int | None:
    """«на два дня позже» → 2, «на день раньше» → −1, «на неделю позже» → 7."""
    match = re.search(
        r"на\s+(\d+|один|одну|два|две|три|четыре|пять|шесть|семь|неделю|день|сутки)"
        r"\s*(дн\w*|суток|сутки|недел\w*)?\s+(позже|раньше)",
        text.casefold(),
    )
    if not match:
        return None
    amount, unit, direction = match.groups()
    days = int(amount) if amount.isdigit() else _NUMBER_WORDS[amount]
    if unit and unit.startswith("недел") and amount != "неделю":
        days *= 7
    return days if direction == "позже" else -days


def parse_money(text: str) -> int | None:
    """Общая сумма, названная явно: «80 тысяч», «до 100к», «тысяч 60», «120 000 ₽»."""
    text = text.casefold().replace(" ", " ")
    per_night = r"(?![\w.]*\s*(?:₽|руб\w*|р\.?)?\s*(?:за|в)\s+(?:ночь|сутки))"
    patterns = (
        (rf"(\d+(?:[.,]\d+)?)\s*(?:тыс\w*|тыщ\w*|т\.\s*р\.?|к\b|k\b){per_night}", 1000),
        (rf"тысяч\w*\s+(\d+(?:[.,]\d+)?){per_night}", 1000),
        (rf"(\d{{1,3}}(?:\s\d{{3}})+|\d{{4,}})\s*(?:₽|руб\w*|р\b){per_night}", 1),
        (rf"бюджет\w*\s*(?:до|в|около|примерно)?\s*(\d{{1,3}}(?:\s\d{{3}})+|\d{{4,}}){per_night}", 1),
    )
    for pattern, multiplier in patterns:
        match = re.search(pattern, text)
        if match:
            number = float(match.group(1).replace(" ", "").replace(",", "."))
            value = int(round(number * multiplier))
            return value if value > 0 else None
    return None


def coerce_money(value: str) -> int | None:
    """Бюджет, который модель записала строкой: «80000», «80 тысяч», «100к»."""
    digits = value.replace(" ", "").replace(" ", "")
    if digits.isdigit():
        return int(digits) or None
    return parse_money(value)


def per_person(text: str) -> bool:
    return bool(re.search(r"на\s+(?:человека|каждого|одного|персону|нос)|с\s+человека|за\s+человека", text.casefold()))


def parse_children_ages(text: str) -> list[int] | None:
    """Возрасты детей, названные числами рядом с «лет/года/годик/месяцев»."""
    text = text.casefold()
    found: list[int] = []
    for match in re.finditer(r"((?:\d{1,2}\s*(?:,|и|-|–)\s*)*\d{1,2})\s*(год\w*|лет|годик\w*|месяц\w*|мес\b)", text):
        numbers = [int(number) for number in re.findall(r"\d{1,2}", match.group(1))]
        if match.group(2).startswith("мес"):
            numbers = [0 for _ in numbers]
        found.extend(number for number in numbers if 0 <= number <= 17)
    return found or None


def parse_adults(text: str) -> int | None:
    text = text.casefold()
    words = {"один": 1, "одна": 1, "двое": 2, "два": 2, "трое": 3, "три": 3, "четверо": 4, "четыре": 4}
    match = re.search(r"(\d|один|одна|двое|два|трое|три|четверо|четыре)\s+взросл", text)
    if match:
        value = match.group(1)
        return int(value) if value.isdigit() else words[value]
    if re.search(r"вдво[её]м|на\s+двоих|с\s+мужем|с\s+женой|с\s+супруг|с\s+парнем|с\s+девушкой", text):
        return 2
    if re.search(r"втро[её]м|на\s+троих", text):
        return 3
    return None


def mentions_pet(text: str) -> bool:
    text = text.casefold()
    if re.search(r"без\s+(?:собак|кош|кот|питом|животн)", text):
        return False
    return bool(re.search(r"собак|\bпс[аоы]\b|\bпёс|\bпес\b|кошк|\bкот\b|\bкота\b|котом|питом|животн", text))


_AMENITIES = (
    (r"бассейн", "бассейн"),
    (r"\bспа\b|\bspa\b", "спа"),
    (r"бесплатн\w*\s+отмен|отмен\w*\s+бесплатн", "бесплатная отмена"),
    (r"без\s+пересад", "без пересадок"),
)
_REQUIRED = re.compile(r"нуж[еннао]|обязательн|обязател|должен|должна|должно|только\s+с|непременно|важно", re.IGNORECASE)


def required_amenities(text: str) -> list[str]:
    """Обязательные условия, если пользователь прямо сказал, что они нужны."""
    lowered = text.casefold()
    result = [name for pattern, name in _AMENITIES if re.search(pattern, lowered)] if _REQUIRED.search(lowered) else []
    stars = re.search(r"(?:не\s+(?:менее|меньше|ниже)|от|минимум)\s+(\d)\s*(?:зв|\*)", lowered)
    if stars:
        result.append(f"не менее {stars.group(1)} звёзд")
    return result


def shift(value: date | None, days: int) -> date | None:
    return value + timedelta(days=days) if value else None
