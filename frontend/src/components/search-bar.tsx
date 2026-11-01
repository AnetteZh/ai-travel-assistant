"use client";

import { useRef, useState } from "react";
import { ArrowLeftRight, ChevronDown, Minus, Plus, Search } from "lucide-react";
import { useTrip } from "@/components/trip-provider";
import type { TripPatch } from "@/lib/types";

/** Города каталога (совпадают с data/world.json). */
const CITIES = [
  "Москва", "Санкт-Петербург", "Сочи", "Казань", "Калининград", "Екатеринбург", "Новосибирск",
  "Иркутск", "Владивосток", "Мурманск", "Петрозаводск", "Минеральные Воды", "Махачкала", "Тюмень",
  "Нижний Новгород", "Ярославль", "Архангельск", "Пермь", "Уфа", "Краснодар",
];

const today = () => new Date().toISOString().slice(0, 10);
const shortDate = (value: string) => value.split("-").reverse().slice(0, 2).join(".");
const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];
const WEEKDAYS = ["вс", "пн", "вт", "ср", "чт", "пт", "сб"];
function prettyDate(value: string): { main: string; sub: string } {
  const [year, month, day] = value.split("-").map(Number);
  const weekday = WEEKDAYS[new Date(year, month - 1, day).getDay()];
  return { main: `${day} ${MONTHS[month - 1]}`, sub: weekday };
}

