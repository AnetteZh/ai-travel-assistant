"""Optional adapter exposing Data functions as LangChain tools for a LangGraph agent.

Install the optional dependencies with: pip install -e '.[langgraph]'
The Data implementation itself does not depend on LangGraph.
"""
from __future__ import annotations
from typing import Any


def _as_json(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def as_langgraph_tools():
    try:
        from langchain_core.tools import StructuredTool
    except ImportError as exc:  # pragma: no cover - exercised only when optional extra missing
        raise RuntimeError(
            "LangGraph adapter dependencies are missing. Install with: pip install -e '.[langgraph]'"
        ) from exc

    from .models import (
        BookToolInput,
        PriceCartToolInput,
        SearchHotelsToolInput,
        SearchTransfersToolInput,
        SearchTransportToolInput,
        SuggestDestinationsToolInput,
    )
    from .tools import book, price_cart, search_hotels, search_transfers, search_transport, suggest_destinations

    def _suggest(request):
        return _as_json(suggest_destinations(request))

    def _hotels(request, destination_id):
        return _as_json(search_hotels(request, destination_id))

    def _transport(request, destination_id):
        return _as_json(search_transport(request, destination_id))

    def _transfers(destination_id, hotel_id, arrival_at, arrival_point="airport", direction="to_hotel"):
        return _as_json(search_transfers(destination_id, hotel_id, arrival_at, arrival_point, direction))

    def _price(selection):
        return _as_json(price_cart(selection))

    def _book(cart):
        return _as_json(book(cart))

    return [
        StructuredTool.from_function(
            func=_suggest,
            name="suggest_destinations",
            description="Предложить несколько направлений, когда пользователь ещё не выбрал место поездки.",
            args_schema=SuggestDestinationsToolInput,
        ),
        StructuredTool.from_function(
            func=_hotels,
            name="search_hotels",
            description="Найти, отфильтровать и ранжировать отели по жёстким требованиям и мягким предпочтениям.",
            args_schema=SearchHotelsToolInput,
        ),
        StructuredTool.from_function(
            func=_transport,
            name="search_transport",
            description="Найти синтетические варианты рейсов и поездов туда и обратно с часовыми поясами.",
            args_schema=SearchTransportToolInput,
        ),
        StructuredTool.from_function(
            func=_transfers,
            name="search_transfers",
            description="Найти трансферы от аэропорта или вокзала к выбранному отелю с учётом времени прибытия.",
            args_schema=SearchTransfersToolInput,
        ),
        StructuredTool.from_function(
            func=_price,
            name="price_cart",
            description="Посчитать стоимость выбранного отеля, транспорта и трансферов детерминированно в коде.",
            args_schema=PriceCartToolInput,
        ),
        StructuredTool.from_function(
            func=_book,
            name="book",
            description="Создать запись демонстрационного бронирования без оплаты и внешнего поставщика.",
            args_schema=BookToolInput,
        ),
    ]
