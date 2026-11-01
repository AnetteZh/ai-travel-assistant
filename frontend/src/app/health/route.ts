export const dynamic = "force-dynamic";

// Проверка готовности для Docker и жюри: интерфейс жив всегда, состояние Core — отдельным полем.
export async function GET() {
  const base = process.env.CORE_API_URL || "http://127.0.0.1:8765";
  let core: "ok" | "unavailable" = "unavailable";
  try {
    const response = await fetch(`${base}/health`, { cache: "no-store", signal: AbortSignal.timeout(2000) });
    if (response.ok) core = "ok";
  } catch {
    // Core недоступен — интерфейс всё равно работает в демо-режиме.
  }
  return Response.json({ status: "ok", core });
}
