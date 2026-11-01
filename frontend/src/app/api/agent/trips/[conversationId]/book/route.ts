/** Демо-бронирование: проксирует POST /trips/{id}/book в Core. Пока ручки нет, Core вернёт 404 Not Found. */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request, context: { params: Promise<{ conversationId: string }> }) {
  const base = process.env.CORE_API_URL || "http://127.0.0.1:8765";
  try {
    const { conversationId } = await context.params;
    const upstream = await fetch(`${base.replace(/\/$/, "")}/trips/${encodeURIComponent(conversationId)}/book`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      cache: "no-store",
      signal: request.signal,
    });
    return new Response(upstream.body, {
      status: upstream.status,
      headers: { "Content-Type": upstream.headers.get("content-type") || "application/json" },
    });
  } catch {
    return Response.json({ error: `Нет ответа по адресу ${base}. Проверь, что FastAPI запущен.` }, { status: 502 });
  }
}
