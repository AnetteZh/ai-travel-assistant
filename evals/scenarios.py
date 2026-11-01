"""Сквозные сценарии: несколько ходов диалога, как у настоящего пользователя.

У каждого хода есть:
* text   — что пишет пользователь (для режима --live с настоящей моделью);
* patch / intent — что должна извлечь модель (для офлайн-режима без модели);
* must_not_call — инструменты, которые при пересборке вызывать не нужно
  (метрика «пересборка без поиска с нуля»).

Даты считаются от сегодняшнего дня, чтобы сценарии не устаревали.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any


def _d(days: int) -> date:
    return date.today() + timedelta(days=days)


def _ru(day: date) -> str:
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
    return f"{day.day} {months[day.month - 1]} {day.year}"


def build_scenarios() -> list[dict[str, Any]]:
    a, b = _d(30), _d(35)
    a2, b2 = _d(32), _d(37)
    w1, w2 = _d(40), _d(47)
    return [
        {
            "id": "s01_family_sea",
            "persona": "Семья с ребёнком 4 лет, хочет к морю, бюджет ограничен",
            "turns": [
                {"text": "Хочу к морю с ребёнком 4 лет, нас двое взрослых", "intent": "suggest_destinations",
                 "patch": {"guests": {"adults": 2, "children_ages": [4]}, "soft": ["у моря"]}},
                {"text": f"Давай Сочи, из Москвы, с {_ru(a)} по {_ru(b)}", "intent": "plan_trip",
                 "patch": {"destination": "Сочи", "origin": "Москва", "date_from": a, "date_to": b}},
                {"text": "А если бюджет 70 тысяч?", "intent": "update", "kind": "update",
                 "patch": {"budget_total": 70000}, "must_not_call": ["search_transport"]},
            ],
        },
        {
            "id": "s02_dog_and_shift_dates",
            "persona": "Пара с собакой, потом сдвигает даты на два дня",
            "turns": [
                {"text": f"Калининград из Москвы с {_ru(a)} по {_ru(b)}, с нами собака", "intent": "plan_trip",
                 "patch": {"destination": "Калининград", "origin": "Москва", "date_from": a, "date_to": b,
                           "guests": {"adults": 2, "pets": 1}, "hard": ["с собакой"]}},
                {"text": "А если на два дня позже?", "intent": "update", "kind": "update",
                 "patch": {"date_from": a2, "date_to": b2}},
            ],
        },
        {
            "id": "s03_far_east_long_trip",
            "persona": "Едет далеко, разница часовых поясов",
            "turns": [
                {"text": f"Владивосток из Москвы, {_ru(w1)} – {_ru(w2)}, один", "intent": "plan_trip",
                 "patch": {"destination": "Владивосток", "origin": "Москва", "date_from": w1, "date_to": w2,
                           "guests": {"adults": 1}}},
                {"text": "Сколько всё стоит и из чего складывается цена?", "intent": "details",
                 "detail_target": "cart", "kind": "details"},
            ],
        },
        {
            "id": "s04_tiny_budget",
            "persona": "Нереалистичный бюджет: агент должен честно сказать и не врать",
            "turns": [
                {"text": f"Казань из Москвы, {_ru(a)} – {_ru(b)}, четверо взрослых, бюджет 15 тысяч", "intent": "plan_trip",
                 "patch": {"destination": "Казань", "origin": "Москва", "date_from": a, "date_to": b,
                           "guests": {"adults": 4}, "budget_total": 15000}},
            ],
        },
        {
            "id": "s05_no_transfers_change_weights",
            "persona": "Меняет приоритеты: важнее цена",
            "turns": [
                {"text": f"Мурманск из Петербурга, {_ru(a)} – {_ru(b)}", "intent": "plan_trip",
                 "patch": {"destination": "Мурманск", "origin": "Санкт-Петербург", "date_from": a, "date_to": b}},
                {"text": "Главное — подешевле, остальное неважно", "intent": "update", "kind": "update",
                 "patch": {"weights": {"price": 0.8, "travel_time": 0.05, "cancellation": 0.05, "location": 0.05, "preferences": 0.05}},
                 "must_not_call": ["search_transport"]},
            ],
        },
        {
            "id": "s06_irkutsk_overnight",
            "persona": "Долгий маршрут: риск потерять ночь в отеле",
            "turns": [
                {"text": f"Иркутск из Москвы, {_ru(a)} – {_ru(b)}, вдвоём", "intent": "plan_trip",
                 "patch": {"destination": "Иркутск", "origin": "Москва", "date_from": a, "date_to": b}},
            ],
        },
        {
            "id": "s07_kids_and_pets_minvody",
            "persona": "Большая семья: трое детей и кот",
            "turns": [
                {"text": f"Минводы из Екатеринбурга, {_ru(w1)} – {_ru(w2)}, двое взрослых, дети 3, 7 и 12, и кот",
                 "intent": "plan_trip",
                 "patch": {"destination": "Минеральные Воды", "origin": "Екатеринбург", "date_from": w1, "date_to": w2,
                           "guests": {"adults": 2, "children_ages": [3, 7, 12], "pets": 1}}},
            ],
        },
        {
            "id": "s08_change_destination",
            "persona": "Передумал: «а давай вообще в горы»",
            "turns": [
                {"text": f"Сочи из Москвы, {_ru(a)} – {_ru(b)}", "intent": "plan_trip",
                 "patch": {"destination": "Сочи", "origin": "Москва", "date_from": a, "date_to": b}},
                {"text": "А давай лучше в Минводы, в горы", "intent": "update", "kind": "update",
                 "patch": {"destination": "Минеральные Воды"}},
            ],
        },
        {
            "id": "s09_inspiration_only",
            "persona": "Не знает, куда ехать",
            "turns": [
                {"text": "Посоветуй, куда поехать на неделю вдвоём", "intent": "suggest_destinations", "patch": {}},
                {"text": "Расскажи про второй вариант подробнее", "intent": "details", "detail_target": "#2", "kind": "details"},
            ],
        },
        {
            "id": "s10_unknown_city",
            "persona": "Город, которого нет в каталоге",
            "turns": [
                {"text": f"В Париж из Москвы {_ru(a)} – {_ru(b)}", "intent": "plan_trip",
                 "patch": {"destination": "Париж", "origin": "Москва", "date_from": a, "date_to": b}},
            ],
        },
    ]
