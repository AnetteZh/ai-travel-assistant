"""Прогон агента по набору реальных запросов через настоящую модель из .env.

Зачем: вместо того чтобы находить ошибки разбора по одному вручную, за раз прогнать 30+
типичных и «неудобных» запросов и получить отчёт со всеми расхождениями и метриками из ТЗ
(доля доведённых до корзины сценариев, число уточнений, время до первого ответа, ошибки
в состыковках).

Запуск из корня проекта (нужен .env с ключом модели):
    python scripts/eval_agent.py
    python scripts/eval_agent.py --only sochi-basic,pets
Отчёт сохраняется в evals/reports/ (Markdown и JSON). Ключи и секреты туда не попадают.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)  # чтобы config прочитал .env из корня проекта

import agent_core.graph as graph  # noqa: E402
from agent_core.contracts import TripState  # noqa: E402
from agent_core.graph import CoreError, TravelAgent  # noqa: E402

UNSET = object()
MODEL_ANSWERS: list[str] = []  # что модель вернула дословно — для разбора упавших кейсов


def _model_answer(result: Any) -> str:
    """Короткая запись ответа модели: аргументы вызова функции или текст, плюс ошибка формата."""
    raw = result.get("raw") if isinstance(result, dict) else None
    parts = []
    for call in getattr(raw, "tool_calls", None) or []:
        parts.append(json.dumps(call.get("args"), ensure_ascii=False, default=str))
    content = getattr(raw, "content", None)
    if isinstance(content, str) and content.strip() and not parts:
        parts.append(content.strip()[:500])
    if not parts and isinstance(result, dict) and result.get("parsed") is not None:
        parsed = result["parsed"]
        parts.append(parsed.model_dump_json(exclude_unset=True) if hasattr(parsed, "model_dump_json") else str(parsed))
    error = result.get("parsing_error") if isinstance(result, dict) else None
    if error is not None:
        parts.append(f"ошибка формата: {str(error).splitlines()[0][:200]}")
    return " | ".join(parts) or repr(result)[:300]


def _record_model_answers() -> None:
    build = graph._build_structured_model

    def recording(settings: Any) -> Any:
        model = build(settings)

        class Recorder:
            def invoke(self, messages: Any) -> Any:
                result = model.invoke(messages)
                MODEL_ANSWERS.append(_model_answer(result))
                return result

        return Recorder()

    graph._build_structured_model = recording


def _get(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return UNSET
        data = data[part]
    return data


def _matches(expected: Any, actual: Any) -> bool:
    if expected is None:
        return actual in (None, UNSET)
    if expected == "*":
        return actual not in (None, UNSET, "", [])
    if isinstance(expected, str) and expected.startswith("*"):
        return isinstance(actual, str) and actual.endswith(expected[1:])
    if isinstance(expected, list) and isinstance(actual, list):
        return sorted(map(str, expected)) == sorted(map(str, actual))
    return expected == actual


def _block_types(state: TripState) -> set[str]:
    return {block.type for block in state.blocks}


def evaluate(case: dict[str, Any], state: TripState | None, error: str | None) -> list[str]:
    """Список расхождений с ожиданиями кейса. Пустой список — кейс пройден."""
    if error:
        return [f"ошибка: {error}"]
    if state is None:
        return ["агент не вернул состояние"]
    expect = case.get("expect", {})
    request = state.request.model_dump(mode="json")
    failures: list[str] = []

    for field, expected in expect.get("request", {}).items():
        if field.endswith("~"):  # список должен содержать подстроки
            values = [str(item).casefold() for item in (_get(request, field[:-1]) or [])]
            for needle in expected:
                if not any(needle.casefold() in value for value in values):
                    failures.append(f"{field[:-1]}: нет «{needle}» в {values}")
            continue
        actual = _get(request, field)
        if not _matches(expected, actual):
            shown = "не задано" if actual is UNSET else json.dumps(actual, ensure_ascii=False)
            failures.append(f"{field}: ожидалось {json.dumps(expected, ensure_ascii=False)}, получено {shown}")

    types = _block_types(state)
    for block in expect.get("shows", []):
        if block not in types:
            failures.append(f"нет блока {block}")
    for block in expect.get("not_shows", []):
        if block in types:
            failures.append(f"лишний блок {block}")
    if "clarification" in expect and bool(state.clarification_question) != expect["clarification"]:
        failures.append("ожидался уточняющий вопрос" if expect["clarification"] else f"лишний вопрос: {state.clarification_question}")

    date_from, date_to = request.get("date_from"), request.get("date_to")
    if "request_nights" in expect or "request_nights_max" in expect:
        if not (date_from and date_to):
            failures.append("даты не заданы, длительность не проверить")
        else:
            nights = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days
            if "request_nights" in expect and nights != expect["request_nights"]:
                failures.append(f"ночей {nights}, ожидалось {expect['request_nights']}")
            if "request_nights_max" in expect and nights > expect["request_nights_max"]:
                failures.append(f"ночей {nights}, ожидалось не больше {expect['request_nights_max']}")
    if "date_from_weekday" in expect and date_from and date.fromisoformat(date_from).weekday() != expect["date_from_weekday"]:
        failures.append(f"начало {date_from} не в нужный день недели")

    if expect.get("hotels_allow_pets"):
        cards = next((b for b in state.blocks if b.type == "option_cards"), None)
        hotels = cards.data.get("hotels", []) if cards else []
        if any(not item["hotel"]["has_pets_allowed"] for item in hotels):
            failures.append("в подборке есть отели без питомцев")

    text = state.response_text or ""
    if "response_contains" in expect and expect["response_contains"] not in text:
        failures.append(f"в ответе нет «{expect['response_contains']}»")
    if "response_not_contains" in expect and expect["response_not_contains"] in text:
        failures.append(f"в ответе есть «{expect['response_not_contains']}»")

    # Проверки, общие для всех кейсов.
    today = date.today().isoformat()
    if date_from and date_from < today:
        failures.append(f"дата в прошлом: {date_from}")
    if state.plan and state.plan.date_from.isoformat() < today:
        failures.append(f"план в прошлом: {state.plan.date_from}")
    blocking = [issue.message for issue in state.issues if issue.severity == "error"]
    if blocking and expect.get("no_blocking", True):
        failures.append("блокирующие несостыковки: " + "; ".join(blocking))
    if not text.strip():
        failures.append("пустой ответ агента")
    if "dest-" in text:
        failures.append("в ответе внутренний id направления")
    return failures


def run_case(agent: Any, case: dict[str, Any]) -> dict[str, Any]:
    state: TripState | None = None
    error = None
    timings: list[float] = []
    MODEL_ANSWERS.clear()
    for turn in case["turns"]:
        started = time.perf_counter()
        try:
            state = agent.run(turn, state)
        except CoreError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 — в отчёт, а не падение всего прогона
            error = f"{type(exc).__name__}: {exc}"
        timings.append(round(time.perf_counter() - started, 2))
        if error:
            break
    failures = evaluate(case, state, error)
    return {
        "id": case["id"],
        "turns": case["turns"],
        "passed": not failures,
        "failures": failures,
        "seconds_per_turn": timings,
        "request": state.request.model_dump(mode="json", exclude={"weights"}) if state else None,
        "assumptions": state.assumptions if state else [],
        "clarification": state.clarification_question if state else None,
        "issues": [f"{i.code}/{i.severity}: {i.message}" for i in state.issues] if state else [],
        "blocks": sorted(_block_types(state)) if state else [],
        "response": state.response_text if state else None,
        "model_answers": list(MODEL_ANSWERS),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(results)
    first_turn = [r["seconds_per_turn"][0] for r in results if r["seconds_per_turn"]]
    with_plan = [r for r in results if "trip_timeline" in r["blocks"]]
    return {
        "cases": total,
        "passed": sum(r["passed"] for r in results),
        "errors": sum(any(f.startswith("ошибка") for f in r["failures"]) for r in results),
        "reached_cart": sum("cart" in r["blocks"] for r in results),
        "clarifications": sum(bool(r["clarification"]) for r in results),
        "plans_with_blocking_issues": sum(any("/error" in i for i in r["issues"]) for r in with_plan),
        "avg_seconds_first_turn": round(sum(first_turn) / len(first_turn), 2) if first_turn else None,
    }


def markdown(results: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        f"# Прогон агента {datetime.now():%Y-%m-%d %H:%M}",
        "",
        f"Пройдено **{summary['passed']} из {summary['cases']}**. Ошибок выполнения: {summary['errors']}. "
        f"Дошли до корзины: {summary['reached_cart']}. Уточняющих вопросов: {summary['clarifications']}. "
        f"Планов с блокирующими несостыковками: {summary['plans_with_blocking_issues']}. "
        f"Среднее время первого ответа: {summary['avg_seconds_first_turn']} с.",
        "",
        "| Кейс | Итог | Что не так |",
        "| --- | --- | --- |",
    ]
    for r in results:
        lines.append(f"| {r['id']} | {'✅' if r['passed'] else '❌'} | {'<br>'.join(r['failures']) or '—'} |")
    lines += ["", "## Подробности по непрошедшим кейсам", ""]
    for r in results:
        if r["passed"]:
            continue
        lines += [
            f"### {r['id']}",
            "",
            *[f"- Сообщение: «{turn}»" for turn in r["turns"]],
            f"- Разбор запроса: `{json.dumps(r['request'], ensure_ascii=False)}`",
            f"- Допущения: {r['assumptions']}",
            f"- Вопрос агента: {r['clarification']}",
            f"- Замечания: {r['issues']}",
            f"- Блоки: {r['blocks']}",
            f"- Ответ: {r['response']}",
            *[f"- Ответ модели {index}: `{answer}`" for index, answer in enumerate(r.get("model_answers", []), start=1)],
            "",
        ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cases", default=str(ROOT / "evals" / "agent_cases.json"))
    parser.add_argument("--only", default="", help="id кейсов через запятую")
    parser.add_argument("--out", default=str(ROOT / "evals" / "reports"))
    args = parser.parse_args()

    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    if args.only:
        wanted = {item.strip() for item in args.only.split(",")}
        cases = [case for case in cases if case["id"] in wanted]
    _record_model_answers()
    agent = TravelAgent()
    results = []
    for index, case in enumerate(cases, start=1):
        result = run_case(agent, case)
        results.append(result)
        mark = "OK " if result["passed"] else "FAIL"
        print(f"[{index:>2}/{len(cases)}] {mark} {case['id']}" + ("" if result["passed"] else f" — {result['failures'][0]}"), flush=True)

    summary = summarize(results)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    (out / f"report-{stamp}.md").write_text(markdown(results, summary), encoding="utf-8")
    (out / f"report-{stamp}.json").write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nПройдено {summary['passed']} из {summary['cases']}. Отчёт: {out / f'report-{stamp}.md'}")
    return 0 if summary["passed"] == summary["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
