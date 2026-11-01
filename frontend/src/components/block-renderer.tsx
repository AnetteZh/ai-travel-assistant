"use client";

import { useEffect, useRef, useState } from "react";
import {
  ArrowRight, BadgeCheck, BedDouble, CalendarDays, Check, CheckCircle2,
  ChevronDown, CircleAlert, Clock3, Coffee, Info, MapPin, Plane, Search, ShieldAlert, ShieldCheck,
  Sparkles, Star, TrainFront, Wallet, Wrench,
} from "lucide-react";
import type { PlanHealth } from "@/lib/plan-health";
import type { Cart, DestinationOption, HotelOption, Issue, TransportOption, TripRequest, TripState, UIBlock } from "@/lib/types";
import { destinationLabel, findBlock, money, nounForm, readableDate, readableDay } from "@/lib/types";

function asText(value: unknown): string { return typeof value === "string" ? value : ""; }

/** Обложки карточек: фото лежат в frontend/public/cities/<ключ>.jpg, стили — .cover-<ключ> в globals.css. */
const cityArt: Record<string, string> = {
  "Москва": "moscow",
  "Санкт-Петербург": "spb",
  "Сочи": "sochi",
  "Казань": "kazan",
  "Калининград": "kaliningrad",
  "Екатеринбург": "ekb",
  "Новосибирск": "nsk",
  "Иркутск": "irkutsk",
  "Владивосток": "vladivostok",
  "Мурманск": "murmansk",
  "Петрозаводск": "petrozavodsk",
  "Минеральные Воды": "minvody",
  "Махачкала": "makhachkala",
  "Тюмень": "tyumen",
  "Нижний Новгород": "nnov",
  "Ярославль": "yaroslavl",
  "Архангельск": "arkhangelsk",
  "Пермь": "perm",
  "Уфа": "ufa",
  "Краснодар": "krasnodar",
};
function cityCover(name: string): string { return cityArt[name] || "generic"; }

type DestinationData = { items?: DestinationOption[]; budget_total?: number | null; nothing_in_budget?: boolean };

export function DestinationCards({ blocks, page, pageIndex = 0, pageCount = 1, onChoose, onMore, onPrev, disabled }: {
  blocks: UIBlock[]; page?: DestinationData; pageIndex?: number; pageCount?: number;
  onChoose: (name: string) => void; onMore?: () => void; onPrev?: () => void; disabled?: boolean;
}) {
  const data = page ?? (findBlock(blocks, "destination_cards") as DestinationData | undefined);
  if (!data?.items?.length) return null;
  const count = data.items.length;
  const budget = data.budget_total ?? null;
  const hasNextCached = pageIndex < pageCount - 1;
  return <section className="board-section" id="destinations">
    <div className="section-title"><div className="section-title-copy"><span className="section-eyebrow"><Sparkles size={14}/> ТВОИ ВАРИАНТЫ</span><h2>Куда отправимся?</h2><p>Направления, которые подходят под твои пожелания</p></div><span className="section-count">{pageCount > 1 ? `Подборка ${pageIndex + 1} из ${pageCount} · ` : ""}{count} {nounForm(count, "направление", "направления", "направлений")}</span></div>
    {data.nothing_in_budget && budget && <div className="budget-miss" role="status"><CircleAlert size={22}/><div><strong>Под ваш бюджет ничего не найдено</strong><p>Бюджет {money(budget)}, а самая недорогая поездка в подборке — от {money(Math.min(...data.items.map(item => item.estimated_trip_price)))}. Ниже ближайшие варианты: можно увеличить бюджет, сократить поездку или поменять даты.</p></div></div>}
    <div className="destination-grid">{data.items.map((item, i) => <article className="destination-card" key={item.destination.id}>
      <div className={`destination-cover cover-${cityCover(item.destination.name)}`}>
        <div className="destination-cover-top"><span className="photo-pill"><MapPin size={12}/>{item.destination.region}</span>{budget && item.estimated_trip_price > budget
          ? <span className="over-budget-pill">Дороже бюджета</span>
          : i === 0 && pageIndex === 0 && <span className="favorite-pill"><Sparkles size={12}/> Ваш матч</span>}</div>
        <div className="destination-cover-title">{item.destination.name}</div><span className="illustration-credit">Иллюстрация</span>
      </div>
      <div className="destination-body">
        <p className="destination-description">{item.why_it_fits || "Подходит под ваши пожелания"}</p>
        <div className="facts-row"><span><Clock3 size={15}/>{item.fastest_travel_minutes ? `от ${Math.round(item.fastest_travel_minutes / 60 * 10) / 10} ч` : "Время уточняется"}</span>{item.weather && <span>☀️ до {item.weather.temperature_max_c}°</span>}</div>
        <div className="card-bottom"><div><span className="small-muted">Ориентир всей поездки</span><strong>{money(item.estimated_trip_price)}</strong></div><button type="button" className="pill-button" disabled={disabled} onClick={() => onChoose(item.destination.name)} aria-label={`Выбрать ${item.destination.name}`}>Выбрать <ArrowRight size={15}/></button></div>
      </div>
    </article>)}</div>
    {(onMore || (onPrev && pageIndex > 0)) && <div className="more-destinations">
      {onPrev && pageIndex > 0 && <button type="button" className="pill-button secondary-pill" disabled={disabled} onClick={onPrev}><ArrowRight className="flip" size={15}/> Предыдущие</button>}
      {onMore && <button type="button" className="pill-button" disabled={disabled} onClick={onMore}>{hasNextCached ? "Следующие" : "Показать другие направления"} <ArrowRight size={15}/></button>}
    </div>}
  </section>;
}

