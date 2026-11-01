"""Тесты защиты ответа: внутренние id, коды рисков, числа без опоры на данные."""

from __future__ import annotations

from travel_data.models import Evidence

from trip_checks import guard_response_text


def ev(field_path: str, value) -> Evidence:
    return Evidence(
        id=f"ev-hotel-hotel-sochi-03-{field_path}", source_type="hotel", source_id="hotel-sochi-03",
        field_path=field_path, value=value, is_synthetic=True,
    )


DATA = [ev("price_per_night", 7400), ev("distance_to_beach_km", 0.6), {"total": 41610, "duration_minutes": 200}]


def test_internal_ids_and_field_names_are_removed():
    report = guard_response_text("Рекомендую hotel-sochi-03 (ev-hotel-hotel-sochi-03-has_pool): есть has_pool.", *DATA)
    assert "hotel-sochi" not in report.text
    assert "ev-" not in report.text
    assert "has_pool" not in report.text
    assert report.removed_ids


def test_risk_codes_become_human_words():
    report = guard_response_text("Риски корзины: OVER_BUDGET, NON_REFUNDABLE.", *DATA)
    assert "OVER_BUDGET" not in report.text
    assert "дороже бюджета" in report.text
    assert "невозвратный тариф" in report.text


def test_supported_numbers_are_kept_in_any_format():
    text = "Отель стоит 7 400 ₽ за ночь. Итого 41 610 ₽. До пляжа 0,6 км. В пути 3 ч 20 мин."
    report = guard_response_text(text, *DATA)
    assert report.text == text
    assert report.unsupported_numbers == []


def test_invented_price_removes_only_that_sentence():
    text = "Отель у моря. Ужин в ресторане стоит 3 500 ₽. Итого 41 610 ₽."
    report = guard_response_text(text, *DATA)
    assert "3 500" not in report.text
    assert "Отель у моря." in report.text
    assert "Итого 41 610 ₽." in report.text
    assert report.unsupported_numbers == ["3 500 ₽"]


def test_invented_percent_and_duration_are_caught():
    report = guard_response_text("Скидка 15%. Лететь 2 ч 5 мин.", *DATA)
    assert report.text == ""
    assert len(report.removed_sentences) == 2


def test_fraction_from_data_supports_percent():
    report = guard_response_text("Вероятность дождя 80%.", {"precipitation_probability": 0.8})
    assert report.text == "Вероятность дождя 80%."


def test_dates_and_times_are_not_treated_as_prices():
    text = "Вылет 26.11 в 06:40, заселение с 14:00."
    assert guard_response_text(text).text == text


def test_bare_large_number_counts_as_money():
    report = guard_response_text("Поездка обойдётся примерно в 99999.", *DATA)
    assert report.unsupported_numbers == ["99999"]
