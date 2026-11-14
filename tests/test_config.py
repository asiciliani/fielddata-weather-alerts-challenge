from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def make(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_defaults() -> None:
    s = make()
    assert s.database_url == "postgresql+asyncpg://agrobot:agrobot@localhost:5432/agrobot"
    assert (s.db_pool_size, s.db_max_overflow) == (5, 10)
    assert s.forecast_timezone == "America/Argentina/Buenos_Aires"
    assert s.forecast_max_age_hours == 24
    assert s.evaluation_interval_seconds == 300
    assert s.evaluation_horizon_days == 7
    assert s.alert_min_affected_area == Decimal("0.10")
    assert s.dispatch_interval_seconds == 15
    assert s.dispatch_batch_size == 20
    assert s.notifier_timeout_seconds == 10
    assert s.notification_backoff_base_seconds == 60
    assert s.notification_backoff_max_seconds == 1800
    assert s.worker_heartbeat_dir == Path("/tmp")
    assert (s.log_level, s.log_format) == ("INFO", "json")
    assert s.tz.key == "America/Argentina/Buenos_Aires"


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALUATION_HORIZON_DAYS", "3")
    monkeypatch.setenv("ALERT_MIN_AFFECTED_AREA", "0.25")
    monkeypatch.setenv("FORECAST_TIMEZONE", "UTC")
    monkeypatch.setenv("LOG_FORMAT", "console")
    s = make()
    assert s.evaluation_horizon_days == 3
    assert s.alert_min_affected_area == Decimal("0.25")
    assert s.forecast_timezone == "UTC"
    assert s.log_format == "console"


@pytest.mark.parametrize(
    "overrides",
    [
        {"forecast_timezone": "Mars/Olympus_Mons"},
        {"evaluation_interval_seconds": 0},
        {"dispatch_interval_seconds": -5},
        {"dispatch_batch_size": 0},
        {"alert_min_affected_area": 0},
        {"alert_min_affected_area": "1.5"},
        {"database_url": "not a url"},
        {"database_url": "mysql://u:p@localhost/db"},
        {"database_url": "postgresql+asyncpg://u:p@localhost"},
        {"log_format": "xml"},
    ],
)
def test_invalid_values_fail_validation(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        make(**overrides)
