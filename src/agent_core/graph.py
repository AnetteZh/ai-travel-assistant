from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, time, timedelta, timezone
import json
import re
from typing import Any, Callable, Literal, TypedDict
from zoneinfo import ZoneInfo

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError
from travel_data import search_hotels, search_transfers, search_transport, suggest_destinations
from travel_data.models import Cart, CartSelection, Evidence
from travel_data.tools import price_cart
from travel_data.world import get_destination
import trip_checks

from .config import Settings, get_settings, prompt_path
from .text_facts import (
    coerce_money,
    is_empty_marker,
    mentions_pet,
    parse_adults,
    parse_children_ages,
    parse_date_range,
    parse_money,
    parse_shift_days,
    parse_single_date,
    per_person,
    required_amenities,
)
from .contracts import (
    RequestExtraction,
    TraceEntry,
    TravelPlan,
    TripIssue,
    TripRequest,
    TripRequestPatch,
    TripState,
    UIBlock,
)


class CoreError(RuntimeError):
    """Expected, user-presentable failure from Core or a Data tool."""


class GraphInput(TypedDict, total=False):
    trip: TripState
    message: str
    patch: TripRequestPatch | None
    intent: str
    clarification_question: str | None
    detail_target: str | None
    action: str


RequestParser = Callable[[str], RequestExtraction]

# Сколько отелей показывать и сравнивать. Data возвращает до 10 на город.
MAX_HOTEL_CARDS = 6
MAX_DESTINATION_CARDS = 5

# План с такими конфликтами физически не работает: корзина не рассчитывается, бронь недоступна.
BLOCKING_CODES = frozenset({"DATE_MISMATCH", "LATE_ARRIVAL", "TRANSFER_OUTSIDE_HOURS"})
# Конфликты, для которых repair_plan умеет подобрать замену (одна попытка за ход).
REPAIRABLE_CODES = BLOCKING_CODES | {"CHECKIN_GAP", "OVER_BUDGET"}

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(Bearer)(\s+)\S+"),
    re.compile(r"(?i)(authorization|credentials|access_token|api_key)(\s*[:=]\s*)[^\s,;]+"),
)


def _safe_model_error(exc: Exception, settings: Settings) -> str:
    message = str(exc).strip() or "Провайдер не вернул описание ошибки."
    for secret in (settings.gigachat_credentials, settings.openai_api_key):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        message = pattern.sub(r"\1\2[REDACTED]", message)
    message = " ".join(message.split())
    if len(message) > 400:
        message = message[:397] + "..."
    return f"{type(exc).__name__}: {message}"


def _validation_error_summary(exc: ValidationError) -> str:
    issues = []
    for error in exc.errors(include_input=False):
        field = ".".join(str(part) for part in error["loc"]) or "response"
        issues.append(f"{field}: {error['msg']}")
    return "; ".join(issues[:3])


def customer_facing_text(value: str) -> str | None:
    lowered = value.casefold()
    if any(marker in lowered for marker in ("synthetic", "синтет", "демонстрац", "мок-мир")):
        return None
    return value


def _build_structured_model(settings: Settings) -> Any:
    if settings.llm_provider == "openai_compatible":
        if not settings.openai_api_key:
            raise CoreError(
                "OpenAI-compatible модель не настроена: задайте OPENAI_API_KEY в локальном .env."
            )
        from langchain_openai import ChatOpenAI

        model = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.openai_timeout_seconds,
            max_retries=1,
        )
        return model.with_structured_output(RequestExtraction, include_raw=True)

    if not settings.gigachat_credentials:
        raise CoreError(
            "GigaChat не настроен: задайте GIGACHAT_CREDENTIALS в локальном .env."
        )
    from langchain_gigachat import GigaChat

    model = GigaChat(
        model=settings.gigachat_model,
        credentials=settings.gigachat_credentials,
        scope=settings.gigachat_scope,
        timeout=settings.openai_timeout_seconds,
        verify_ssl_certs=False,
        max_retries=1,
    )
    return model.with_structured_output(RequestExtraction, method="function_calling", include_raw=True)


def _parser_context(trip: TripState) -> str:
    choices: dict[str, list[dict[str, str | int]]] = {
        "destinations": [],
        "hotels": [],
        "hotel_alternatives": [],
        "outbound_transport": [],
        "return_transport": [],
    }
    if trip.destinations:
        choices["destinations"] = [
            {
                "number": index,
                "id": item.destination.id,
                "name": item.destination.name,
            }
            for index, item in enumerate(trip.destinations.options[:MAX_DESTINATION_CARDS], start=1)
        ]
    if trip.hotels:
        hotel_options = trip.hotels.options[:MAX_HOTEL_CARDS] or trip.hotels.alternatives[:MAX_HOTEL_CARDS]
        key = "hotels" if trip.hotels.options else "hotel_alternatives"
        choices[key] = [
            {"number": index, "id": item.hotel.id, "name": item.hotel.name}
            for index, item in enumerate(hotel_options, start=1)
        ]
    if trip.transport:
        for key, options in (
            ("outbound_transport", trip.transport.outbound[:3]),
            ("return_transport", trip.transport.return_options[:3]),
        ):
            choices[key] = [
                {
                    "number": index,
                    "id": option.id,
                    "name": (
                        f"{option.transport_type} {option.origin_city}–{option.destination_city} "
                        f"{option.departure_at.isoformat()}"
                    ),
                }
                for index, option in enumerate(options, start=1)
            ]
    if trip.plan:
        choices["current_plan"] = [{
            "number": 1,
            "id": "current_plan",
            "name": (
                f"{trip.plan.hotel.hotel.name}; "
                f"туда: {trip.plan.outbound.id if trip.plan.outbound else 'не выбран'}; "
                f"обратно: {trip.plan.inbound.id if trip.plan.inbound else 'не выбран'}"
            ),
        }]
    if trip.cart:
        choices["cart"] = [{
            "number": 1,
            "id": "cart",
            "name": (
                f"текущая корзина, итог {trip.cart.total} RUB, "
                f"позиции: {len(trip.cart.items)}"
            ),
        }]
    return json.dumps(choices, ensure_ascii=False)


_MONEY_MENTION = re.compile(r"\d|тыс|млн|миллион|рубл|₽", re.IGNORECASE)
# Число само по себе — не дата («бюджет 80 тысяч»): нужны месяц, слово-время или формат даты.
_DATE_MENTION = re.compile(
    r"январ|феврал|\bмарт|апрел|\bма[йяе]\b|\bиюн|\bиюл|август|сентябр|октябр|ноябр|декабр|"
    r"сегодня|завтра|выходн|праздник|через|следующ|ближайш|весн|летом|\bлето\b|осен|\bзим|"
    r"позж|раньш|сдвин|перен[её]с|перенест|продл|сократ|"
    r"\d{1,2}[./]\d{1,2}|\d{4}-\d{2}-\d{2}|\bс\s+\d{1,2}\b|\d{1,2}\s*(?:-?го|числ)",
    re.IGNORECASE,
)
_CHILDREN_MENTION = re.compile(r"\bдет|ребён|ребен|малыш|\bсын|\bдоч", re.IGNORECASE)
# Сказано что-то о составе путешественников. Без этого модель на каждом шаге присылает guests
# «по умолчанию» и затирает уже известный состав (например, питомца или детей).
_GUESTS_MENTION = re.compile(
    r"вдво|втро|вчетвер|\bдвое\b|\bтрое\b|\bчетверо\b|взросл|\bдет|ребён|ребен|малыш|\bсын|\bдоч|"
    r"собак|\bпс[аоы]|\bпёс|\bпес\b|кош|\bкот|питом|животн|\bодин\b|\bодна\b|\bсам\b|\bсама\b|"
    r"\bмуж|\bжен|супруг|семь|человек|\bдруз|\bдруг\b|\bдруга\b|подруг|\bпарн|девушк|компани|"
    r"родител|\bмам|\bпап|бабушк|дедушк|гост",
    re.IGNORECASE,
)
# Названия городов демо-мира с разговорными формами. Ключ — название, как его хранит Data.
_CITY_PATTERNS: dict[str, str] = {
    "Москва": r"москв\w*|\bмск\b",
    "Санкт-Петербург": r"петербург\w*|\bпитер\w*|\bспб\b",
    "Сочи": r"\bсочи\b",
    "Казань": r"\bказан\w*",
    "Калининград": r"калининград\w*",
    "Екатеринбург": r"екатеринбург\w*|\bекб\b",
    "Новосибирск": r"новосибирск\w*",
    "Иркутск": r"иркутск\w*",
    "Владивосток": r"владивосток\w*",
    "Мурманск": r"мурманск\w*",
    "Петрозаводск": r"петрозаводск\w*",
    "Минеральные Воды": r"минеральн\w*\s+вод\w*|\bминвод\w*",
    "Махачкала": r"махачкал\w*",
    "Тюмень": r"\bтюмен\w*",
    "Нижний Новгород": r"нижн\w*\s+новгород\w*",
    "Ярославль": r"ярославл\w*",
    "Архангельск": r"архангельск\w*",
    "Пермь": r"\bперм[ьию]\b|\bпермью\b",
    "Уфа": r"\bуф[аеуы]\b|\bуфой\b",
    "Краснодар": r"краснодар\w*",
}
_ORIGIN_PREFIX = re.compile(r"(?:\bиз|\bот)\s+$", re.IGNORECASE)
_OTHER_PLACES = re.compile(r"\bдруг\w*|кроме|вместо|\bещ[её]\b|похож|\bиде[яию]", re.IGNORECASE)
_WEEKEND = re.compile(r"выходн", re.IGNORECASE)
_EXPLICIT_DATE = re.compile(
    r"январ|феврал|\bмарт|апрел|\bма[йяе]\b|\bиюн|\bиюл|август|сентябр|октябр|ноябр|декабр|"
    r"\d{1,2}[./]\d{1,2}|\d{4}-\d{2}-\d{2}",
    re.IGNORECASE,
)


