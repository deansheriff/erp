"""The one place the application reads the current time (UTC). Tests may patch `now`."""

from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC)
