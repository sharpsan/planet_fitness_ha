"""Pure logic: schedule, crowd insights, check-in stats."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from custom_components.planet_fitness import insights
from custom_components.planet_fitness.api import CheckIn, Club, CrowdProfile, DayHours, Holiday, HolidayDate

from .conftest import load

TZ = ZoneInfo("America/Chicago")
HOME = Club.from_api(load("home_club"))  # 24/7 with Thanksgiving 2026 override
REGULAR = Club.from_api(load("club_other"))  # Mon-Fri 05-23, Sat-Sun 07-19
PROFILE = CrowdProfile.from_api(load("club_capacity"))


def at(*args: int) -> datetime:
    return datetime(*args, tzinfo=TZ)


# ---- schedule --------------------------------------------------------------


def test_24_7_club_is_open_with_far_holiday_closure() -> None:
    state = insights.open_state(HOME, at(2026, 11, 20, 3, 0), TZ)
    assert state is not None and state.is_open
    assert state.next_close == at(2026, 11, 25, 20, 0)
    assert state.next_open == at(2026, 11, 27, 6, 0)


def test_24_7_club_without_holiday_in_horizon_has_no_next_close() -> None:
    state = insights.open_state(HOME, at(2026, 6, 1, 12, 0), TZ)
    assert state is not None and state.is_open
    assert state.next_close is None
    assert state.next_open is None


def test_holiday_closed_day() -> None:
    state = insights.open_state(HOME, at(2026, 11, 26, 12, 0), TZ)
    assert state is not None and not state.is_open
    assert state.next_open == at(2026, 11, 27, 6, 0)


def test_regular_hours_open_and_closed() -> None:
    # Saturday 2026-11-07
    open_state = insights.open_state(REGULAR, at(2026, 11, 7, 12, 0), TZ)
    assert open_state is not None and open_state.is_open
    assert open_state.next_close == at(2026, 11, 7, 19, 0)
    assert open_state.next_open == at(2026, 11, 8, 7, 0)
    closed = insights.open_state(REGULAR, at(2026, 11, 7, 20, 0), TZ)
    assert closed is not None and not closed.is_open
    assert closed.next_open == at(2026, 11, 8, 7, 0)
    assert closed.next_close == at(2026, 11, 8, 19, 0)


def test_overnight_hours() -> None:
    club = replace(REGULAR, hours=tuple(DayHours(d, 5 * 60, 60, "open") for d in range(1, 8)))  # 05:00-01:00
    late = insights.open_state(club, at(2026, 11, 7, 0, 30), TZ)
    assert late is not None and late.is_open
    assert late.next_close == at(2026, 11, 7, 1, 0)
    gap = insights.open_state(club, at(2026, 11, 7, 3, 0), TZ)
    assert gap is not None and not gap.is_open
    assert gap.next_open == at(2026, 11, 7, 5, 0)


def test_not_operating_club_is_closed() -> None:
    state = insights.open_state(replace(HOME, status="PRESALE"), at(2026, 6, 1, 12, 0), TZ)
    assert state == insights.OpenState(False, None, None)


def test_no_hours_is_unknown() -> None:
    assert insights.open_state(replace(HOME, hours=(), holidays=()), at(2026, 6, 1, 12, 0), TZ) is None


def test_dst_change_keeps_wall_clock_hours() -> None:
    # DST ends 2026-11-01; Sunday hours are 07:00-19:00 local.
    state = insights.open_state(REGULAR, at(2026, 11, 1, 6, 0), TZ)
    assert state is not None and state.next_open == at(2026, 11, 1, 7, 0)


def test_upcoming_holidays() -> None:
    assert [h.name for h in insights.upcoming_holidays(HOME, date(2026, 11, 7))] == ["Thanksgiving"]
    assert insights.upcoming_holidays(HOME, date(2026, 7, 1)) == []


# ---- crowd -----------------------------------------------------------------


def test_crowd_basics() -> None:
    assert insights.estimated_people(23, 300) == 69
    assert insights.estimated_people(None, 300) is None
    assert insights.estimated_people(23, 0) is None
    assert insights.crowd_status(10, 25, 50) == "low"
    assert insights.crowd_status(25, 25, 50) == "moderate"
    assert insights.crowd_status(50, 25, 50) == "high"
    assert insights.crowd_status(None, 25, 50) is None


def test_typical_and_profile() -> None:
    six_pm = at(2026, 11, 6, 18, 30)  # Friday
    typical = insights.typical_now(PROFILE, six_pm)
    assert typical is not None and typical == round(PROFILE.visits[(4, 18)] / 300 * 100, 1)
    day = insights.day_profile(PROFILE, 4)
    assert len(day) == 24 and max(v for v in day if v is not None) == day[18]


def test_busier_than_usual() -> None:
    assert insights.busier_than_usual(30, 20.0, 1.25) is True
    assert insights.busier_than_usual(24, 20.0, 1.25) is False  # not 25 yet
    assert insights.busier_than_usual(6, 2.0, 1.25) is False  # needs +5 points
    assert insights.busier_than_usual(None, 20.0, 1.25) is None


def test_quietest_time_today_skips_closed_hours() -> None:
    now = at(2026, 11, 7, 12, 10)  # Saturday, open 07-19
    best = insights.quietest_time_today(PROFILE, REGULAR, now, TZ)
    assert best is not None and best.date() == now.date()
    assert 13 <= best.hour <= 18
    assert insights.quietest_time_today(PROFILE, REGULAR, at(2026, 11, 7, 23, 30), TZ) is None


def test_crowd_trend() -> None:
    now = at(2026, 11, 7, 18, 0)
    rising = [(now - timedelta(minutes=20), 20), (now - timedelta(minutes=10), 22), (now, 26)]
    assert insights.crowd_trend(rising, now) == "rising"
    assert insights.crowd_trend([(t, 30 - v + 20) for t, v in rising], now) == "falling"
    assert insights.crowd_trend([(now - timedelta(minutes=20), 20), (now, 21)], now) == "steady"
    assert insights.crowd_trend([(now - timedelta(minutes=5), 20), (now, 30)], now) is None  # too short


# ---- check-ins -------------------------------------------------------------


def visit(*args: int) -> CheckIn:
    return CheckIn(datetime(*args), "Example Central")


def test_week_starts_sunday() -> None:
    assert insights.week_start(date(2026, 11, 7)) == date(2026, 11, 1)  # Saturday -> Sunday
    assert insights.week_start(date(2026, 11, 1)) == date(2026, 11, 1)


def test_check_in_stats() -> None:
    visits = [
        visit(2026, 11, 5, 6),
        visit(2026, 11, 4, 12),
        visit(2026, 11, 1, 7),
        visit(2026, 10, 27, 18),
        visit(2026, 10, 20, 18),
        visit(2026, 1, 3, 10),
        visit(2025, 12, 31, 10),
    ]
    stats = insights.check_in_stats(visits, date(2026, 11, 7))
    assert stats.last == visits[0]
    assert (stats.today, stats.this_week, stats.this_month, stats.this_year, stats.total) == (0, 3, 3, 6, 7)
    assert stats.weekly_streak == 3  # weeks of Nov 1, Oct 25, Oct 18


def test_streak_survives_until_week_ends() -> None:
    visits = [visit(2026, 10, 30, 18), visit(2026, 10, 22, 18)]
    # Nothing yet this week (starting Sunday Nov 1), but last week counts.
    assert insights.check_in_stats(visits, date(2026, 11, 2)).weekly_streak == 2


def test_empty_stats() -> None:
    stats = insights.check_in_stats([], date(2026, 11, 7))
    assert stats.last is None and stats.total == 0 and stats.weekly_streak == 0


def test_closure_at_the_edge_of_the_window_is_shown() -> None:
    # Thanksgiving's 8 PM closing is the last day scanned from Oct 25.
    state = insights.open_state(HOME, at(2026, 10, 25, 12, 0), TZ)
    assert state is not None and state.next_close == at(2026, 11, 25, 20, 0)


def test_24hrs_row_without_times_is_open_all_day() -> None:
    row = DayHours.from_api({"dayOfWeek": 1, "status": "24hrs", "opens": None, "closes": None})
    assert row is not None and (row.opens, row.closes, row.closed) == (0, 1440, False)


def test_open_holiday_without_times_keeps_regular_hours() -> None:
    odd = Holiday("Odd day", (), (HolidayDate(date(2026, 11, 9), None, None, "open", None),))
    club = replace(REGULAR, holidays=(odd,))
    state = insights.open_state(club, at(2026, 11, 9, 12, 0), TZ)  # Monday, regular 05:00-23:00
    assert state is not None and state.is_open and state.next_close == at(2026, 11, 9, 23, 0)
