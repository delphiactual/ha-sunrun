"""Tests for hourly statistics aggregation."""

from __future__ import annotations

import importlib.util
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

# Loaded by path so the package __init__ (which needs Home Assistant) is not imported.
_path = Path(__file__).parent.parent / "custom_components" / "sunrun" / "stats_math.py"
_spec = importlib.util.spec_from_file_location("stats_math", _path)
stats_math = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stats_math)
build_rows = stats_math.build_rows

TZ = ZoneInfo("America/Denver")
SLOT = timedelta(minutes=15)


def day_slots(day: date, kwh: float = 0.1) -> list[tuple[datetime, float]]:
    """Every 15-minute slot of a local day, as the API reports them (slot start)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=TZ).astimezone(UTC)
    out, ts = [], start
    while ts < end:
        out.append((ts.astimezone(TZ), kwh))
        ts += SLOT
    return out


def local_hours(rows):
    return [start.astimezone(TZ) for start, _ in rows]


def test_quarter_hours_sum_into_the_hour():
    day = date(2026, 6, 1)
    slots = [
        (datetime(2026, 6, 1, 9, m, tzinfo=TZ), v)
        for m, v in ((0, 0.1), (15, 0.2), (30, 0.3), (45, 0.4))
    ]
    rows = build_rows(slots, {}, TZ, minute_from=day)
    assert len(rows) == 1
    assert local_hours(rows)[0] == datetime(2026, 6, 1, 9, tzinfo=TZ)
    assert rows[0][1] == pytest.approx(1.0)


def test_last_slot_of_day_lands_in_23_hour():
    day = date(2026, 6, 1)
    rows = build_rows([(datetime(2026, 6, 1, 23, 45, tzinfo=TZ), 0.5)], {}, TZ, minute_from=day)
    assert local_hours(rows)[0].hour == 23
    assert local_hours(rows)[0].date() == day


def test_input_order_does_not_matter():
    day = date(2026, 6, 1)
    slots = day_slots(day)
    assert build_rows(slots, {}, TZ, minute_from=day) == build_rows(
        list(reversed(slots)), {}, TZ, minute_from=day
    )


def test_spring_forward_has_no_2am_hour():
    day = date(2026, 3, 8)
    rows = build_rows(day_slots(day), {}, TZ, minute_from=day)
    hours = local_hours(rows)
    assert len(rows) == 23
    assert 2 not in {h.hour for h in hours}
    assert rows[-1][1] == pytest.approx(92 * 0.1)


def test_fall_back_keeps_both_1am_hours():
    day = date(2026, 11, 1)
    rows = build_rows(day_slots(day), {}, TZ, minute_from=day)
    assert len(rows) == 25
    assert len({start for start, _ in rows}) == 25
    assert sum(1 for h in local_hours(rows) if h.hour == 1) == 2
    assert rows[-1][1] == pytest.approx(100 * 0.1)


def test_gap_day_falls_back_to_daily_total():
    d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
    daily = {d1: 9.6, d2: 5.0, d3: 9.6}
    slots = day_slots(d1, 0.1) + day_slots(d3, 0.1)  # d2 missing from the minute data
    rows = build_rows(slots, daily, TZ, minute_from=d1)
    gap = [r for r in rows if r[0].astimezone(TZ).date() == d2]
    assert len(gap) == 1
    assert local_hours(gap)[0].hour == 23
    assert rows[-1][1] == pytest.approx(9.6 + 5.0 + 9.6)


def test_daily_hands_off_to_hourly_without_a_jump():
    d1, d2, d3 = date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)
    daily = {d1: 8.0, d2: 7.0}
    rows = build_rows(day_slots(d3, 0.1), daily, TZ, minute_from=d3)
    sums = [s for _, s in rows]
    assert sums == sorted(sums)
    first_hourly = next(r for r in rows if r[0].astimezone(TZ).date() == d3)
    assert first_hourly[1] == pytest.approx(15.0 + 0.4)
    daily_rows = [r for r in rows if r[0].astimezone(TZ).date() in (d1, d2)]
    assert [h.hour for h in local_hours(daily_rows)] == [23, 23]
    assert rows[-1][1] == pytest.approx(15.0 + 9.6)


def test_daily_rows_are_dated_on_their_own_day():
    d1 = date(2026, 6, 1)
    rows = build_rows([], {d1: 8.0}, TZ, minute_from=date(2026, 6, 10))
    assert local_hours(rows)[0] == datetime(2026, 6, 1, 23, tzinfo=TZ)


def test_baseline_is_a_zero_row_the_night_before_install():
    d1 = date(2026, 6, 1)
    rows = build_rows([], {d1: 8.0}, TZ, minute_from=date(2026, 6, 10), baseline=True)
    assert local_hours(rows)[0] == datetime(2026, 5, 31, 23, tzinfo=TZ)
    assert [s for _, s in rows] == [0.0, 8.0]


def test_base_sum_anchors_a_rolling_window():
    day = date(2026, 6, 1)
    rows = build_rows(day_slots(day), {}, TZ, minute_from=day, base_sum=1000.0)
    assert rows[0][1] == pytest.approx(1000.4)
    assert rows[-1][1] == pytest.approx(1009.6)


def test_rows_start_on_the_hour_in_utc():
    day = date(2026, 6, 1)
    for start, _ in build_rows(day_slots(day), {day: 9.6}, TZ, minute_from=day):
        assert (start.minute, start.second, start.microsecond) == (0, 0, 0)
        assert start.tzinfo is UTC
