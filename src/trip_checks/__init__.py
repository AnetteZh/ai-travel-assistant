"""Проверки и качество: детерминированная проверка плана и защита ответа агента.

Модуль не вызывает LLM. Всё, что можно проверить кодом (даты, время,
часовые пояса, бюджет, числа в тексте), проверяется здесь.
"""

from .alternatives import RejectedOption, why_not, why_not_payload
from .guardrails import GuardrailReport, guard_response_text
from .plan_checks import (
    MAX_CHECKIN_WAIT_MINUTES,
    MIN_TERMINAL_BUFFER_MINUTES,
    check_cart,
    check_plan,
    has_blocking_issues,
)

__all__ = [
    "GuardrailReport",
    "RejectedOption",
    "MAX_CHECKIN_WAIT_MINUTES",
    "MIN_TERMINAL_BUFFER_MINUTES",
    "check_cart",
    "check_plan",
    "guard_response_text",
    "has_blocking_issues",
    "why_not",
    "why_not_payload",
]
