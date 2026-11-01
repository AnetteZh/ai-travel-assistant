/** Server-side proxy: Next.js and Core share one origin as seen by the browser. */
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const base = process.env.CORE_API_URL || "http://127.0.0.1:8765";
  try {
    const payload: unknown = await request.json();
    const upstream = await fetch(`${base.replace(/\/$/, "")}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
      body: JSON.stringify(payload),
      cache: "no-store",
      signal: request.signal,
    });

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("content-type") || "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
      },
    });
  } catch {
    return Response.json({ error: `Нет ответа по адресу ${base}. Проверь, что FastAPI запущен и адрес в frontend/.env.local верный.` }, { status: 502 });
  }
}
