import json
import logging
import sys

from app.config import Settings
from app.logging import ConsoleFormatter, JsonFormatter, configure_logging


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        "app.test", logging.WARNING, __file__, 1, "something_happened", None, None
    )
    record.__dict__.update(extra)
    return record


def test_json_formatter_includes_extra_fields() -> None:
    line = JsonFormatter().format(_record(alert_id=7, notification_ids=[1, 2]))
    payload = json.loads(line)
    assert payload["message"] == "something_happened"
    assert payload["level"] == "WARNING"
    assert payload["logger"] == "app.test"
    assert payload["alert_id"] == 7
    assert payload["notification_ids"] == [1, 2]
    assert "time" in payload


def test_json_formatter_includes_traceback() -> None:
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = _record()
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert "RuntimeError: boom" in payload["exc_info"]


def test_console_formatter_appends_extras() -> None:
    line = ConsoleFormatter().format(_record(alert_id=7))
    assert line.endswith("something_happened alert_id=7")


def test_configure_logging_replaces_only_its_own_handler() -> None:
    root = logging.getLogger()
    before = list(root.handlers)
    settings = Settings(_env_file=None, log_format="json", log_level="DEBUG")
    configure_logging(settings)
    configure_logging(settings)
    added = [h for h in root.handlers if h not in before]
    assert len(added) == 1
    assert isinstance(added[0].formatter, JsonFormatter)
    assert root.level == logging.DEBUG
    root.removeHandler(added[0])


def test_server_logs_go_through_the_app_handler() -> None:
    access = logging.getLogger("uvicorn.access")
    access.addHandler(logging.NullHandler())
    access.propagate = False
    configure_logging(Settings(_env_file=None))
    assert access.handlers == [] and access.propagate is True
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h.formatter, JsonFormatter)]:
        root.removeHandler(h)
