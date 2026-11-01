/** Patch TripRequest without converting the change back into a chat message. */
export const runtime = "nodejs";

export async function PATCH(request: Request, context: { params: Promise<{ conversationId: string }> }) {
  const base = process.env.CORE_API_URL || "http://127.0.0.1:8765";
  try {
    const { conversationId } = await context.params;
    const payload: unknown = await request.json();
    const upstream = await fetch(`${base.replace(/\/$/, "")}/trips/${encodeURIComponent(conversationId)}`, {
      method: "PATCH",
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
