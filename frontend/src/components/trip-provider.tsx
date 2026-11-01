"use client";

import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { demoDestinations, demoPlan, demoPriceByCity, demoSuggestion } from "@/lib/mock";
import { planHealth } from "@/lib/plan-health";
import { readSse } from "@/lib/sse";
import {
  findBlock, money,
  type AgentError, type Booking, type ChatMessage, type DestinationOption, type ServerEvent, type TripPatch, type TripState, type UIBlock,
} from "@/lib/types";

type Mode = "demo" | "live";
const greeting: ChatMessage = { id: "welcome", role: "assistant", text: "Привет! Расскажи, какой отдых тебе хочется. Я найду направления, сравню отели и соберу поездку целиком 💚" };

/** Одна «страница» подборки направлений: то, что пришло в блоке destination_cards. */
export type DestinationPage = { items: DestinationOption[]; budget_total?: number | null; nothing_in_budget?: boolean };

type Saved = {
  mode: Mode; messages: ChatMessage[]; blocks: UIBlock[]; tripState: TripState | null;
  conversationId: string | null; hasDemoPlan: boolean; city: string; budget: string;
  /** Все подборки, полученные кнопкой «Показать другие»: по ним можно листать назад и вперёд. */
  destinationPages: DestinationPage[]; destinationPage: number;
};
const initial: Saved = {
  mode: "demo", messages: [greeting], blocks: demoSuggestion().blocks, tripState: demoSuggestion().state,
  conversationId: null, hasDemoPlan: false, city: "Сочи", budget: "80000",
  destinationPages: [], destinationPage: 0,
};
const pageKey = (page: DestinationPage | undefined) => page?.items.map(item => item.destination.id).join("|") ?? "";
// v3.2: новая структура сохранённого состояния (бронь больше не хранится в снимке).
const STORAGE_KEY = "odin-zapros-v3.2-session";

type TripContext = Saved & {
  text: string; setText: (s: string) => void; setBudget: (s: string) => void;
  busy: boolean; status: string | null; error: AgentError | null; booking: Booking | null;
  /** Отправить сообщение агенту. Возвращает новые блоки или null, если запрос не удался. */
  send: (message: string) => Promise<UIBlock[] | null>;
  /** Новый поиск направлений. Остальные условия поездки (бюджет, гости, город вылета) сохраняются. */
  findDestinations: () => Promise<UIBlock[] | null>;
  /** Следующая подборка: из уже показанных, а если их нет — новая у агента. */
  nextDestinations: () => Promise<UIBlock[] | null>;
  /** Предыдущая подборка, без запроса к агенту. */
  prevDestinations: () => void;
  /** Поиск по фильтрам (откуда, куда, даты, гости, бюджет): условия уходят в Core патчем, без разбора текста. */
  searchWithFilters: (patch: TripPatch, summary: string) => Promise<UIBlock[] | null>;
  chooseDestination: (name: string) => Promise<UIBlock[] | null>;
  applyBudget: () => Promise<void>;
  book: () => Promise<void>;
  closeBooking: () => void;
  /** Повторить последнее неудачное действие. Возвращает блоки, если повтор удался. */
  retry: () => Promise<UIBlock[] | void | null>;
  dismissError: () => void;
  reset: (mode?: Mode) => void;
};
const Context = createContext<TripContext | null>(null);

/** Ошибка с уже подготовленным текстом для пользователя. */
class AgentFailure extends Error {
  constructor(public info: AgentError) { super(info.title); }
}

const NETWORK_ERROR: AgentError = {
  kind: "network", title: "Нет связи с сервером интерфейса",
  detail: "Проверь, что в папке frontend запущен npm run dev, и повтори запрос.", canRetry: true,
};

function errorFromCoreEvent(code: string, message: string): AgentError {
  if (/gigachat|openai|модел|провайдер/i.test(message)) {
    return { kind: "llm", title: "Языковая модель не ответила", detail: message, canRetry: true };
  }
  return { kind: "core", title: code === "INTERNAL_ERROR" ? "Внутренняя ошибка Core" : "Core не смог обработать запрос", detail: message, canRetry: true };
}