def _mentioned_cities(text: str) -> tuple[list[str], list[str]]:
    """Города из сообщения: (куда, откуда). «из Москвы» и «от Казани» считаются городом вылета."""
    destinations: list[str] = []
    origins: list[str] = []
    for name, pattern in _CITY_PATTERNS.items():
        for match in re.finditer(pattern, text, re.IGNORECASE):
            target = origins if _ORIGIN_PREFIX.search(text[: match.start()]) else destinations
            if name not in target:
                target.append(name)
    return [name for name in destinations if name not in origins], origins


def _nearest_weekend(today: date) -> tuple[date, date]:
    """Ближайшие выходные: заезд в субботу, выезд в понедельник. В субботу и воскресенье — следующие."""
    saturday = today + timedelta(days=(5 - today.weekday()) % 7 or 7)
    return saturday, saturday + timedelta(days=2)


_CHOICE = re.compile(r"давай|бер[уём]|выбира|выбер|подход|поехал|остановимся|хочу", re.IGNORECASE)
_ORDINALS = (
    (re.compile(r"\bперв\w*|(?:вариант\w*|номер|№)\s*1\b", re.IGNORECASE), 0),
    (re.compile(r"\bвтор\w*|(?:вариант\w*|номер|№)\s*2\b", re.IGNORECASE), 1),
    (re.compile(r"\bтрет\w*|(?:вариант\w*|номер|№)\s*3\b", re.IGNORECASE), 2),
    (re.compile(r"\bчетв[её]рт\w*|(?:вариант\w*|номер|№)\s*4\b", re.IGNORECASE), 3),
    (re.compile(r"\bпят(?:ый|ое|ую|ая|ого|ой)\b|(?:вариант\w*|номер|№)\s*5\b", re.IGNORECASE), 4),
    (re.compile(r"\bпоследн\w*", re.IGNORECASE), -1),
)


def _ground_extraction(extraction: RequestExtraction, message: str, trip: TripState | None) -> RequestExtraction:
    """Сверить ответ модели с текстом пользователя и текущей поездкой.

    Модель (особенно GigaChat) дописывает то, чего пользователь не говорил, и пропускает то, что
    сказано прямо. Здесь только проверяемые по тексту правила:
    - пустые значения (null) не стирают известные условия;
    - бюджет — только если названа сумма; даты — только если о них говорили;
    - «на выходные» — ближайшие суббота–понедельник, считаем сами;
    - состав гостей — только если о нём говорили; дети без возраста — вопрос о возрасте;
    - названный город и «давай первый вариант» не теряются, даже если модель их пропустила;
    - когда нужно предложить идеи, лишних уточняющих вопросов не задаём.
    """
    original = extraction.patch.model_dump(exclude_unset=True)
    # null от модели означает «не знаю», а не «удалить»: иначе «бюджет поменьше» стирал город вылета.
    updates = {key: value for key, value in original.items() if value is not None and not is_empty_marker(value)}
    intent = extraction.intent
    clarification = extraction.clarification_question
    if is_empty_marker(clarification):
        clarification = None  # GigaChat пишет строку «null» вместо пустого поля
    text = message.casefold()
    today = date.today()
    request = trip.request if trip is not None else TripRequest()
    current_guests = request.guests.model_dump()

    # --- Даты: только если о них говорили; явный диапазон и сдвиг считаем сами.
    if updates.get("date_from") or updates.get("date_to"):
        starts_today = updates.get("date_from") == today and "сегодня" not in text
        if not _DATE_MENTION.search(text) or starts_today:
            updates.pop("date_from", None)
            updates.pop("date_to", None)
    explicit_range = parse_date_range(text, today)
    shift_days = parse_shift_days(text)
    if explicit_range:
        same_days = all(
            isinstance(updates.get(key), date) and (updates[key].month, updates[key].day) == (value.month, value.day)
            for key, value in zip(("date_from", "date_to"), explicit_range)
        )
        if not same_days:  # модель потеряла или исказила даты; год, если день совпал, оставляем её
            updates["date_from"], updates["date_to"] = explicit_range
    elif shift_days and request.date_from and request.date_to:
        updates["date_from"] = request.date_from + timedelta(days=shift_days)
        updates["date_to"] = request.date_to + timedelta(days=shift_days)
    elif _WEEKEND.search(text) and not _EXPLICIT_DATE.search(text) and intent != "details":
        updates["date_from"], updates["date_to"] = _nearest_weekend(today)

    # --- Состав: только если о нём говорили; возраст детей и питомца берём из текста.
    if "guests" in updates and not _GUESTS_MENTION.search(text):
        updates.pop("guests")
    guests = dict(updates.get("guests") or current_guests)
    adults = parse_adults(text)
    if adults and adults != guests.get("adults"):
        guests["adults"] = adults
    if mentions_pet(text) and not guests.get("pets"):
        guests["pets"] = 1
    children_mentioned = _CHILDREN_MENTION.search(text) and "без дет" not in text
    text_ages = parse_children_ages(text) if children_mentioned else None
    if text_ages:
        guests["children_ages"] = text_ages
    if guests != (updates.get("guests") or current_guests):
        updates["guests"] = guests
    if children_mentioned and not text_ages and not request.guests.children_ages:
        ages = (updates.get("guests") or {}).get("children_ages") or []
        numbers = set(re.findall(r"\d+", text))
        if not ages or not all(str(age) in numbers for age in ages):
            updates.pop("guests", None)  # состав гостей без возраста детей неполный: уточняем
            clarification = clarification or "Сколько лет детям? От возраста зависят размещение и цена."

    # --- Бюджет: только названная сумма; если модель её потеряла — из текста.
    if "budget_total" in updates and not _MONEY_MENTION.search(text):
        updates.pop("budget_total")
    if "budget_total" not in updates and intent != "details":
        money = parse_money(text)
        if money:
            people = (updates.get("guests") or current_guests).get("adults") or 1
            updates["budget_total"] = money * people if per_person(text) else money

    # --- Обязательные условия, сказанные прямо («нужен бассейн», «не меньше 4 звёзд»).
    hard = list(updates.get("hard") or request.hard)
    missing = [item for item in required_amenities(text) if not any(item.split()[0] in known.casefold() for known in hard)]
    if missing:
        updates["hard"] = [*hard, *missing]

    # --- Города.
    named, origins = _mentioned_cities(text)
    if intent == "suggest_destinations" and len(named) == 1 and not _OTHER_PLACES.search(text):
        intent = "plan_trip"  # город назван прямо — идеи не нужны
    if intent in ("plan_trip", "update"):
        if "destination" not in updates and len(named) == 1 and named[0] != request.destination:
            updates["destination"] = named[0]
        if "origin" not in updates and len(origins) == 1 and origins[0] != request.origin:
            updates["origin"] = origins[0]

    # «Давай первый вариант» после списка направлений: номер → название города.
    cards = trip.destinations.options[:MAX_DESTINATION_CARDS] if trip is not None and trip.destinations else []  # как показаны в интерфейсе
    choosing = intent in ("plan_trip", "update", "suggest_destinations") or (intent == "details" and _CHOICE.search(text))
    if cards and not request.destination and not named and "destination" not in updates and choosing:
        for pattern, index in _ORDINALS:
            if pattern.search(text):
                if -len(cards) <= index < len(cards):
                    updates["destination"] = cards[index].destination.name
                    intent = "plan_trip"
                break

    # Условия поменялись, а модель назвала это вопросом или подробностями — это изменение поездки.
    if intent in ("question", "details") and any(key in updates for key in ("date_from", "budget_total", "hard", "guests")):
        if trip is not None and (request.destination or trip.plan):
            intent = "update"

    has_destination = bool(updates.get("destination") or (request.destination and intent != "suggest_destinations"))
    about_children = bool(clarification and re.search(r"дет|ребён|ребен|возраст", clarification.casefold()))
    if clarification and not has_destination and intent in ("suggest_destinations", "question") and not about_children:
        clarification = None  # без города лучше сразу показать идеи, чем спрашивать
    if clarification and intent == "update" and not about_children:
        clarification = None  # пользователь меняет условия — применяем, а не переспрашиваем

    if updates == original and intent == extraction.intent and clarification == extraction.clarification_question:
        return extraction
    return extraction.model_copy(update={
        "patch": TripRequestPatch.model_validate(updates),
        "intent": intent,
        "clarification_question": clarification,
    })


def _clean_value(value: Any) -> Any:
    return None if value is None or is_empty_marker(value) else value


