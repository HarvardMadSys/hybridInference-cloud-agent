"""The time ranges an owner's Usage page offers, and the buckets they are drawn in.

Every range is in UTC calendar days, the convention the Usage page and the
Gateway's daily quota already share, and every read of one owner's history —
model usage, container use, context insights — resolves its range here, so
the three describe the same interval:

- ``today``: from the current UTC day's midnight to now.
- ``7d`` and ``30d``: the last 7 or 30 UTC days, today included, to now.
- ``all``: from the UTC day of the owner's earliest retained record (their
  first run or their first container, whichever came first) to now. Nothing
  narrows it: an owner whose history is too large for one bounded read is read
  in bounded batches instead (see :mod:`cloud_agent.usage_batches`).

A range's bars are buckets chosen here, so the browser never merges or sums
them: days for up to :data:`MAX_DAY_BUCKETS`, ISO weeks (Monday, UTC) for up
to :data:`MAX_WEEK_BUCKETS`, and calendar months beyond.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

RangePreset = Literal["today", "7d", "30d", "all"]
RANGE_PRESETS: tuple[RangePreset, ...] = ("today", "7d", "30d", "all")
BucketSize = Literal["day", "week", "month"]

_PRESET_DAYS: dict[str, int] = {"today": 1, "7d": 7, "30d": 30}
#: A quarter of days still draws as one bar per day.
MAX_DAY_BUCKETS = 92
#: Three years of weeks; longer histories are drawn by month.
MAX_WEEK_BUCKETS = 156

Bucket = tuple[datetime, datetime]


class UsageRange(BaseModel):
    """The interval one read covers, stated so a reader can label it."""

    model_config = ConfigDict(extra="forbid")

    preset: RangePreset | None = None
    #: Days, for a read that named a day count rather than a preset.
    days: int | None = None
    since: datetime
    until: datetime
    day_timezone: Literal["UTC"] = "UTC"
    bucket: BucketSize
    #: For ``all``: the earliest retained record the range starts from; None
    #: when the owner has none and the range is today alone.
    first_record_at: datetime | None = None


def _midnight(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def bucket_size(since: datetime, until: datetime) -> BucketSize:
    """The coarsest bar a range needs to stay readable and its response bounded."""
    days = (_midnight(until - timedelta(microseconds=1)) - _midnight(since)).days + 1
    if days <= MAX_DAY_BUCKETS:
        return "day"
    if days <= MAX_WEEK_BUCKETS * 7:
        return "week"
    return "month"


def bucket_start(value: datetime, size: BucketSize) -> datetime:
    """The start of the bucket an instant falls in."""
    day = _midnight(value)
    if size == "week":
        return day - timedelta(days=day.weekday())
    if size == "month":
        return day.replace(day=1)
    return day


def _next_start(start: datetime, size: BucketSize) -> datetime:
    if size == "day":
        return start + timedelta(days=1)
    if size == "week":
        return start + timedelta(days=7)
    year, month = (start.year + 1, 1) if start.month == 12 else (start.year, start.month + 1)
    return start.replace(year=year, month=month)


def buckets(since: datetime, until: datetime, size: BucketSize) -> list[Bucket]:
    """Every bucket intersecting ``[since, until)``, clipped to it."""
    spans: list[Bucket] = []
    start = bucket_start(since, size)
    while start < until:
        end = _next_start(start, size)
        spans.append((max(start, since), min(end, until)))
        start = end
    return spans


def bucket_key(value: datetime, size: BucketSize) -> str:
    """Name the bucket a run created at ``value`` is counted in."""
    return bucket_start(value, size).date().isoformat()


def resolve_range(
    preset: RangePreset,
    *,
    first_record_at: datetime | None,
    now: datetime | None = None,
) -> UsageRange:
    """Resolve a preset against now and, for ``all``, the owner's first record."""
    until = (now or datetime.now(UTC)).astimezone(UTC)
    today = _midnight(until)
    first = first_record_at.astimezone(UTC) if first_record_at is not None else None
    if preset == "all":
        since = min(_midnight(first), today) if first is not None else today
    else:
        since = today - timedelta(days=_PRESET_DAYS[preset] - 1)
    return UsageRange(
        preset=preset,
        since=since,
        until=until,
        bucket=bucket_size(since, until),
        first_record_at=first if preset == "all" else None,
    )


def day_range(days: int, *, max_days: int, now: datetime | None = None) -> UsageRange:
    """The legacy ``days=N`` read: the last N UTC days, today included."""
    if not 1 <= days <= max_days:
        raise ValueError(f"Choose between 1 and {max_days} days.")
    until = (now or datetime.now(UTC)).astimezone(UTC)
    since = _midnight(until) - timedelta(days=days - 1)
    return UsageRange(days=days, since=since, until=until, bucket=bucket_size(since, until))


def bucket_dates(spans: list[Bucket]) -> list[tuple[date, date]]:
    """Each bucket's first day and the day after its last, for day-keyed tables."""
    result = []
    for start, end in spans:
        last = _midnight(end - timedelta(microseconds=1))
        result.append((_midnight(start).date(), last.date() + timedelta(days=1)))
    return result