async function errorFromResponse(response: Response, scope: "chat" | "patch" | "book"): Promise<AgentError> {
  let detail = "";
  try {
    const body = await response.clone().json() as { error?: string; detail?: unknown };
    if (typeof body.error === "string") detail = body.error;
    else if (typeof body.detail === "string") detail = body.detail;
    else if (Array.isArray(body.detail)) detail = body.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).slice(0, 2).join("; ");
  } catch {
    detail = (await response.text().catch(() => "")).slice(0, 300);
  }
  if (response.status === 502 || response.status === 503 || response.status === 504) {
    return { kind: "network", title: "Core недоступен", detail: detail || "Проверь, что FastAPI запущен и CORE_API_URL в frontend/.env.local указывает на него.", canRetry: true };
  }
  if (response.status === 404 && scope !== "chat") {
    return { kind: "session", title: "Поездка не найдена на сервере", detail: "Скорее всего, Core перезапускался: он хранит поездки в памяти. Начни новую поездку.", canRetry: false };
  }
  if (response.status === 422) {
    return { kind: "input", title: "Core не принял данные", detail: detail || `HTTP ${response.status}`, canRetry: false };
  }
  return { kind: "core", title: "Core вернул ошибку", detail: detail || `HTTP ${response.status}`, canRetry: true };
}

function toAgentError(cause: unknown): AgentError {
  if (cause instanceof AgentFailure) return cause.info;
  if (cause instanceof TypeError) return NETWORK_ERROR;
  return { kind: "core", title: "Что-то пошло не так", detail: cause instanceof Error ? cause.message : undefined, canRetry: true };
}

const newId = () => crypto.randomUUID();
const assistant = (text: string, tone?: "notice"): ChatMessage => ({ id: newId(), role: "assistant", text, ...(tone ? { tone } : {}) });
const messageText = (blocks: UIBlock[]) => {
  const text = findBlock(blocks, "message")?.text;
  return typeof text === "string" ? text : null;
};