def _drop_empty_values(arguments: dict[str, Any]) -> dict[str, Any]:
    """Привести «сырые» значения модели к контракту, а заведомо пустые — убрать.

    GigaChat вместо пропуска поля присылает `budget_total: 0`, `""`, строку `"null"`, а даты и
    суммы иногда словами («15 декабря», «60 тысяч»). Контракт такие значения справедливо
    отклоняет, и из-за одного поля пропадал весь ответ. Здесь понятное переводим в формат,
    пустое убираем; всё остальное по-прежнему проверяет Pydantic.
    """
    today = date.today()
    cleaned = {key: value for key, value in arguments.items() if _clean_value(value) is not None}
    patch = cleaned.get("patch")
    if isinstance(patch, dict):
        patch = {key: value for key, value in patch.items() if _clean_value(value) is not None}
        budget = patch.get("budget_total")
        if isinstance(budget, str):
            budget = coerce_money(budget)
        if isinstance(budget, float) and budget.is_integer():
            budget = int(budget)
        if isinstance(budget, (int, float)) and budget > 0:
            patch["budget_total"] = budget
        else:
            patch.pop("budget_total", None)
        for key in ("date_from", "date_to"):
            if isinstance(patch.get(key), str):
                parsed = parse_single_date(patch[key], today)
                if parsed:
                    patch[key] = parsed.isoformat()
                else:
                    patch.pop(key)
        if isinstance(patch.get("flexible_days"), (int, float)) and not 0 <= patch["flexible_days"] <= 14:
            patch.pop("flexible_days")
        guests = patch.get("guests")
        if isinstance(guests, dict):
            guests = {key: value for key, value in guests.items() if _clean_value(value) is not None}
            for key in ("adults", "pets"):
                if isinstance(guests.get(key), str) and guests[key].strip().isdigit():
                    guests[key] = int(guests[key])
                if isinstance(guests.get(key), bool):
                    guests[key] = int(guests[key])
            if isinstance(guests.get("adults"), (int, float)) and guests["adults"] < 1:
                guests.pop("adults")
            if isinstance(guests.get("pets"), (int, float)) and guests["pets"] < 0:
                guests.pop("pets")
            ages = guests.get("children_ages")
            if isinstance(ages, (int, str)):
                ages = [ages]
            if isinstance(ages, list):
                ages = [int(age) if isinstance(age, str) and age.strip().isdigit() else age for age in ages]
                guests["children_ages"] = [age for age in ages if isinstance(age, int) and not isinstance(age, bool) and 0 <= age <= 17]
            patch["guests"] = guests
        elif guests is not None:
            patch.pop("guests")
        for key in ("hard", "soft"):
            if isinstance(patch.get(key), str):
                patch[key] = [patch[key]]
            if isinstance(patch.get(key), list):
                patch[key] = [item for item in patch[key] if isinstance(item, str) and item.strip() and not is_empty_marker(item)]
                if not patch[key]:
                    patch.pop(key)  # пустой список — «ничего не сказано», а не «удалить пожелания»
        cleaned["patch"] = patch
    elif "patch" in cleaned:
        cleaned.pop("patch")
    return cleaned


def _raw_arguments(raw: Any) -> dict[str, Any] | None:
    """Аргументы структурированного ответа из сырого сообщения модели (tool call или JSON в тексте)."""
    for call in getattr(raw, "tool_calls", None) or []:
        if isinstance(call.get("args"), dict):
            return call["args"]
    content = getattr(raw, "content", None)
    if isinstance(content, str) and content.strip().startswith("{"):
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _validated_extraction(result: Any) -> RequestExtraction:
    """Ответ модели → RequestExtraction. При ошибке валидации пробуем ещё раз без пустых значений."""
    if isinstance(result, dict) and "raw" in result and "parsed" in result:
        parsed = result.get("parsed")
        if parsed is not None and result.get("parsing_error") is None:
            # Даже валидный ответ чистим: GigaChat шлёт null и пустые списки для всех полей,
            # о которых не было речи, а явный null в патче стёр бы уже известное условие.
            if isinstance(parsed, BaseModel):
                parsed = parsed.model_dump(exclude_unset=True)
            return RequestExtraction.model_validate(_drop_empty_values(parsed))
        arguments = _raw_arguments(result.get("raw"))
        if arguments is None:
            error = result.get("parsing_error")
            if isinstance(error, ValidationError):
                raise error
            raise ValueError("Модель не вернула структурированный ответ.") from error
        return RequestExtraction.model_validate(_drop_empty_values(arguments))
    if isinstance(result, dict):
        return RequestExtraction.model_validate(_drop_empty_values(result))
    return RequestExtraction.model_validate(result)


def _request_context(trip: TripState) -> str:
    """Текущие условия поездки для модели: без них «на два дня позже» или «бюджет меньше» не разобрать."""
    request = trip.request.model_dump(mode="json", exclude={"weights", "flexible_days"})
    known = {key: value for key, value in request.items() if value not in (None, [], "")}
    if not known:
        return "Текущие условия поездки: пока не заданы.\n\n"
    return (
        "Текущие условия поездки (в патч включай только то, что пользователь меняет):\n"
        + json.dumps(known, ensure_ascii=False)
        + "\n\n"
    )


def _parse_with_model(message: str, trip: TripState | None = None) -> RequestExtraction:
    settings = get_settings()
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        model = _build_structured_model(settings)
        prompt = prompt_path().read_text(encoding="utf-8")
        # Модель не знает текущую дату и без неё придумывает прошедшие (2023–2025).
        today_line = f"Сегодня {date.today().isoformat()}. Даты поездки не могут быть раньше этой даты.\n\n"
        context_message = today_line + (
            _request_context(trip)
            + "Текущие варианты из состояния поездки (для detail_target используй точный id):\n"
            + _parser_context(trip)
            if trip is not None
            else "Текущих найденных вариантов нет."
        )
        for attempt in range(2):
            retry_instruction = (
                "\n\nПредыдущий ответ не прошёл проверку формата. Верни те же условия, "
                "но в правильном формате: даты YYYY-MM-DD, бюджет целым числом рублей, "
                "возраст детей целыми числами. Поле, значение которого не можешь записать "
                "в нужном формате, не включай. Не добавляй уточняющий вопрос из-за формата."
                if attempt
                else ""
            )
            try:
                extracted = model.invoke([
                    SystemMessage(content=prompt + retry_instruction),
                    HumanMessage(
                        content=(
                            f"{context_message}\n\n"
                            f"Новое сообщение пользователя:\n{message}"
                        )
                    ),
                ])
                return _ground_extraction(_validated_extraction(extracted), message, trip)
            except ValidationError as exc:
                if attempt == 0:
                    continue
                provider_name = "GigaChat" if settings.llm_provider == "gigachat" else "OpenAI-compatible"
                summary = _validation_error_summary(exc)
                raise CoreError(
                    f"{provider_name} дважды вернул некорректные данные поездки"
                    + (f": {summary}" if summary else ".")
                ) from exc
    except CoreError:
        raise
    except Exception as exc:
        provider_name = "GigaChat" if settings.llm_provider == "gigachat" else "OpenAI-compatible"
        detail = _safe_model_error(exc, settings)
        raise CoreError(
            f"Ошибка запроса к {provider_name}: {detail}"
        ) from exc


def _with_trace(trip: TripState, stage: str, outcome: str, reason: str, tool: str | None = None) -> list[TraceEntry]:
    return [*trip.trace, TraceEntry(stage=stage, outcome=outcome, reason=reason, tool=tool)]


def _update_trip(trip: TripState, **updates: object) -> TripState:
    return TripState.model_validate({**trip.model_dump(), **updates})


def _checked_call(trip: TripState, tool_name: str, function: Callable[..., object], *args: object, **kwargs: object):
    limit = get_settings().max_search_calls_per_tool
    calls = trip.tool_calls.get(tool_name, 0)
    if calls >= limit:
        raise CoreError(f"Превышен лимит вызовов инструмента {tool_name}.")
    result = function(*args, **kwargs)
    trip.tool_calls[tool_name] = calls + 1
    return result


def _data_request(request: TripRequest, destination_id: str | None):
    return request.to_data_request(destination_id)


def _destination_for(request: TripRequest):
    return get_destination(request.destination) if request.destination else None


def _tool_evidence(*groups: list[Evidence]) -> list[Evidence]:
    return [item for group in groups for item in group]


def _data_assumptions(*results: object) -> list[str]:
    assumptions: list[str] = []
    for result in results:
        assumptions.extend(getattr(result, "assumptions", []))
    return assumptions


def _search_request(trip: TripState) -> TripState:
    destination = _destination_for(trip.request)
    if not trip.request.destination:
        data_request = _data_request(trip.request, None).model_copy(
            update={"exclude_destination_ids": list(trip.shown_destination_ids)}
        )
        result = _checked_call(trip, "suggest_destinations", suggest_destinations, data_request)
        assumptions = list(dict.fromkeys([*trip.assumptions, *_data_assumptions(result)]))
        shown_now = [item.destination.id for item in result.options[:MAX_DESTINATION_CARDS]]
        already = [] if any(item in trip.shown_destination_ids for item in shown_now) else list(trip.shown_destination_ids)
        return _update_trip(
            trip,
            destinations=result,
            shown_destination_ids=list(dict.fromkeys([*already, *shown_now])),
            assumptions=assumptions,
            stage="search_destinations",
            trace=_with_trace(trip, "search", "success", "Направление не выбрано.", "suggest_destinations"),
        )
    if destination is None:
        return _update_trip(
            trip,
            stage="clarification",
            clarification_question="Не удалось определить это направление в доступных данных. Какой город вы имеете в виду?",
            trace=_with_trace(trip, "search", "clarification", "Направление не сопоставлено с данными."),
        )
    if not trip.request.origin and destination.name == "Москва":
        # По умолчанию Data считает город вылета Москвой: без вопроса получится поездка
        # «из Москвы в Москву» без транспорта.
        return _update_trip(
            trip,
            stage="clarification",
            clarification_question="Из какого города вы поедете в Москву? От этого зависят дорога и итоговая цена.",
            trace=_with_trace(trip, "search", "clarification", "Город вылета не указан, а направление совпадает с городом по умолчанию."),
        )

    data_request = _data_request(trip.request, destination.id)
    hotels = _checked_call(trip, "search_hotels", search_hotels, data_request, destination.id)
    transport = _checked_call(trip, "search_transport", search_transport, data_request, destination.id)
    # Допущение «город вылета не указан → Москва» добавляет сам Data (search_transport);
    # Core его не дублирует.
    assumptions = list(dict.fromkeys([
        *trip.assumptions,
        *_data_assumptions(hotels, transport),
    ]))
    return _update_trip(
        trip,
        hotels=hotels,
        transport=transport,
        assumptions=list(dict.fromkeys(assumptions)),
        stage="search_options",
        trace=[
            *_with_trace(trip, "search", "success", "Подобраны варианты отелей.", "search_hotels"),
            TraceEntry(stage="search", tool="search_transport", outcome="success", reason="Выполнен поиск транспорта."),
        ],
    )