/** Ключ для схлопывания одинаковых по смыслу допущений: Core и Data пишут их разными словами. */
function assumptionKey(text: string): string {
  const t = text.toLocaleLowerCase("ru");
  if (/город (вылета|отправления) не указан/.test(t)) return "origin";
  if (/даты не указаны/.test(t)) return "dates";
  return t.replace(/[^\p{L}\p{N}]+/gu, " ").trim();
}

function uniqueAssumptions(items: string[], request: TripRequest | undefined): string[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const key = assumptionKey(item);
    if (key === "origin" && request?.origin) return false; // город указан: старое допущение неактуально
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function guestsLabel(guests: NonNullable<TripRequest["guests"]>): string {
  const parts = [`${guests.adults} ${nounForm(guests.adults, "взрослый", "взрослых", "взрослых")}`];
  const kids = guests.children_ages.length;
  if (kids) parts.push(`${kids} ${nounForm(kids, "ребёнок", "ребёнка", "детей")}`);
  if (guests.pets) parts.push(guests.pets === 1 ? "питомец" : `${guests.pets} питомца`);
  return parts.join(", ");
}

type Param = { label: string; value: string; missing?: boolean; assumed?: boolean };
function requestParams(request: TripRequest | undefined, blocks: UIBlock[]): Param[] {
  if (!request) return [];
  const params: Param[] = [];
  if ("origin" in request) params.push({ label: "Откуда", value: request.origin || "не указано", missing: !request.origin });
  if ("destination" in request) params.push({ label: "Куда", value: destinationLabel(request.destination, blocks) || "подбираем направление" });
  if ("date_from" in request || "date_to" in request) {
    const stay = ((findBlock(blocks, "trip_timeline") as { events?: { event: string; date_from?: string; date_to?: string }[] } | undefined)?.events ?? [])
      .find((e) => e.event === "hotel_stay");
    if (request.date_from && request.date_to) params.push({ label: "Даты", value: `${readableDay(request.date_from)} — ${readableDay(request.date_to, true)}` });
    else if (stay?.date_from && stay.date_to) params.push({ label: "Даты", value: `${readableDay(stay.date_from)} — ${readableDay(stay.date_to, true)}`, assumed: true });
    else params.push({ label: "Даты", value: "не указаны", missing: true });
  }
  if (request.guests) params.push({ label: "Гости", value: guestsLabel(request.guests) });
  if ("budget_total" in request) params.push({ label: "Бюджет", value: request.budget_total ? money(request.budget_total) : "не указан", missing: !request.budget_total });
  return params;
}

export function Assumptions({ blocks, tripState }: { blocks: UIBlock[]; tripState: TripState | null }) {
  const data = findBlock(blocks, "assumptions") as { items?: string[] } | undefined;
  const items = uniqueAssumptions(data?.items ?? [], tripState?.request);
  const params = requestParams(tripState?.request, blocks);
  if (!items.length && !params.length) return null;
  return <section className="assumptions">
    {!!params.length && <>
      <div className="mini-heading"><div className="mini-icon"><Search size={16}/></div><strong>Как агент понял запрос</strong><span className="small-muted">Неверно? Поправь в чате</span></div>
      <div className="param-row">{params.map((p) => <span key={p.label} className={`param-chip${p.missing ? " missing" : ""}`}><small>{p.label}</small>{p.value}{p.assumed && <em>предположение</em>}</span>)}</div>
    </>}
    {!!items.length && <>
      <div className="mini-heading assumptions-heading"><div className="mini-icon"><Sparkles size={16}/></div><strong>Что агент предположил</strong></div>
      <div className="chip-row">{items.map((a) => <span className="chip" key={a}><Check size={13}/>{a}</span>)}</div>
    </>}
  </section>;
}

const cancelLabel: Record<string, string> = {
  free_until_7_days: "Бесплатная отмена до 7 дней",
  free_until_3_days: "Бесплатная отмена до 3 дней",
  partial: "Частичный возврат",
  non_refundable: "Без возврата",
};

/** Вариант, который выбран в плане (по таймлайну), а не просто первый в списке поиска. */
function selectedTransport(blocks: UIBlock[], event: "outbound" | "return", options: TransportOption[]): TransportOption {
  const events = (findBlock(blocks, "trip_timeline") as { events?: { event: string; departure_at?: string; arrival_at?: string }[] } | undefined)?.events ?? [];
  const chosen = events.find((e) => e.event === event);
  const same = (a?: string, b?: string) => !!a && !!b && new Date(a).getTime() === new Date(b).getTime();
  return options.find((o) => same(o.departure_at, chosen?.departure_at) && same(o.arrival_at, chosen?.arrival_at)) ?? options[0];
}

function TransportStrip({ title, option }: { title: string; option: TransportOption }) {
  const Icon = option.transport_type === "train" ? TrainFront : Plane;
  return <div className="transport-strip"><div className="transport-icon"><Icon size={20}/></div><div><strong>{title} · {option.transport_type === "train" ? "поезд" : "самолёт"}</strong><span>{option.origin_city} → {option.destination_city} · {readableDate(option.departure_at)} → {readableDate(option.arrival_at)}</span></div><strong>{money(option.price)}</strong></div>;
}

export function HotelCards({ blocks }: { blocks: UIBlock[] }) {
  const [selected, setSelected] = useState<HotelOption | null>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!selected) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButton.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") setSelected(null); };
    window.addEventListener("keydown", onKeyDown);
    return () => { window.removeEventListener("keydown", onKeyDown); previousFocus?.focus(); };
  }, [selected]);

  const data = findBlock(blocks, "option_cards") as { hotels?: HotelOption[]; transport_outbound?: TransportOption[]; transport_return?: TransportOption[] } | undefined;
  if (!data?.hotels?.length) return null;
  return <>
    <section className="board-section" id="hotels"><div className="section-title"><div className="section-title-copy"><span className="section-eyebrow"><BedDouble size={14}/> ПОДБОР ПО ПРЕДПОЧТЕНИЯМ</span><h2>Отели и дорога</h2><p>Подобраны с учётом ваших пожеланий</p></div><span className="section-count">{data.hotels.length} {nounForm(data.hotels.length, "отель", "отеля", "отелей")}</span></div>
      <div className="hotel-list">{data.hotels.map((option, i) => <article className="hotel-card" key={option.hotel.id}>
        <div className={`hotel-photo hotel-photo-${i % 3}`}><span className="hotel-photo-credit">Иллюстрация</span><span className="hotel-photo-tag"><Star size={12} fill="currentColor"/> {option.hotel.rating} · {option.hotel.stars}★</span></div>
        <div className="hotel-body"><div className="hotel-heading"><div><span className="small-muted"><MapPin size={12}/> {option.hotel.city}</span><h3>{option.hotel.name}</h3></div></div>
        <p className="hotel-why">{option.why_it_fits || "Соответствует запросу"}</p>
        <div className="chip-row compact"><span className="soft-chip"><ShieldCheck size={13}/>{cancelLabel[option.hotel.cancellation_policy] || option.hotel.cancellation_policy}</span>{option.hotel.breakfast_included && <span className="soft-chip"><Coffee size={13}/> Завтрак</span>}{option.hotel.has_pets_allowed && <span className="soft-chip">🐾 С питомцем</span>}</div>
        {!!option.cons?.length && <p className="risk-caption"><ShieldAlert size={14}/> {option.cons[0]}</p>}
        <div className="card-bottom"><div><span className="small-muted">За проживание</span><strong>{money(option.estimated_lodging_total)}</strong></div><button type="button" className="outline-button" onClick={() => setSelected(option)} aria-label={`Подробнее об отеле ${option.hotel.name}`}>Подробнее <ArrowRight size={15}/></button></div></div>
      </article>)}</div>
      {(!!data.transport_outbound?.length || !!data.transport_return?.length) && <div className="transport-group">
        {!!data.transport_outbound?.length && <TransportStrip title="Туда" option={selectedTransport(blocks, "outbound", data.transport_outbound)}/>}
        {!!data.transport_return?.length && <TransportStrip title="Обратно" option={selectedTransport(blocks, "return", data.transport_return)}/>}
      </div>}
    </section>
    {selected && <div className="hotel-detail-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelected(null); }}>
      <section className="hotel-detail-modal" role="dialog" aria-modal="true" aria-labelledby="hotel-detail-title">
        <div className="hotel-detail-top">
          <div className="hotel-detail-mark"><BedDouble size={24}/></div>
          <button ref={closeButton} className="hotel-detail-close" type="button" onClick={() => setSelected(null)} aria-label="Закрыть подробности">✕</button>
        </div>
        <span className="hotel-detail-overline"><MapPin size={16}/> {selected.hotel.city} · {selected.hotel.stars} {nounForm(selected.hotel.stars, "звезда", "звезды", "звёзд")} · рейтинг {selected.hotel.rating}</span>
        <h2 id="hotel-detail-title">{selected.hotel.name}</h2>
        <p className="hotel-detail-description">{selected.why_it_fits || "Подходит под ваши пожелания"}</p>
        <div className="hotel-detail-prices">
          <div><span>Стоимость за ночь</span><strong>{money(selected.hotel.price_per_night)}</strong></div>
          <div><span>Ориентир за проживание</span><strong>{money(selected.estimated_lodging_total)}</strong></div>
        </div>
        <div className="hotel-detail-facts">
          <span><Coffee size={18}/>{selected.hotel.breakfast_included ? "Завтрак включён" : "Без завтрака"}</span>
          <span><ShieldCheck size={18}/>{cancelLabel[selected.hotel.cancellation_policy] || "Условия отмены уточняются"}</span>
          <span>🐾 {selected.hotel.has_pets_allowed ? "С питомцами можно" : "С питомцами нельзя"}</span>
          <span>👨‍👩‍👧 {selected.hotel.family_friendly ? "Для семей" : "Семейные удобства не подтверждены"}</span>
        </div>
        <div className="hotel-detail-lists">
          <div><h3><CheckCircle2 size={19}/> Почему подходит</h3>{selected.pros?.length ? <ul>{selected.pros.map((line,i) => <li key={i}>{line}</li>)}</ul> : <p>Сведения об удобствах ограничены.</p>}</div>
          <div><h3><ShieldAlert size={19}/> Компромиссы и риски</h3>{selected.cons?.length ? <ul>{selected.cons.map((line,i) => <li key={i}>{line}</li>)}</ul> : <p>В доступных данных риски не указаны.</p>}</div>
        </div>
        <p className="hotel-detail-note"><Info size={17}/> {selected.hotel.is_synthetic ? "Демонстрационные данные. Не являются предложением реального отеля или ценой бронирования." : "Информация из ответа агента. Перед оплатой требуется проверка наличия и условий."}</p>
        <button className="hotel-detail-done" type="button" onClick={() => setSelected(null)}>Вернуться к вариантам</button>
      </section>
    </div>}
  </>;
}

