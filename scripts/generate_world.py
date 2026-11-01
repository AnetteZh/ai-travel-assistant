#!/usr/bin/env python3
"""Generate a repeatable, internally consistent synthetic travel world."""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from travel_data.models import WorldData  # noqa: E402

DESTINATION_SEEDS = [
    # id, name, region, airport, station, lat, lon, timezone, tags, typical nightly, avg summer max, seasonal amplitude
    ("dest-moscow", "Москва", "Москва", "Шереметьево (SVO)", "Москва-Пассажирская", 55.7558, 37.6173, "Europe/Moscow", ["город", "музеи", "гастрономия", "семейный отдых"], 8200, 24, 15),
    ("dest-spb", "Санкт-Петербург", "Ленинградская область", "Пулково (LED)", "Московский вокзал", 59.9343, 30.3351, "Europe/Moscow", ["город", "музеи", "гастрономия", "архитектура"], 6900, 22, 13),
    ("dest-sochi", "Сочи", "Краснодарский край", "Сочи (AER)", "Сочи", 43.5855, 39.7231, "Europe/Moscow", ["море", "пляж", "горы", "семейный отдых", "спа"], 7800, 27, 9),
    ("dest-kazan", "Казань", "Республика Татарстан", "Казань (KZN)", "Казань-Пассажирская", 55.7961, 49.1064, "Europe/Moscow", ["город", "гастрономия", "культура", "семейный отдых"], 5600, 25, 15),
    ("dest-kaliningrad", "Калининград", "Калининградская область", "Храброво (KGD)", "Калининград-Пассажирский", 54.7104, 20.4522, "Europe/Kaliningrad", ["море", "пляж", "история", "природа"], 6100, 21, 12),
    ("dest-ekb", "Екатеринбург", "Свердловская область", "Кольцово (SVX)", "Екатеринбург-Пассажирский", 56.8389, 60.6057, "Asia/Yekaterinburg", ["город", "горы", "активный отдых"], 5200, 23, 17),
    ("dest-nsk", "Новосибирск", "Новосибирская область", "Толмачёво (OVB)", "Новосибирск-Главный", 55.0084, 82.9357, "Asia/Novosibirsk", ["город", "природа", "активный отдых"], 4900, 24, 20),
    ("dest-irkutsk", "Иркутск", "Иркутская область", "Иркутск (IKT)", "Иркутск-Пассажирский", 52.2869, 104.3050, "Asia/Irkutsk", ["природа", "озёра", "горы", "активный отдых"], 5700, 23, 20),
    ("dest-vladivostok", "Владивосток", "Приморский край", "Кневичи (VVO)", "Владивосток", 43.1155, 131.8855, "Asia/Vladivostok", ["море", "гастрономия", "природа"], 6900, 24, 11),
    ("dest-murmansk", "Мурманск", "Мурманская область", "Мурманск (MMK)", "Мурманск", 68.9585, 33.0827, "Europe/Moscow", ["север", "природа", "активный отдых", "горы"], 6300, 15, 18),
    ("dest-petrozavodsk", "Петрозаводск", "Республика Карелия", "Петрозаводск (PES)", "Петрозаводск", 61.7891, 34.3596, "Europe/Moscow", ["природа", "озёра", "лес", "тихо"], 5100, 21, 14),
    ("dest-minvody", "Минеральные Воды", "Ставропольский край", "Минеральные Воды (MRV)", "Минеральные Воды", 44.2089, 43.1383, "Europe/Moscow", ["горы", "спа", "природа", "релакс"], 4800, 27, 13),
    ("dest-makhachkala", "Махачкала", "Республика Дагестан", "Уйташ (MCX)", "Махачкала", 42.9849, 47.5047, "Europe/Moscow", ["море", "пляж", "горы", "гастрономия"], 4700, 28, 10),
    ("dest-tyumen", "Тюмень", "Тюменская область", "Рощино (TJM)", "Тюмень", 57.1522, 65.5272, "Asia/Yekaterinburg", ["город", "термы", "спа", "релакс"], 5300, 24, 18),
    ("dest-nnov", "Нижний Новгород", "Нижегородская область", "Стригино (GOJ)", "Нижний Новгород", 56.2965, 43.9361, "Europe/Moscow", ["город", "архитектура", "гастрономия", "семейный отдых"], 5500, 24, 15),
    ("dest-yaroslavl", "Ярославль", "Ярославская область", "Туношна (IAR)", "Ярославль-Главный", 57.6261, 39.8845, "Europe/Moscow", ["город", "история", "тихо", "семейный отдых"], 4500, 23, 15),
    ("dest-arkhangelsk", "Архангельск", "Архангельская область", "Талаги (ARH)", "Архангельск-Город", 64.5393, 40.5187, "Europe/Moscow", ["север", "природа", "история"], 4600, 19, 17),
    ("dest-perm", "Пермь", "Пермский край", "Большое Савино (PEE)", "Пермь-2", 58.0105, 56.2502, "Asia/Yekaterinburg", ["город", "горы", "природа", "активный отдых"], 4700, 23, 17),
    ("dest-ufa", "Уфа", "Республика Башкортостан", "Уфа (UFA)", "Уфа", 54.7388, 55.9721, "Asia/Yekaterinburg", ["город", "горы", "гастрономия", "природа"], 4600, 24, 16),
    ("dest-krasnodar", "Краснодар", "Краснодарский край", "Пашковский (KRR)", "Краснодар-1", 45.0355, 38.9753, "Europe/Moscow", ["город", "гастрономия", "семейный отдых", "море"], 5200, 29, 11),
]

