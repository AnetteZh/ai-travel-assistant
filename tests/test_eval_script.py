"""Проверка самого скрипта прогона: что он правильно находит расхождения (без модели и сети)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from agent_core.contracts import RequestExtraction, TripRequestPatch
from agent_core.graph import TravelAgent

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_agent.py"
spec = importlib.util.spec_from_file_location("eval_agent", SCRIPT)
eval_agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_agent)


def agent_with(patch: TripRequestPatch, intent: str = "plan_trip") -> TravelAgent:
    return TravelAgent(lambda _: RequestExtraction(patch=patch, intent=intent))


def test_passing_case_has_no_failures():
    case = {"id": "ok", "turns": ["Сочи"], "expect": {"request": {"origin": "Москва", "destination": "Сочи", "budget_total": None}, "shows": ["cart"]}}
    result = eval_agent.run_case(agent_with(TripRequestPatch(origin="Москва", destination="Сочи")), case)

    assert result["passed"], result["failures"]


def test_mismatches_are_reported_per_field():
    case = {"id": "bad", "turns": ["Сочи"], "expect": {"request": {"budget_total": 80000, "hard~": ["бассейн"]}, "clarification": True}}
    result = eval_agent.run_case(agent_with(TripRequestPatch(origin="Москва", destination="Сочи")), case)

    assert not result["passed"]
    text = " ".join(result["failures"])
    assert "budget_total" in text and "бассейн" in text and "уточняющий" in text


def test_every_bundled_case_has_turns_and_expectations():
    import json

    cases = json.loads((SCRIPT.parents[1] / "evals" / "agent_cases.json").read_text(encoding="utf-8"))
    assert len(cases) >= 30
    assert len({case["id"] for case in cases}) == len(cases)
    assert all(case["turns"] and case["expect"] for case in cases)
