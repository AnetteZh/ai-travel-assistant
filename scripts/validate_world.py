#!/usr/bin/env python3
"""Validate referential integrity and basic sanity of the generated world."""
from __future__ import annotations

import sys
from datetime import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_data.models import WorldData  # noqa: E402
from travel_data.world import default_world_path  # noqa: E402


def validate() -> list[str]:
    world = WorldData.model_validate_json(default_world_path().read_text(encoding="utf-8"))
    errors: list[str] = []
    destination_ids = [d.id for d in world.destinations]
    hotel_ids = [h.id for h in world.hotels]
    route_ids = [r.id for r in world.transport_templates]
    transfer_ids = [t.id for t in world.transfer_templates]
    for label, values in [("destination", destination_ids), ("hotel", hotel_ids), ("route", route_ids), ("transfer", transfer_ids)]:
        if len(values) != len(set(values)):
            errors.append(f"Duplicate {label} IDs")

    destinations = set(destination_ids)
    cities = {d.name for d in world.destinations}
    destinations_by_id = {d.id: d for d in world.destinations}
    hotels_per_destination: dict[str, int] = {identifier: 0 for identifier in destination_ids}
    for hotel in world.hotels:
        if hotel.destination_id not in destinations:
            errors.append(f"Hotel {hotel.id} points to missing destination {hotel.destination_id}")
            continue
        hotels_per_destination[hotel.destination_id] += 1
        if not hotel.is_synthetic:
            errors.append(f"Mock hotel {hotel.id} must have is_synthetic=true")
        if hotel.price_per_night <= 0 or not 1 <= hotel.stars <= 5 or not 1 <= hotel.rating <= 5:
            errors.append(f"Hotel {hotel.id} has an invalid price/stars/rating")
        destination = destinations_by_id[hotel.destination_id]
        lat_distance = abs(hotel.latitude - destination.latitude)
        lon_distance = abs(hotel.longitude - destination.longitude)
        if lat_distance > 0.2 or lon_distance > 0.25:
            errors.append(f"Hotel {hotel.id} coordinates are unexpectedly far from {destination.name}")
        if hotel.distance_to_beach_km is not None and not any(tag in destination.tags for tag in ("море", "пляж")):
            errors.append(f"Non-coastal hotel {hotel.id} has a beach distance")
        try:
            checkin_from = time.fromisoformat(hotel.checkin_from)
            checkin_until = time.fromisoformat(hotel.checkin_until)
            checkout = time.fromisoformat(hotel.checkout_time)
            reception_from = time.fromisoformat(hotel.reception_open_from)
            reception_until = time.fromisoformat(hotel.reception_open_until)
            if checkin_from >= checkin_until:
                errors.append(f"Hotel {hotel.id} has inconsistent check-in hours")
            if reception_from >= reception_until:
                errors.append(f"Hotel {hotel.id} has inconsistent reception hours")
            if checkout <= time(0, 0) or checkout >= time(23, 59):
                errors.append(f"Hotel {hotel.id} has an implausible checkout time")
        except ValueError:
            errors.append(f"Hotel {hotel.id} has a malformed time field")
    for destination_id, count in hotels_per_destination.items():
        if count != 10:
            errors.append(f"Expected 10 hotels for {destination_id}, got {count}")

    route_keys = set()
    for route in world.transport_templates:
        if route.destination_id not in destinations:
            errors.append(f"Route {route.id} points to missing destination {route.destination_id}")
        if route.origin_city not in cities:
            errors.append(f"Route {route.id} has unknown origin {route.origin_city}")
        if route.origin_city not in {d.name for d in world.destinations if d.id != route.destination_id}:
            errors.append(f"Route {route.id} is a self-route or invalid route")
        if not route.departure_times:
            errors.append(f"Route {route.id} has no departure times")
        if not route.is_synthetic:
            errors.append(f"Mock route {route.id} must have is_synthetic=true")
        route_keys.add((route.origin_city, route.destination_id, route.transport_type))
    for origin in world.destinations:
        for target in world.destinations:
            if origin.id == target.id:
                continue
            for mode in ("flight", "train"):
                if (origin.name, target.id, mode) not in route_keys:
                    errors.append(f"Missing {mode} route {origin.name} -> {target.name}")

    for transfer in world.transfer_templates:
        if transfer.destination_id not in destinations:
            errors.append(f"Transfer {transfer.id} points to missing destination {transfer.destination_id}")
        if transfer.operates_24h and transfer.transfer_type != "taxi":
            errors.append(f"Only taxi should be configured as 24h in this demo: {transfer.id}")
        if not transfer.is_synthetic:
            errors.append(f"Mock transfer {transfer.id} must have is_synthetic=true")
        try:
            time.fromisoformat(transfer.operating_start)
            time.fromisoformat(transfer.operating_end)
        except ValueError:
            errors.append(f"Transfer {transfer.id} has malformed opening hours")

    moscow = next((d for d in world.destinations if d.name == "Москва"), None)
    if moscow:
        for destination in world.destinations:
            if destination.id == moscow.id:
                continue
            outbound = any(r.origin_city == moscow.name and r.destination_id == destination.id for r in world.transport_templates)
            inbound = any(r.origin_city == destination.name and r.destination_id == moscow.id for r in world.transport_templates)
            if not outbound or not inbound:
                errors.append(f"No round-trip transport templates between Moscow and {destination.name}")

    for destination in world.destinations:
        for arrival_point in ("airport", "station"):
            kinds = {t.transfer_type for t in world.transfer_templates if t.destination_id == destination.id and t.arrival_point == arrival_point}
            if "taxi" not in kinds:
                errors.append(f"No taxi transfer for {destination.name} / {arrival_point}")

    if len(world.destinations) != 20:
        errors.append(f"Expected 20 destinations, got {len(world.destinations)}")
    if len(world.hotels) != 200:
        errors.append(f"Expected 200 hotels, got {len(world.hotels)}")
    if not 0.10 <= sum(1 for h in world.hotels if h.noise_level == "noisy" or h.cancellation_policy == "non_refundable") / max(1, len(world.hotels)) <= 0.30:
        errors.append("Expected roughly 10–30% of hotels to contain intentional trade-offs")
    return errors


def main() -> None:
    errors = validate()
    if errors:
        print("WORLD INVALID")
        for error in errors:
            print(f" - {error}")
        raise SystemExit(1)
    world = WorldData.model_validate_json(default_world_path().read_text(encoding="utf-8"))
    print("WORLD OK")
    print(f"Destinations: {len(world.destinations)}")
    print(f"Hotels: {len(world.hotels)}")
    print(f"Transport templates: {len(world.transport_templates)}")
    print(f"Transfer templates: {len(world.transfer_templates)}")
    print("Integrity: unique IDs, valid references, route symmetry from/to Moscow, transfer coverage, and basic hotel constraints checked.")


if __name__ == "__main__":
    main()