type RejectedOption = { kind: "hotel" | "outbound"; name: string; saving: number; reason: string; text: string };

export function Compare({ blocks }: { blocks: UIBlock[] }) {
  const data = findBlock(blocks, "comparison") as { hotels?: { name: string; score: number }[]; why_not?: RejectedOption[] } | undefined;
  if (!data?.hotels?.length) return null;
  const rejected = data.why_not ?? [];
  return <section className="board-section"><div className="section-title"><div className="section-title-copy"><span className="section-eyebrow"><BadgeCheck size={14}/> ПРОЗРАЧНЫЙ ВЫБОР</span><h2>Почему именно они?</h2><p>Сравнение вариантов по твоим приоритетам</p></div></div>
    <div className="comparison"><div className="comparison-head"><span>Отель</span><span>Совпадение с запросом</span></div>{data.hotels.map((h, i) => <div className="comparison-line" key={i}><span>{h.name}</span><div className="score-bar"><i style={{ width: `${Math.min(100, Math.max(0, h.score * 100))}%` }}/></div><b>{Math.round(h.score * 100)}%</b></div>)}</div>
    {rejected.length > 0 && <div className="why-not">
      <h3><CircleAlert size={18}/> Почему не дешевле</h3>
      <p className="why-not-lead">Агент проверил варианты подешевле и отказался от них:</p>
      <ul>{rejected.map((item, i) => <li key={i}>
        <span className="why-not-kind">{item.kind === "hotel" ? <BedDouble size={16}/> : item.name.startsWith("Поезд") ? <TrainFront size={16}/> : <Plane size={16}/>}</span>
        <div><strong>{item.kind === "hotel" ? `«${item.name}»` : item.name}</strong> <span className="why-not-saving">−{money(item.saving)}</span><p>но {item.reason}</p></div>
      </li>)}</ul>
    </div>}
  </section>;
}

