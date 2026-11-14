"""Agrobot stand-ins: users, fields, memberships, zones, field zones and forecasts.

These tables already exist in Agrobot. This revision creates minimal versions of them for the
demo and the tests only; in Agrobot, ``0002_weather_alerts`` would be attached to Agrobot's own
latest migration instead.

Revision ID: 0001_agrobot_stand_ins
Revises:
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001_agrobot_stand_ins"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _created_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _updated_at() -> sa.Column[sa.DateTime]:
    return sa.Column(
        "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )


def _id() -> sa.Column[sa.BigInteger]:
    return sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False)


def upgrade() -> None:
    op.create_table(
        "users",
        _id(),
        sa.Column("first_name", sa.Text, nullable=False),
        sa.Column("last_name", sa.Text, nullable=False),
        sa.Column("phone_number", sa.Text, nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("phone_number", name="uq_users_phone_number"),
    )
    op.create_table(
        "fields",
        _id(),
        sa.Column("name", sa.Text, nullable=False),
        _created_at(),
        _updated_at(),
        sa.PrimaryKeyConstraint("id", name="pk_fields"),
    )
    op.create_table(
        "field_members",
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("field_id", sa.BigInteger, nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'agronomist', 'manager')", name=op.f("ck_field_members_role")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_field_members_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["field_id"],
            ["fields.id"],
            name="fk_field_members_field_id_fields",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "field_id", name="pk_field_members"),
    )
    op.create_table(
        "zones",
        _id(),
        sa.Column("latitude", sa.Numeric(7, 4), nullable=False),
        sa.Column("longitude", sa.Numeric(7, 4), nullable=False),
        _created_at(),
        sa.CheckConstraint("latitude BETWEEN -90 AND 90", name=op.f("ck_zones_latitude_range")),
        sa.CheckConstraint("longitude BETWEEN -180 AND 180", name=op.f("ck_zones_longitude_range")),
        sa.PrimaryKeyConstraint("id", name="pk_zones"),
        sa.UniqueConstraint("latitude", "longitude", name="uq_zones_latitude_longitude"),
    )
    op.create_table(
        "field_zones",
        sa.Column("field_id", sa.BigInteger, nullable=False),
        sa.Column("zone_id", sa.BigInteger, nullable=False),
        sa.Column("coverage", sa.Numeric(5, 4), nullable=False),
        sa.CheckConstraint(
            "coverage > 0 AND coverage <= 1", name=op.f("ck_field_zones_coverage_range")
        ),
        sa.ForeignKeyConstraint(
            ["field_id"], ["fields.id"], name="fk_field_zones_field_id_fields", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name="fk_field_zones_zone_id_zones", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("field_id", "zone_id", name="pk_field_zones"),
    )
    op.create_index("ix_field_zones_zone_id", "field_zones", ["zone_id"])
    op.create_table(
        "weather_forecasts",
        _id(),
        sa.Column("zone_id", sa.BigInteger, nullable=False),
        # Its foreign key to event_types is added by 0002_weather_alerts, which owns that table.
        sa.Column("event_type_id", sa.BigInteger, nullable=False),
        sa.Column("forecast_date", sa.Date, nullable=False),
        sa.Column("probability", sa.SmallInteger, nullable=False),
        _created_at(),
        _updated_at(),
        sa.CheckConstraint(
            "probability BETWEEN 0 AND 100", name=op.f("ck_weather_forecasts_probability_range")
        ),
        sa.ForeignKeyConstraint(
            ["zone_id"], ["zones.id"], name="fk_weather_forecasts_zone_id_zones", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_weather_forecasts"),
        sa.UniqueConstraint(
            "zone_id",
            "event_type_id",
            "forecast_date",
            name="uq_weather_forecasts_zone_id_event_type_id_forecast_date",
        ),
    )


def downgrade() -> None:
    op.drop_table("weather_forecasts")
    op.drop_index("ix_field_zones_zone_id", table_name="field_zones")
    op.drop_table("field_zones")
    op.drop_table("zones")
    op.drop_table("field_members")
    op.drop_table("fields")
    op.drop_table("users")
