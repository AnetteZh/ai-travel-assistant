from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from travel_data import (
    Guests,
    Weights,
)
from travel_data.models import (
    Cart,
    DestinationSearchResult,
    HotelSearchResult,
    TravelRequest,
    TransferSearchResult,
    TransportOption,
    TransportSearchResult,
    Evidence,
    RankedHotel,
    TransferOption,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class TripRequest(StrictModel):
    origin: str | None = None
    destination: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    flexible_days: int = Field(default=0, ge=0, le=14)
    guests: Guests = Field(default_factory=Guests)
    budget_total: int | None = Field(default=None, gt=0)
    hard: list[str] = Field(default_factory=list)
    soft: list[str] = Field(default_factory=list)
    mood: str | None = None
    weights: Weights = Field(default_factory=Weights)

    @model_validator(mode="after")
    def validate_dates(self) -> "TripRequest":
        if self.date_from and self.date_to and self.date_to <= self.date_from:
            raise ValueError("date_to must be later than date_from")
        return self

    def to_data_request(self, destination_id: str | None) -> TravelRequest:
        return TravelRequest(
            origin=self.origin,
            destination_id=destination_id,
            date_from=self.date_from,
            date_to=self.date_to,
            flexible_days=self.flexible_days,
            guests=self.guests,
            budget_total=self.budget_total,
            hard_requirements=self.hard,
            soft_preferences=self.soft,
            mood=self.mood,
            weights=self.weights,
        )


class TripRequestPatch(StrictModel):
    origin: str | None = None
    destination: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    flexible_days: int | None = Field(default=None, ge=0, le=14)
    guests: Guests | None = None
    budget_total: int | None = Field(default=None, gt=0)
    hard: list[str] | None = None
    soft: list[str] | None = None
    mood: str | None = None
    weights: Weights | None = None


class RequestExtraction(StrictModel):
    """Structured extraction of trip updates from one user message."""

    patch: TripRequestPatch = Field(default_factory=TripRequestPatch)
    intent: Literal["suggest_destinations", "plan_trip", "update", "question", "details"] = "plan_trip"
    clarification_question: str | None = None
    detail_target: str | None = None


class TravelPlan(StrictModel):
    destination_id: str
    hotel: RankedHotel
    outbound: TransportOption | None = None
    inbound: TransportOption | None = None
    arrival_transfer: TransferOption | None = None
    departure_transfer: TransferOption | None = None
    date_from: date
    date_to: date


class TripIssue(StrictModel):
    code: str
    message: str
    severity: Literal["info", "warning", "error"] = "warning"
    evidence: list[Evidence] = Field(default_factory=list)


class TraceEntry(StrictModel):
    stage: str
    tool: str | None = None
    outcome: str
    reason: str
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class UIBlock(StrictModel):
    type: Literal[
        "message",
        "assumptions",
        "destination_cards",
        "option_cards",
        "comparison",
        "trip_timeline",
        "plan_diff",
        "cart",
        "trace",
    ]
    data: dict[str, Any]


class TripState(StrictModel):
    request: TripRequest = Field(default_factory=TripRequest)
    assumptions: list[str] = Field(default_factory=list)
    destinations: DestinationSearchResult | None = None
    hotels: HotelSearchResult | None = None
    transport: TransportSearchResult | None = None
    transfers: TransferSearchResult | None = None
    departure_transfers: TransferSearchResult | None = None
    plan: TravelPlan | None = None
    previous_plan: TravelPlan | None = None
    cart: Cart | None = None
    issues: list[TripIssue] = Field(default_factory=list)
    stage: str = "idle"
    version: int = Field(default=0, ge=0)
    plan_diff: dict[str, Any] | None = None
    previous_cart: Cart | None = None
    repair_attempts: int = Field(default=0, ge=0, le=1)
    versioned_this_turn: bool = False
    clarification_question: str | None = None
    detail_target: str | None = None
    response_text: str | None = None
    response_mode: Literal["summary", "details"] = "summary"
    blocks: list[UIBlock] = Field(default_factory=list)
    trace: list[TraceEntry] = Field(default_factory=list)
    tool_calls: dict[str, int] = Field(default_factory=dict)
    # Направления, уже показанные в этой подборке: «покажи ещё» их исключает.
    shown_destination_ids: list[str] = Field(default_factory=list)


class ChatInput(StrictModel):
    message: str = Field(min_length=1, max_length=12000)
    conversation_id: str | None = None


class PatchInput(StrictModel):
    patch: TripRequestPatch
