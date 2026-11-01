import { findBlock, readableDay, type Issue, type TripState, type UIBlock } from "./types";

/**
 * Оценка готовности плана для интерфейса.
 *
 * Источник правды — замечания Core (`check_plan`). Фронтенд только решает, как их показать:
 * блокирующие ошибки запрещают оформление, предупреждения показываются, но не блокируют.
 * Дополнительно здесь есть две страховочные проверки по таймлайну (прошедшие даты и прибытие
 * позже начала проживания). Их нужно перенести в check_plan Core; после этого фронтенд
 * перестанет дублировать замечание, потому что дедуплицирует по коду.
 */

/** Коды, при которых план физически не работает. Согласовать список с модулем проверок. */
export const BLOCKING_CODES = new Set(["DATE_MISMATCH", "LATE_ARRIVAL", "TRANSFER_OUTSIDE_HOURS", "PAST_DATES", "ARRIVAL_AFTER_CHECKIN_DATE"]);

export type PlanStatus = "none" | "ok" | "warning" | "blocked";
export type PlanHealth = { status: PlanStatus; blocking: Issue[]; warnings: Issue[] };

type TimelineEvent = { event: string; departure_at?: string; arrival_at?: string; date_from?: string; date_to?: string };

const isoDay = (value: string) => value.slice(0, 10);

function todayIso(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

export function interfaceChecks(blocks: UIBlock[]): Issue[] {
  const timeline = findBlock(blocks, "trip_timeline") as { events?: TimelineEvent[] } | undefined;
  const events = timeline?.events ?? [];
  const stay = events.find((e) => e.event === "hotel_stay");
  const outbound = events.find((e) => e.event === "outbound");
  const issues: Issue[] = [];
  if (stay?.date_from && stay.date_from < todayIso()) {
    issues.push({
      code: "PAST_DATES", severity: "error", source: "interface",
      message: `Даты проживания уже прошли (${readableDay(stay.date_from, true)}). Попроси агента подобрать поездку на актуальные даты.`,
    });
  }
  // Сравниваем календарные дни из строк ISO: обе даты указаны в местном времени направления.
  if (stay?.date_from && outbound?.arrival_at && isoDay(outbound.arrival_at) > stay.date_from) {
    issues.push({
      code: "ARRIVAL_AFTER_CHECKIN_DATE", severity: "error", source: "interface",
      message: `Прибытие ${readableDay(isoDay(outbound.arrival_at))}, а проживание начинается ${readableDay(stay.date_from)}: первая оплаченная ночь пропадает.`,
    });
  }
  return issues;
}

function coreIssues(blocks: UIBlock[], tripState: TripState | null): Issue[] {
  const fromCart = (findBlock(blocks, "cart") as { issues?: Issue[] } | undefined)?.issues;
  const fromComparison = (findBlock(blocks, "comparison") as { issues?: Issue[] } | undefined)?.issues;
  return (fromCart ?? fromComparison ?? tripState?.issues ?? []).map((issue) => ({ ...issue, source: "core" as const }));
}

export function planHealth(blocks: UIBlock[], tripState: TripState | null): PlanHealth {
  const hasPlan = blocks.some((b) => b.type === "cart" || b.type === "trip_timeline");
  if (!hasPlan) return { status: "none", blocking: [], warnings: [] };
  const seen = new Set<string>();
  const fromCore = coreIssues(blocks, tripState);
  // Если Core уже сообщил о несовпадении дат, страховочная проверка интерфейса не дублирует его.
  const coreHasDates = fromCore.some((issue) => issue.code === "DATE_MISMATCH");
  const fromInterface = interfaceChecks(blocks).filter((issue) => !(coreHasDates && issue.code === "ARRIVAL_AFTER_CHECKIN_DATE"));
  const all = [...fromCore, ...fromInterface].filter((issue) => {
    const key = `${issue.code}|${issue.message}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return issue.severity !== "info";
  });
  const blocking = all.filter((i) => i.severity === "error" || BLOCKING_CODES.has(i.code));
  const warnings = all.filter((i) => !blocking.includes(i));
  const status: PlanStatus = blocking.length ? "blocked" : warnings.length ? "warning" : "ok";
  return { status, blocking, warnings };
}
