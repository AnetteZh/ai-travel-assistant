from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from .models import WorldData


def default_world_path() -> Path:
    configured = os.getenv("TRAVEL_DATA_WORLD_PATH")
    if configured:
        configured_path = Path(configured).expanduser()
        return configured_path.resolve() if configured_path.is_absolute() else (Path.cwd() / configured_path).resolve()
    project_path = Path(__file__).resolve().parents[2] / "data" / "world.json"
    if project_path.exists():
        return project_path
    return Path(__file__).resolve().parent / "data" / "world.json"


@lru_cache(maxsize=1)
def load_world() -> WorldData:
    path = default_world_path()
    if not path.exists():
        raise FileNotFoundError(
            f"World data not found at {path}. Run: python scripts/generate_world.py"
        )
    with path.open("r", encoding="utf-8") as file:
        raw = json.load(file)
    return WorldData.model_validate(raw)


def reload_world() -> WorldData:
    load_world.cache_clear()
    return load_world()


def get_destination(identifier_or_city: str):
    needle = identifier_or_city.strip().casefold()
    world = load_world()
    for destination in world.destinations:
        if needle in {destination.id.casefold(), destination.name.casefold()}:
            return destination
    # Accept common city names that are stored separately from destination IDs.
    for destination in world.destinations:
        if needle == destination.region.casefold():
            return destination
    return None


def get_hotel(hotel_id: str):
    return next((hotel for hotel in load_world().hotels if hotel.id == hotel_id), None)
