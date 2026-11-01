"""Тесты инструментов оценки: эталон корректен, подсчёт полей работает, сценарии проходят."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_core.contracts import RequestExtraction, TripRequestPatch
from evals.scoring import FIELDS, score_case

GOLDEN = Path(__file__).parents[1] / "evals" / "golden.jsonl"


def load_golden():
    return [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_golden_set_is_well_formed():
    cases = load_golden()
    assert len(cases) >= 40
    assert len({case["id"] for case in cases}) == len(cases)
    holdout = [case for case in cases if case["split"] == "holdout"]
    assert 0.15 <= len(holdout) / len(cases) <= 0.3
    for case in cases:
        assert set(case["expected"]) <= set(FIELDS), case["id"]


def test_scoring_counts_each_field():
    case = {
        "expected": {
            "destination": "dest-sochi", "origin": "Москва", "budget_total": 70000,
            "children_ages": [4], "pets": 1, "hard_contains": ["пересад"], "intent": "plan_trip",
        }
    }
    extraction = RequestExtraction(
        patch=TripRequestPatch(
            destination="Сочи", origin="москва", budget_total=60000,
            guests={"adults": 2, "children_ages": [4], "pets": 1}, hard=["без пересадок"],
        ),
        intent="plan_trip",
    )
    result = score_case(case, extraction)
    assert result == {
        "intent": True, "origin": True, "destination": True, "budget_total": False,
        "children_ages": True, "pets": True, "hard_contains": True,
    }


def test_unset_fields_are_scored_as_none():
    case = {"expected": {"destination": None, "date_from": None, "intent": "suggest_destinations"}}
    extraction = RequestExtraction(intent="suggest_destinations")
    assert all(score_case(case, extraction).values())


@pytest.fixture()
def offline_weather(monkeypatch):
    monkeypatch.setenv("MET_NO_ENABLED", "false")
    monkeypatch.setenv("OPEN_METEO_ENABLED", "false")


def test_offline_scenarios_run_without_crashes(offline_weather):
    from evals.run_scenarios import run_scenario, summarize
    from evals.scenarios import build_scenarios

    results = [run_scenario(scenario, live=False) for scenario in build_scenarios()]
    summary = summarize(results)
    assert summary["crashed"] == 0
    assert all(not result["blocking"] for result in results if result["reached_cart"])
