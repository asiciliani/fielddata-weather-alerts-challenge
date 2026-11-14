"""The two value sets owned by the code, stored as lowercase strings with a CHECK."""

from enum import StrEnum


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"


class FieldRole(StrEnum):
    OWNER = "owner"
    AGRONOMIST = "agronomist"
    MANAGER = "manager"
