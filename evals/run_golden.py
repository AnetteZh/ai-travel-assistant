"""Прогон эталона: точность разбора запроса по полям.

Нужна настоящая модель (OPENAI_API_KEY или GIGACHAT_CREDENTIALS в .env).

    python -m evals.run_golden                 # только dev-часть
    python -m evals.run_golden --split holdout # отложенная часть, честная оценка
    python -m evals.run_golden --split all --limit 10
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from agent_core.graph import CoreError, _parse_with_model

from .scoring import FIELDS, merged_request, score_case

HERE = Path(__file__).parent
REPORTS = HERE / "reports"


def load_cases(split: str) -> list[dict]:
    cases = [json.loads(line) for line in (HERE / "golden.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    return cases if split == "all" else [case for case in cases if case["split"] == split]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "holdout", "all"], default="dev")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    cases = load_cases(args.split)[: args.limit]
    per_field: dict[str, list[bool]] = defaultdict(list)
    rows = []
    errors = 0
    latencies = []
    for case in cases:
        started = time.perf_counter()
        try:
            extraction = _parse_with_model(case["message"])
        except CoreError as exc:
            errors += 1
            rows.append({"id": case["id"], "error": str(exc)})
            print(f"{case['id']}: ОШИБКА {exc}")
            continue
        latencies.append(time.perf_counter() - started)
        result = score_case(case, extraction)
        for field, ok in result.items():
            per_field[field].append(ok)
        failed = [field for field, ok in result.items() if not ok]
        rows.append({
            "id": case["id"],
            "message": case["message"],
            "failed_fields": failed,
            "got": merged_request(extraction).model_dump(mode="json"),
            "intent": extraction.intent,
            "expected": case["expected"],
        })
        print(f"{case['id']}: {'OK' if not failed else 'ошибки в ' + ', '.join(failed)}")

    total_checks = sum(len(values) for values in per_field.values())
    total_ok = sum(sum(values) for values in per_field.values())
    exact = sum(1 for row in rows if "failed_fields" in row and not row["failed_fields"])
    summary = {
        "split": args.split,
        "cases": len(cases),
        "model_errors": errors,
        "field_accuracy": round(total_ok / total_checks, 3) if total_checks else None,
        "exact_match": round(exact / len(cases), 3) if cases else None,
        "avg_parse_seconds": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "per_field": {field: round(sum(per_field[field]) / len(per_field[field]), 3) for field in FIELDS if per_field[field]},
    }

    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (REPORTS / f"golden-{args.split}-{stamp}.json").write_text(
        json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        f"# Эталон разбора запроса ({args.split}, {stamp})",
        "",
        f"- Запросов: {summary['cases']}, ошибок модели: {errors}",
        f"- Точность по полям: {summary['field_accuracy']}",
        f"- Полностью верно разобрано: {summary['exact_match']}",
        f"- Среднее время разбора, с: {summary['avg_parse_seconds']}",
        "",
        "| Поле | Точность | Проверок |",
        "|---|---|---|",
        *[f"| {field} | {summary['per_field'][field]:.0%} | {len(per_field[field])} |" for field in summary["per_field"]],
        "",
        "## Ошибки",
        *[f"- **{row['id']}** «{row['message']}»: {', '.join(row['failed_fields'])}" for row in rows if row.get("failed_fields")],
        *[f"- **{row['id']}**: {row['error']}" for row in rows if "error" in row],
    ]
    report_path = REPORTS / f"golden-{args.split}-{stamp}.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:7]))
    print(f"\nОтчёт: {report_path}")


if __name__ == "__main__":
    main()
