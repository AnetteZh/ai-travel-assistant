from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from travel_data import book

from .contracts import ChatInput, PatchInput, TripState
from .graph import CoreError, TravelAgent


class _SkipHealthChecks(logging.Filter):
    """Docker проверяет /health каждые 10 секунд — эти строки только засоряют docker compose logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/health" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(_SkipHealthChecks())


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def create_app(agent: TravelAgent | None = None) -> FastAPI:
    app = FastAPI(title="О!дин запрос — Core AI", version="0.1.0")
    travel_agent = agent or TravelAgent()
    sessions: dict[str, TripState] = {}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat")
    async def chat(payload: ChatInput) -> StreamingResponse:
        conversation_id = (
            payload.conversation_id
            if payload.conversation_id in sessions
            else uuid.uuid4().hex
        )
        current = sessions.get(conversation_id, TripState())

        async def events() -> AsyncIterator[str]:
            try:
                result = None
                status_messages = {
                    "parse_request": ("parsing", "Разбираю запрос."),
                    "search": ("searching", "Ищу варианты поездки."),
                    "check_plan": ("checking_plan", "Проверяю план и расчёт."),
                }
                async for update in travel_agent.astream(payload.message, current):
                    for node, node_update in update.items():
                        if node in status_messages:
                            stage, message = status_messages[node]
                            yield _sse("status", {"stage": stage, "message": message})
                        if isinstance(node_update, dict) and "trip" in node_update:
                            result = TripState.model_validate(node_update["trip"])
                if result is None:
                    raise CoreError("Граф завершился без состояния поездки.")
                sessions[conversation_id] = result
                for block in result.blocks:
                    yield _sse("block", {"conversation_id": conversation_id, "block": block.model_dump(mode="json")})
                yield _sse("complete", {
                    "conversation_id": conversation_id,
                    "state": result.model_dump(mode="json"),
                })
            except CoreError as exc:
                yield _sse("error", {"code": "CORE_ERROR", "message": str(exc)})
            except Exception:
                yield _sse("error", {
                    "code": "INTERNAL_ERROR",
                    "message": "Не удалось обработать запрос. Попробуйте ещё раз.",
                })

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/trips")
    async def create_trip(payload: PatchInput) -> dict[str, object]:
        """Новая поездка сразу из фильтров интерфейса (даты, гости, бюджет) — без разбора текста моделью."""
        conversation_id = uuid.uuid4().hex
        sessions[conversation_id] = TripState()
        return await patch_trip(conversation_id, payload)

    @app.patch("/trips/{conversation_id}")
    async def patch_trip(conversation_id: str, payload: PatchInput) -> dict[str, object]:
        current = sessions.get(conversation_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Поездка не найдена.")
        try:
            result = None
            async for update in travel_agent.astream("", current, payload.patch):
                for node_update in update.values():
                    if isinstance(node_update, dict) and "trip" in node_update:
                        result = TripState.model_validate(node_update["trip"])
            if result is None:
                raise CoreError("Граф завершился без состояния поездки.")
        except CoreError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        sessions[conversation_id] = result
        return {
            "conversation_id": conversation_id,
            "state": result.model_dump(mode="json"),
            "blocks": [block.model_dump(mode="json") for block in result.blocks],
        }

    @app.post("/trips/{conversation_id}/book")
    def book_trip(conversation_id: str) -> dict[str, object]:
        """Демонстрационная бронь рассчитанной корзины. Реальной оплаты и поставщиков нет."""
        current = sessions.get(conversation_id)
        if current is None:
            raise HTTPException(status_code=404, detail="Поездка не найдена.")
        if current.cart is None:
            raise HTTPException(status_code=409, detail="Корзина ещё не рассчитана: сначала соберите поездку.")
        if any(issue.severity == "error" for issue in current.issues):
            raise HTTPException(status_code=409, detail="В плане есть блокирующие несостыковки, бронь недоступна.")
        result = book(current.cart)
        if isinstance(result, dict):
            raise HTTPException(status_code=409, detail=str(result.get("message", "Не удалось оформить бронь.")))
        return {
            "booking_id": result.booking_id,
            "status": result.status,
            "message": result.message,
            "booked_at": result.booked_at.isoformat(),
            "cart": result.cart.model_dump(mode="json"),
        }

    return app


app = create_app()
