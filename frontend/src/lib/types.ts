export type BlockType =
  | "message"
  | "assumptions"
  | "destination_cards"
  | "option_cards"
  | "comparison"
  | "trip_timeline"
  | "plan_diff"
  | "cart"
  | "trace";

export interface UIBlock {
  type: BlockType;
  data: Record<string, unknown>;
}

export type Evidence = {
  id: string;
  source_type: string;
  source_id: string;
  field_path: string;
  value: unknown;
  is_synthetic: boolean;
};

export type DestinationOption = {
  destination: { id: string; name: string; region: string; tags?: string[]; is_synthetic: boolean };
  estimated_trip_price: number;
  fastest_travel_minutes: number | null;
  weather?: { temperature_min_c: number; temperature_max_c: number; source: string; is_synthetic: boolean };
  why_it_fits?: string;
  pros: string[];
  cons: string[];
  reason_codes?: string[];
  evidence?: Evidence[];
};

export type HotelOption = {
  hotel: {
    id: string; name: string; city: string; stars: number; rating: number;
    price_per_night: number; breakfast_included: boolean;
    has_pets_allowed: boolean; family_friendly: boolean;
    cancellation_policy: string; is_synthetic: boolean;
  };
  estimated_lodging_total: number;
  why_it_fits?: string;
  pros?: string[];
  cons?: string[];
  risk_codes?: string[];
  score: number;
};

export type TransportOption = {
  id: string; origin_city: string; destination_city: string;
  transport_type: "flight" | "train"; departure_at: string; arrival_at: string;
  price: number; duration_minutes: number; stops: number; score: number;
};

/** `source: "interface"` — замечание нашла страховочная проверка фронтенда, а не Core. */
export type Issue = { code: string; message: string; severity: "info" | "warning" | "error"; source?: "core" | "interface" };
export type Cart = {
  id: string;
  items: { id: string; item_type: string; title: string; amount: number; is_synthetic: boolean }[];
  total: number; subtotal: number; nights: number; currency: string; is_synthetic: boolean;
  over_budget: boolean; budget_total: number | null; note?: string;
};

export type Guests = { adults: number; children_ages: number[]; pets: number };

export type TripRequest = {
  origin?: string | null;
  destination?: string | null;
  date_from?: string | null;
  date_to?: string | null;
  guests?: Guests;
  budget_total?: number | null;
};

/** Поля TripRequestPatch из Core. `destination: null` явно сбрасывает направление. */
export type TripPatch = Partial<Omit<TripRequest, "guests">> & { guests?: Guests };

export type TripState = {
  request?: TripRequest;
  stage?: string;
  version?: number;
  cart?: Cart | null;
  issues?: Issue[];
  blocks?: UIBlock[];
};

export type ChatMessage = { id: string; role: "assistant" | "user"; text: string; tone?: "notice" };
export type ServerEvent =
  | { event: "status"; data: { stage: string; message: string } }
  | { event: "block"; data: { conversation_id: string; block: UIBlock } }
  | { event: "complete"; data: { conversation_id: string; state: TripState } }
  | { event: "error"; data: { code: string; message: string } };

export type AgentErrorKind = "network" | "core" | "llm" | "stream" | "session" | "input";
export type AgentError = { kind: AgentErrorKind; title: string; detail?: string; canRetry: boolean };

export type Booking = { id: string; message: string; source: "core" | "interface" };

export const money = (n: number) => new Intl.NumberFormat("ru-RU").format(n) + " ₽";

const currentYear = () => new Date().getFullYear();

const ISO_WALL_TIME = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/;

/**
 * Дата и время по МЕСТНОМУ времени пункта, как в билете: Core отдаёт время с часовым поясом
 * места (вылет — по Москве, прилёт — по Владивостоку), и мы показываем его без пересчёта
 * в пояс браузера. Год — только если он не текущий: так сразу видно ошибочный 2025.
 */
export const readableDate = (value: string) => {
  const match = ISO_WALL_TIME.exec(value);
  if (match) return `${readableDay(match[1])}, ${match[2]}:${match[3]}`;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  const withYear = date.getFullYear() !== currentYear();
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
    ...(withYear ? { year: "numeric" } : {}),
  }).format(date);
};

/** Календарная дата из строки YYYY-MM-DD без сдвига часового пояса. */
export const readableDay = (value: string, forceYear = false) => {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(value);
  if (!match) return value;
  const [, y, m, d] = match;
  const date = new Date(Number(y), Number(m) - 1, Number(d));
  const withYear = forceYear || date.getFullYear() !== currentYear();
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", ...(withYear ? { year: "numeric" } : {}) }).format(date);
};

export function nounForm(count: number, one: string, few: string, many: string): string {
  const n = Math.abs(count) % 100;
  const unit = n % 10;
  return n > 10 && n < 20 ? many : unit === 1 ? one : unit >= 2 && unit <= 4 ? few : many;
}

export function findBlock(blocks: UIBlock[], kind: BlockType) {
  return blocks.find((item) => item.type === kind)?.data;
}

/**
 * Название направления для показа. Модель иногда кладёт в request.destination внутренний id
 * («dest-kaliningrad»): берём имя из карточек направлений или город отеля в подборке.
 */
export function destinationLabel(value: string | null | undefined, blocks: UIBlock[]): string | null {
  if (!value) return null;
  const cards = (findBlock(blocks, "destination_cards") as { items?: DestinationOption[] } | undefined)?.items;
  const card = cards?.find((item) => item.destination.id === value);
  if (card) return card.destination.name;
  if (/^dest-/.test(value)) {
    const hotels = (findBlock(blocks, "option_cards") as { hotels?: HotelOption[] } | undefined)?.hotels;
    if (hotels?.[0]?.hotel.city) return hotels[0].hotel.city;
  }
  return value;
}