def _hhmm(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def _local(moment: datetime, timezone_name: str) -> datetime:
    return moment.astimezone(ZoneInfo(timezone_name))


def _reception_open(hotel: Any, local_moment: datetime) -> bool:
    start, end = _hhmm(hotel.reception_open_from), _hhmm(hotel.reception_open_until)
    moment = local_moment.time().replace(tzinfo=None)
    return start <= moment <= end if start <= end else moment >= start or moment <= end


def _arrival_rank(options: list[Any], timezone_name: str, hotel: Any, date_from: date) -> list[Any]:
    """Состыковка важнее цены.

    Сначала варианты, которые прибывают в день заселения; если таких нет — ближайшие к нему
    по дню прибытия. Затем — прибытие в часы работы ресепшена. Внутри группы сохраняется
    порядок ранжирования Data (цена, время в пути и т. д.).
    """
    now = datetime.now(timezone.utc)

    def key(item: tuple[int, Any]) -> tuple[bool, int, bool, int]:
        index, option = item
        arrival = _local(option.arrival_at, timezone_name)
        departed = option.departure_at <= now  # рейс сегодня утром, а сейчас вечер
        return (departed, abs((arrival.date() - date_from).days), not _reception_open(hotel, arrival), index)

    return [option for _, option in sorted(enumerate(options), key=key)]


def _can_call(trip: TripState, tool_name: str) -> bool:
    return trip.tool_calls.get(tool_name, 0) < get_settings().max_search_calls_per_tool


def _terminal(option: Any) -> str:
    return "airport" if option.transport_type == "flight" else "station"


def _build_plan(trip: TripState) -> TripState:
    if not trip.hotels or not trip.transport:
        return _update_trip(trip, stage="build_plan")
    hotel_result = trip.hotels
    if not hotel_result.options:
        issues = [
            TripIssue(code=code, message=reason)
            for alternative in hotel_result.alternatives
            for code, reason in zip(alternative.violated_requirements, alternative.cons, strict=False)
        ]
        return _update_trip(trip, issues=issues, stage="no_matching_hotel")
    destination = get_destination(hotel_result.destination_id)
    if destination is None:
        raise CoreError("Data вернул направление, которого нет в каталоге.")
    hotel = hotel_result.options[0]
    transport_result = trip.transport
    # Не берём просто самый дешёвый вариант: ночной поезд, который приезжает на следующий день,
    # «съедает» первую оплаченную ночь. Предпочитаем прибытие в день заселения.
    ranked_outbound = _arrival_rank(transport_result.outbound, destination.timezone, hotel.hotel, hotel_result.date_from)
    outbound = ranked_outbound[0] if ranked_outbound else None
    inbound = transport_result.return_options[0] if transport_result.return_options else None
    arrival_transfer = None
    departure_transfer = None
    arrival_result = None
    departure_result = None
    if outbound is not None:
        arrival_result = _checked_call(
            trip, "search_transfers", search_transfers,
            destination.id,
            hotel.hotel.id,
            outbound.arrival_at,
            "airport" if outbound.transport_type == "flight" else "station",
            "to_hotel",
        )
        if arrival_result.options:
            arrival_transfer = arrival_result.options[0]
    if inbound is not None:
        departure_result = _checked_call(
            trip, "search_transfers", search_transfers,
            destination.id,
            hotel.hotel.id,
            inbound.departure_at,
            "airport" if inbound.transport_type == "flight" else "station",
            "to_terminal",
        )
        if departure_result.options:
            departure_transfer = departure_result.options[0]
    plan = TravelPlan(
        destination_id=destination.id,
        hotel=hotel,
        outbound=outbound,
        inbound=inbound,
        arrival_transfer=arrival_transfer,
        departure_transfer=departure_transfer,
        date_from=hotel_result.date_from,
        date_to=hotel_result.date_to,
    )
    transfer_assumptions = _data_assumptions(arrival_result, departure_result)
    return _update_trip(
        trip,
        plan=plan,
        previous_plan=trip.plan or trip.previous_plan,
        transfers=arrival_result,
        departure_transfers=departure_result,
        assumptions=list(dict.fromkeys([*trip.assumptions, *transfer_assumptions])),
        stage="plan_built",
        trace=_with_trace(trip, "build_plan", "success", "Собран предварительный вариант поездки."),
    )


def _check_plan(trip: TripState) -> TripState:
    plan = trip.plan
    if plan is None:
        return _update_trip(trip, stage="plan_checked")
    # Проверки состыковок живут в модуле trip_checks (роль «Проверки и качество»).
    weather = None
    if trip.destinations:
        weather = next(
            (item.weather for item in trip.destinations.options if item.destination.id == plan.destination_id),
            None,
        )
    issues: list[TripIssue] = trip_checks.check_plan(
        plan,
        trip.request,
        transfers=trip.transfers,
        departure_transfers=trip.departure_transfers,
        weather=weather,
    )

    if not any(issue.severity == "error" for issue in issues):
        selection = CartSelection(
            destination_id=plan.destination_id,
            hotel_id=plan.hotel.hotel.id,
            date_from=plan.date_from,
            date_to=plan.date_to,
            transport_ids=[item.id for item in (plan.outbound, plan.inbound) if item],
            transfer_ids=[
                item.id for item in (plan.arrival_transfer, plan.departure_transfer) if item
            ],
            transport_options=[item for item in (plan.outbound, plan.inbound) if item],
            transfer_options=[
                item for item in (plan.arrival_transfer, plan.departure_transfer) if item
            ],
            guests=trip.request.guests,
            budget_total=trip.request.budget_total,
            origin=trip.request.origin,
        )
        cart_result = price_cart(selection)
        if not isinstance(cart_result, Cart):
            raise CoreError("Не удалось рассчитать корзину выбранного варианта.")
        issues.extend(trip_checks.check_cart(cart_result, trip.request.budget_total))
        cart = cart_result
    else:
        cart = None

    changed_plan = plan.model_dump(mode="json")
    old_plan = trip.previous_plan
    previous_total = trip.previous_cart.total if trip.previous_cart else None
    diff = None
    version = trip.version
    if cart is not None and trip.versioned_this_turn:
        diff = trip.plan_diff
    elif cart is not None:
        version += 1
        old_components = old_plan.model_dump(mode="json") if old_plan else {}
        diff = {
            "version": version,
            "kept": [
                key for key in ("destination_id", "hotel", "outbound", "inbound")
                if key in old_components and old_components.get(key) == changed_plan.get(key)
            ],
            "changed": [
                key for key in ("destination_id", "hotel", "outbound", "inbound")
                if key in old_components and old_components.get(key) != changed_plan.get(key)
            ],
            "previous_total": previous_total,
            "total": cart.total,
        } if old_plan else None
    return _update_trip(
        trip,
        cart=cart,
        issues=issues,
        version=version,
        versioned_this_turn=trip.versioned_this_turn or cart is not None,
        plan_diff=diff,
        stage="plan_checked",
        trace=_with_trace(trip, "check_plan", "success", "План проверен кодом, стоимость рассчитана Travel Data."),
    )


def _repair_plan(trip: TripState) -> TripState:
    """Одна ограниченная попытка исправить конфликты плана.

    Сначала чиним то, что делает план неработоспособным (прибытие, трансферы), затем заселение
    в часы ресепшена, в конце бюджет. Замены берутся из уже найденных вариантов Data; новые
    поиски — только трансферы и только в пределах лимита вызовов инструмента.
    """
    plan = trip.plan
    if plan is None:
        return _update_trip(trip, stage="repair_skipped")
    destination = get_destination(plan.destination_id)
    if destination is None:
        return _update_trip(trip, stage="repair_skipped")
    codes = {issue.code for issue in trip.issues}
    timezone_name = destination.timezone
    repaired = plan
    transfer_result = None
    departure_result = None
    notes: list[str] = []

    # 1. Транспорт туда: прибытие не в день заселения, нет трансфера или закрыт ресепшен.
    if codes & {"DATE_MISMATCH", "LATE_ARRIVAL", "CHECKIN_GAP"} and plan.outbound and trip.transport:
        only_reception = not codes & {"DATE_MISMATCH", "LATE_ARRIVAL"}
        candidates = [
            option for option in _arrival_rank(trip.transport.outbound, timezone_name, plan.hotel.hotel, plan.date_from)
            if option.id != plan.outbound.id
            and option.departure_at > datetime.now(timezone.utc)
            and _local(option.arrival_at, timezone_name).date() == plan.date_from
            and (not only_reception or _reception_open(plan.hotel.hotel, _local(option.arrival_at, timezone_name)))
        ]
        for candidate in candidates:
            if not _can_call(trip, "search_transfers"):
                break
            transfer_result = _checked_call(
                trip, "search_transfers", search_transfers,
                plan.destination_id, plan.hotel.hotel.id, candidate.arrival_at,
                _terminal(candidate), "to_hotel",
            )
            if transfer_result.options:
                repaired = repaired.model_copy(update={
                    "outbound": candidate,
                    "arrival_transfer": transfer_result.options[0],
                })
                arrival = _local(candidate.arrival_at, timezone_name)
                notes.append(f"заменила транспорт туда: прибытие {arrival:%d.%m в %H:%M}, в день заселения.")
                break
        arrival_day = _local(repaired.outbound.arrival_at, timezone_name).date() if repaired.outbound else None
        if repaired is plan and arrival_day and plan.date_from < arrival_day < plan.date_to:
            # Ни один вариант не приезжает в первый день: сдвигаем заселение на день прибытия,
            # чтобы не платить за ночь, которой не будет. Итог пересчитает price_cart.
            repaired = repaired.model_copy(update={"date_from": arrival_day})
            notes.append(f"перенесла заселение на {arrival_day:%d.%m}: подходящего транспорта с прибытием в первый день нет.")

    # 2. Трансфер к обратному транспорту.
    if "TRANSFER_OUTSIDE_HOURS" in codes and repaired.inbound and trip.transport:
        for candidate in trip.transport.return_options:
            if candidate.id == repaired.inbound.id:
                continue
            if not _can_call(trip, "search_transfers"):
                break
            departure_result = _checked_call(
                trip, "search_transfers", search_transfers,
                plan.destination_id, repaired.hotel.hotel.id, candidate.departure_at,
                _terminal(candidate), "to_terminal",
            )
            if departure_result.options:
                repaired = repaired.model_copy(update={
                    "inbound": candidate,
                    "departure_transfer": departure_result.options[0],
                })
                notes.append("заменила обратный транспорт: к нему есть трансфер от отеля.")
                break

    # 3. Бюджет: более дешёвый отель из уже найденных.
    if "OVER_BUDGET" in codes and trip.hotels and trip.cart:
        cheaper = [
            option for option in trip.hotels.options
            if option.hotel.id != repaired.hotel.hotel.id
            and option.estimated_lodging_total < repaired.hotel.estimated_lodging_total
        ]
        if cheaper and (repaired.outbound is None or _can_call(trip, "search_transfers")):
            replacement = min(cheaper, key=lambda option: option.estimated_lodging_total)
            arrival_transfer = None
            if repaired.outbound:
                transfer_result = _checked_call(
                    trip, "search_transfers", search_transfers,
                    plan.destination_id, replacement.hotel.id, repaired.outbound.arrival_at,
                    _terminal(repaired.outbound), "to_hotel",
                )
                arrival_transfer = transfer_result.options[0] if transfer_result.options else None
            repaired = repaired.model_copy(update={
                "hotel": replacement,
                "arrival_transfer": arrival_transfer,
                "departure_transfer": None,
            })
            notes.append(f"выбрала более дешёвый отель «{replacement.hotel.name}», чтобы приблизиться к бюджету.")

    if repaired is plan:
        return _update_trip(
            trip,
            stage="repair_failed",
            trace=_with_trace(trip, "repair_plan", "no_alternative", "Подходящая альтернатива не найдена в пределах лимита."),
        )
    return _update_trip(
        trip,
        plan=repaired,
        repair_attempts=trip.repair_attempts + 1,
        cart=None,
        issues=[],
        transfers=transfer_result or trip.transfers,
        departure_transfers=departure_result or trip.departure_transfers,
        stage="repair_attempted",
        trace=_with_trace(trip, "repair_plan", "attempted", " ".join(notes)),
    )


# Служебные пометки Data в скобках латиницей: «(synthetic monthly climate estimate)»,
# «(MET Norway Locationforecast API)». В скобках без кириллицы — это источник, а не текст для людей.
_INTERNAL_NOTE = re.compile(r"\s*\((?=[^()]*[A-Za-z])[^()А-Яа-яЁё]*\)")


def _clean_customer_text(value: Any) -> Any:
    """Убрать служебные пометки Data вроде «(MET Norway Locationforecast API)» из текста для людей."""
    if isinstance(value, str):
        return _INTERNAL_NOTE.sub("", value)
    if isinstance(value, list):
        return [_clean_customer_text(item) for item in value]
    return value


def _customer_destination(item: Any) -> dict[str, Any]:
    data = item.model_dump(mode="json")
    for key in ("why_it_fits", "pros", "cons"):
        if key in data:
            data[key] = _clean_customer_text(data[key])
    return data


def _selected_first(options: list[Any], selected: Any | None, limit: int = 3) -> list[Any]:
    """Варианты для карточек: выбранный в плане — первым, даже если он дальше третьего в выдаче."""
    if selected is None:
        return options[:limit]
    return [selected, *[option for option in options if option.id != selected.id]][:limit]


def _make_blocks(trip: TripState, response_text: str) -> list[UIBlock]:
    blocks = [UIBlock(type="message", data={"text": response_text})]
    if trip.assumptions:
        blocks.append(UIBlock(type="assumptions", data={"items": trip.assumptions}))
    if trip.destinations and trip.destinations.options:
        blocks.append(UIBlock(
            type="destination_cards",
            data={
                "items": [_customer_destination(item) for item in trip.destinations.options[:MAX_DESTINATION_CARDS]],
                "budget_total": trip.request.budget_total,
                "nothing_in_budget": _nothing_in_budget(trip),
            },
        ))
    if trip.hotels:
        options = trip.hotels.options[:MAX_HOTEL_CARDS] or trip.hotels.alternatives[:MAX_HOTEL_CARDS]
        if options:
            blocks.append(UIBlock(
                type="option_cards",
                data={
                    "hotels": [item.model_dump(mode="json") for item in options],
                    "transport_outbound": [
                        item.model_dump(mode="json")
                        for item in _selected_first(trip.transport.outbound if trip.transport else [], trip.plan.outbound if trip.plan else None)
                    ],
                    "transport_return": [
                        item.model_dump(mode="json")
                        for item in _selected_first(trip.transport.return_options if trip.transport else [], trip.plan.inbound if trip.plan else None)
                    ],
                },
            ))
    if trip.plan:
        plan = trip.plan
        timeline: list[dict[str, object]] = []
        if plan.outbound:
            timeline.append({
                "event": "outbound",
                "departure_at": plan.outbound.departure_at.isoformat(),
                "arrival_at": plan.outbound.arrival_at.isoformat(),
                "evidence": [item.model_dump(mode="json") for item in plan.outbound.evidence],
            })
        if plan.arrival_transfer:
            timeline.append({
                "event": "arrival_transfer",
                "pickup_at": plan.arrival_transfer.pickup_at.isoformat(),
                "duration_minutes": plan.arrival_transfer.duration_minutes,
                "evidence": [item.model_dump(mode="json") for item in plan.arrival_transfer.evidence],
            })
        timeline.append({
            "event": "hotel_stay",
            "date_from": plan.date_from.isoformat(),
            "date_to": plan.date_to.isoformat(),
            "checkin_from": plan.hotel.hotel.checkin_from,
            "checkout_time": plan.hotel.hotel.checkout_time,
            "evidence": [item.model_dump(mode="json") for item in plan.hotel.evidence],
        })
        if plan.departure_transfer:
            timeline.append({
                "event": "departure_transfer",
                "pickup_at": plan.departure_transfer.pickup_at.isoformat(),
                "duration_minutes": plan.departure_transfer.duration_minutes,
                "evidence": [item.model_dump(mode="json") for item in plan.departure_transfer.evidence],
            })
        if plan.inbound:
            timeline.append({
                "event": "return",
                "departure_at": plan.inbound.departure_at.isoformat(),
                "arrival_at": plan.inbound.arrival_at.isoformat(),
                "evidence": [item.model_dump(mode="json") for item in plan.inbound.evidence],
            })
        blocks.append(UIBlock(type="trip_timeline", data={"events": timeline}))
    if trip.cart:
        blocks.append(UIBlock(type="cart", data={"cart": trip.cart.model_dump(mode="json"), "issues": [item.model_dump(mode="json") for item in trip.issues]}))
    if trip.plan_diff:
        blocks.append(UIBlock(type="plan_diff", data=trip.plan_diff))
    if trip.hotels or trip.transport:
        blocks.append(UIBlock(
            type="comparison",
            data={
                "weights": trip.request.weights.model_dump(mode="json"),
                # «Почему не другой вариант»: чем плохи более дешёвые отели и рейсы (считает trip_checks).
                "why_not": trip_checks.why_not_payload(trip) if trip.plan else [],
                "hotels": [
                    {
                        "name": item.hotel.name,
                        "score": item.score,
                        "reason_codes": item.reason_codes,
                        "evidence": [evidence.model_dump(mode="json") for evidence in item.evidence],
                    }
                    for item in (trip.hotels.options[:MAX_HOTEL_CARDS] if trip.hotels else [])
                ],
                "transport": [
                    {
                        "id": item.id,
                        "score": item.score,
                        "reason_codes": item.reason_codes,
                        "evidence": [evidence.model_dump(mode="json") for evidence in item.evidence],
                    }
                    for item in (
                        ([*trip.transport.outbound[:2], *trip.transport.return_options[:2]])
                        if trip.transport else []
                    )
                ],
                "issues": [item.model_dump(mode="json") for item in trip.issues],
            },
        ))
    elif trip.issues:
        blocks.append(UIBlock(type="comparison", data={"issues": [item.model_dump(mode="json") for item in trip.issues]}))
    blocks.append(UIBlock(type="trace", data={"items": [item.model_dump(mode="json") for item in trip.trace]}))
    return blocks


def _detail_response(trip: TripState, target: str | None) -> str:
    if not target:
        return "Какой именно вариант разобрать подробнее? Назовите направление, отель или номер варианта."

    def format_datetime(value: datetime) -> str:
        return value.strftime("%d.%m.%Y %H:%M %z")

    def format_money(value: int) -> str:
        return f"{value:,}".replace(",", " ")

    def evidence_value(evidence: list[Evidence], field_path: str) -> str | None:
        value = next(
            (item.value for item in evidence if item.field_path == field_path),
            None,
        )
        return str(value) if value is not None else None

    def transport_details(title: str, option: Any | None) -> list[str]:
        if option is None:
            return [f"{title}: вариант не выбран."]
        transport_name = "авиаперелёт" if option.transport_type == "flight" else "поезд"
        details = [
            f"{title}: {transport_name} {option.origin_city} — {option.destination_city}.",
            f"Пункт отправления: {option.origin_point}; пункт прибытия: {option.destination_point}.",
            (
                f"Отправление: {format_datetime(option.departure_at)}; "
                f"прибытие: {format_datetime(option.arrival_at)}."
            ),
            (
                f"В пути: {option.duration_minutes // 60} ч {option.duration_minutes % 60} мин; "
                f"пересадок: {option.stops}; стоимость: {option.price:,} ₽."
            ).replace(",", " "),
            "Багаж включён." if option.baggage_included else "Багаж не включён.",
        ]
        if option.transport_type == "flight":
            details.append("Название авиакомпании в данных этого варианта не указано.")
        if option.reason_codes:
            details.append(f"Причины выбора: {', '.join(option.reason_codes)}.")
        if option.risk_codes:
            details.append(f"Риски: {', '.join(option.risk_codes)}.")
        if option.why_it_fits:
            rationale = customer_facing_text(option.why_it_fits)
            if rationale:
                details.append(f"Почему выбран: {rationale}")
        details.append(f"Оценка варианта при подборе: {option.score:g}.")
        if option.pros:
            details.append(f"Преимущества: {'; '.join(option.pros)}.")
        if option.cons:
            customer_cons = [
                text for value in option.cons
                if (text := customer_facing_text(value))
            ]
            if customer_cons:
                details.append(f"Что учесть: {'; '.join(customer_cons)}.")
        return details

    def transfer_details(title: str, option: Any | None) -> list[str]:
        if option is None:
            return [f"{title}: трансфер не выбран."]
        details = [
            (
                f"{title}: {option.transfer_type}, "
                f"{option.arrival_point}; расстояние {option.distance_km:g} км; "
                f"в пути {option.duration_minutes} мин; цена {option.price:,} ₽."
            ).replace(",", " "),
            f"Время подачи: {format_datetime(option.pickup_at)}.",
            (
                f"Связано с прибытием рейса/поезда: {format_datetime(option.arrival_at)}."
                if option.direction == "to_hotel" and option.arrival_at
                else f"Отправление рейса/поезда: {format_datetime(option.terminal_departure_at)}."
                if option.terminal_departure_at
                else ""
            ),
        ]
        opens = evidence_value(option.evidence, "operating_start")
        closes = evidence_value(option.evidence, "operating_end")
        if opens and closes:
            details.append(f"Часы работы по данным трансфера: {opens}–{closes}.")
        if not option.is_available and option.unavailable_reason:
            details.append(f"Недоступен: {option.unavailable_reason}")
        if option.reason_codes:
            details.append(f"Причины выбора: {', '.join(option.reason_codes)}.")
        if option.risk_codes:
            details.append(f"Риски: {', '.join(option.risk_codes)}.")
        if option.why_it_fits:
            rationale = customer_facing_text(option.why_it_fits)
            if rationale:
                details.append(f"Почему выбран: {rationale}")
        if option.pros:
            details.append(f"Преимущества: {'; '.join(option.pros)}.")
        if option.cons:
            cons = [
                text for value in option.cons
                if (text := customer_facing_text(value))
            ]
            if cons:
                details.append(f"Что учесть: {'; '.join(cons)}.")
        return [detail for detail in details if detail]

    def cart_details() -> list[str]:
        if trip.cart is None:
            return ["Корзина ещё не рассчитана."]
        details = [
            f"Расчёт корзины: {trip.cart.nights} ночей, {trip.cart.guests.adults} взрослых, "
            f"{len(trip.cart.guests.children_ages)} детей, питомцев: {trip.cart.guests.pets}."
        ]
        hotel = trip.plan.hotel.hotel if trip.plan else None
        for item in trip.cart.items:
            line = f"- {item.title}: {format_money(item.amount)} ₽"
            if item.item_type == "hotel" and hotel is not None:
                line += f" ({format_money(hotel.price_per_night)} ₽ × {trip.cart.nights} ночей)"
            elif item.item_type == "fee":
                if "гостей" in item.title.casefold():
                    extra_adults = max(0, trip.cart.guests.adults - 2)
                    children = len(trip.cart.guests.children_ages)
                    line += (
                        f" ({extra_adults} доп. взрослых × 650 ₽ + {children} детей × "
                        f"300 ₽ за ночь, {trip.cart.nights} ночей)"
                    )
                elif "питомц" in item.title.casefold():
                    line += f" ({trip.cart.guests.pets} питомцев × 500 ₽)"
            details.append(line)
        details.extend([
            f"Подытог позиций: {trip.cart.subtotal:,} ₽.".replace(",", " "),
            f"Итого по расчёту: {trip.cart.total:,} ₽.".replace(",", " "),
        ])
        if trip.cart.budget_total is not None:
            details.append(f"Бюджет: {trip.cart.budget_total:,} ₽.".replace(",", " "))
            details.append(
                f"Превышение бюджета: {trip.cart.over_budget_percent:g}%."
                if trip.cart.over_budget
                else "Бюджет не превышен."
            )
        if trip.cart.risk_codes:
            details.append(f"Риски корзины: {', '.join(trip.cart.risk_codes)}.")
        return details

    if target in {"current_plan", "plan", "itinerary", "trip"}:
        if trip.plan is None:
            return "План поездки ещё не собран. Сначала выберите направление и задайте даты."
        plan = trip.plan
        destination = get_destination(plan.destination_id)
        hotel = plan.hotel.hotel
        cancellation_policy = {
            "free_until_7_days": "бесплатная отмена не позднее чем за 7 дней",
            "free_until_3_days": "бесплатная отмена не позднее чем за 3 дня",
            "partial": "частичный возврат",
            "non_refundable": "без возврата",
        }[hotel.cancellation_policy]
        details = [
            f"План поездки: {destination.name if destination else trip.request.destination}, "
            f"{plan.date_from:%d.%m.%Y} — {plan.date_to:%d.%m.%Y}.",
            *(
                [
                    f"Направление: {destination.region}, {destination.country}; "
                    f"аэропорт: {destination.airport}; вокзал: {destination.station}; "
                    f"часовой пояс: {destination.timezone}; особенности: "
                    f"{', '.join(destination.tags) or 'не указаны'}."
                ]
                if destination
                else []
            ),
            f"Отель: {hotel.name}, {hotel.city}, {hotel.stars} звёзд, рейтинг {hotel.rating:g}/5.",
            f"Описание: {customer_facing_text(hotel.description) or 'не указано'}.",
            f"Цена отеля: {hotel.price_per_night:,} ₽ за ночь; оценка проживания: "
            f"{plan.hotel.estimated_lodging_total:,} ₽.".replace(",", " "),
            (
                f"Заселение: {hotel.checkin_from}–{hotel.checkin_until}; "
                f"выезд до {hotel.checkout_time}; ресепшен "
                f"{hotel.reception_open_from}–{hotel.reception_open_until}."
            ),
            f"Условия отмены: {cancellation_policy}.",
            f"Завтрак включён: {'да' if hotel.breakfast_included else 'нет'}; "
            f"бассейн: {'да' if hotel.has_pool else 'нет'}; спа: {'да' if hotel.has_spa else 'нет'}; "
            f"размещение с питомцами: {'да' if hotel.has_pets_allowed else 'нет'}.",
            f"Для семей: {'да' if hotel.family_friendly else 'нет'}; детский клуб: {'да' if hotel.kids_club else 'нет'}.",
            f"Шумность района: {hotel.noise_level}; удобства: {', '.join(hotel.amenities) or 'не указаны'}.",
            f"Расстояние до центра: {hotel.distance_to_center_km:g} км."
            + (
                f" До пляжа: {hotel.distance_to_beach_km:g} км."
                if hotel.distance_to_beach_km is not None
                else ""
            ),
            f"Координаты отеля: {hotel.latitude:g}, {hotel.longitude:g}.",
            f"Оценка отеля в подборе: {plan.hotel.score:g}/1; "
            f"причины: {', '.join(plan.hotel.reason_codes) or 'не указаны'}.",
            *(
                [f"Что учесть по отелю: {'; '.join(plan.hotel.cons)}."]
                if plan.hotel.cons
                else []
            ),
            *(
                [f"Преимущества отеля: {'; '.join(plan.hotel.pros)}."]
                if plan.hotel.pros
                else []
            ),
            *(
                [f"Риски отеля: {', '.join(plan.hotel.risk_codes)}."]
                if plan.hotel.risk_codes
                else []
            ),
            *(
                [f"Не выполнены требования: {'; '.join(plan.hotel.violated_requirements)}."]
                if plan.hotel.violated_requirements
                else []
            ),
            *(
                [f"Почему выбран отель: {customer_facing_text(plan.hotel.why_it_fits)}"]
                if customer_facing_text(plan.hotel.why_it_fits)
                else []
            ),
            *transport_details("Туда", plan.outbound),
            *transport_details("Обратно", plan.inbound),
            *transfer_details("Трансфер после прибытия", plan.arrival_transfer),
            *transfer_details("Трансфер к обратному рейсу/поезду", plan.departure_transfer),
            *cart_details(),
        ]
        issues = [issue.message for issue in trip.issues]
        if issues:
            details.append(f"Замечания к плану: {'; '.join(issues)}.")
        return "Подробности выбранного варианта:\n" + "\n".join(details)

    if target in {"cart", "price", "cost", "расчёт", "корзина"}:
        return "Подробный расчёт:\n" + "\n".join(cart_details())

    if trip.destinations:
        for item in trip.destinations.options:
            if target in {item.destination.id, item.destination.name}:
                facts = [f"{item.destination.name} — {item.destination.region}."]
                facts.append(
                    f"Ориентир стоимости поездки: {item.estimated_trip_price:,} ₽."
                    .replace(",", " ")
                )
                facts.append(
                    f"Ориентир проживания: {item.destination.typical_price_per_night:,} ₽ за ночь."
                    .replace(",", " ")
                )
                if item.fastest_travel_minutes is not None:
                    hours, minutes = divmod(item.fastest_travel_minutes, 60)
                    duration = f"{hours} ч"
                    if minutes:
                        duration += f" {minutes} мин"
                    facts.append(f"Самый быстрый найденный маршрут: {duration}.")
                weather_description = customer_facing_text(item.weather.description)
                weather = (
                    f"Ориентир погоды на {item.weather.target_date:%d.%m.%Y}: "
                    f"{item.weather.temperature_min_c:g}…{item.weather.temperature_max_c:g} °C"
                )
                if weather_description:
                    weather += f", {weather_description}"
                facts.append(weather + ".")
                why_it_fits = customer_facing_text(item.why_it_fits)
                if why_it_fits:
                    facts.append(f"Почему вариант может подойти: {why_it_fits}")
                cons = [text for value in item.cons[:3] if (text := customer_facing_text(value))]
                if cons:
                    facts.append(f"Что учесть: {'; '.join(cons)}.")
                return "Подробнее: " + " ".join(facts)

    if trip.hotels:
        for item in [*trip.hotels.options, *trip.hotels.alternatives]:
            if target in {item.hotel.id, item.hotel.name}:
                hotel = item.hotel
                cancellation = {
                    "free_until_7_days": "бесплатная отмена до 7 дней",
                    "free_until_3_days": "бесплатная отмена до 3 дней",
                    "partial": "частичный возврат",
                    "non_refundable": "без возврата",
                }[hotel.cancellation_policy]
                facts = [
                    f"{hotel.name}, {hotel.stars} звёзд, рейтинг {hotel.rating:g}.",
                    (
                        f"Цена по данным варианта — {hotel.price_per_night:,} ₽ за ночь; "
                        f"расчёт проживания за {trip.hotels.nights} ночей — "
                        f"{item.estimated_lodging_total:,} ₽."
                    ).replace(",", " "),
                    f"Условия отмены: {cancellation}.",
                    f"Заселение: {hotel.checkin_from}–{hotel.checkin_until}; выезд до {hotel.checkout_time}.",
                    f"До центра: {hotel.distance_to_center_km:g} км.",
                ]
                facts.extend([
                    f"Город: {hotel.city}.",
                    f"Координаты: {hotel.latitude:g}, {hotel.longitude:g}.",
                    f"Ресепшен: {hotel.reception_open_from}–{hotel.reception_open_until}.",
                    f"Завтрак: {'включён' if hotel.breakfast_included else 'не включён'}.",
                    f"Семейный отель: {'да' if hotel.family_friendly else 'нет'}; "
                    f"детский клуб: {'есть' if hotel.kids_club else 'нет'}.",
                    f"Бассейн: {'есть' if hotel.has_pool else 'нет'}; спа: "
                    f"{'есть' if hotel.has_spa else 'нет'}; питомцы: "
                    f"{'разрешены' if hotel.has_pets_allowed else 'не разрешены'}.",
                    f"Шумность района: {hotel.noise_level}.",
                    f"Удобства: {', '.join(hotel.amenities) or 'не указаны'}.",
                ])
                description = customer_facing_text(hotel.description)
                if description:
                    facts.insert(1, description)
                if hotel.distance_to_beach_km is not None:
                    facts.append(f"До пляжа: {hotel.distance_to_beach_km:g} км.")
                amenities = []
                if hotel.breakfast_included:
                    amenities.append("завтрак включён")
                if hotel.has_pool:
                    amenities.append("есть бассейн")
                if hotel.has_spa:
                    amenities.append("есть спа")
                if hotel.has_pets_allowed:
                    amenities.append("размещение с питомцами разрешено")
                if amenities:
                    facts.append(f"Удобства: {', '.join(amenities)}.")
                if item.pros:
                    facts.append(f"Преимущества: {'; '.join(item.pros)}.")
                if item.risk_codes:
                    facts.append(f"Риски: {', '.join(item.risk_codes)}.")
                why_it_fits = customer_facing_text(item.why_it_fits)
                if why_it_fits:
                    facts.append(f"Почему подходит: {why_it_fits}")
                cons = [text for value in item.cons[:3] if (text := customer_facing_text(value))]
                if cons:
                    facts.append(f"Что учесть: {'; '.join(cons)}.")
                if item.violated_requirements:
                    facts.append(f"Не выполнены обязательные условия: {'; '.join(item.violated_requirements)}.")
                return "Подробнее об отеле: " + " ".join(facts)

    if trip.transport:
        for direction, options in (
            ("туда", trip.transport.outbound),
            ("обратно", trip.transport.return_options),
        ):
            for item in options:
                if target == item.id:
                    return "Подробнее о транспорте:\n" + "\n".join(
                        transport_details(direction.capitalize(), item)
                    )

    return "Не удалось однозначно найти этот вариант среди ранее показанных. Назовите направление, отель или номер варианта."


def _repair_note(trip: TripState) -> str | None:
    """Что repair_plan исправил за текущий ход (ход начинается с merge_request)."""
    turn: list[TraceEntry] = []
    for entry in trip.trace:
        if entry.stage == "merge_request":
            turn = []
        turn.append(entry)
    notes = [entry.reason for entry in turn if entry.stage == "repair_plan" and entry.outcome == "attempted"]
    return notes[-1] if notes else None


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _nothing_in_budget(trip: TripState) -> bool:
    """Бюджет задан, а ни одно показанное направление в него не укладывается."""
    budget = trip.request.budget_total
    options = trip.destinations.options[:MAX_DESTINATION_CARDS] if trip.destinations else []
    return bool(budget and options and all(item.estimated_trip_price > budget for item in options))


def _destinations_text(trip: TripState) -> str:
    budget = trip.request.budget_total
    options = trip.destinations.options[:MAX_DESTINATION_CARDS] if trip.destinations else []
    if _nothing_in_budget(trip):
        cheapest = min(item.estimated_trip_price for item in options)
        return (
            f"Под ваш бюджет ничего не найдено: самая недорогая поездка — от {_money(cheapest)} ₽, "
            f"а бюджет {_money(budget)} ₽. Ниже ближайшие варианты: можно увеличить бюджет, "
            "сократить поездку или поменять даты."
        )
    if budget:
        fitting = [item.destination.name for item in options if item.estimated_trip_price <= budget]
        if len(fitting) < len(options):
            return f"В бюджет {_money(budget)} ₽ укладываются: {', '.join(fitting)}. Остальные направления дороже."
    return "Подобрала несколько направлений. Какой вариант рассказать подробнее?"


def _explain(
    trip: TripState,
    intent: str,
    clarification: str | None,
    detail_target: str | None = None,
) -> str:
    if clarification or trip.clarification_question:
        return clarification or trip.clarification_question or "Уточните, пожалуйста, параметры поездки."
    if intent == "details":
        return _detail_response(trip, detail_target)
    if trip.destinations and trip.destinations.options:
        return _destinations_text(trip)
    if trip.hotels and not trip.hotels.options:
        return "Не найден отель, который выполняет все обязательные условия. Ниже показаны ближайшие альтернативы и их ограничения."
    repair_note = _repair_note(trip)
    fixed = f" По ходу {repair_note}" if repair_note else ""
    blocking = [issue for issue in trip.issues if issue.severity == "error"]
    if trip.plan and blocking:
        return (
            "Собрала вариант поездки, но в нём есть несостыковки, которые не получилось исправить "
            f"автоматически: {' '.join(issue.message for issue in blocking)} Стоимость не рассчитываю, "
            f"пока план не исправлен: попробуй другие даты или другой транспорт.{fixed}"
        )
    if trip.cart and trip.cart.over_budget and trip.cart.budget_total:
        warnings = [issue for issue in trip.issues if issue.code != "OVER_BUDGET"]
        extra = (" Есть что учесть: " + " ".join(issue.message for issue in warnings)) if warnings else ""
        return (
            f"Под ваш бюджет ничего не найдено: ближайший вариант стоит {_money(trip.cart.total)} ₽ — "
            f"это на {trip.cart.over_budget_percent:g}% больше бюджета {_money(trip.cart.budget_total)} ₽. "
            f"Показываю ближайший вариант: можно увеличить бюджет, сократить поездку или выбрать другое направление.{fixed}{extra}"
        )
    if trip.cart:
        rejected = trip_checks.why_not(trip)
        status = f" Почему не дешевле: {rejected[0].sentence()}" if rejected else ""
        warnings = [issue for issue in trip.issues if issue.code != "OVER_BUDGET"]
        if warnings:
            status += " Есть что учесть: " + " ".join(issue.message for issue in warnings)
        return f"Собрала план поездки и рассчитала стоимость.{fixed}{status}"
    if intent == "question":
        return "Я помогу подобрать направление, транспорт и отель. Напишите, куда и когда хотите поехать."
    return "Уточните, пожалуйста, направление поездки или попросите предложить варианты."


def build_graph(parser: RequestParser | None = None):
    parse_message = parser or _parse_with_model

    def parse_node(state: GraphInput) -> dict[str, object]:
        if state.get("patch") is not None:
            extracted = RequestExtraction(patch=state["patch"], intent="update")
        elif parser is None:
            extracted = _parse_with_model(state.get("message", ""), state["trip"])
        else:
            extracted = parse_message(state.get("message", ""))
        return {
            "patch": extracted.patch,
            "intent": extracted.intent,
            "clarification_question": extracted.clarification_question,
            "detail_target": extracted.detail_target,
        }

    def merge_node(state: GraphInput) -> dict[str, object]:
        trip = state["trip"]
        patch = state.get("patch") or TripRequestPatch()
        updates = patch.model_dump(exclude_unset=True)
        date_assumption = None
        notes: list[str] = []
        # «Предложи направления», когда город уже выбран: без явного сброса граф снова
        # собрал бы поездку в прежний город. Остальные условия поездки сохраняются.
        if state.get("intent") == "suggest_destinations" and "destination" not in updates and trip.request.destination:
            updates["destination"] = None
        # Модель иногда придумывает прошедшие даты. Их не применяем: Data возьмёт ближайший период.
        past = [updates[key] for key in ("date_from", "date_to") if updates.get(key) is not None and updates[key] < date.today()]
        if past:
            updates.pop("date_from", None)
            updates.pop("date_to", None)
            notes.append(f"Даты {', '.join(f'{day:%d.%m.%Y}' for day in past)} уже прошли, поэтому не учтены.")
        candidate_from = updates.get("date_from", trip.request.date_from)
        candidate_to = updates.get("date_to", trip.request.date_to)
        if candidate_from and candidate_to and candidate_to <= candidate_from:
            duration = (
                trip.request.date_to - trip.request.date_from
                if trip.request.date_from and trip.request.date_to
                else None
            )
            if duration and "date_from" in updates and "date_to" not in updates:
                updates["date_to"] = candidate_from + duration
                date_assumption = f"После изменения даты начала сохранена прежняя длительность поездки: {duration.days} ночей."
            elif duration and "date_to" in updates and "date_from" not in updates:
                updates["date_from"] = candidate_to - duration
                date_assumption = f"После изменения даты возвращения сохранена прежняя длительность поездки: {duration.days} ночей."
            else:
                raise CoreError("Новые даты поездки должны образовывать корректный период.")
        merged_request = TripRequest.model_validate({**trip.request.model_dump(), **updates})
        # Модель может вернуть id направления («dest-kaliningrad»). Храним название города,
        # чтобы id не попадал в ответы и интерфейс.
        if merged_request.destination:
            resolved = get_destination(merged_request.destination)
            if resolved is not None and resolved.name != merged_request.destination:
                merged_request = merged_request.model_copy(update={"destination": resolved.name})
        # Едут с питомцем — отель обязан это разрешать, даже если модель не вынесла это в hard.
        if merged_request.guests.pets > 0 and not any(
            token in item.casefold() for item in merged_request.hard for token in ("питом", "собак", "кош", "животн")
        ):
            merged_request = merged_request.model_copy(update={"hard": [*merged_request.hard, "с питомцем"]})
        changed = merged_request != trip.request
        # «Покажи ещё / другие варианты» при тех же условиях — следующая порция направлений.
        more_destinations = (
            state.get("intent") == "suggest_destinations"
            and not changed
            and trip.destinations is not None
            and bool(_OTHER_PLACES.search(state.get("message", "") or ""))
        )
        assumptions = [] if changed else list(trip.assumptions)
        if date_assumption:
            assumptions.append(date_assumption)
        assumptions.extend(notes)
        trip = _update_trip(
            trip,
            request=merged_request,
            previous_plan=trip.plan,
            previous_cart=trip.cart,
            plan=None if changed else trip.plan,
            cart=None if changed else trip.cart,
            destinations=None if changed or "destination" in updates else trip.destinations,
            shown_destination_ids=list(trip.shown_destination_ids) if more_destinations else [],
            hotels=None if changed else trip.hotels,
            transport=None if changed else trip.transport,
            transfers=None if changed else trip.transfers,
            departure_transfers=None if changed else trip.departure_transfers,
            issues=[] if changed else trip.issues,
            assumptions=assumptions,
            plan_diff=None,
            clarification_question=state.get("clarification_question"),
            detail_target=state.get("detail_target"),
            response_text=None,
            response_mode="summary",
            tool_calls={},
            repair_attempts=0,
            versioned_this_turn=False,
            stage="request_merged",
            trace=_with_trace(trip, "merge_request", "success", "Патч применён без сброса остальных полей запроса."),
        )
        return {"trip": trip}

    def assumptions_node(state: GraphInput) -> dict[str, object]:
        trip = state["trip"]
        # Допущения о данных, которых нет в запросе (город вылета, даты), добавляет Data во время
        # поиска — по одному на каждое. Здесь только фиксируем то, что уже накоплено.
        assumptions = list(trip.assumptions)
        trip = _update_trip(
            trip,
            assumptions=list(dict.fromkeys(assumptions)),
            stage="assumptions_resolved",
            trace=_with_trace(trip, "resolve_assumptions", "success", "Явные допущения сохранены в состоянии поездки."),
        )
        return {"trip": trip}

    def choose_node(state: GraphInput) -> dict[str, object]:
        trip = state["trip"]
        if state.get("clarification_question"):
            action = "clarify"
        elif state.get("intent") == "details":
            action = "details"
        elif not trip.request.destination:
            action = "suggest_destinations"
        else:
            action = "search_trip"
        trip = _update_trip(
            trip,
            stage="action_selected",
            trace=_with_trace(trip, "choose_action", "success", f"Выбрано действие: {action}."),
        )
        return {"trip": trip, "action": action}

    def search_node(state: GraphInput) -> dict[str, object]:
        return {"trip": _search_request(state["trip"])}

    def build_node(state: GraphInput) -> dict[str, object]:
        return {"trip": _build_plan(state["trip"])}

    def check_node(state: GraphInput) -> dict[str, object]:
        return {"trip": _check_plan(state["trip"])}

    def repair_node(state: GraphInput) -> dict[str, object]:
        return {"trip": _repair_plan(state["trip"])}

    def explain_node(state: GraphInput) -> dict[str, object]:
        trip = state["trip"]
        intent = state.get("intent", "plan_trip")
        text = _explain(
            trip,
            intent,
            state.get("clarification_question"),
            state.get("detail_target"),
        )
        return {
            "trip": _update_trip(
                trip,
                response_text=text,
                response_mode="details" if intent == "details" else "summary",
                stage="explained",
            )
        }

    def response_node(state: GraphInput) -> dict[str, object]:
        trip = state["trip"]
        text = trip.response_text or _explain(
            trip,
            state.get("intent", "plan_trip"),
            state.get("clarification_question"),
            state.get("detail_target"),
        )
        # Защита ответа (trip_checks.guardrails): убрать внутренние id и числа без опоры на данные.
        # Разница в цене с отвергнутыми вариантами считается кодом — это тоже подтверждённые числа.
        savings = [item.saving for item in trip_checks.why_not(trip)] if trip.plan else []
        report = trip_checks.guard_response_text(text, trip, extra_known=savings)
        if report.changed:
            text = report.text or text
            trip = _update_trip(
                trip,
                response_text=text,
                trace=_with_trace(
                    trip,
                    "guardrails",
                    "repaired",
                    (
                        f"Убрано id/полей: {len(report.removed_ids)}, "
                        f"заменено кодов: {len(report.replaced_codes)}, "
                        f"предложений с UNSUPPORTED_NUMBER: {len(report.removed_sentences)}."
                    ),
                ),
            )
        return {"trip": _update_trip(trip, blocks=_make_blocks(trip, text), stage="completed")}

    def after_action(state: GraphInput) -> Literal["clarify", "details", "search"]:
        action = state.get("action")
        if action == "details":
            return "details"
        return "clarify" if action == "clarify" else "search"

    def after_check(state: GraphInput) -> Literal["repair", "explain"]:
        trip = state["trip"]
        repair_available = get_settings().max_repairs > 0
        codes = {issue.code for issue in trip.issues}
        should_repair = (
            repair_available
            and bool(codes & REPAIRABLE_CODES)
            and trip.stage == "plan_checked"
            and trip.repair_attempts < get_settings().max_repairs
        )
        return "repair" if should_repair else "explain"

    def after_repair(state: GraphInput) -> Literal["check", "explain"]:
        return "check" if state["trip"].stage == "repair_attempted" else "explain"

    workflow = StateGraph(GraphInput)
    workflow.add_node("parse_request", parse_node)
    workflow.add_node("merge_request", merge_node)
    workflow.add_node("resolve_assumptions", assumptions_node)
    workflow.add_node("choose_action", choose_node)
    workflow.add_node("search", search_node)
    workflow.add_node("build_plan", build_node)
    workflow.add_node("check_plan", check_node)
    workflow.add_node("repair_plan", repair_node)
    workflow.add_node("explain", explain_node)
    workflow.add_node("build_response", response_node)
    workflow.add_edge(START, "parse_request")
    workflow.add_edge("parse_request", "merge_request")
    workflow.add_edge("merge_request", "resolve_assumptions")
    workflow.add_edge("resolve_assumptions", "choose_action")
    workflow.add_conditional_edges("choose_action", after_action, {
        "clarify": "explain",
        "details": "explain",
        "search": "search",
    })
    workflow.add_conditional_edges("search", lambda state: "explain" if state["trip"].stage == "clarification" else "build", {"explain": "explain", "build": "build_plan"})
    workflow.add_edge("build_plan", "check_plan")
    workflow.add_conditional_edges("check_plan", after_check, {"repair": "repair_plan", "explain": "explain"})
    workflow.add_conditional_edges("repair_plan", after_repair, {"check": "check_plan", "explain": "explain"})
    workflow.add_edge("explain", "build_response")
    workflow.add_edge("build_response", END)
    return workflow.compile()


class TravelAgent:
    def __init__(self, parser: RequestParser | None = None):
        self.graph = build_graph(parser)

    def run(
        self,
        message: str = "",
        trip: TripState | None = None,
        patch: TripRequestPatch | None = None,
    ) -> TripState:
        initial: GraphInput = {
            "trip": trip or TripState(),
            "message": message,
            "patch": patch,
        }
        try:
            result = self.graph.invoke(initial)
            return TripState.model_validate(result["trip"])
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("Не удалось завершить обработку поездки.") from exc

    async def astream(
        self,
        message: str = "",
        trip: TripState | None = None,
        patch: TripRequestPatch | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        initial: GraphInput = {
            "trip": trip or TripState(),
            "message": message,
            "patch": patch,
        }
        try:
            async for update in self.graph.astream(initial, stream_mode="updates"):
                yield update
        except CoreError:
            raise
        except Exception as exc:
            raise CoreError("Не удалось завершить обработку поездки.") from exc
