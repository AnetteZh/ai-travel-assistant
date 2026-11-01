import type { ServerEvent } from "./types";

/** POST /chat emits event: status|block|complete|error + data: JSON, not token streams. */
export async function readSse(
  response: Response,
  onEvent: (event: ServerEvent) => void,
): Promise<void> {
  if (!response.ok) throw new Error(`Ошибка сервера: HTTP ${response.status}`);
  if (!response.body) throw new Error("Сервер не вернул поток событий.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  const parseFrame = (frame: string) => {
    const lines = frame.split("\n");
    const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
    const dataText = lines.filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
    if (!event || !dataText) return;
    if (!["status", "block", "complete", "error"].includes(event)) return;
    let data: unknown;
    try {
      data = JSON.parse(dataText);
    } catch {
      throw new Error("Некорректный JSON в SSE-ответе Core.");
    }
    onEvent({ event, data } as ServerEvent);
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      buffer = buffer.replace(/\r\n/g, "\n");
      let separator = buffer.indexOf("\n\n");
      while (separator !== -1) {
        parseFrame(buffer.slice(0, separator));
        buffer = buffer.slice(separator + 2);
        separator = buffer.indexOf("\n\n");
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) parseFrame(buffer.trim());
  } finally {
    reader.releaseLock();
  }
}
