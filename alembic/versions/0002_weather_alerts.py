"""Weather alerts: event types (with the four types), alerts and notifications.

Revision ID: 0002_weather_alerts
Revises: 0001_agrobot_stand_ins
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_weather_alerts"
down_revision: str | None = "0001_agrobot_stand_ins"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EVENT_TYPES = [
    {"code": "frost", "name": "helada"},
    {"code": "rain", "name": "lluvia"},
    {"code": "strong_wind", "name": "viento fuerte"},
    {"code": "heat", "name": "calor extremo"},
]


def _timestamp(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(name, sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


def _id() -> sa.Column[sa.BigInteger]:
    return sa.Column("id", sa.BigInteger, sa.Identity(always=True), nullable=False)


def upgrade() -> None:
    event_types = op.create_table(
        "event_types",
        _id(),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id", name="pk_event_types"),
        sa.UniqueConstraint("code", name="uq_event_types_code"),
    )
    op.bulk_insert(event_types, EVENT_TYPES)
    op.create_foreign_key(
        "fk_weather_forecasts_event_type_id_event_types",
        "weather_forecasts",
        "event_types",
        ["event_type_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "alerts",
        _id(),
        sa.Column("user_id", sa.BigInteger, nullable=False),
        sa.Column("field_id", sa.BigInteger, nullable=False),
        sa.Column("event_type_id", sa.BigInteger, nullable=False),
        sa.Column("threshold", sa.SmallInteger, nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint("threshold BETWEEN 1 AND 100", name=op.f("ck_alerts_threshold_range")),
        # The alert's user must be a member of its field; removing the membership removes it.
        sa.ForeignKeyConstraint(
            ["user_id", "field_id"],
            ["field_members.user_id", "field_members.field_id"],
            name="fk_alerts_user_id_field_members",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_alerts_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["field_id"], ["fields.id"], name="fk_alerts_field_id_fields", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["event_type_id"],
            ["event_types.id"],
            name="fk_alerts_event_type_id_event_types",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        sa.UniqueConstraint(
            "user_id", "field_id", "event_type_id", name="uq_alerts_user_id_field_id_event_type_id"
        ),
    )
    op.create_index("ix_alerts_field_id", "alerts", ["field_id"])

    op.create_table(
        "notifications",
        _id(),
        sa.Column("alert_id", sa.BigInteger, nullable=False),
        sa.Column("forecast_id", sa.BigInteger, nullable=True),
        sa.Column("forecast_date", sa.Date, nullable=False),
        sa.Column("threshold", sa.SmallInteger, nullable=False),
        sa.Column("probability", sa.SmallInteger, nullable=False),
        sa.Column("affected_area", sa.Numeric(5, 4), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.SmallInteger, server_default="0", nullable=False),
        _timestamp("next_attempt_at"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(
            "threshold BETWEEN 1 AND 100", name=op.f("ck_notifications_threshold_range")
        ),
        sa.CheckConstraint(
            "probability BETWEEN 0 AND 100", name=op.f("ck_notifications_probability_range")
        ),
        sa.CheckConstraint(
            "affected_area > 0 AND affected_area <= 1",
            name=op.f("ck_notifications_affected_area_range"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_notifications_attempts_non_negative")),
        sa.CheckConstraint(
            "status <> 'sent' OR sent_at IS NOT NULL",
            name=op.f("ck_notifications_sent_has_timestamp"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed')", name=op.f("ck_notifications_status")
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"], name="fk_notifications_alert_id_alerts", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["forecast_id"],
            ["weather_forecasts.id"],
            name="fk_notifications_forecast_id_weather_forecasts",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
        sa.UniqueConstraint(
            "alert_id", "forecast_date", name="uq_notifications_alert_id_forecast_date"
        ),
    )
    # Serves the dispatcher's claim.
    op.create_index(
        "ix_notifications_next_attempt_at",
        "notifications",
        ["next_attempt_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Serves the SET NULL when the ingestion job purges old forecasts.
    op.create_index(
        "ix_notifications_forecast_id",
        "notifications",
        ["forecast_id"],
        postgresql_where=sa.text("forecast_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_forecast_id", table_name="notifications")
    op.drop_index("ix_notifications_next_attempt_at", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_alerts_field_id", table_name="alerts")
    op.drop_table("alerts")
    op.drop_constraint(
        "fk_weather_forecasts_event_type_id_event_types", "weather_forecasts", type_="foreignkey"
    )
    op.drop_table("event_types")
