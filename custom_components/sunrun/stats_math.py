"""Turn Sunrun production data into cumulative hourly statistics rows.

Pure functions with no Home Assistant imports so they can be tested anywhere.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo


def _hour_floor(dt: datetime) -> datetime:
    # Floor in UTC: statistics starts must sit on a UTC hour, which also keeps the
    # repeated hour at a DST fall-back as two distinct rows.
    return dt.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _daily_slot(day: date, tz: tzinfo) -> datetime:
    """Daily totals have no intra-day shape, so they land on the day's last hour."""
    return _hour_floor(datetime.combine(day, time(23), tzinfo=tz))


def build_rows(
    minute_slots: Iterable[tuple[datetime, float]],
    daily: Mapping[date, float],
    tz: tzinfo,
    *,
    minute_from: date,
    base_sum: float = 0.0,
    baseline: bool = False,
) -> list[tuple[datetime, float]]:
    """Return ascending (hour start in UTC, running total) rows.

    Days before `minute_from` come from `daily`, later days from the 15-minute slots.
    A later day with a daily total but no slots still gets that total, so a gap in the
    minute data does not break the running sum. `baseline` adds a zero row the night
    before the earliest day, so the first real hour has a prior sum to diff against.
    """
    energy: dict[datetime, float] = defaultdict(float)
    slot_days: set[date] = set()

    for ts, kwh in minute_slots:
        local_day = ts.astimezone(tz).date()
        if local_day < minute_from:
            continue
        energy[_hour_floor(ts)] += kwh
        slot_days.add(local_day)

    for day, kwh in daily.items():
        if day < minute_from or day not in slot_days:
            energy[_daily_slot(day, tz)] += kwh

    if baseline:
        first_day = min(slot_days | set(daily), default=None)
        if first_day is not None:
            energy.setdefault(_daily_slot(first_day - timedelta(days=1), tz), 0.0)

    total = base_sum
    rows = []
    for start in sorted(energy):
        total += energy[start]
        rows.append((start, round(total, 6)))
    return rows