/** Итог проверки плана: блокирующие несостыковки, предупреждения или «всё в порядке». */
export function PlanStatusPanel({ health }: { health: PlanHealth }) {
  if (health.status === "none") return null;
  const issueLine = (issue: Issue, i: number) => <li key={`${issue.code}-${i}`}><span>{issue.message}</span>{issue.source === "interface" && <small title="Эту несостыковку нашла проверка интерфейса. Core пока её не ловит.">проверка интерфейса</small>}</li>;
  if (health.status === "ok") {
    return <section className="plan-status ok"><div className="plan-status-head"><CheckCircle2 size={22}/><div><strong>Проверка пройдена</strong><p>Даты, время прибытия, трансферы и бюджет совместимы.</p></div></div></section>;
  }
  const blocked = health.status === "blocked";
  return <section className={`plan-status ${blocked ? "blocked" : "warning"}`} role="status">
    <div className="plan-status-head">{blocked ? <CircleAlert size={22}/> : <ShieldAlert size={22}/>}<div>
      <strong>{blocked ? "В плане есть несостыковки" : "План собран, но есть что учесть"}</strong>
      <p>{blocked ? "Оформление недоступно, пока их не исправить. Напиши агенту, например: «давай другие даты» или «подбери другой транспорт»." : "Оформить можно, но сначала посмотри предупреждения."}</p>
    </div></div>
    {!!health.blocking.length && <ul className="plan-issues blocking">{health.blocking.map(issueLine)}</ul>}
    {!!health.warnings.length && <ul className="plan-issues">{health.warnings.map(issueLine)}</ul>}
  </section>;
}

