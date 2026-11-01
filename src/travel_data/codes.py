"""Shared reason/risk code glossary; review with the QA/checks role before integration."""

REASON_CODE_DESCRIPTIONS = {
    "MATCHES_HARD": "Вариант выполняет применимые жёсткие требования пользователя.",
    "KID_FRIENDLY": "Есть семейные удобства, подтверждённые полями мок-модели.",
    "FREE_CANCELLATION": "У тарифа указана бесплатная отмена в пределах установленного срока.",
    "SHORT_TRAVEL": "Продолжительность маршрута не превышает внутренний ориентир в 4 часа.",
    "NEAR_POI": "Отель расположен менее чем в 1 км от центра или пляжа по синтетическим данным.",
}

RISK_CODE_DESCRIPTIONS = {
    "OVER_BUDGET": "Итоговая стоимость корзины превышает бюджет; процент превышения рассчитывается кодом.",
    "LATE_ARRIVAL": "Трансфер закрыт в момент прибытия.",
    "TRANSFER_OUTSIDE_HOURS": "Трансфер закрыт в расчётное время выезда из отеля.",
    "CHECKIN_GAP": "Между прибытием и доступным заселением слишком большой интервал; проверяется Core/Checks.",
    "TIGHT_CONNECTION": "Пересадка короче минимального времени; проверяется Core/Checks при наличии длительности пересадки.",
    "NON_REFUNDABLE": "У тарифа нет возврата.",
    "WEATHER_RISK": "Погода может не соответствовать ожиданиям пользователя.",
    "DATE_MISMATCH": "Даты отеля и транспорта не совпадают; проверяется Core/Checks.",
}

# Data-specific filter diagnostics. These are explanations of rejected candidates, not risk codes.
HARD_REQUIREMENT_VIOLATIONS = {
    "PETS_NOT_ALLOWED": "Отель не разрешает размещение с питомцами.",
    "POOL_REQUIRED": "Нет обязательного бассейна.",
    "SPA_REQUIRED": "Нет обязательного спа.",
    "FREE_CANCELLATION_REQUIRED": "Нет тарифа с бесплатной отменой.",
    "FAMILY_FRIENDLY_REQUIRED": "Нет подтверждённых семейных удобств.",
}
