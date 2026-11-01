"use client";
import { useEffect, useMemo, useRef } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight, Check, CheckCheck, CircleAlert, Compass, Loader2, MapPinned, RefreshCw, RotateCcw, Send,
  ShieldCheck, Sparkles, Wallet, X,
} from "lucide-react";
import {
  Assumptions, CartView, Compare, DestinationCards, HotelCards,
  PlanDiff, PlanStatusPanel, Timeline, TraceView,
} from "@/components/block-renderer";
import { useTrip } from "@/components/trip-provider";
import { SearchBar } from "@/components/search-bar";
import { planHealth } from "@/lib/plan-health";
import { destinationLabel, type UIBlock } from "@/lib/types";

type View = "home" | "destinations" | "selection" | "checkout";
const pagePaths: Record<View, string> = {
  home: "/", destinations: "/destinations", selection: "/selection", checkout: "/checkout",
};
const suggestions = ["К морю на неделю", "Выходные без суеты", "С ребёнком до 100 000 ₽"];
const navItems: { id: View; label: string; icon: typeof Compass }[] = [
  { id: "home", label: "Главная", icon: Compass },
  { id: "destinations", label: "Направления", icon: MapPinned },
  { id: "selection", label: "Подбор поездки", icon: Sparkles },
  { id: "checkout", label: "Корзина", icon: Wallet },
];

function SyntheticNotice({ live }: { live: boolean }) {
  return <div className="v3-info"><Sparkles size={21}/><span>{live
    ? "Подбор выполняет AI-агент. Данные об отелях, транспорте и ценах демонстрационные."
    : "Демо-режим: заранее подготовленные примеры, агент не вызывается."}</span><span className="synthetic-badge">Синтетические данные</span></div>;
}

function Loading({ text }: { text: string }) {
  return <div className="v3-empty loading" aria-live="polite"><Loader2 size={44} className="spin"/><h2>{text}</h2><p>Обычно это занимает несколько секунд.</p></div>;
}

