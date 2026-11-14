"""Application settings, read from environment variables and validated at startup."""

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, PositiveFloat, PositiveInt, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "console"]


class Settings(BaseSettings):
    """Every setting has a default; each one can be overridden by an environment variable."""

    # Validation errors never echo the value: DATABASE_URL carries a password.
    model_config = SettingsConfigDict(
        env_file=".env", extra="ignore", frozen=True, hide_input_in_errors=True
    )

    database_url: str = "postgresql+asyncpg://agrobot:agrobot@localhost:5432/agrobot"
    db_pool_size: PositiveInt = 5
    db_max_overflow: PositiveInt = 10

    forecast_timezone: str = "America/Argentina/Buenos_Aires"
    forecast_max_age_hours: PositiveInt = 24

    evaluation_interval_seconds: PositiveFloat = 300
    evaluation_horizon_days: PositiveInt = 7
    alert_min_affected_area: Annotated[Decimal, Field(gt=0, le=1)] = Decimal("0.10")

    dispatch_interval_seconds: PositiveFloat = 15
    dispatch_batch_size: PositiveInt = 20
    # Below the worker's 30 s idle-in-transaction timeout (app/db.py) with a margin: a batch's
    # transaction is idle while it sends, and PostgreSQL would end it mid-batch.
    notifier_timeout_seconds: Annotated[float, Field(gt=0, le=25)] = 10
    notification_backoff_base_seconds: PositiveFloat = 60
    notification_backoff_max_seconds: PositiveFloat = 1800

    worker_heartbeat_dir: Path = Path("/tmp")

    log_level: LogLevel = "INFO"
    log_format: LogFormat = "json"

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        try:
            url = make_url(value)
        except ArgumentError as exc:
            raise ValueError("DATABASE_URL is not a valid database URL") from exc
        if url.drivername != "postgresql+asyncpg":
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg driver")
        if not url.database:
            raise ValueError("DATABASE_URL must name a database")
        return value

    @field_validator("forecast_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    @property
    def tz(self) -> ZoneInfo:
        """The forecast timezone, in which every forecast date and "today" are expressed."""
        return ZoneInfo(self.forecast_timezone)


@lru_cache
def get_settings() -> Settings:
    """The process-wide settings, built once from the environment."""
    return Settings()