type TimelineEvent = { event: string; departure_at?: string; arrival_at?: string; pickup_at?: string; date_from?: string; date_to?: string; checkin_from?: string; checkout_time?: string; duration_minutes?: number };
const timelineNames: Record<string, string> = { outbound: "Выезжаем в путешествие", arrival_transfer: "Трансфер до отеля", hotel_stay: "Заселение и отдых", departure_transfer: "Трансфер обратно", return: "Дорога домой" };
function timelineText(e: TimelineEvent): string {
  if (e.departure_at) return `${readableDate(e.departure_at)} → ${e.arrival_at ? readableDate(e.arrival_at) : "—"}`;
  if (e.pickup_at) return `${readableDate(e.pickup_at)} · ${e.duration_minutes ?? "—"} мин в пути`;
  if (e.date_from && e.date_to) {
    const times = [e.checkin_from && `заселение с ${e.checkin_from}`, e.checkout_time && `выезд до ${e.checkout_time}`].filter(Boolean).join(", ");
    return `${readableDay(e.date_from)} — ${readableDay(e.date_to, true)}${times ? ` · ${times}` : ""}`;
  }
  return "Этап путешествия";
}
export function Timeline({ blocks }: { blocks: UIBlock[] }) {
  const data = findBlock(blocks, "trip_timeline") as { events?: TimelineEvent[] } | undefined;
  if (!data?.events?.length) return null;
  return <section className="board-section" id="timeline"><div className="section-title"><div className="section-title-copy"><span className="section-eyebrow"><CalendarDays size={14}/> МАРШРУТ ПО ЧАСАМ</span><h2>План по шагам</h2><p>Транспорт, трансфер и заселение — в едином плане. Время везде местное.</p></div></div><div className="timeline">
    {data.events.map((e, i) => <div className="timeline-item" key={i}><span className="timeline-dot"/><div><strong>{timelineNames[e.event] || e.event}</strong><p>{timelineText(e)}</p></div></div>)}
  </div></section>;
}

