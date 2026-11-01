/** Новая поездка из фильтров поиска: передаём патч TripRequest в Core без текста для модели. */
export const runtime = "nodejs";

export async function POST(request: Request) {
  const base = process.env.CORE_API_URL || "http://127.0.0.1:8765";
  try {
    const payload: unknown = await request.json();
    const upstream = await fetch(`${base.replace(/\/$/, "")}/trips`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
      signal: request.signal,
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": "application/json" },
    });
  } catch {
    return Response.json({ error: `Нет ответа по адресу ${base}. Проверь, что FastAPI запущен.` }, { status: 502 });
  }
}
