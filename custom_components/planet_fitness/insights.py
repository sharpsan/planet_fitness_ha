"""Pure functions: club schedule, crowd insights and check-in statistics.

All datetimes passed in are timezone-aware unless noted. Club hours and the crowd
profile are in the club's local time; check-in times are naive club-local times.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from .api.models import CheckIn, Club, CrowdProfile, Holiday
from .const import TREND_MIN_SPAN, TREND_WINDOW

SCHEDULE_HORIZON_DAYS = 31  # far enough to see the next holiday closure

# --------------------------------------------------------------------------- #
# Schedule
# --------------------------------------------------------------------------- #


def _day_interval(club: Club, day: date, tz: tzinfo) -> tuple[datetime, datetime] | None:
    """Opening interval that starts on `day`, honoring holiday overrides."""
    override = next((d for h in club.holidays for d in h.dates if d.day == day), None)
    if override is not None and override.closed:
        return None
    if override is not None and override.has_hours:
        opens, closes = override.opens, override.closes
    else:
        regular = next((h for h in club.hours if h.day_of_week == day.isoweekday()), None)
        if regular is None or regular.closed:
            return None
        opens, closes = regular.opens, regular.closes
    assert opens is not None and closes is not None
    midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
    start = midnight + timedelta(minutes=opens)
    if closes <= opens:  # overnight (e.g. 05:00-01:00) or 00:00-00:00 = all day
        closes += 1440
    return start, midnight + timedelta(minutes=closes)


def open_intervals(
    club: Club, now: datetime, tz: tzinfo, days: int = SCHEDULE_HORIZON_DAYS
) -> list[tuple[datetime, datetime]]:
    """Merged opening intervals from yesterday through `days` ahead (club time)."""
    if not club.operating or (not club.hours and not club.holidays):
        return []
    today = now.astimezone(tz).date()
    raw = [
        iv
        for offset in range(-1, days + 1)
        if (iv := _day_interval(club, today + timedelta(days=offset), tz)) is not None
    ]
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(raw):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


@dataclass(frozen=True, slots=True)
class OpenState:
    is_open: bool
    next_close: datetime | None  # None: not within the horizon
    next_open: datetime | None


def open_state(club: Club, now: datetime, tz: tzinfo) -> OpenState | None:
    """Open/closed now plus the next transitions. None if the club has no hours."""
    if club.operating and not club.hours:
        return None
    intervals = open_intervals(club, now, tz)
    # Hours are known through the last day scanned; an interval reaching past
    # that may continue, so its end isn't a known closing time.
    last_day = now.astimezone(tz).date() + timedelta(days=SCHEDULE_HORIZON_DAYS + 1)
    horizon = datetime(last_day.year, last_day.month, last_day.day, tzinfo=tz)
    for index, (start, end) in enumerate(intervals):
        if start <= now < end:
            following = intervals[index + 1][0] if index + 1 < len(intervals) else None
            return OpenState(True, end if end < horizon else None, following)
        if start > now:
            return OpenState(False, end if end < horizon else None, start)
    return OpenState(False, None, None)


def upcoming_holidays(club: Club, today: date, days: int = 30) -> list[Holiday]:
    """Holidays with an override date between today and `days` ahead."""
    last = today + timedelta(days=days)
    return [h for h in club.holidays if any(today <= d.day <= last for d in h.dates)]


# --------------------------------------------------------------------------- #
# Crowd
# --------------------------------------------------------------------------- #


def estimated_people(percentage: int | None, max_capacity: int | None) -> int | None:
    if percentage is None or not max_capacity:
        return None
    return round(percentage * max_capacity / 100)


def crowd_status(percentage: int | None, low: float, high: float) -> str | None:
    if percentage is None:
        return None
    if percentage < low:
        return "low"
    if percentage < high:
        return "moderate"
    return "high"


def typical_now(profile: CrowdProfile, local_now: datetime) -> float | None:
    value = profile.typical_percent(local_now.weekday(), local_now.hour)
    return round(value, 1) if value is not None else None


def day_profile(profile: CrowdProfile, weekday: int) -> list[float | None]:
    return [round(v, 1) if (v := profile.typical_percent(weekday, hour)) is not None else None for hour in range(24)]


def busier_than_usual(live: int | None, typical: float | None, factor: float) -> bool | None:
    """Live crowd clearly above the typical level for this hour."""
    if live is None or typical is None:
        return None
    return live > max(typical * factor, typical + 5)


def quietest_time_today(profile: CrowdProfile, club: Club, local_now: datetime, tz: tzinfo) -> datetime | None:
    """Start of the remaining hour today with the lowest typical crowd while open."""
    intervals = open_intervals(club, local_now, tz, days=1) if club.hours else []
    best: tuple[float, datetime] | None = None
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    for hour in range(local_now.hour + 1, 24):
        slot = midnight + timedelta(hours=hour)
        if club.hours and not any(start <= slot < end for start, end in intervals):
            continue
        value = profile.typical_percent(slot.weekday(), hour)
        if value is not None and (best is None or value < best[0]):
            best = (value, slot)
    return best[1] if best else None


def crowd_trend(samples: Sequence[tuple[datetime, int]], now: datetime) -> str | None:
    """rising / steady / falling, comparing now with ~15-45 minutes ago."""
    window = [(t, v) for t, v in samples if now - t <= TREND_WINDOW]
    if len(window) < 2 or window[-1][0] - window[0][0] < TREND_MIN_SPAN:
        return None
    delta = window[-1][1] - window[0][1]
    if delta >= 3:
        return "rising"
    if delta <= -3:
        return "falling"
    return "steady"


# --------------------------------------------------------------------------- #
# Check-ins
# --------------------------------------------------------------------------- #


def week_start(day: date) -> date:
    """Sunday on or before `day` (the app's week runs Sunday-Saturday)."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


@dataclass(frozen=True, slots=True)
class CheckInStats:
    last: CheckIn | None
    today: int
    this_week: int
    this_month: int
    this_year: int
    total: int
    weekly_streak: int


def check_in_stats(check_ins: Iterable[CheckIn], today: date) -> CheckInStats:
    visits = sorted(check_ins, key=lambda c: c.local_time, reverse=True)
    days = [c.local_time.date() for c in visits]
    this_week = week_start(today)
    weeks = {week_start(d) for d in days}
    cursor = this_week if this_week in weeks else this_week - timedelta(days=7)
    streak = 0
    while cursor in weeks:
        streak += 1
        cursor -= timedelta(days=7)
    return CheckInStats(
        last=visits[0] if visits else None,
        today=sum(1 for d in days if d == today),
        this_week=sum(1 for d in days if this_week <= d <= today),
        this_month=sum(1 for d in days if d.year == today.year and d.month == today.month),
        this_year=sum(1 for d in days if d.year == today.year),
        total=len(visits),
        weekly_streak=streak,
    )
