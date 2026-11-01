import type { Cart, DestinationOption, HotelOption, TripState, UIBlock } from "./types";

/* UI FIXTURES ONLY. These are illustrative values, NOT a live result from Core/Data. */
const dateAdd = (n: number) => {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
};

const destination = (name: string, price: number, temp: number, why: string): DestinationOption => ({
  destination: { id: `fixture-${name}`, name, region: "Россия", tags: ["Отдых", "Маршрут"], is_synthetic: true },
  estimated_trip_price: price,
  fastest_travel_minutes: name === "Сочи" ? 240 : 150,
  weather: { temperature_min_c: temp - 5, temperature_max_c: temp, source: "UI fixture", is_synthetic: true },
  why_it_fits: why,
  pros: ["Есть варианты размещения", "Можно сравнить дорогу"],
  cons: ["Цены и погода здесь демонстрационные"],
});

// Шесть ИЛЛЮСТРАТИВНЫХ направлений. В реальном режиме Core вернет свою подборку из Data.
export const demoDestinations = [
  destination("Сочи", 82400, 19, "Море, горы и прогулки по набережной"),
  destination("Калининград", 74100, 14, "Балтийская атмосфера, кухня и уютные прогулки"),
  destination("Казань", 67300, 10, "Архитектура, гастрономия и городские открытия"),
  destination("Санкт-Петербург", 79200, 9, "Музеи, каналы и красивые городские маршруты"),
  destination("Минеральные Воды", 71800, 16, "Кавказские горы, природа и спокойный отдых"),
  destination("Петрозаводск", 68900, 8, "Озёра, леса и путешествие без суеты"),
];
export const demoPriceByCity: Record<string, number> = Object.fromEntries(
  demoDestinations.map(item => [item.destination.name, item.estimated_trip_price]),
);

const hotels: HotelOption[] = [
  { hotel: { id: "fixture-h1", name: "Отель Сочи Комфорт", city: "Сочи", stars: 4, rating: 4.6, price_per_night: 8400, breakfast_included: true, has_pets_allowed: false, family_friendly: true, cancellation_policy: "free_until_3_days", is_synthetic: true }, estimated_lodging_total: 58800, score: 0.91, why_it_fits: "Завтрак включён, подходит для спокойного отдыха", pros: ["Завтрак включён", "Гибкая отмена"], cons: ["Питомцы не разрешены"] },
  { hotel: { id: "fixture-h2", name: "Гостиница Сочи Премиум", city: "Сочи", stars: 3, rating: 4.3, price_per_night: 4500, breakfast_included: false, has_pets_allowed: true, family_friendly: true, cancellation_policy: "partial", is_synthetic: true }, estimated_lodging_total: 31500, score: 0.84, why_it_fits: "Экономичнее, можно с питомцем", pros: ["Ниже стоимость", "Разрешены питомцы"], cons: ["Завтрак не включён"] },
  { hotel: { id: "fixture-h3", name: "Сочи Гранд Резорт", city: "Сочи", stars: 5, rating: 4.8, price_per_night: 12500, breakfast_included: true, has_pets_allowed: false, family_friendly: true, cancellation_policy: "free_until_7_days", is_synthetic: true }, estimated_lodging_total: 87500, score: 0.88, why_it_fits: "Больше комфорта и гибкая отмена", pros: ["Высокий рейтинг", "Завтрак включён", "Бесплатная отмена до 7 дней"], cons: ["Выше стоимость", "Питомцы не разрешены"] },
  { hotel: { id: "fixture-h4", name: "Сочи Сити Лайт", city: "Сочи", stars: 3, rating: 4.1, price_per_night: 3900, breakfast_included: false, has_pets_allowed: true, family_friendly: false, cancellation_policy: "non_refundable", is_synthetic: true }, estimated_lodging_total: 27300, score: 0.76, why_it_fits: "Бюджетный вариант для активного путешествия", pros: ["Самая низкая цена в примере", "Разрешены питомцы"], cons: ["Тариф невозвратный", "Без завтрака"] },
];

function cart(total = 82400, city = "Сочи", budgetTotal = 100000): Cart {
  return {
    id: "fixture-cart", nights: 7, currency: "RUB", total,
    subtotal: total, is_synthetic: true, budget_total: budgetTotal, over_budget: total > budgetTotal,
    note: "Тестовая корзина UI. Цены демонстрационные, реальные брони и оплаты нет.",
    items: [
      { id: "fixture-hotel", item_type: "hotel", title: `Отель ${city} · 7 ночей`, amount: total - 23600, is_synthetic: true },
      { id: "fixture-flight", item_type: "transport", title: "Перелёт туда и обратно", amount: 20400, is_synthetic: true },
      { id: "fixture-transfer", item_type: "transfer", title: "Трансфер до отеля", amount: 3200, is_synthetic: true },
    ],
  };
}