const changedLabels: Record<string, string> = { destination_id: "Направление", hotel: "Отель", outbound: "Дорога туда", inbound: "Дорога обратно" };
export function PlanDiff({ blocks }: { blocks: UIBlock[] }) {
  const data = findBlock(blocks, "plan_diff") as { kept?: string[]; changed?: string[]; previous_total?: number | null; total?: number } | undefined;
  if (!data) return null;
  return <section className="board-section diff-panel"><div className="mini-heading"><div className="mini-icon"><BadgeCheck size={18}/></div><strong>План пересобран</strong></div>
    {!!data.kept?.length && <p><CheckCircle2 size={16}/> Сохранено: {data.kept.map(x => changedLabels[x] || x).join(", ")}</p>}
    {!!data.changed?.length && <p><ArrowRight size={16}/> Изменено: {data.changed.map(x => changedLabels[x] || x).join(", ")}</p>}
    {data.previous_total != null && data.total != null && <div className="diff-total"><span>Стоимость поездки</span><strong><s>{money(data.previous_total)}</s> {money(data.total)}</strong></div>}
  </section>;
}

/** Делаем технические названия позиций из Data читаемыми. */
function cartTitle(title: string): string {
  return title
    .replace(/^train:\s*/, "Поезд: ")
    .replace(/^flight:\s*/, "Перелёт: ")
    .replace(/Трансфер taxi/, "Трансфер на такси")
    .replace(/Трансфер bus/, "Трансфер на автобусе")
    .replace(/Трансфер shuttle/, "Трансфер на шаттле");
}

export function CartView({ blocks, onBook, busy, health }: { blocks: UIBlock[]; onBook: () => void; busy: boolean; health: PlanHealth }) {
  const data = findBlock(blocks, "cart") as { cart?: Cart } | undefined;
  if (!data?.cart) return null;
  const cart = data.cart;
  const blocked = health.status === "blocked";
  const pill = blocked
    ? <div className="safe-status blocked"><CircleAlert size={16}/> Есть несостыковки</div>
    : health.status === "warning"
      ? <div className="safe-status warning"><ShieldAlert size={16}/> Есть предупреждения</div>
      : <div className="safe-status"><CheckCircle2 size={16}/> План проверен</div>;
  return <section className="cart-panel" id="cart"><div className="cart-intro"><div><span className="section-eyebrow"><Wallet size={14}/> КОРЗИНА ПУТЕШЕСТВИЯ</span><h2>{blocked ? "Почти готово" : "Почти в отпуске!"}</h2><p>Все части поездки в одном месте</p></div>{pill}</div>
    <div className="cart-items">{cart.items.map((item) => <div className="cart-row" key={item.id}><span>{cartTitle(item.title)}</span><strong>{money(item.amount)}</strong></div>)}</div>
    <div className="cart-total"><div><span>Итого за поездку</span><strong>{money(cart.total)}</strong><small>{cart.budget_total ? `Бюджет ${money(cart.budget_total)}${cart.over_budget ? " · превышен" : ""}` : "Все цены демонстрационные"}</small></div>
      <button className="primary-button" disabled={blocked || busy} onClick={onBook} title={blocked ? "Сначала исправь несостыковки в плане" : undefined}>{blocked ? "Сначала исправь план" : "Оформить демо-бронь"}<ArrowRight size={18}/></button></div>
    <p className="helper-text"><Info size={14}/> {cart.note || "Синтетические цены. Реальной оплаты нет."}</p>
  </section>;
}

