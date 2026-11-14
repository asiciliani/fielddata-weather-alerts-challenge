"""ORM models: the nine tables of the schema.

``users``, ``fields``, ``field_members``, ``zones``, ``field_zones`` and ``weather_forecasts``
stand in for tables Agrobot already has; ``event_types``, ``alerts`` and ``notifications`` belong
to this feature. Every rule the schema can express is a constraint here, so code that bypasses
the API cannot store data that breaks it.
"""

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.enums import FieldRole, NotificationStatus

NAMING_CONVENTION = {
    "pk": "pk_%(table_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ix": "ix_%(column_0_N_label)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}


def _str_enum(enum_cls: type[StrEnum], name: str) -> sa.Enum:
    """A short string column with a CHECK listing the enum's lowercase values."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=16,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


class Base(DeclarativeBase):
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    # Read server-generated values (ids, timestamps) back in the same statement.
    __mapper_args__ = {"eager_defaults": True}


def _id() -> Mapped[int]:
    return mapped_column(sa.BigInteger, sa.Identity(always=True), primary_key=True)


def _created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()
    )


# --- Agrobot stand-ins -------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = _id()
    first_name: Mapped[str] = mapped_column(sa.Text)
    last_name: Mapped[str] = mapped_column(sa.Text)
    phone_number: Mapped[str] = mapped_column(sa.Text, unique=True)  # E.164, the WhatsApp id
    created_at: Mapped[datetime] = _created_at()


class Field(Base):
    __tablename__ = "fields"

    id: Mapped[int] = _id()
    name: Mapped[str] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class FieldMember(Base):
    __tablename__ = "field_members"

    user_id: Mapped[int] = mapped_column(
        sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    field_id: Mapped[int] = mapped_column(
        sa.ForeignKey("fields.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[FieldRole] = mapped_column(_str_enum(FieldRole, "role"))


class Zone(Base):
    """One cell of the weather provider's grid, identified by its centre."""

    __tablename__ = "zones"
    __table_args__ = (
        sa.UniqueConstraint("latitude", "longitude"),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name="latitude_range"),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name="longitude_range"),
    )

    id: Mapped[int] = _id()
    latitude: Mapped[Decimal] = mapped_column(sa.Numeric(7, 4))
    longitude: Mapped[Decimal] = mapped_column(sa.Numeric(7, 4))
    created_at: Mapped[datetime] = _created_at()


class FieldZone(Base):
    """A zone a field touches, with the fraction of the field inside it."""

    __tablename__ = "field_zones"
    __table_args__ = (sa.CheckConstraint("coverage > 0 AND coverage <= 1", name="coverage_range"),)

    field_id: Mapped[int] = mapped_column(
        sa.ForeignKey("fields.id", ondelete="CASCADE"), primary_key=True
    )
    zone_id: Mapped[int] = mapped_column(
        sa.ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    coverage: Mapped[Decimal] = mapped_column(sa.Numeric(5, 4))


class WeatherForecast(Base):
    """One probability per zone, event type and date, written by the ingestion job."""

    __tablename__ = "weather_forecasts"
    __table_args__ = (
        # The ingestion upsert key; in this column order it also serves the evaluation join.
        sa.UniqueConstraint("zone_id", "event_type_id", "forecast_date"),
        sa.CheckConstraint("probability BETWEEN 0 AND 100", name="probability_range"),
    )

    id: Mapped[int] = _id()
    zone_id: Mapped[int] = mapped_column(sa.ForeignKey("zones.id", ondelete="CASCADE"))
    event_type_id: Mapped[int] = mapped_column(sa.ForeignKey("event_types.id", ondelete="RESTRICT"))
    forecast_date: Mapped[date] = mapped_column(sa.Date)
    probability: Mapped[int] = mapped_column(sa.SmallInteger)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# --- This feature ------------------------------------------------------------------------------


class EventType(Base):
    __tablename__ = "event_types"

    id: Mapped[int] = _id()
    code: Mapped[str] = mapped_column(sa.String(32), unique=True)
    name: Mapped[str] = mapped_column(sa.Text)  # Spanish label used in messages
    created_at: Mapped[datetime] = _created_at()


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        # The alert's user must be a member of its field; removing the membership removes it.
        sa.ForeignKeyConstraint(
            ["user_id", "field_id"],
            ["field_members.user_id", "field_members.field_id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("user_id", "field_id", "event_type_id"),
        sa.CheckConstraint("threshold BETWEEN 1 AND 100", name="threshold_range"),
    )

    id: Mapped[int] = _id()
    # The plain foreign keys give the ORM many-to-one paths to the user and the field.
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"))
    field_id: Mapped[int] = mapped_column(
        sa.ForeignKey("fields.id", ondelete="CASCADE"), index=True
    )
    event_type_id: Mapped[int] = mapped_column(sa.ForeignKey("event_types.id", ondelete="RESTRICT"))
    threshold: Mapped[int] = mapped_column(sa.SmallInteger)
    is_active: Mapped[bool] = mapped_column(sa.Boolean, server_default=sa.true())
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    user: Mapped[User] = relationship()
    field: Mapped[Field] = relationship()
    # Four rows, never null: every alert query carries its type, nothing lazy-loads it.
    event_type: Mapped[EventType] = relationship(lazy="joined", innerjoin=True)


class Notification(Base):
    """A detected (alert, date) pair, and the outbox row that delivers it."""

    __tablename__ = "notifications"
    __table_args__ = (
        sa.UniqueConstraint("alert_id", "forecast_date"),
        sa.CheckConstraint("threshold BETWEEN 1 AND 100", name="threshold_range"),
        sa.CheckConstraint("probability BETWEEN 0 AND 100", name="probability_range"),
        sa.CheckConstraint("affected_area > 0 AND affected_area <= 1", name="affected_area_range"),
        sa.CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        sa.CheckConstraint("status <> 'sent' OR sent_at IS NOT NULL", name="sent_has_timestamp"),
        # Serves the dispatcher's claim.
        sa.Index(None, "next_attempt_at", postgresql_where=sa.text("status = 'pending'")),
        # Serves the SET NULL when the ingestion job purges old forecasts.
        sa.Index(None, "forecast_id", postgresql_where=sa.text("forecast_id IS NOT NULL")),
    )

    # Rows are created by INSERT ... SELECT, so every default is a database-side default.
    id: Mapped[int] = _id()
    alert_id: Mapped[int] = mapped_column(sa.ForeignKey("alerts.id", ondelete="CASCADE"))
    forecast_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("weather_forecasts.id", ondelete="SET NULL")
    )
    forecast_date: Mapped[date] = mapped_column(sa.Date)
    threshold: Mapped[int] = mapped_column(sa.SmallInteger)
    probability: Mapped[int] = mapped_column(sa.SmallInteger)
    affected_area: Mapped[Decimal] = mapped_column(sa.Numeric(5, 4))
    status: Mapped[NotificationStatus] = mapped_column(
        _str_enum(NotificationStatus, "status"), server_default=NotificationStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(sa.SmallInteger, server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now()
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text)
    sent_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    alert: Mapped[Alert] = relationship()
