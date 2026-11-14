"""Request and response models of the API, and the page wrapper."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Self

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from app.enums import FieldRole, NotificationStatus

Threshold = Annotated[StrictInt, Field(ge=1, le=100, description="probability in percent")]
EventTypeCode = Annotated[str, Field(min_length=1, max_length=32, examples=["frost"])]


class Page[T](BaseModel):
    items: list[T]
    limit: int
    offset: int


class FieldRead(BaseModel):
    id: int
    name: str
    role: FieldRole


class EventTypeRead(BaseModel):
    code: str
    name: str


class AlertCreate(BaseModel):
    field_id: Annotated[StrictInt, Field(ge=1)]
    event_type: EventTypeCode
    threshold: Threshold
    is_active: StrictBool = True


class AlertUpdate(BaseModel):
    """Only the threshold and the active flag can change; at least one, never ``null``."""

    model_config = ConfigDict(extra="forbid")

    threshold: Threshold | None = None
    is_active: StrictBool | None = None

    @model_validator(mode="after")
    def _at_least_one_non_null(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("provide threshold, is_active or both")
        for name in self.model_fields_set:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    def changes(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.model_fields_set}


class AlertRead(BaseModel):
    id: int
    field_id: int
    event_type: str
    threshold: int
    is_active: bool
    zones_mapped: bool = Field(
        description="false while the field has no zones yet: the alert cannot fire until it does"
    )
    created_at: datetime
    updated_at: datetime


class NotificationRead(BaseModel):
    id: int
    alert_id: int
    forecast_id: int | None
    field_id: int
    event_type: str
    forecast_date: date
    threshold: int
    probability: int
    affected_area: Decimal
    status: NotificationStatus
    sent_at: datetime | None
    created_at: datetime