const message = (text: string): UIBlock => ({ type: "message", data: { text } });
const trace = (): UIBlock => ({ type: "trace", data: { items: [
  { stage: "search", outcome: "success", reason: "Подобраны демонстрационные варианты.", tool: "fixture_search" },
  { stage: "check_plan", outcome: "success", reason: "В живом режиме проверку выполнит Core.", tool: null },
] } });

export function demoSuggestion() {
  const blocks: UIBlock[] = [
    message("Подобрала шесть идей для путешествия. Выберите направление, и я покажу пример собранной поездки."),
    { type: "assumptions", data: { items: ["Для демонстрации считаем 2 взрослых и 7 ночей.", "Все варианты ниже — UI-фикстуры."] } },
    { type: "destination_cards", data: { items: demoDestinations } },
    trace(),
  ];
  return { blocks, state: { version: 1, stage: "destinations", request: { budget_total: 100000 } } as TripState };
}

export function demoPlan(total = 82400, changed = false, city = "Сочи", budgetTotal = 100000) {
  const newCart = cart(total, city, budgetTotal);
  const displayedHotels = hotels.map((option) => ({ ...option, hotel: { ...option.hotel, city, name: option.hotel.name.replaceAll("Сочи", city) } }));
  const blocks: UIBlock[] = [
    message(changed ? "Пересчитала пример поездки для нового бюджета. В реальном режиме это делает Core." : `Собрала пример поездки в ${city}. Можно сравнить отели и посмотреть стыковки.`),
    { type: "assumptions", data: { items: ["Для демонстрации: 2 взрослых, 7 ночей", "Цены и доступность синтетические"] } },
    { type: "destination_cards", data: { items: demoDestinations } },
    { type: "option_cards", data: { hotels: displayedHotels, transport_outbound: [
      { id: "fixture-t1", origin_city: "Москва", destination_city: city, transport_type: "flight", departure_at: `${dateAdd(14)}T09:30:00+03:00`, arrival_at: `${dateAdd(14)}T13:30:00+03:00`, duration_minutes: 240, stops: 0, price: 10200, score: .9 },
    ], transport_return: [
      { id: "fixture-t2", origin_city: city, destination_city: "Москва", transport_type: "flight", departure_at: `${dateAdd(21)}T17:00:00+03:00`, arrival_at: `${dateAdd(21)}T21:00:00+03:00`, duration_minutes: 240, stops: 0, price: 10200, score: .9 },
    ] } },
    { type: "comparison", data: { hotels: displayedHotels.map((h) => ({ name: h.hotel.name, score: h.score, reason_codes: ["FITS_PREFERENCES"] })), weights: { price: 0.35, travel_time: 0.2, cancellation: 0.2, location: 0.15, preferences: 0.1 }, issues: [] } },
    { type: "trip_timeline", data: { events: [
      { event: "outbound", departure_at: `${dateAdd(14)}T09:30:00+03:00`, arrival_at: `${dateAdd(14)}T13:30:00+03:00` },
      { event: "arrival_transfer", pickup_at: `${dateAdd(14)}T14:00:00+03:00`, duration_minutes: 40 },
      { event: "hotel_stay", date_from: dateAdd(14), date_to: dateAdd(21), checkin_from: "15:00" },
      { event: "return", departure_at: `${dateAdd(21)}T17:00:00+03:00`, arrival_at: `${dateAdd(21)}T21:00:00+03:00` },
    ] } },
    { type: "cart", data: { cart: newCart, issues: newCart.over_budget ? [{ code: "OVER_BUDGET", message: "Демонстрационный план превышает указанный бюджет.", severity: "warning" }] : [] } },
    ...(changed ? [{ type: "plan_diff", data: { version: 2, kept: ["destination_id", "outbound", "inbound"], changed: ["hotel"], previous_total: 82400, total } } as UIBlock] : []),
    trace(),
  ];
  return { blocks, state: { version: changed ? 2 : 1, stage: "plan_checked", cart: newCart, request: { destination: city, budget_total: budgetTotal } } as TripState };
}