export function TripProvider({ children }: { children: ReactNode }) {
  const [saved, setSaved] = useState<Saved>(initial);
  const [hydrated, setHydrated] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [error, setError] = useState<AgentError | null>(null);
  const [booking, setBooking] = useState<Booking | null>(null);
  const locked = useRef(false);
  const stateRef = useRef(saved);
  const lastAction = useRef<{ status: string; run: () => Promise<UIBlock[] | void> } | null>(null);
  const restoredText = useRef<string | null>(null);

  useEffect(() => { stateRef.current = saved; }, [saved]);

  useEffect(() => {
    try {
      const value = sessionStorage.getItem(STORAGE_KEY);
      if (value) {
        const parsed = JSON.parse(value) as Partial<Saved>;
        if ((parsed.mode === "demo" || parsed.mode === "live") && Array.isArray(parsed.messages) && Array.isArray(parsed.blocks)) {
          const restored = { ...initial, ...parsed };
          stateRef.current = restored;
          // Снимок доступен только в браузере, поэтому восстанавливаем его после монтирования.
          // eslint-disable-next-line react-hooks/set-state-in-effect
          setSaved(restored);
        }
      }
    } catch { /* повреждённый снимок: начинаем с чистого демо */ }
    setHydrated(true);
  }, []);
  useEffect(() => {
    if (!hydrated) return;
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(saved)); } catch { /* приватный режим браузера */ }
  }, [saved, hydrated]);

  function update(patch: Partial<Saved>) { setSaved(previous => ({ ...previous, ...patch })); }

  // Новая подборка направлений: после «Показать другие» добавляем страницу, иначе начинаем список заново.
  const wantsMore = useRef(false);
  useEffect(() => {
    const data = findBlock(saved.blocks, "destination_cards") as DestinationPage | undefined;
    if (!data?.items?.length) return;
    const pages = saved.destinationPages ?? [];
    const key = pageKey(data);
    const known = pages.findIndex(page => pageKey(page) === key);
    if (known >= 0) return;
    const more = wantsMore.current;
    wantsMore.current = false;
    // Подборка приходит из ответа агента, а не из действий пользователя на странице.
    setSaved(old => more
      ? { ...old, destinationPages: [...(old.destinationPages ?? []), data], destinationPage: (old.destinationPages ?? []).length }
      : { ...old, destinationPages: [data], destinationPage: 0 });
  }, [saved.blocks, saved.destinationPages]);

  async function nextDestinations() {
    const { destinationPages: pages = [], destinationPage: index = 0 } = stateRef.current;
    if (index < pages.length - 1) { update({ destinationPage: index + 1 }); return null; }
    wantsMore.current = true;
    const result = await send("Покажи другие направления");
    if (!result || !result.some(block => block.type === "destination_cards")) wantsMore.current = false;
    return result;
  }

  function prevDestinations() {
    const index = stateRef.current.destinationPage ?? 0;
    if (index > 0) update({ destinationPage: index - 1 });
  }
  function addMessages(...items: ChatMessage[]) { setSaved(old => ({ ...old, messages: [...old.messages, ...items] })); }

  /** Общая обёртка: блокировка, статус, ошибка и возможность повторить последнее действие. */
  async function perform<T extends UIBlock[] | void>(statusText: string, run: () => Promise<T>): Promise<T | null> {
    if (locked.current) return null;
    locked.current = true; setBusy(true); setError(null); setStatus(statusText);
    lastAction.current = { status: statusText, run };
    try {
      const result = await run();
      lastAction.current = null;
      return result;
    } catch (cause) {
      setError(toAgentError(cause));
      return null;
    } finally { locked.current = false; setBusy(false); setStatus(null); }
  }

  /** Применить ответ Core. Блоки заменяются только после успешного завершения запроса. */
  function commitLive(blocks: UIBlock[], state: TripState, conversationId: string) {
    const previousId = stateRef.current.conversationId;
    const reply = messageText(blocks);
    const notices = previousId && previousId !== conversationId
      ? [assistant("Core начал новую сессию: сервер перезапускался, и прежние условия поездки на нём не сохранились. Если чего-то не хватает, напиши условия ещё раз.", "notice")]
      : [];
    setSaved(old => ({
      ...old, conversationId, tripState: state, blocks,
      messages: [...old.messages, ...notices, assistant(reply ?? "Готово! Смотри варианты в конструкторе.")],
    }));
    return blocks;
  }

  async function liveChat(message: string, conversationId: string | null): Promise<UIBlock[]> {
    const response = await fetch("/api/agent/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, conversation_id: conversationId }),
    });
    if (!response.ok) throw new AgentFailure(await errorFromResponse(response, "chat"));
    const upcoming: UIBlock[] = [];
    let completed: { conversation_id: string; state: TripState } | null = null;
    let streamError: AgentError | null = null;
    try {
      await readSse(response, (event: ServerEvent) => {
        if (event.event === "status") setStatus(event.data.message);
        if (event.event === "error") streamError = errorFromCoreEvent(event.data.code, event.data.message);
        if (event.event === "block") upcoming.push(event.data.block);
        if (event.event === "complete") completed = event.data;
      });
    } catch (cause) {
      if (cause instanceof TypeError) throw new AgentFailure({ kind: "stream", title: "Соединение оборвалось во время ответа", detail: "План не изменился. Повтори запрос.", canRetry: true });
      throw new AgentFailure({ kind: "stream", title: "Не удалось прочитать ответ Core", detail: cause instanceof Error ? cause.message : undefined, canRetry: true });
    }
    const failed = streamError as AgentError | null;
    if (failed) throw new AgentFailure(failed);
    const done = completed as { conversation_id: string; state: TripState } | null;
    if (!done) throw new AgentFailure({ kind: "stream", title: "Ответ Core оборвался", detail: "Поток закрылся без подтверждения. План не изменился, повтори запрос.", canRetry: true });
    return commitLive(done.state.blocks?.length ? done.state.blocks : upcoming, done.state, done.conversation_id);
  }

  async function livePatch(patch: TripPatch): Promise<UIBlock[]> {
    const conversationId = stateRef.current.conversationId;
    if (!conversationId) throw new AgentFailure({ kind: "session", title: "Поездка ещё не создана", detail: "Сначала напиши агенту в чат.", canRetry: false });
    const response = await fetch(`/api/agent/trips/${encodeURIComponent(conversationId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ patch }),
    });
    if (!response.ok) throw new AgentFailure(await errorFromResponse(response, "patch"));
    const payload = await response.json() as { conversation_id: string; state: TripState; blocks: UIBlock[] };
    return commitLive(payload.blocks, payload.state, payload.conversation_id ?? conversationId);
  }

  /** PATCH, а если сессия на сервере потеряна — новый разговор с тем же смыслом. */
  async function patchOrRestart(patch: TripPatch, fallbackMessage: string): Promise<UIBlock[]> {
    try {
      return await livePatch(patch);
    } catch (cause) {
      if (!(cause instanceof AgentFailure) || cause.info.kind !== "session") throw cause;
      stateRef.current = { ...stateRef.current, conversationId: null };
      update({ conversationId: null });
      addMessages(assistant("Сессия на сервере не найдена (Core перезапускался). Начинаю новый поиск, условия поездки при необходимости уточни в чате.", "notice"));
      return liveChat(fallbackMessage, null);
    }
  }

  function demoReply(message: string): UIBlock[] {
    const before = stateRef.current;
    const foundCity = demoDestinations.map(item => item.destination.name).find(city => message.toLocaleLowerCase("ru").includes(city.toLocaleLowerCase("ru")));
    const city = foundCity || before.city;
    const cheaper = /дешевле|уменьши|меньше|бюджет/i.test(message);
    const shouldSuggest = !before.hasDemoPlan && !foundCity && !/выбираю|дешевле|уменьши|меньше|бюджет/i.test(message);
    const result = shouldSuggest ? demoSuggestion() : demoPlan(cheaper ? Math.max(45000, (demoPriceByCity[city] ?? 82400) - 13000) : (demoPriceByCity[city] ?? 82400), cheaper, city);
    setSaved(old => ({ ...old, city, blocks: result.blocks, tripState: result.state, hasDemoPlan: Boolean(result.state.cart),
      messages: [...old.messages, assistant(messageText(result.blocks) ?? "Поездка обновлена")] }));
    return result.blocks;
  }

  async function send(raw: string) {
    const message = raw.trim();
    if (!message || locked.current) return null;
    setText(""); setBooking(null);
    const mode = stateRef.current.mode;
    // conversationId читаем в момент выполнения: при «Повторить» он мог уже обновиться.
    const result = await withUserMessage(message, mode === "demo" ? "Подбираю варианты…" : "Агент анализирует запрос…",
      async () => mode === "demo" ? demoReply(message) : liveChat(message, stateRef.current.conversationId));
    if (result === null) { restoredText.current = message; setText(current => current || message); }
    return result;
  }

  /**
   * Сообщение пользователя остаётся в истории, только если запрос удался. При ошибке оно
   * убирается (текст возвращается в поле ввода), а «Повторить» добавит его снова. Так в чате
   * не копятся одинаковые неотправленные сообщения.
   */
  async function withUserMessage(text: string, statusText: string, run: () => Promise<UIBlock[]>) {
    const userMessage: ChatMessage = { id: newId(), role: "user", text };
    return perform(statusText, async () => {
      setSaved(old => old.messages.some(m => m.id === userMessage.id) ? old : { ...old, messages: [...old.messages, userMessage] });
      try {
        return await run();
      } catch (cause) {
        setSaved(old => ({ ...old, messages: old.messages.filter(m => m.id !== userMessage.id) }));
        throw cause;
      }
    });
  }

  async function findDestinations() {
    if (locked.current) return null;
    setBooking(null);
    const { mode, conversationId, tripState } = stateRef.current;
    const request = "Предложи направления для отдыха";
    if (mode === "demo") {
      return withUserMessage("Покажи другие направления", "Подбираю направления…", async () => {
        const result = demoSuggestion();
        setSaved(old => ({ ...old, blocks: result.blocks, tripState: result.state, hasDemoPlan: false,
          messages: [...old.messages, assistant(messageText(result.blocks) ?? "Вот направления")] }));
        return result.blocks;
      });
    }
    const hasDestination = Boolean(tripState?.request?.destination);
    // Если направление уже выбрано, обычное сообщение Core поймёт как продолжение поездки
    // в тот же город. Поэтому явно сбрасываем destination через PATCH, остальные условия остаются.
    return withUserMessage(hasDestination ? "Хочу посмотреть другие направления" : request, "Подбираю направления…", () => hasDestination && conversationId
      ? patchOrRestart({ destination: null }, request)
      : liveChat(request, conversationId));
  }

  async function chooseDestination(name: string) {
    if (locked.current) return null;
    setBooking(null);
    const { mode, conversationId } = stateRef.current;
    const phrase = `Выбираю ${name}. Покажи поездку и подходящие отели.`;
    if (mode === "demo") return send(phrase);
    // Выбор карточки — точное действие, поэтому передаём его патчем, без разбора текста моделью.
    return withUserMessage(`Выбираю ${name}`, `Собираю поездку: ${name}…`, () => conversationId
      ? patchOrRestart({ destination: name }, phrase)
      : liveChat(phrase, null));
  }

  async function searchWithFilters(patch: TripPatch, summary: string) {
    if (locked.current) return null;
    setBooking(null);
    const { mode, conversationId } = stateRef.current;
    if (mode === "demo") {
      return withUserMessage(summary, "Подбираю направления…", async () => {
        const result = demoSuggestion();
        setSaved(old => ({ ...old, blocks: result.blocks, tripState: result.state, hasDemoPlan: false,
          messages: [...old.messages, assistant("В демо-режиме фильтры не применяются: показываю заготовленные примеры. Переключись на «Настоящий агент», чтобы искать по датам и гостям.", "notice")] }));
        return result.blocks;
      });
    }
    return withUserMessage(summary, "Подбираю варианты…", async () => {
      if (conversationId) return patchOrRestart(patch, summary);
      const response = await fetch("/api/agent/trips", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ patch }),
      });
      if (!response.ok) throw new AgentFailure(await errorFromResponse(response, "patch"));
      const payload = await response.json() as { conversation_id: string; state: TripState; blocks: UIBlock[] };
      return commitLive(payload.blocks, payload.state, payload.conversation_id);
    });
  }

  async function applyBudget() {
    const before = stateRef.current;
    const value = Number(before.budget);
    if (!Number.isSafeInteger(value) || value <= 0) {
      setError({ kind: "input", title: "Введи положительный бюджет в рублях", canRetry: false });
      return;
    }
    setBooking(null);
    await perform("Пересобираю поездку…", async () => {
      if (before.mode === "demo") {
        const result = demoPlan(Math.min(value, demoPriceByCity[before.city] ?? 82400), true, before.city, value);
        setSaved(old => ({ ...old, blocks: result.blocks, tripState: result.state, hasDemoPlan: true,
          messages: [...old.messages, assistant(`Показала демонстрационную пересборку с бюджетом ${money(value)}.`)] }));
        return result.blocks;
      }
      return livePatch({ budget_total: value });
    });
  }

  async function book() {
    const { mode, conversationId, blocks, tripState } = stateRef.current;
    if (planHealth(blocks, tripState).status === "blocked") {
      setError({ kind: "input", title: "В плане есть несостыковки", detail: "Оформление недоступно, пока их не исправить.", canRetry: false });
      return;
    }
    await perform("Оформляю демо-бронь…", async () => {
      const interfaceBooking = (note: string): Booking => ({
        id: `DEMO-UI-${newId().slice(0, 4).toUpperCase()}`, source: "interface", message: note,
      });
      if (mode === "demo" || !conversationId) {
        setBooking(interfaceBooking("Это демонстрационная бронь. Реальной оплаты и подтверждения от отеля нет."));
        return;
      }
      const response = await fetch(`/api/agent/trips/${encodeURIComponent(conversationId)}/book`, { method: "POST" });
      if (response.ok) {
        const payload = await response.json() as { booking_id?: string; message?: string };
        setBooking({ id: payload.booking_id ?? "DEMO", source: "core", message: payload.message ?? "Демонстрационная бронь оформлена в Core. Реальной оплаты нет." });
        return;
      }
      const body = await response.clone().json().catch(() => ({})) as { detail?: unknown };
      if (response.status === 404 && body.detail === "Not Found") {
        // В Core ещё нет ручки бронирования: оформляем имитацию на стороне интерфейса и честно это пишем.
        setBooking(interfaceBooking("Core пока не оформляет брони, поэтому заказ имитирован в интерфейсе. Реальной оплаты и подтверждения от отеля нет."));
        return;
      }
      throw new AgentFailure(await errorFromResponse(response, "book"));
    });
  }

  async function retry() {
    const action = lastAction.current;
    if (!action) return null;
    const result = await perform(action.status, action.run);
    // После успешного повтора убираем из поля ввода текст, который вернули туда при ошибке.
    if (result !== null && restoredText.current) {
      const stale = restoredText.current;
      setText(current => current === stale ? "" : current);
      restoredText.current = null;
    }
    return result ?? null;
  }

  function reset(mode: Mode = stateRef.current.mode) {
    if (locked.current) return;
    lastAction.current = null;
    update({ ...initial, mode, blocks: mode === "demo" ? demoSuggestion().blocks : [], tripState: mode === "demo" ? demoSuggestion().state : null });
    setError(null); setText(""); setStatus(null); setBooking(null);
  }

  return <Context.Provider value={{
    ...saved, text, setText, setBudget: (budget) => update({ budget }), busy, status, error, booking,
    send, findDestinations, nextDestinations, prevDestinations, searchWithFilters, chooseDestination, applyBudget, book, closeBooking: () => setBooking(null),
    retry, dismissError: () => setError(null), reset,
  }}>{children}</Context.Provider>;
}

export function useTrip() {
  const context = useContext(Context);
  if (!context) throw new Error("TripProvider missing");
  return context;
}
