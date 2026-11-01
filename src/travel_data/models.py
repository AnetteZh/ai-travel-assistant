from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    """Shared boundary model: reject undeclared fields and disallow reassignment."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Guests(StrictModel):
    adults: int = Field(default=2, ge=1, le=12)
    children_ages: list[int] = Field(default_factory=list, max_length=10)
    pets: int = Field(default=0, ge=0, le=5)

    @model_validator(mode="after")
    def validate_children(self) -> "Guests":
        if any(age < 0 or age > 17 for age in self.children_ages):
            raise ValueError("children_ages must contain ages from 0 to 17")
        return self


class Weights(StrictModel):
    price: float = Field(default=0.35, ge=0)
    travel_time: float = Field(default=0.20, ge=0)
    cancellation: float = Field(default=0.20, ge=0)
    location: float = Field(default=0.15, ge=0)
    preferences: float = Field(default=0.10, ge=0)

    @model_validator(mode="after")
    def validate_sum(self) -> "Weights":
        if sum((self.price, self.travel_time, self.cancellation, self.location, self.preferences)) <= 0:
            raise ValueError("At least one ranking weight must be greater than zero")
        return self


class TravelBrief(StrictModel):
    """User preferences subset shared by Core and Data: guests, hard/soft and mood."""

    guests: Guests = Field(default_factory=Guests)
    hard_requirements: list[str] = Field(default_factory=list)
    soft_preferences: list[str] = Field(default_factory=list)
    mood: str | None = None


class TravelRequest(TravelBrief):
    """Request envelope used by Data tools; Core maps its TripRequest to this model."""

    origin: str | None = None
    destination_id: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    flexible_days: int = Field(default=0, ge=0, le=14)
    budget_total: int | None = Field(default=None, gt=0)
    weights: Weights = Field(default_factory=Weights)
    # «Покажи ещё»: направления, которые пользователь уже видел, в подборку не попадают.
    exclude_destination_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_dates(self) -> "TravelRequest":
        if self.date_from and self.date_to and self.date_to <= self.date_from:
            raise ValueError("date_to must be later than date_from")
        return self


class DestinationRecord(StrictModel):
    id: str
    name: str
    region: str
    country: str = "Россия"
    airport: str
    station: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str
    tags: list[str] = Field(default_factory=list)
    typical_price_per_night: int = Field(gt=0)
    climate_monthly: dict[str, dict[str, float]]
    is_synthetic: bool = True


class HotelRecord(StrictModel):
    id: str
    destination_id: str
    name: str
    city: str
    price_per_night: int = Field(gt=0)
    stars: int = Field(ge=1, le=5)
    has_pool: bool
    has_pets_allowed: bool
    has_spa: bool
    rating: float = Field(ge=1, le=5)
    description: str
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    cancellation_policy: Literal["free_until_7_days", "free_until_3_days", "partial", "non_refundable"]
    breakfast_included: bool
    family_friendly: bool
    kids_club: bool
    checkin_from: str
    checkin_until: str
    checkout_time: str
    reception_open_from: str
    reception_open_until: str
    distance_to_center_km: float = Field(ge=0)
    distance_to_beach_km: float | None = Field(default=None, ge=0)
    noise_level: Literal["quiet", "moderate", "noisy"]
    amenities: list[str] = Field(default_factory=list)
    is_synthetic: bool = True


class TransportTemplate(StrictModel):
    id: str
    origin_city: str
    destination_id: str
    transport_type: Literal["flight", "train"]
    duration_minutes: int = Field(gt=0)
    base_price: int = Field(gt=0)
    stops: int = Field(ge=0, le=5)
    departure_times: list[str] = Field(min_length=1)
    baggage_included: bool
    is_synthetic: bool = True


class TransferTemplate(StrictModel):
    id: str
    destination_id: str
    arrival_point: Literal["airport", "station"]
    transfer_type: Literal["taxi", "shuttle", "bus"]
    operating_start: str
    operating_end: str
    duration_minutes: int = Field(gt=0)
    base_price: int = Field(ge=0)
    operates_24h: bool = False
    is_synthetic: bool = True


class WorldData(StrictModel):
    destinations: list[DestinationRecord]
    hotels: list[HotelRecord]
    transport_templates: list[TransportTemplate]
    transfer_templates: list[TransferTemplate]


class Evidence(StrictModel):
    id: str
    source_type: Literal["hotel", "destination", "transport", "transfer", "weather", "cart"]
    source_id: str
    field_path: str
    value: Any
    is_synthetic: bool


class WeatherSummary(StrictModel):
    destination_id: str
    target_date: date
    temperature_min_c: float
    temperature_max_c: float
    precipitation_probability: float | None = None
    weather_code: int | None = None
    description: str
    source: str
    is_synthetic: bool


class DestinationSuggestion(StrictModel):
    destination: DestinationRecord
    estimated_trip_price: int
    fastest_travel_minutes: int | None
    weather: WeatherSummary
    score: float
    reason_codes: list[str]
    pros: list[str]
    cons: list[str]
    why_it_fits: str
    evidence: list[Evidence]


class DestinationSearchResult(StrictModel):
    options: list[DestinationSuggestion]
    assumptions: list[str] = Field(default_factory=list)
    message: str
    is_synthetic: bool = True


class RankedHotel(StrictModel):
    hotel: HotelRecord
    estimated_lodging_total: int
    score: float
    reason_codes: list[str]
    risk_codes: list[str]
    pros: list[str]
    cons: list[str]
    why_it_fits: str
    violated_requirements: list[str] = Field(default_factory=list)
    evidence: list[Evidence]


class HotelSearchResult(StrictModel):
    destination_id: str
    date_from: date
    date_to: date
    nights: int
    options: list[RankedHotel]
    alternatives: list[RankedHotel]
    message: str
    assumptions: list[str] = Field(default_factory=list)
    is_synthetic: bool = True


class TransportOption(StrictModel):
    id: str
    origin_city: str
    destination_city: str
    origin_point: str
    destination_point: str
    transport_type: Literal["flight", "train"]
    departure_at: datetime
    arrival_at: datetime
    duration_minutes: int = Field(gt=0)
    stops: int = Field(ge=0)
    baggage_included: bool
    price: int = Field(gt=0)
    score: float = 0
    reason_codes: list[str] = Field(default_factory=list)
    risk_codes: list[str] = Field(default_factory=list)
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    why_it_fits: str = ""
    is_synthetic: bool = True
    evidence: list[Evidence] = Field(default_factory=list)


class TransportSearchResult(StrictModel):
    origin: str
    destination_id: str
    outbound: list[TransportOption]
    return_options: list[TransportOption]
    assumptions: list[str] = Field(default_factory=list)
    message: str
    is_synthetic: bool = True


class TransferOption(StrictModel):
    id: str
    destination_id: str
    hotel_id: str
    arrival_point: Literal["airport", "station"]
    direction: Literal["to_hotel", "to_terminal"] = "to_hotel"
    transfer_type: Literal["taxi", "shuttle", "bus"]
    arrival_at: datetime | None = None
    terminal_departure_at: datetime | None = None
    pickup_at: datetime
    duration_minutes: int = Field(gt=0)
    distance_km: float = Field(ge=0)
    price: int = Field(ge=0)
    is_available: bool
    unavailable_reason: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    risk_codes: list[str] = Field(default_factory=list)
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    why_it_fits: str = ""
    is_synthetic: bool = True
    evidence: list[Evidence] = Field(default_factory=list)


class TransferSearchResult(StrictModel):
    options: list[TransferOption]
    unavailable_options: list[TransferOption]
    message: str
    is_synthetic: bool = True


class CartSelection(StrictModel):
    destination_id: str
    hotel_id: str
    date_from: date
    date_to: date
    transport_ids: list[str] = Field(default_factory=list)
    transfer_ids: list[str] = Field(default_factory=list)
    # Full selected options can be passed from TripState so repricing works after process restart.
    transport_options: list[TransportOption] = Field(default_factory=list)
    transfer_options: list[TransferOption] = Field(default_factory=list)
    guests: Guests = Field(default_factory=Guests)
    budget_total: int | None = Field(default=None, gt=0)
    origin: str | None = None

    @model_validator(mode="after")
    def validate_dates(self) -> "CartSelection":
        if self.date_to <= self.date_from:
            raise ValueError("date_to must be later than date_from")
        return self


class CartItem(StrictModel):
    id: str
    item_type: Literal["hotel", "transport", "transfer", "fee"]
    title: str
    amount: int = Field(ge=0)
    is_synthetic: bool = True
    evidence: list[Evidence] = Field(default_factory=list)


class Cart(StrictModel):
    id: str
    destination_id: str
    hotel_id: str
    date_from: date
    date_to: date
    nights: int = Field(gt=0)
    guests: Guests
    items: list[CartItem]
    subtotal: int = Field(ge=0)
    total: int = Field(ge=0)
    currency: Literal["RUB"] = "RUB"
    budget_total: int | None = None
    over_budget: bool = False
    over_budget_percent: float = 0
    risk_codes: list[str] = Field(default_factory=list)
    is_synthetic: bool = True
    note: str = "Цены синтетические; бронирование не выполняется у реальных поставщиков."


class BookingResult(StrictModel):
    booking_id: str
    status: Literal["confirmed_demo"] = "confirmed_demo"
    booked_at: datetime
    cart: Cart
    message: str = "Это демонстрационная бронь. Реальная оплата и бронирование не выполнялись."


# Explicit tool input contracts for the LangChain/LangGraph adapter.
class SuggestDestinationsToolInput(StrictModel):
    request: TravelRequest = Field(default_factory=TravelRequest)


class SearchHotelsToolInput(StrictModel):
    request: TravelRequest
    destination_id: str


class SearchTransportToolInput(StrictModel):
    request: TravelRequest
    destination_id: str


class SearchTransfersToolInput(StrictModel):
    destination_id: str
    hotel_id: str
    arrival_at: datetime
    arrival_point: Literal["airport", "station"] = "airport"
    direction: Literal["to_hotel", "to_terminal"] = "to_hotel"


class PriceCartToolInput(StrictModel):
    selection: CartSelection


class BookToolInput(StrictModel):
    cart: Cart
