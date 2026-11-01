"""Прогон сквозных сценариев и метрики из ТЗ.

    python -m evals.run_scenarios            # офлайн: без модели, извлечение задано в сценарии
    python -m evals.run_scenarios --live     # с настоящей моделью (нужен ключ в .env)

Офлайн-режим проверяет всё, кроме разбора текста: поиск, сборку, check_plan,
починку конфликтов, корзину, защиту ответа. Его можно гонять после каждого
изменения в main, он быстрый и детерминированный.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from agent_core.contracts import RequestExtraction, TripRequestPatch, TripState
from agent_core.graph import CoreError, TravelAgent
from travel_data import book
from trip_checks import has_blocking_issues

from .scenarios import build_scenarios

REPORTS = Path(__file__).parent / "reports"
SEARCH_TOOLS = ("suggest_destinations", "search_hotels", "search_transport", "search_transfers")
TIMING_CODES = {"DATE_MISMATCH", "LATE_ARRIVAL", "TIGHT_CONNECTION", "TRANSFER_OUTSIDE_HOURS"}


def _offline_parser(turn: dict[str, Any], trip: TripState):
    target = turn.get("detail_target")
    if target and target.startswith("#") and trip.destinations:
        target = trip.destinations.options[int(target[1:]) - 1].destination.id
    extraction = RequestExtraction(
        patch=TripRequestPatch.model_validate(turn.get("patch", {})),
        intent=turn.get("intent", "plan_trip"),
        detail_target=target,
    )
    return lambda _message: extraction


def _has_recommendation(trip: TripState) -> bool:
    return bool(
        (trip.destinations and trip.destinations.options)
        or (trip.hotels and (trip.hotels.options or trip.hotels.alternatives))
    )


def run_scenario(scenario: dict[str, Any], live: bool) -> dict[str, Any]:
    trip: TripState | None = None
    started = time.perf_counter()
    first_rec_seconds = None
    clarifications_before_rec = 0
    rebuilds = []
    guard_repairs = 0
    error = None
    for index, turn in enumerate(scenario["turns"]):
        agent = TravelAgent() if live else TravelAgent(_offline_parser(turn, trip or TripState()))
        previous = trip
        try:
            trip = agent.run(turn["text"], trip)
        except CoreError as exc:
            error = f"ход {index + 1}: {exc}"
            break
        guard_repairs += sum(1 for entry in trip.trace if entry.stage == "guardrails")
        if first_rec_seconds is None:
            if _has_recommendation(trip):
                first_rec_seconds = time.perf_counter() - started
            elif trip.clarification_question:
                clarifications_before_rec += 1
        if turn.get("kind") == "update":
            called = {tool: count for tool, count in trip.tool_calls.items() if tool in SEARCH_TOOLS and count}
            avoidable = [tool for tool in turn.get("must_not_call", []) if tool in called]
            rebuilds.append({
                "turn": index + 1,
                "called": called,
                "partial": not avoidable,
                "avoidable_calls": avoidable,
                "diff_shown": trip.plan_diff is not None,
                "version_before": previous.version if previous else 0,
                "version_after": trip.version,
            })

    final = trip or TripState()
    booked = None
    if final.cart is not None and not has_blocking_issues(final.issues):
        booking = book(final.cart)
        booked = getattr(booking, "booking_id", None)
    issue_codes = [issue.code for issue in final.issues]
    return {
        "id": scenario["id"],
        "persona": scenario["persona"],
        "error": error,
        "reached_cart": final.cart is not None,
        "booked": booked is not None,
        "first_rec_seconds": round(first_rec_seconds, 2) if first_rec_seconds is not None else None,
        "clarifications_before_rec": clarifications_before_rec,
        "final_issue_codes": issue_codes,
        "timing_conflicts": [code for code in issue_codes if code in TIMING_CODES],
        "blocking": has_blocking_issues(final.issues),
        "repairs": sum(1 for entry in final.trace if entry.stage == "repair_plan" and entry.outcome == "attempted"),
        "rebuilds": rebuilds,
        "guard_repairs": guard_repairs,
        "total": final.cart.total if final.cart else None,
        "budget": final.request.budget_total,
        "response": final.response_text,
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(results)
    with_rec = [r["first_rec_seconds"] for r in results if r["first_rec_seconds"] is not None]
    rebuilds = [rb for r in results for rb in r["rebuilds"]]
    planned = [r for r in results if r["reached_cart"]]
    return {
        "scenarios": n,
        "completed_to_booking": f"{sum(r['booked'] for r in results)}/{n}",
        "reached_cart": f"{sum(r['reached_cart'] for r in results)}/{n}",
        "crashed": sum(1 for r in results if r["error"]),
        "avg_clarifications_before_rec": round(mean(r["clarifications_before_rec"] for r in results), 2) if n else None,
        "avg_seconds_to_first_rec": round(mean(with_rec), 2) if with_rec else None,
        "timing_conflicts_in_final_plans": sum(len(r["timing_conflicts"]) for r in planned),
        "plans_with_timing_conflicts": f"{sum(1 for r in planned if r['timing_conflicts'])}/{len(planned)}",
        "conflicts_auto_repaired": sum(r["repairs"] for r in results),
        "partial_rebuilds": f"{sum(rb['partial'] for rb in rebuilds)}/{len(rebuilds)}",
        "diff_shown_after_change": f"{sum(rb['diff_shown'] for rb in rebuilds)}/{len(rebuilds)}",
        "guardrail_repairs": sum(r["guard_repairs"] for r in results),
    }


LABELS = {
    "scenarios": "Сценариев",
    "completed_to_booking": "Доведено до оформления (book)",
    "reached_cart": "Дошли до корзины",
    "crashed": "Упали с ошибкой",
    "avg_clarifications_before_rec": "Уточнений до первой рекомендации (среднее)",
    "avg_seconds_to_first_rec": "Время до первой рекомендации, с",
    "timing_conflicts_in_final_plans": "Ошибок в состыковках в финальных планах",
    "plans_with_timing_conflicts": "Планов с ошибками состыковок",
    "conflicts_auto_repaired": "Конфликтов, исправленных агентом самостоятельно",
    "partial_rebuilds": "Пересборок без лишних поисков",
    "diff_shown_after_change": "Пересборок с показанным диффом",
    "guardrail_repairs": "Срабатываний защиты ответа",
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="разбор текста настоящей моделью")
    parser.add_argument("--only", default=None, help="id сценария")
    args = parser.parse_args()
    if not args.live:
        # Офлайн: погода из синтетического климата, без сети — прогон быстрый и повторяемый.
        os.environ.setdefault("MET_NO_ENABLED", "false")
        os.environ.setdefault("OPEN_METEO_ENABLED", "false")

    scenarios = [s for s in build_scenarios() if not args.only or s["id"] == args.only]
    results = []
    for scenario in scenarios:
        result = run_scenario(scenario, args.live)
        results.append(result)
        status = "ОШИБКА " + result["error"] if result["error"] else (
            "бронь" if result["booked"] else "корзина" if result["reached_cart"] else "без корзины"
        )
        extra = f"; риски: {', '.join(result['final_issue_codes'])}" if result["final_issue_codes"] else ""
        print(f"{result['id']}: {status}{extra}")

    summary = summarize(results)
    mode = "live" if args.live else "offline"
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"scenarios-{mode}-{stamp}.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    lines = [
        f"# Метрики агента ({mode}, {stamp})",
        "",
        "| Метрика | Значение |",
        "|---|---|",
        *[f"| {LABELS[key]} | {value} |" for key, value in summary.items()],
        "",
        "## Сценарии",
        "",
        "| Сценарий | Итог | Риски в финальном плане | Починок | Пересборка |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        outcome = r["error"] or ("бронь" if r["booked"] else "корзина" if r["reached_cart"] else "без корзины")
        rebuild = "; ".join(
            ("частичная" if rb["partial"] else f"лишние: {', '.join(rb['avoidable_calls'])}") for rb in r["rebuilds"]
        ) or "—"
        lines.append(f"| {r['id']} | {outcome} | {', '.join(r['final_issue_codes']) or '—'} | {r['repairs']} | {rebuild} |")
    report_path = REPORTS / f"scenarios-{mode}-{stamp}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print()
    print("\n".join(lines[2:4 + len(summary)]))
    print(f"\nОтчёт: {report_path}")


if __name__ == "__main__":
    main()