type TraceItem = { stage: string; tool?: string | null; outcome: string; reason: string };
const MAIN_STAGES = new Set(["search", "build_plan", "check_plan", "repair_plan"]);
function traceStep(item: TraceItem): { label: string; kind: "search" | "check" | "repair" | "issue" } {
  if (item.outcome === "clarification") return { label: "Нужно уточнение: направление не найдено в данных", kind: "issue" };
  if (item.stage === "search") {
    if (item.tool === "suggest_destinations") return { label: "Подбор направлений", kind: "search" };
    if (item.tool === "search_hotels") return { label: "Поиск отелей", kind: "search" };
    if (item.tool === "search_transport") return { label: "Поиск транспорта", kind: "search" };
    return { label: asText(item.reason) || "Поиск вариантов", kind: "search" };
  }
  if (item.stage === "build_plan") return { label: "Сборка варианта поездки", kind: "check" };
  if (item.stage === "check_plan") return { label: "Проверка состыковок и расчёт стоимости", kind: "check" };
  if (item.stage === "repair_plan") return item.outcome === "attempted"
    ? { label: asText(item.reason) ? `Исправление: ${asText(item.reason)}` : "Исправление: найдена замена, план перепроверен", kind: "repair" }
    : { label: "Исправление: подходящей альтернативы нет", kind: "issue" };
  return { label: asText(item.reason) || asText(item.stage), kind: "check" };
}
const stepIcon = { search: Search, check: ShieldCheck, repair: Wrench, issue: ShieldAlert };

/** Действия агента за последний запрос: главные шаги сразу, полный список по кнопке. */
export function TraceView({ blocks, health }: { blocks: UIBlock[]; health?: PlanHealth }) {
  const data = findBlock(blocks, "trace") as { items?: TraceItem[] } | undefined;
  const [open, setOpen] = useState(false);
  if (!data?.items?.length) return null;
  // Core копит trace за весь разговор; ход начинается с merge_request.
  const start = data.items.map((i) => i.stage).lastIndexOf("merge_request");
  const turn = start >= 0 ? data.items.slice(start) : data.items;
  const main = turn.filter((i) => MAIN_STAGES.has(i.stage) || i.outcome === "clarification").map(traceStep);
  const issues = health ? health.blocking.length + health.warnings.length : 0;
  if (issues) main.push({ label: `Найдено ${issues} ${nounForm(issues, "замечание", "замечания", "замечаний")} — подробности в блоке проверки`, kind: "issue" });
  const shown = main.slice(-5);
  return <section className="trace-wrapper">
    <div className="trace-summary"><span className="trace-title"><Sparkles size={17}/> Что сделал агент</span>
      <ol className="trace-steps">{shown.map((step, i) => { const Icon = stepIcon[step.kind]; return <li key={i} className={`trace-step ${step.kind}`}><Icon size={16}/>{step.label}</li>; })}</ol>
    </div>
    <button className="trace-trigger" aria-expanded={open} onClick={() => setOpen(!open)}><span>{open ? "Скрыть подробности" : `Показать все действия (${turn.length})`}</span><ChevronDown size={18} className={open ? "rotate" : ""}/></button>
    {open && <div className="trace-entries">{turn.map((item, index) => <div key={index}><span className="trace-dot"/><p><b>{item.tool || item.stage}</b> · {asText(item.reason)}</p></div>)}</div>}
  </section>;
}
