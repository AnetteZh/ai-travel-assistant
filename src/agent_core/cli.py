from __future__ import annotations

from collections.abc import Callable

from .contracts import TripState
from .graph import CoreError, TravelAgent, customer_facing_text


def format_summary(trip: TripState) -> str:
    """Render the agent's structured result as a concise human-readable report."""
    if trip.response_mode == "details" and trip.response_text:
        return trip.response_text

    lines: list[str] = []

    request = trip.request
    request_details = []
    if request.origin:
        request_details.append(f"из {request.origin}")
    if request.destination:
        request_details.append(f"в {request.destination}")
    if request.date_from or request.date_to:
        start = request.date_from.strftime("%d.%m.%Y") if request.date_from else "дата не указана"
        end = request.date_to.strftime("%d.%m.%Y") if request.date_to else "дата не указана"
        request_details.append(f"{start} — {end}")
    if request.budget_total is not None:
        request_details.append(f"бюджет {request.budget_total:,} ₽".replace(",", " "))
    if request_details:
        lines.append(f"Запрос: {'; '.join(request_details)}.")

    message = next(
        (
            str(block.data["text"])
            for block in trip.blocks
            if block.type == "message" and block.data.get("text")
        ),
        None,
    )
    if message:
        lines.append(message)

    if trip.destinations and trip.destinations.options:
        lines.append("\nНаправления:")
        for index, item in enumerate(trip.destinations.options[:3], start=1):
            details = [f"около {item.estimated_trip_price:,} ₽ за поездку".replace(",", " ")]
            if item.fastest_travel_minutes is not None:
                hours, minutes = divmod(item.fastest_travel_minutes, 60)
                duration = f"{hours} ч"
                if minutes:
                    duration += f" {minutes} мин"
                details.append(f"в пути от {duration}")
            if item.weather:
                details.append(
                    f"погода: {item.weather.temperature_min_c:g}…"
                    f"{item.weather.temperature_max_c:g} °C"
                )
            lines.append(f"  {index}. {item.destination.name} — {'; '.join(details)}")
            why_it_fits = customer_facing_text(item.why_it_fits)
            if why_it_fits:
                lines.append(f"     Почему: {why_it_fits}")
            cons = [text for value in item.cons[:2] if (text := customer_facing_text(value))]
            if cons:
                lines.append(f"     Ограничения: {'; '.join(cons)}")

    if trip.hotels:
        options = trip.hotels.options[:3] or trip.hotels.alternatives[:3]
        if options:
            title = "Подходящие отели" if trip.hotels.options else "Ближайшие альтернативы (есть нарушения)"
            lines.append(f"\n{title}:")
            for index, item in enumerate(options, start=1):
                selected = (
                    trip.plan is not None
                    and item.hotel.id == trip.plan.hotel.hotel.id
                )
                details = [
                    f"{item.hotel.stars}★",
                    f"рейтинг {item.hotel.rating:g}",
                    f"расчёт за проживание {item.estimated_lodging_total:,} ₽".replace(",", " "),
                ]
                recommendation = " (входит в текущий план)" if selected else ""
                lines.append(f"  {index}. {item.hotel.name}{recommendation} — {'; '.join(details)}")
                pros = [text for value in item.pros[:2] if (text := customer_facing_text(value))]
                if pros:
                    lines.append(f"     Подходит: {'; '.join(pros)}")
                if item.violated_requirements:
                    cons = [text for value in item.cons[:2] if (text := customer_facing_text(value))]
                    if cons:
                        lines.append(f"     Не выполнено: {'; '.join(cons)}")

    if trip.transport:
        outbound = trip.transport.outbound[:1]
        inbound = trip.transport.return_options[:1]
        if outbound or inbound:
            lines.append("\nТранспорт:")
            if outbound:
                option = outbound[0]
                lines.append(
                    f"  Туда: {option.transport_type}, "
                    f"{option.departure_at:%d.%m %H:%M} → {option.arrival_at:%d.%m %H:%M}, "
                    f"{option.duration_minutes // 60} ч {option.duration_minutes % 60} мин, "
                    f"{option.price:,} ₽".replace(",", " ")
                )
            if inbound:
                option = inbound[0]
                lines.append(
                    f"  Обратно: {option.transport_type}, "
                    f"{option.departure_at:%d.%m %H:%M} → {option.arrival_at:%d.%m %H:%M}, "
                    f"{option.duration_minutes // 60} ч {option.duration_minutes % 60} мин, "
                    f"{option.price:,} ₽".replace(",", " ")
                )

    if trip.cart:
        lines.append(f"\nИтого по расчёту Travel Data: {trip.cart.total:,} ₽".replace(",", " "))
        if trip.cart.budget_total is not None:
            lines.append(f"Бюджет: {trip.cart.budget_total:,} ₽".replace(",", " "))
        lines.append(
            "Проверка бюджета: превышен"
            if trip.cart.over_budget
            else "Проверка бюджета: в пределах бюджета"
            if trip.cart.budget_total is not None
            else "Проверка бюджета: бюджет не задан"
        )

    if trip.issues:
        lines.append("\nПроблемы и риски:")
        for issue in trip.issues:
            lines.append(f"  - [{issue.code}] {issue.message}")

    if trip.assumptions:
        lines.append("\nДопущения (можно изменить следующим сообщением):")
        for assumption in trip.assumptions:
            lines.append(f"  - {assumption}")

    if trip.plan_diff:
        labels = {
            "destination_id": "направление",
            "hotel": "отель",
            "outbound": "транспорт туда",
            "inbound": "обратный транспорт",
        }
        kept = ", ".join(labels.get(key, key) for key in trip.plan_diff.get("kept", [])) or "нет"
        changed = ", ".join(labels.get(key, key) for key in trip.plan_diff.get("changed", [])) or "нет"
        diff = f"\nПересборка плана v{trip.version}: сохранено — {kept}; изменено — {changed}."
        previous_total = trip.plan_diff.get("previous_total")
        total = trip.plan_diff.get("total")
        if previous_total is not None and total is not None:
            diff += (
                f" Сумма: {previous_total:,} → {total:,} ₽."
                .replace(",", " ")
            )
        lines.append(diff)

    return "\n".join(lines) if lines else "Пока нет результатов для отображения."


def _print_help(output: Callable[[str], None]) -> None:
    output(
        "Введите сообщение о поездке, чтобы продолжить диалог.\n"
        "Команды: /state — повторить сводку; /reset — начать новую поездку; "
        "/help — помощь; /quit — выход."
    )


def run_cli(
    agent: TravelAgent | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> None:
    """Run a single-user interactive conversation in the terminal."""
    travel_agent = agent or TravelAgent()
    trip = TripState()
    output_fn("Core AI — терминальная проверка (не UI). Введите /help для команд.")

    while True:
        try:
            message = input_fn("Вы: ").strip()
        except EOFError:
            output_fn("\nЗавершение диалога.")
            return

        if not message:
            continue
        if message == "/quit":
            output_fn("Завершение диалога.")
            return
        if message == "/help":
            _print_help(output_fn)
            continue
        if message == "/reset":
            trip = TripState()
            output_fn("Начат новый диалог.")
            continue
        if message == "/state":
            output_fn(format_summary(trip))
            continue

        try:
            trip = travel_agent.run(message, trip)
        except CoreError as exc:
            output_fn(f"Ошибка агента: {exc}")
            continue
        output_fn(f"\nАгент:\n{format_summary(trip)}")


def main() -> None:
    try:
        run_cli()
    except KeyboardInterrupt:
        print("\nЗавершение диалога.")


if __name__ == "__main__":
    main()