export function TravelApp({ view }: { view: View }) {
  const router = useRouter();
  const { mode, messages, blocks, tripState, text, setText, budget, setBudget,
    busy, status, error, booking, send, findDestinations, nextDestinations, prevDestinations, chooseDestination, applyBudget, book, closeBooking,
    retry, dismissError, reset, hasDemoPlan, city, destinationPages = [], destinationPage = 0 } = useTrip();
  const chatScrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => { const node = chatScrollRef.current; if (node) node.scrollTo({ top: node.scrollHeight, behavior: "smooth" }); }, [messages, status, error]);
  const health = useMemo(() => planHealth(blocks, tripState), [blocks, tripState]);
  const live = mode === "live";

  // Сравниваем с текущим адресом в момент вызова: после await замыкание может хранить старый view.
  function navigate(next: View) { if (window.location.pathname !== pagePaths[next]) router.push(pagePaths[next]); }
  function goBack() { const parent: Record<View, View> = { home: "home", destinations: "home", selection: "destinations", checkout: "selection" }; navigate(parent[view]); }
  function openResult(result: UIBlock[] | null) {
    if (!result) return;
    if (result.some(b => b.type === "option_cards" || b.type === "cart")) navigate("selection");
    else if (result.some(b => b.type === "destination_cards")) navigate("destinations");
  }
  async function submitAndOpen(input: string) {
    if (!input.trim() || busy) return;
    if (view === "home") navigate("destinations");
    openResult(await send(input));
  }
  async function newDestinations() {
    navigate("destinations");
    openResult(await findDestinations());
  }
  async function pickDestination(name: string) {
    navigate("selection");
    const result = await chooseDestination(name);
    if (result && !result.some(b => b.type === "option_cards")) openResult(result);
  }

  const currentCart = blocks.some(b => b.type === "cart");
  const hasDirections = blocks.some(b => b.type === "destination_cards");
  const hasHotels = blocks.some(b => b.type === "option_cards");
  const activeCity = destinationLabel(tripState?.request?.destination, blocks) || (hasDemoPlan ? city : null);
  const blocked = health.status === "blocked";

  const selectionCard = health.status === "blocked"
    ? { overline: "НУЖНО ВНИМАНИЕ", title: "В маршруте есть несостыковки", text: "Посмотри замечания выше и попроси агента поправить план. Корзину можно открыть, но оформление пока недоступно." }
    : health.status === "warning"
      ? { overline: "ПЛАН СОБРАН", title: "Маршрут готов, есть предупреждения", text: "Проверь замечания выше. Можно изменить бюджет или перейти к корзине." }
      : { overline: "ВСЁ СОБРАНО", title: "Твой маршрут готов!", text: "Проверка состыковок пройдена. Можно изменить бюджет или перейти к корзине." };

  return <main className="v3-shell">
    <header className="v3-header">
      <button className="v3-brand" onClick={() => navigate("home")} aria-label="На главную">
        <span className="v3-brand-mark">О<span>!</span></span>
        <span><strong>О!дин запрос</strong><small>Поездка начинается с идеи</small></span>
      </button>
      <nav className="v3-nav" aria-label="Основная навигация">{navItems.map(({id,label,icon:Icon}) =>
        <button key={id} className={view === id ? "selected" : ""} onClick={() => navigate(id)} aria-current={view === id ? "page" : undefined}><Icon size={19}/>{label}</button>)}</nav>
      <button className="v3-reset" onClick={() => { reset(); navigate("home"); }} disabled={busy}><RotateCcw size={18}/> Новая поездка</button>
    </header>

    <div className="v3-layout">
      <section className="v3-content" aria-label="Конструктор путешествий">
        {view !== "home" && <div className="v3-breadcrumb"><button onClick={goBack}><ArrowRight className="v3-arrow-back" size={18}/> Назад</button><span>О!дин запрос / {navItems.find(n => n.id === view)?.label}</span><button onClick={() => navigate("home")}>На главную</button></div>}

        {view === "home" && <>
          <div className="v3-hero">
            <div className="v3-hero-copy"><span className="v3-pill">✦ AI-путешествия без десятка вкладок</span>
              <h1>Твоя поездка.<br/><span>О!дин запрос.</span></h1>
              <p>Расскажи, куда хочется отправиться. Мы подберём направление, отели, дорогу и соберём путешествие целиком.</p>
              <button className="v3-main-cta" onClick={() => navigate("destinations")}>Начать путешествие <ArrowRight size={22}/></button>
              <div className="v3-hero-points"><span><Check size={18}/> Один запрос</span><span><Check size={18}/> Проверка деталей</span><span><Check size={18}/> Всё в одной корзине</span></div>
            </div>
            <div className="v3-hero-art" aria-hidden="true"><div className="v3-sun"/><div className="v3-photo-scene"><span>ПОЕХАЛИ!</span></div><div className="v3-floating-card"><span>✈</span><strong>Путешествие мечты</strong><small>маршрут собирает AI</small></div></div>
          </div>
          <SearchBar onDone={(cards) => navigate(cards ? "destinations" : "selection")}/>
          <div className="v3-section-heading"><div><span className="v3-overline">ВЫБИРАЙ ВПЕЧАТЛЕНИЯ</span><h2>С чего начнём?</h2><p>Начни с идеи или посмотри направления, которые агент уже подобрал.</p></div></div>
          <div className="v3-feature-row"><button className="v3-feature" disabled={busy} onClick={() => void submitAndOpen("Хочу на море, отдохнуть на пляже")}><span>🌊</span><strong>К морю</strong><small>Солнце, пляж и отдых</small><ArrowRight size={21}/></button><button className="v3-feature" disabled={busy} onClick={() => void submitAndOpen("Хочу интересный городской отдых на выходные")}><span>🏙️</span><strong>В город</strong><small>Еда, улицы, открытия</small><ArrowRight size={21}/></button><button className="v3-feature" disabled={busy} onClick={() => void submitAndOpen("Хочу спокойное путешествие на природу")}><span>⛰️</span><strong>На природу</strong><small>Перезагрузка и красивые виды</small><ArrowRight size={21}/></button></div>
          <div className="v3-next-card"><div><span className="v3-overline">ПРОДОЛЖИ ПЛАНИРОВАНИЕ</span><h3>{currentCart ? `Ваша поездка: ${activeCity || "маршрут"}` : "Готовы выбрать направление?"}</h3><p>{currentCart ? "Мы сохранили план. Вернитесь к отелям или откройте корзину." : "Откройте подборку и выберите вариант. Всё можно изменить позже."}</p></div><button onClick={() => navigate(currentCart ? "checkout" : "destinations")}>{currentCart ? "Открыть корзину" : "Смотреть направления"}<ArrowRight size={20}/></button></div>
        </>}

        {view === "destinations" && <><div className="v3-page-head"><span className="v3-overline">ШАГ 01 / ВДОХНОВЕНИЕ</span><h1>Куда отправимся?</h1><p>Не нужно заранее знать город. Посмотри идеи и расскажи агенту, чего хочется от поездки.</p></div>
          <SyntheticNotice live={live}/>
          <SearchBar onDone={(cards) => navigate(cards ? "destinations" : "selection")}/>
          {hasDirections
            ? <DestinationCards blocks={blocks} page={destinationPages[destinationPage]} pageIndex={destinationPage} pageCount={destinationPages.length}
                disabled={busy} onChoose={(name) => void pickDestination(name)}
                onMore={live ? () => void nextDestinations() : undefined} onPrev={prevDestinations}/>
            : busy
              ? <Loading text="Агент подбирает направления…"/>
              : activeCity
                ? <div className="v3-empty"><MapPinned size={48}/><h2>Сейчас в плане: {activeCity}</h2><p>Можно подобрать другие направления. Бюджет, гости и город вылета сохранятся.</p><div className="v3-empty-actions"><button onClick={() => void newDestinations()}>Подобрать другие направления <ArrowRight size={19}/></button><button className="secondary" onClick={() => navigate("selection")}>Вернуться к поездке</button></div></div>
                : <div className="v3-empty"><Compass size={48}/><h2>Попроси агента предложить направления</h2><p>Он найдёт варианты с учётом твоих пожеланий.</p><button onClick={() => void newDestinations()}>Найти направления <ArrowRight size={19}/></button></div>}
          {hasDirections && hasHotels && <div className="v3-next-card"><div><h3>План уже подготовлен</h3><p>Переходи к отелям, транспорту и сравнению.</p></div><button onClick={() => navigate("selection")}>К подбору <ArrowRight size={19}/></button></div>}
        </>}

        {view === "selection" && <><div className="v3-page-head"><span className="v3-overline">ШАГ 02 / ПОДБОР</span><h1>{activeCity ? `Собираем поездку: ${activeCity}` : "Твоя идеальная поездка"}</h1><p>Отель, дорога и трансфер — всё проверяем на совместимость, прежде чем перейти к оформлению.</p></div>
          <Assumptions blocks={blocks} tripState={tripState}/>
          {hasHotels
            ? <><PlanStatusPanel health={health}/><HotelCards blocks={blocks}/><Compare blocks={blocks}/><Timeline blocks={blocks}/><PlanDiff blocks={blocks}/><TraceView blocks={blocks} health={health}/></>
            : busy
              ? <Loading text="Агент собирает поездку…"/>
              : <div className="v3-empty"><Compass size={48}/><h2>Сначала выбери направление</h2><p>Или отправь запрос помощнику справа — агент начнёт подбор.</p><button onClick={() => navigate("destinations")}>Выбрать направление <ArrowRight size={19}/></button></div>}
          {(currentCart || health.status === "blocked") && <div className={`v3-next-card ${health.status}`}><div><span className="v3-overline">{selectionCard.overline}</span><h3>{selectionCard.title}</h3><p>{selectionCard.text}</p></div><button onClick={() => navigate("checkout")}>К корзине <ArrowRight size={20}/></button></div>}
        </>}

        {view === "checkout" && <><div className="v3-page-head"><span className="v3-overline">ШАГ 03 / ОФОРМЛЕНИЕ</span><h1>{!currentCart && !blocked ? "Корзина" : blocked ? "Почти готово к поездке" : "Всё готово к поездке!"}</h1><p>Проверь итоговую стоимость, маршрут и состав заказа. Бронирование и оплата демонстрационные.</p></div>
          {currentCart ? <><PlanStatusPanel health={health}/><Timeline blocks={blocks}/><section className="budget-panel"><div className="budget-heading"><div className="budget-heading-icon"><Wallet size={24}/></div><div><strong>Хочешь изменить бюджет?</strong><p>Агент пересоберёт план, не начиная всё заново.</p></div></div><div className="budget-controls"><label htmlFor="budget">Общий бюджет, ₽</label><div><input id="budget" inputMode="numeric" type="number" min="1" value={budget} onChange={(e) => setBudget(e.target.value)}/><button className="outline-button" onClick={() => void applyBudget()} disabled={busy}>Пересобрать <ArrowRight size={18}/></button></div></div></section><PlanDiff blocks={blocks}/><CartView blocks={blocks} busy={busy} health={health} onBook={() => void book()}/></> : blocked ? <><PlanStatusPanel health={health}/><Timeline blocks={blocks}/><div className="v3-empty"><Wallet size={48}/><h2>Стоимость появится после исправления плана</h2><p>Для маршрута с несостыковками Core не считает корзину. Вернись к подбору и попроси агента поправить даты или транспорт.</p><button onClick={() => navigate("selection")}>К подбору <ArrowRight size={19}/></button></div></> : <div className="v3-empty"><Wallet size={48}/><h2>Корзина пока пуста</h2><p>Выбери направление и собери поездку с AI-помощником.</p><button onClick={() => navigate("destinations")}>Посмотреть направления <ArrowRight size={19}/></button></div>}
        </>}
        <div className="v3-footer-note">О!дин запрос · прототип для хакатона · данные, цены и бронирование демонстрационные</div>
      </section>

      <aside className="v3-chat" aria-label="AI-помощник">
        <div className="v3-chat-header"><div className="v3-ai-icon"><Sparkles size={26}/></div><div><span>ТВОЙ ТРЕВЕЛ-АССИСТЕНТ</span><h2>Давай спланируем!</h2></div><span className="v3-online" title="Интерфейс готов"/></div>
        <div className="v3-mode"><button className={mode === "demo" ? "active" : ""} disabled={busy} onClick={() => reset("demo")}>Демо</button><button className={live ? "active" : ""} disabled={busy} onClick={() => reset("live")}>Настоящий агент</button></div>
        <div className="v3-chat-messages" ref={chatScrollRef}><div className="v3-chat-date">НАЧНЁМ ПУТЕШЕСТВИЕ?</div>
          {messages.map((m) => <div key={m.id} className={`v3-msg ${m.role}${m.tone ? ` ${m.tone}` : ""}`}><span>{m.role === "assistant" ? (m.tone === "notice" ? "!" : "✦") : ""}</span><div>{m.text}</div></div>)}
          {status && <div className="v3-progress"><span className="pulse-dot"/>{status}</div>}
          {error && <div className={`v3-error-card ${error.kind}`} role="alert">
            <div className="v3-error-head"><CircleAlert size={18}/><strong>{error.title}</strong><button className="v3-error-close" onClick={dismissError} aria-label="Скрыть ошибку"><X size={16}/></button></div>
            {error.detail && <p>{error.detail}</p>}
            <div className="v3-error-actions">
              {error.canRetry && <button onClick={() => void retry().then((result) => openResult(result || null))} disabled={busy}><RefreshCw size={15}/> Повторить</button>}
              {error.kind === "session" && <button onClick={() => { reset("live"); navigate("home"); }}><RotateCcw size={15}/> Начать заново</button>}
            </div>
          </div>}
        </div>
        <div className="v3-chat-bottom"><div className="v3-chat-prompts"><strong>ПОПРОБУЙ СПРОСИТЬ</strong>{suggestions.map((s) => <button key={s} disabled={busy} onClick={() => void submitAndOpen(s)}>{s}<ArrowRight size={17}/></button>)}</div><form onSubmit={(e) => { e.preventDefault(); void submitAndOpen(text); }} className="v3-input"><textarea rows={2} aria-label="Написать AI-помощнику" placeholder="Куда мечтаешь поехать?" value={text} onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void submitAndOpen(text); } }} disabled={busy}/><button type="submit" disabled={busy || !text.trim()} aria-label="Отправить"><Send size={23}/></button></form><div className="v3-privacy"><ShieldCheck size={17}/> {live ? "AI-агент · данные и цены демонстрационные" : "Демо-режим · тестовые данные, без реальных броней"}</div></div>
      </aside>
    </div>
    {booking && <div className="modal-backdrop" role="dialog" aria-modal="true" aria-label="Демо-бронирование"><div className="booking-modal"><button className="icon-button modal-close" onClick={closeBooking} aria-label="Закрыть"><X size={22}/></button><div className="success-icon"><CheckCheck size={38}/></div><h2>Поездка оформлена!</h2><p>{booking.message}</p><div className="demo-order-id"><span>{booking.source === "core" ? "Демо-заказ в Core" : "Демо-заказ"}</span><strong>{booking.id}</strong></div><button className="primary-button" onClick={() => { closeBooking(); navigate("checkout"); }}>Вернуться к поездке <ArrowRight size={20}/></button></div></div>}
  </main>;
}
