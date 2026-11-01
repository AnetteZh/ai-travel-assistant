from __future__ import annotations

from datetime import date, timedelta

from dotenv import load_dotenv

load_dotenv()

from travel_data import (
    CartSelection,
    Guests,
    TravelRequest,
    book,
    price_cart,
    search_hotels,
    search_transfers,
    search_transport,
    suggest_destinations,
)
from travel_data.world import load_world


def main() -> None:
    today = date.today()
    departure = today + timedelta(days=7)
    return_date = departure + timedelta(days=5)

    print("World:", len(load_world().destinations), "destinations /", len(load_world().hotels), "hotels")
    request = TravelRequest(
        origin="Москва",
        date_from=departure,
        date_to=return_date,
        guests=Guests(adults=2, children_ages=[7], pets=0),
        budget_total=85000,
        hard_requirements=["family-friendly"],
        soft_preferences=["море", "завтрак", "тихо"],
        mood="relax",
    )

    ideas = suggest_destinations(request)
    print("\nDestination suggestions:")
    for idea in ideas.options[:3]:
        print(f"- {idea.destination.name}: approx. {idea.estimated_trip_price} RUB; {idea.weather.description}; synthetic weather={idea.weather.is_synthetic}")

    destination_id = "dest-sochi"
    hotels = search_hotels(request, destination_id)
    if not hotels.options:
        print("No hotels matched; alternatives:", [x.hotel.id for x in hotels.alternatives])
        return
    selected_hotel = hotels.options[0].hotel
    print("\nSelected hotel:", selected_hotel.id, selected_hotel.name, selected_hotel.price_per_night, "RUB/night")

    transport = search_transport(request, destination_id)
    if not transport.outbound or not transport.return_options:
        print("No suitable transport route:", transport.message)
        return
    outbound = transport.outbound[0]
    inbound = transport.return_options[0]
    transfer_result = search_transfers(destination_id, selected_hotel.id, outbound.arrival_at, "airport")
    transfer_ids = [transfer_result.options[0].id] if transfer_result.options else []

    selection = CartSelection(
        destination_id=destination_id,
        hotel_id=selected_hotel.id,
        date_from=departure,
        date_to=return_date,
        transport_ids=[outbound.id, inbound.id],
        transfer_ids=transfer_ids,
        guests=request.guests,
        budget_total=request.budget_total,
        origin=request.origin,
    )
    cart = price_cart(selection)
    if isinstance(cart, dict):
        print("Could not price cart:", cart)
        return
    print("\nCart:", cart.id, "total =", cart.total, "RUB", "risk_codes =", cart.risk_codes)
    booking = book(cart)
    if isinstance(booking, dict):
        print("Could not book demo cart:", booking)
    else:
        print("Demo booking:", booking.booking_id, booking.message)


if __name__ == "__main__":
    main()