NAME_PREFIXES = ["Дом", "Отель", "Гостиница", "Апарт-отель", "Бутик-отель", "Парк-отель", "Гранд-отель", "Сити-отель", "Резиденция", "Уютный двор"]
NAME_SUFFIXES = ["на Набережной", "Северный", "Центральный", "Панорама", "Лесной", "Комфорт", "Премиум", "Видовой", "Семейный", "Старый город"]
DESCRIPTIONS = [
    "Синтетическая карточка для демонстрационного подбора поездок. Условия размещения и удобства перечислены в структурированных полях.",
    "Этот объект создан для сравнения вариантов в прототипе. Перед выбором проверьте цену, условия отмены и список удобств в карточке.",
    "Демонстрационный вариант для тестирования поиска и фильтрации отелей. Информация о возможностях объекта хранится в отдельных полях.",
    "Синтетический объект в каталоге направления. Ключевые параметры для ранжирования доступны в структурированной записи.",
    "Запись предназначена для сравнения вариантов размещения. Точные условия тарифа и доступные услуги задаются отдельными полями карточки.",
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_world() -> dict:
    rng = random.Random(20261025)
    destinations = []
    for seed in DESTINATION_SEEDS:
        (identifier, name, region, airport, station, lat, lon, timezone, tags,
         typical_price, summer_max, amplitude) = seed
        climate = {}
        for month in range(1, 13):
            # A simple seasonal curve for transparent synthetic fallback values.
            peak = math.cos((month - 7) * math.pi / 6)
            tmax = round(summer_max - amplitude + amplitude * peak, 1)
            tmin = round(tmax - rng.uniform(5.0, 8.0), 1)
            climate[str(month)] = {
                "temperature_min_c": tmin,
                "temperature_max_c": tmax,
                "precipitation_probability": round(rng.uniform(25, 68), 1),
            }
        destinations.append({
            "id": identifier,
            "name": name,
            "region": region,
            "country": "Россия",
            "airport": airport,
            "station": station,
            "latitude": lat,
            "longitude": lon,
            "timezone": timezone,
            "tags": tags,
            "typical_price_per_night": typical_price,
            "climate_monthly": climate,
            "is_synthetic": True,
        })

    hotels = []
    cancellation_policies = ["free_until_7_days", "free_until_3_days", "partial", "free_until_7_days", "free_until_7_days", "free_until_3_days", "partial", "free_until_3_days", "non_refundable", "non_refundable"]
    star_pattern = [3, 4, 2, 4, 5, 3, 4, 2, 3, 2]
    for seed in DESTINATION_SEEDS:
        destination_id, city, region, airport, station, lat, lon, timezone, tags, typical_price, *_ = seed
        coastal = any(tag in tags for tag in ["море", "пляж"])
        for index in range(10):
            hotel_id = f"hotel-{destination_id.removeprefix('dest-')}-{index + 1:02d}"
            is_challenging = index in (8, 9)  # 20% per city have deliberate trade-offs.
            stars = star_pattern[index]
            price_factor = [0.68, 1.08, 0.58, 1.22, 1.65, 0.92, 1.34, 0.72, 0.62, 0.78][index]
            price = max(2200, int(round(typical_price * price_factor / 100) * 100))
            has_pool = index in (3, 4, 6) or rng.random() < 0.12
            has_spa = index in (4, 6) or rng.random() < 0.10
            pets = index in (0, 2, 5, 7) or rng.random() < 0.12
            family = index in (3, 4, 6, 7) or rng.random() < 0.15
            kids_club = index in (4, 6) or (family and rng.random() < 0.25)
            cancellation = "non_refundable" if is_challenging else cancellation_policies[index % len(cancellation_policies)]
            noise = "noisy" if is_challenging else ("quiet" if index % 3 == 0 else "moderate")
            center_distance = round((4.4 + (index - 8) * 0.6) if is_challenging else (0.3 + (index % 5) * 0.37), 1)
            beach_distance = round((5.0 + index * 0.3) if is_challenging else (0.25 + (index % 5) * 0.42), 1) if coastal else None
            rating = round((3.5 + (index % 6) * 0.25) if is_challenging else (4.0 + (index % 5) * 0.18), 1)
            if is_challenging:
                rating = min(rating, 3.9)
            amenities = ["Wi-Fi", "кондиционер"]
            if has_pool:
                amenities.append("бассейн")
            if pets:
                amenities.append("размещение с питомцами")
            if has_spa:
                amenities.append("спа")
            if family:
                amenities.append("семейные номера")
            if index % 2 == 0:
                amenities.append("завтрак")
            reception_until = "20:00" if is_challenging else ("22:00" if index % 4 == 0 else "23:59")
            hotel_name = f"{NAME_PREFIXES[index]} {city} {NAME_SUFFIXES[(index + len(city)) % len(NAME_SUFFIXES)]}"
            hotels.append({
                "id": hotel_id,
                "destination_id": destination_id,
                "name": hotel_name,
                "city": city,
                "price_per_night": price,
                "stars": stars,
                "has_pool": has_pool,
                "has_pets_allowed": pets,
                "has_spa": has_spa,
                "rating": rating,
                "description": DESCRIPTIONS[index % len(DESCRIPTIONS)],
                "latitude": round(lat + rng.uniform(-0.035, 0.035), 6),
                "longitude": round(lon + rng.uniform(-0.045, 0.045), 6),
                "cancellation_policy": cancellation,
                "breakfast_included": index % 2 == 0,
                "family_friendly": family,
                "kids_club": kids_club,
                "checkin_from": "14:00",
                "checkin_until": "23:00" if not is_challenging else "20:00",
                "checkout_time": "12:00",
                "reception_open_from": "07:00",
                "reception_open_until": reception_until,
                "distance_to_center_km": center_distance,
                "distance_to_beach_km": beach_distance,
                "noise_level": noise,
                "amenities": amenities,
                "is_synthetic": True,
            })

    transport_templates = []
    for origin_seed in DESTINATION_SEEDS:
        origin_id, origin_city, _, _, _, origin_lat, origin_lon, *_ = origin_seed
        for dest_seed in DESTINATION_SEEDS:
            dest_id, dest_city, _, _, _, dest_lat, dest_lon, *_ = dest_seed
            if origin_id == dest_id:
                continue
            distance = haversine_km(origin_lat, origin_lon, dest_lat, dest_lon)
            flight_duration = max(55, int(distance / 650 * 60 + 45))
            train_duration = max(90, int(distance / 62 * 60 + 35))
            # Fixed synthetic schedules; concrete dates/timestamps are created by search_transport.
            transport_templates.extend([
                {
                    "id": f"route-{origin_id.removeprefix('dest-')}-{dest_id.removeprefix('dest-')}-flight",
                    "origin_city": origin_city,
                    "destination_id": dest_id,
                    "transport_type": "flight",
                    "duration_minutes": flight_duration,
                    "base_price": max(2800, int(2100 + distance * 4.7)),
                    "stops": 0 if distance < 1800 else 1,
                    "departure_times": ["06:40", "12:20", "20:35"],
                    "baggage_included": distance < 800,
                    "is_synthetic": True,
                },
                {
                    "id": f"route-{origin_id.removeprefix('dest-')}-{dest_id.removeprefix('dest-')}-train",
                    "origin_city": origin_city,
                    "destination_id": dest_id,
                    "transport_type": "train",
                    "duration_minutes": train_duration,
                    "base_price": max(1200, int(900 + distance * 2.05)),
                    "stops": 0,
                    "departure_times": ["07:15", "18:30"],
                    "baggage_included": True,
                    "is_synthetic": True,
                },
            ])

    transfer_templates = []
    for dest_seed in DESTINATION_SEEDS:
        dest_id = dest_seed[0]
        for arrival_point in ("airport", "station"):
            for transfer_type, start, end, duration, price, always_open in [
                ("taxi", "00:00", "23:59", 35, 1500, True),
                ("shuttle", "06:00", "23:00", 50, 750, False),
                ("bus", "06:00", "22:00", 65, 220, False),
            ]:
                transfer_templates.append({
                    "id": f"transfer-{dest_id.removeprefix('dest-')}-{arrival_point}-{transfer_type}",
                    "destination_id": dest_id,
                    "arrival_point": arrival_point,
                    "transfer_type": transfer_type,
                    "operating_start": start,
                    "operating_end": end,
                    "duration_minutes": duration,
                    "base_price": price,
                    "operates_24h": always_open,
                    "is_synthetic": True,
                })

    world = {
        "destinations": destinations,
        "hotels": hotels,
        "transport_templates": transport_templates,
        "transfer_templates": transfer_templates,
    }
    WorldData.model_validate(world)
    return world


def main() -> None:
    output = ROOT / "data" / "world.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    world = build_world()
    content = json.dumps(world, ensure_ascii=False, indent=2)
    output.write_text(content, encoding="utf-8")
    packaged = ROOT / "src" / "travel_data" / "data" / "world.json"
    packaged.parent.mkdir(parents=True, exist_ok=True)
    packaged.write_text(content, encoding="utf-8")
    print(f"Generated {output}")
    print(f"Updated packaged copy {packaged}")
    print(f"destinations={len(world['destinations'])}, hotels={len(world['hotels'])}, "
          f"transport_templates={len(world['transport_templates'])}, "
          f"transfer_templates={len(world['transfer_templates'])}")


if __name__ == "__main__":
    main()