/** Поле даты: красивый текст сверху, системный календарь открывается по клику. */
function DateField({ label, value, min, placeholder, onChange }: { label: string; value: string; min: string; placeholder: string; onChange: (next: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const shown = value ? prettyDate(value) : null;
  function open() {
    const node = input.current;
    if (!node) return;
    try { node.showPicker(); } catch { node.focus(); }
  }
  return <div className="sb-seg sb-date" onClick={open}>
    <span className="sb-label">{label}</span>
    <span className={shown ? "sb-value" : "sb-value sb-placeholder"}>{shown ? <>{shown.main}<small>, {shown.sub}</small></> : placeholder}</span>
    {value && <button type="button" className="sb-clear" aria-label={`Сбросить: ${label}`} onClick={(e) => { e.stopPropagation(); onChange(""); }}>×</button>}
    <input ref={input} type="date" min={min} value={value} aria-label={label} onChange={(e) => onChange(e.target.value)}/>
  </div>;
}

/** Выпадающий список без системного вида: свой текст и стрелка. */
function SelectField({ label, value, options, onChange }: { label: string; value: string; options: { value: string; label: string }[]; onChange: (next: string) => void }) {
  const current = options.find(option => option.value === value)?.label ?? value;
  return <label className="sb-seg sb-select">
    <span className="sb-label">{label}</span>
    <span className={value ? "sb-value" : "sb-value sb-placeholder"}>{current}</span>
    <ChevronDown className="sb-chevron" size={16}/>
    <select value={value} aria-label={label} onChange={(e) => onChange(e.target.value)}>{options.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select>
  </label>;
}
const ageWord = (age: number) => (age % 10 === 1 && age % 100 !== 11 ? "год" : [2, 3, 4].includes(age % 10) && ![12, 13, 14].includes(age % 100) ? "года" : "лет");

function Counter({ label, value, min, max, onChange }: { label: string; value: number; min: number; max: number; onChange: (next: number) => void }) {
  return <div className="sb-counter">
    <span>{label}</span>
    <div>
      <button type="button" aria-label={`Меньше: ${label}`} disabled={value <= min} onClick={() => onChange(value - 1)}><Minus size={14}/></button>
      <strong>{value}</strong>
      <button type="button" aria-label={`Больше: ${label}`} disabled={value >= max} onClick={() => onChange(value + 1)}><Plus size={14}/></button>
    </div>
  </div>;
}

/** Строка поиска как у агрегаторов: откуда, куда, даты, гости, бюджет. */
export function SearchBar({ onDone }: { onDone?: (hasCards: boolean) => void }) {
  const { tripState, busy, searchWithFilters } = useTrip();
  const request = tripState?.request;
  const [origin, setOrigin] = useState(request?.origin || "Москва");
  const [destination, setDestination] = useState(request?.destination || "");
  const [dateFrom, setDateFrom] = useState(request?.date_from || "");
  const [dateTo, setDateTo] = useState(request?.date_to || "");
  const [adults, setAdults] = useState(request?.guests?.adults ?? 2);
  const [childAges, setChildAges] = useState<number[]>(request?.guests?.children_ages ?? []);
  const [pets, setPets] = useState(request?.guests?.pets ?? 0);
  const [budget, setBudget] = useState(request?.budget_total ? request.budget_total.toLocaleString("ru-RU") : "");
  const [guestsOpen, setGuestsOpen] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  function setChildren(count: number) {
    setChildAges(old => count > old.length ? [...old, ...Array(count - old.length).fill(7)] : old.slice(0, count));
  }

  const people = adults + childAges.length;
  const guestsLabel = `${people} ${people === 1 ? "гость" : people < 5 ? "гостя" : "гостей"}`;
  const guestsSub = [
    `${adults} взр.`,
    childAges.length ? `${childAges.length} дет.` : null,
    pets ? `${pets} питом.` : null,
  ].filter(Boolean).join(" · ");

  async function submit() {
    if (dateFrom && dateFrom < today()) return setProblem("Дата поездки уже прошла.");
    if (dateFrom && dateTo && dateTo <= dateFrom) return setProblem("Дата возвращения должна быть позже даты выезда.");
    if (!dateFrom && dateTo) return setProblem("Укажи и дату выезда.");
    const budgetValue = budget ? Number(budget.replace(/\D/g, "")) : null;
    if (budgetValue !== null && (!Number.isSafeInteger(budgetValue) || budgetValue <= 0)) return setProblem("Бюджет — целое число в рублях.");
    if (destination && destination === origin) return setProblem("Город вылета и направление совпадают.");
    setProblem(null);

    const patch: TripPatch = {
      origin,
      destination: destination || null,
      guests: { adults, children_ages: childAges, pets },
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
      ...(budgetValue ? { budget_total: budgetValue } : {}),
    };
    const parts = [
      `из ${origin} → ${destination || "любое направление"}`,
      dateFrom ? `${shortDate(dateFrom)}${dateTo ? `–${shortDate(dateTo)}` : ""}` : "даты гибкие",
      `взрослых: ${adults}`,
      childAges.length ? `дети: ${childAges.map(age => `${age} ${ageWord(age)}`).join(", ")}` : null,
      pets ? `питомцев: ${pets}` : null,
      budgetValue ? `бюджет ${budgetValue.toLocaleString("ru-RU")} ₽` : null,
    ].filter(Boolean);
    setGuestsOpen(false);
    const blocks = await searchWithFilters(patch, `Ищу: ${parts.join(", ")}`);
    if (blocks) onDone?.(blocks.some(block => block.type === "destination_cards"));
  }

  const cityOptions = CITIES.map(city => ({ value: city, label: city }));
  return <form className="search-bar" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <div className="sb-row">
      <SelectField label="Откуда" value={origin} options={cityOptions} onChange={setOrigin}/>
      <button type="button" className="sb-swap" aria-label="Поменять местами" disabled={!destination}
        onClick={() => { if (destination) { setOrigin(destination); setDestination(origin); } }}><ArrowLeftRight size={15}/></button>
      <SelectField label="Куда" value={destination}
        options={[{ value: "", label: "Куда угодно" }, ...cityOptions.filter(option => option.value !== origin)]}
        onChange={setDestination}/>
      <DateField label="Туда" value={dateFrom} min={today()} placeholder="Когда?" onChange={setDateFrom}/>
      <DateField label="Обратно" value={dateTo} min={dateFrom || today()} placeholder="Обратно?" onChange={setDateTo}/>
      <div className="sb-seg sb-guests">
        <button type="button" className="sb-guests-toggle" aria-expanded={guestsOpen} onClick={() => setGuestsOpen(open => !open)}>
          <span className="sb-label">Гости</span>
          <span className="sb-value">{guestsLabel}<small>{guestsSub}</small></span>
        </button>
        {guestsOpen && <div className="sb-popover">
          <Counter label="Взрослые" value={adults} min={1} max={8} onChange={setAdults}/>
          <Counter label="Дети" value={childAges.length} min={0} max={6} onChange={setChildren}/>
          {childAges.map((age, index) => <label className="sb-child" key={index}>
            <span>Возраст ребёнка {index + 1}</span>
            <select value={age} onChange={(e) => setChildAges(old => old.map((item, i) => i === index ? Number(e.target.value) : item))}>
              {Array.from({ length: 18 }, (_, value) => <option key={value} value={value}>{value} {ageWord(value)}</option>)}
            </select>
          </label>)}
          <Counter label="Питомцы" value={pets} min={0} max={3} onChange={setPets}/>
          <button type="button" className="sb-done" onClick={() => setGuestsOpen(false)}>Готово</button>
        </div>}
      </div>
      <label className="sb-seg sb-budget">
        <span className="sb-label">Бюджет</span>
        <span className="sb-budget-input">
          <input inputMode="numeric" placeholder="Не важен" value={budget}
            onChange={(e) => { const digits = e.target.value.replace(/\D/g, ""); setBudget(digits ? Number(digits).toLocaleString("ru-RU") : ""); }}/>
          {budget && <em>₽</em>}
        </span>
      </label>
      <button type="submit" className="sb-submit" disabled={busy}><Search size={20}/><span>Найти</span></button>
    </div>
    {problem && <p className="sb-problem" role="alert">{problem}</p>}
  </form>;
}
