"""How the integration behaves when Planet Fitness misbehaves, and how often it asks."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
import logging
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.calendar import CalendarEntityDescription
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState, ConfigSubentry, ConfigSubentryData
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.planet_fitness import _async_sync_clubs
from custom_components.planet_fitness.api import (
    CheckIn,
    CrowdProfile,
    MembershipInfo,
    PlanetFitnessAuthError,
    PlanetFitnessError,
    PlanetFitnessRateLimitError,
)
from custom_components.planet_fitness.calendar import VISITS
from custom_components.planet_fitness.const import (
    CONF_BUSIER_FACTOR,
    CONF_CLUB_ID,
    CONF_HIGH_THRESHOLD,
    CONF_LOW_THRESHOLD,
    CONF_TOKEN,
    CONF_VISIT_MINUTES,
    DOMAIN,
    SUBENTRY_CLUB,
)
from custom_components.planet_fitness.coordinator import _update_error

from .conftest import HOME_ID, OTHER_ID, SUBJECT, check_ins, make_token

NOW = "2026-11-07 18:20:00-06:00"  # Saturday evening, Central time
TOTAL = "sensor.planet_fitness_membership_total_check_ins"
CHECKED_IN = "binary_sensor.planet_fitness_membership_checked_in"
EVENT = "event.planet_fitness_membership_check_in"
CAPACITY = "sensor.example_central_capacity"


@pytest.fixture(autouse=True)
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(NOW)
    return freezer


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def advance(hass: HomeAssistant, frozen: FrozenDateTimeFactory, **delta: float) -> None:
    frozen.tick(timedelta(**delta))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


def state(hass: HomeAssistant, entity_id: str) -> str:
    return hass.states.get(entity_id).state


# ---- history -----------------------------------------------------------------


async def test_empty_history_response_keeps_visits(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    calls = mock_client.async_get_check_ins.await_count
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_check_ins.await_count == calls + 1
    assert state(hass, TOTAL) == "8"
    assert state(hass, EVENT) == "unknown"


async def test_history_failure_backs_off_and_keeps_the_rest(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    calls = mock_client.async_get_check_ins.await_count
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 503", status=503)
    mock_client.async_get_current_checkin.return_value = {"club": "x"}  # makes history due now

    await advance(hass, frozen, minutes=11)
    assert mock_client.async_get_check_ins.await_count == calls + 1
    assert state(hass, CHECKED_IN) == "on"  # the account update still succeeded
    assert state(hass, TOTAL) == "8"  # stale history stays

    await advance(hass, frozen, minutes=11)
    assert mock_client.async_get_check_ins.await_count == calls + 1  # backing off

    await advance(hass, frozen, minutes=21)
    assert mock_client.async_get_check_ins.await_count == calls + 1  # not more often than when healthy
    await advance(hass, frozen, minutes=30)
    assert mock_client.async_get_check_ins.await_count == calls + 2  # an hour later


async def test_full_history_once_then_year_daily_and_recent_hourly(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    calls = len(mock_client.async_get_check_ins.await_args_list)
    assert calls == 5  # every year back to the membership start

    await advance(hass, frozen, minutes=61)
    start, end = mock_client.async_get_check_ins.await_args_list[-1].args
    assert (end - start).days == 34  # the last 35 days
    assert len(mock_client.async_get_check_ins.await_args_list) == calls + 1

    await advance(hass, frozen, hours=24)
    start, end = mock_client.async_get_check_ins.await_args_list[-1].args
    assert (end - start).days == 364  # just the last year, not every year again
    assert len(mock_client.async_get_check_ins.await_args_list) == calls + 2


async def test_rejected_future_end_date_falls_back_to_today(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    today = date(2026, 11, 7)

    async def strict(start: date, end: date) -> list[CheckIn]:
        if end > today:
            raise PlanetFitnessError("HTTP 400", status=400)
        return [c for c in check_ins() if start <= c.local_time.date() <= end]

    mock_client.async_get_check_ins.side_effect = strict
    await setup(hass, config_entry)
    ends = [c.args[1] for c in mock_client.async_get_check_ins.await_args_list]
    assert ends[:2] == [date(2026, 11, 8), today]  # tried tomorrow, then today
    assert ends[2] < today  # later windows didn't try tomorrow again
    assert state(hass, TOTAL) == "8"


async def test_unrelated_400_does_not_stop_using_tomorrow(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 400", status=400)
    await advance(hass, frozen, minutes=61)
    assert hass.config_entries.async_entries(DOMAIN)[0].runtime_data.account._future_end_ok is True


# ---- crowd profile and billing ------------------------------------------------


async def test_crowd_profile_failure_retries_in_an_hour(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    profile = mock_client.async_get_crowd_profile.return_value
    mock_client.async_get_crowd_profile.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert state(hass, "sensor.example_central_crowd_level") == "23"
    assert state(hass, "sensor.example_central_capacity") == "unknown"

    mock_client.async_get_crowd_profile.side_effect = None
    mock_client.async_get_crowd_profile.return_value = profile
    await advance(hass, frozen, minutes=30)
    assert mock_client.async_get_crowd_profile.await_count == 1
    await advance(hass, frozen, minutes=31)
    assert mock_client.async_get_crowd_profile.await_count == 2
    assert state(hass, "sensor.example_central_capacity") == "300"


async def test_rate_limit_on_billing_pauses_billing_and_history(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessRateLimitError(3600)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED
    assert state(hass, CHECKED_IN) == "off"
    assert mock_client.async_get_check_ins.await_count == 0  # paused before history
    billing = mock_client.async_get_membership_info.await_count

    mock_client.async_get_fee_schedule.side_effect = None
    await advance(hass, frozen, minutes=11)
    assert mock_client.async_get_membership_info.await_count == billing  # still paused
    assert mock_client.async_get_check_ins.await_count == 0
    await advance(hass, frozen, minutes=50)
    assert state(hass, TOTAL) == "8"


# ---- setup and auth ----------------------------------------------------------


async def test_rate_limit_at_setup_waits_for_retry_after(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    home = mock_client.async_get_home_club.return_value
    mock_client.async_get_home_club.side_effect = PlanetFitnessRateLimitError(900)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "waiting until" in (config_entry.reason or "")  # like every setup 429
    assert mock_client.async_get_home_club.await_count == 1

    await hass.config_entries.async_reload(config_entry.entry_id)
    assert mock_client.async_get_home_club.await_count == 1  # no request while paused

    mock_client.async_get_home_club.side_effect = None
    mock_client.async_get_home_club.return_value = home
    frozen.tick(timedelta(seconds=901))
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED


async def test_auth_failure_while_running_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_club.side_effect = PlanetFitnessAuthError("expired")
    await advance(hass, frozen, minutes=6)
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]
    assert state(hass, "sensor.example_central_crowd_level") == "unavailable"


# ---- options and clubs -------------------------------------------------------


async def test_options_change_updates_states_without_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    with patch("custom_components.planet_fitness.PlanetFitnessClient", return_value=mock_client) as factory:
        await setup(hass, config_entry)
        assert state(hass, "sensor.example_central_crowd_status") == "low"
        options = {CONF_LOW_THRESHOLD: 20, CONF_HIGH_THRESHOLD: 40, CONF_BUSIER_FACTOR: 1.25, CONF_VISIT_MINUTES: 60}
        hass.config_entries.async_update_entry(config_entry, options=options)
        await hass.async_block_till_done()
    assert state(hass, "sensor.example_central_crowd_status") == "moderate"
    assert factory.call_count == 1


async def test_removing_a_club_stops_it_without_a_reload(
    hass: HomeAssistant, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SUBJECT,
        data={CONF_TOKEN: make_token()},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_CLUB_ID: OTHER_ID}, subentry_type=SUBENTRY_CLUB, title="Example North", unique_id=OTHER_ID
            )
        ],
    )
    await setup(hass, entry)
    assert hass.states.get("sensor.example_north_crowd_level") is not None
    checkins = mock_client.async_get_current_checkin.await_count

    hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
    await hass.async_block_till_done()
    assert hass.states.get("sensor.example_north_crowd_level") is None
    assert OTHER_ID not in entry.runtime_data.clubs
    assert mock_client.async_get_current_checkin.await_count == checkins  # nothing refetched

    polls = [c.args[0] for c in mock_client.async_get_club.await_args_list].count(OTHER_ID)
    await advance(hass, frozen, minutes=6)
    await advance(hass, frozen, minutes=6)
    assert [c.args[0] for c in mock_client.async_get_club.await_args_list].count(OTHER_ID) == polls  # stopped


# ---- calendar ----------------------------------------------------------------


async def test_calendar_lists_visits_in_range(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await hass.config.async_set_time_zone("America/Chicago")
    await setup(hass, config_entry)
    result = await hass.services.async_call(
        "calendar",
        "get_events",
        {
            "entity_id": "calendar.planet_fitness_membership_visits",
            "start_date_time": "2026-11-01T00:00:00-06:00",
            "end_date_time": "2026-11-08T00:00:00-06:00",
        },
        blocking=True,
        return_response=True,
    )
    events = result["calendar.planet_fitness_membership_visits"]["events"]
    assert [e["summary"] for e in events] == ["Example North", "Example Central", "Example Central"]
    assert events[0]["start"] == "2026-11-01T07:15:00-06:00"
    assert events[0]["end"] == "2026-11-01T08:15:00-06:00"


# ---- round 2 ---------------------------------------------------------------


def window_starts(mock: MagicMock) -> list[date]:
    return [c.args[0] for c in mock.async_get_check_ins.await_args_list]


async def test_failing_older_year_keeps_the_rest_and_resumes(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    bad_year = date(2023, 11, 10)  # the third window back

    older_visit = CheckIn(check_ins()[0].local_time.replace(year=2025, month=2, day=3), "Example North")

    async def flaky(start: date, end: date) -> list[CheckIn]:
        if start == bad_year:
            raise PlanetFitnessError("HTTP 503", status=503)
        extra = [older_visit] if start <= older_visit.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = flaky
    await setup(hass, config_entry)
    assert window_starts(mock_client) == [date(2025, 11, 9), date(2024, 11, 9), bad_year]
    assert state(hass, TOTAL) == "unknown"  # a year is missing, so a total would be too low
    assert state(hass, "sensor.planet_fitness_membership_check_ins_this_year") == "7"

    await advance(hass, frozen, minutes=11)  # the next poll fetches recent visits; the older year waits
    assert state(hass, TOTAL) == "unknown"

    mock_client.async_get_check_ins.side_effect = good
    await advance(hass, frozen, minutes=50)
    assert window_starts(mock_client)[-3:] == [bad_year, date(2022, 11, 10), date(2022, 2, 14)]
    assert state(hass, TOTAL) == "9"  # the older visit stayed


async def test_history_goes_back_further_once_the_start_date_is_known(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    membership = mock_client.async_get_membership_info.return_value
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await setup(hass, config_entry)
    assert window_starts(mock_client) == [date(2025, 11, 9), date(2024, 11, 9)]  # stopped at an empty year

    mock_client.async_get_membership_info.side_effect = None
    mock_client.async_get_membership_info.return_value = membership
    await advance(hass, frozen, minutes=61)  # billing retries, learns the start date, and history goes on
    assert window_starts(mock_client)[2:] == [date(2023, 11, 10), date(2022, 11, 10), date(2022, 2, 14)]


async def test_billing_failure_retries_in_an_hour(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await setup(hass, config_entry)
    await advance(hass, frozen, minutes=11)
    assert mock_client.async_get_fee_schedule.await_count == 1
    await advance(hass, frozen, minutes=50)
    assert mock_client.async_get_fee_schedule.await_count == 2


@pytest.mark.entity_defaults
async def test_fees_and_balance_are_skipped_while_their_entities_are_off(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)  # a new install: nothing registered yet, so all load once
    assert mock_client.async_get_fee_schedule.await_count == 1
    assert mock_client.async_get_balance.await_count == 1

    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessError("HTTP 500", status=500)
    mock_client.async_get_balance.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await advance(hass, frozen, hours=12, minutes=1)
    assert mock_client.async_get_membership_info.await_count == 2  # history still needs the start date
    assert mock_client.async_get_fee_schedule.await_count == 1
    assert mock_client.async_get_balance.await_count == 1
    ours = [r for r in caplog.records if r.name.startswith("custom_components.planet_fitness")]
    assert not [r for r in ours if r.levelno >= logging.WARNING]  # nothing about data nobody shows

    await hass.config_entries.async_reload(config_entry.entry_id)  # like a restart
    await hass.async_block_till_done()
    assert mock_client.async_get_membership_info.await_count == 3
    assert mock_client.async_get_fee_schedule.await_count == 1
    assert mock_client.async_get_balance.await_count == 1


@pytest.mark.entity_defaults
async def test_turning_on_a_billing_entity_loads_its_data(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_client.async_get_fee_schedule.await_count == 1

    monthly_fee = "sensor.planet_fitness_membership_monthly_fee"
    er.async_get(hass).async_update_entity(monthly_fee, disabled_by=None)
    await advance(hass, frozen, seconds=31)  # Home Assistant reloads the entry
    assert mock_client.async_get_fee_schedule.await_count == 2
    assert mock_client.async_get_balance.await_count == 1  # its entities are still off
    assert state(hass, monthly_fee) == "15.0"


async def test_crowd_profile_rate_limit_waits_at_least_as_asked(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_crowd_profile.side_effect = PlanetFitnessRateLimitError(7200)
    await setup(hass, config_entry)
    await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_crowd_profile.await_count == 1
    await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_crowd_profile.await_count == 2


async def test_old_visit_appearing_late_does_not_fire_the_event(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    stale = CheckIn(check_ins()[0].local_time.replace(day=2, hour=9, minute=0, second=0), "Example Central")

    async def with_stale(start: date, end: date) -> list[CheckIn]:
        return [stale, *[c for c in check_ins() if start <= c.local_time.date() <= end]]

    mock_client.async_get_check_ins.side_effect = with_stale
    await advance(hass, frozen, minutes=61)
    assert state(hass, TOTAL) == "9"  # counted
    assert state(hass, EVENT) == "unknown"  # but five days old, so no event


async def test_adding_a_club_starts_it_once_without_a_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    real_get_club = mock_client.async_get_club.side_effect

    async def slow_get_club(club_id: str):  # yields like a real request, opening the race window
        await asyncio.sleep(0)
        return real_get_club(club_id)

    mock_client.async_get_club.side_effect = slow_get_club
    with patch("custom_components.planet_fitness.PlanetFitnessClient", return_value=mock_client) as factory:
        await setup(hass, config_entry)
        checkins = mock_client.async_get_current_checkin.await_count
        subentry = ConfigSubentry(
            data=MappingProxyType({CONF_CLUB_ID: OTHER_ID}),
            subentry_type=SUBENTRY_CLUB,
            title="Example North",
            unique_id=OTHER_ID,
        )
        hass.config_entries.async_add_subentry(config_entry, subentry)
        # A token save lands while the new club's first refresh is still running.
        token = {**config_entry.data[CONF_TOKEN], "access_token": "rotated"}
        hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_TOKEN: token})
        await hass.async_block_till_done()
    assert state(hass, "sensor.example_north_crowd_level") == "41"
    assert [c.args[0] for c in mock_client.async_get_club.await_args_list] == [OTHER_ID]  # once
    assert factory.call_count == 1  # no reload
    assert mock_client.async_get_current_checkin.await_count == checkins
    assert "does not generate unique IDs" not in caplog.text
    assert set(config_entry.runtime_data.clubs) == {HOME_ID, OTHER_ID}


async def test_rate_limit_on_first_account_refresh_pauses_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_current_checkin.side_effect = PlanetFitnessRateLimitError(900)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert "waiting until" in (config_entry.reason or "")
    homes = mock_client.async_get_home_club.await_count
    await hass.config_entries.async_reload(config_entry.entry_id)
    assert mock_client.async_get_home_club.await_count == homes  # paused: no request


async def test_club_sync_waits_for_a_loaded_entry(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)  # not set up, so there's no runtime data yet
    _async_sync_clubs(hass, config_entry)  # must not raise


def test_rate_limit_retry_never_runs_early() -> None:
    err = _update_error(PlanetFitnessRateLimitError(12.2))
    assert err.retry_after == 14  # rounded up, plus a second to spare
    err = _update_error(PlanetFitnessRateLimitError(1), timedelta(minutes=5))
    assert err.retry_after == 300  # never sooner than the usual interval


async def test_a_short_retry_after_doesnt_speed_up_polling(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_club.side_effect = PlanetFitnessRateLimitError(1)
    await advance(hass, frozen, minutes=5, seconds=1)  # the next regular poll gets the 429
    calls = mock_client.async_get_club.await_count
    for _ in range(60):
        await advance(hass, frozen, seconds=1)
    assert mock_client.async_get_club.await_count == calls  # not every 2 seconds


async def test_a_shorter_rate_limit_doesnt_shorten_the_pause_a_reload_waits_for(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    with patch("custom_components.planet_fitness.PlanetFitnessClient", return_value=mock_client) as client_class:
        await setup(hass, config_entry)
        on_pause = client_class.call_args.kwargs["on_pause"]
        on_pause(3600)
        on_pause(60)  # an overlapping request's shorter 429
        await advance(hass, frozen, minutes=2)
        calls = mock_client.async_get_home_club.await_count
        await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.SETUP_RETRY
        assert mock_client.async_get_home_club.await_count == calls  # still the hour's pause


# ---- degraded responses ----------------------------------------------------------


async def test_crowd_profile_without_capacity_is_retried_and_never_replaces_a_good_one(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_crowd_profile.return_value
    no_capacity = CrowdProfile(0, dict(good.visits))  # as Planet Fitness sent it overnight
    mock_client.async_get_crowd_profile.return_value = no_capacity
    await setup(hass, config_entry)
    assert state(hass, CAPACITY) == "unknown"

    mock_client.async_get_crowd_profile.return_value = good
    await advance(hass, frozen, minutes=61)  # an hour, not the usual day
    assert mock_client.async_get_crowd_profile.await_count == 2
    assert state(hass, CAPACITY) == "300"

    mock_client.async_get_crowd_profile.return_value = no_capacity
    await advance(hass, frozen, hours=24, minutes=1)
    assert mock_client.async_get_crowd_profile.await_count == 3
    assert state(hass, CAPACITY) == "300"  # kept the last good profile


async def test_crowd_profile_with_capacity_but_no_crowd_data_is_used(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_crowd_profile.return_value = CrowdProfile(300, {})  # a new club, say
    await setup(hass, config_entry)
    assert state(hass, CAPACITY) == "300"
    assert state(hass, "sensor.example_central_typical_crowd_now") == "unknown"


async def test_missing_crowd_profile_is_retried_less_and_less_often(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_crowd_profile.return_value = CrowdProfile(0, {})  # never a capacity
    await setup(hass, config_entry)
    counts = []
    for _ in range(48):  # two days, an hour at a time
        await advance(hass, frozen, minutes=60)
        counts.append(mock_client.async_get_crowd_profile.await_count)
    # 1 at setup, then after 1, 2, 4, 8 and 16 hours, then daily.
    assert [i + 1 for i in range(48) if counts[i] != (counts[i - 1] if i else 1)] == [1, 3, 7, 15, 31]
    assert counts[-1] == 6


async def test_empty_history_at_setup_retries_the_year_hourly_then_loads_older_years(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    older_visit = CheckIn(check_ins()[0].local_time.replace(year=2025, month=2, day=3), "Example North")
    this_morning = CheckIn(datetime(2026, 11, 7, 7, 0), "Example Central")  # before Home Assistant started

    async def recovered(start: date, end: date) -> list[CheckIn]:
        extra = [c for c in (this_morning, older_visit) if start <= c.local_time.date() <= end]
        return extra + await good(start, end)

    # Overnight, Planet Fitness returned no history and a 500 for membership info.
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)
    assert state(hass, TOTAL) == "unknown"  # not 0: it may be a gap in the data
    assert window_starts(mock_client) == [date(2025, 11, 9)]

    mock_client.async_get_check_ins.side_effect = recovered
    await advance(hass, frozen, minutes=61)  # the whole last year again, not just 35 days
    start, end = mock_client.async_get_check_ins.await_args_list[-1].args
    assert (end - start).days == 364
    assert state(hass, TOTAL) == "unknown"  # until every year loads again
    assert state(hass, "sensor.planet_fitness_membership_check_ins_this_year") == "unknown"  # likewise
    assert state(hass, EVENT) == "unknown"  # this morning's visit was before Home Assistant started

    await advance(hass, frozen, minutes=11)  # then the older years it skipped
    assert window_starts(mock_client)[-2:] == [date(2024, 11, 9), date(2023, 11, 10)]
    assert state(hass, TOTAL) == "unknown"  # without the join date, it may stop early

    mock_client.async_get_membership_info.side_effect = None
    await advance(hass, frozen, hours=4)
    assert state(hass, TOTAL) == "10"


async def test_history_back_days_after_an_empty_start_loads_every_older_year(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    # Membership info works, so the start date is known, but history is empty at
    # setup: every year back to the start loads empty.
    good = mock_client.async_get_check_ins.side_effect
    near_a_year_ago = CheckIn(datetime(2025, 11, 10, 9, 0), "Example North")
    older_visit = CheckIn(datetime(2024, 2, 3, 9, 0), "Example North")

    async def recovered(start: date, end: date) -> list[CheckIn]:
        extra = [c for c in (near_a_year_ago, older_visit) if start <= c.local_time.date() <= end]
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)
    assert window_starts(mock_client)[-1] == date(2022, 2, 14)  # all the way to the start
    assert state(hass, TOTAL) == "unknown"  # not 0: it may be a gap in the data

    # History stays empty for three days, then comes back.
    await advance(hass, frozen, days=3)
    mock_client.async_get_check_ins.side_effect = recovered
    await advance(hass, frozen, minutes=61)
    await advance(hass, frozen, minutes=11)
    starts = window_starts(mock_client)
    assert starts[-5:] == [
        date(2025, 11, 12),  # every year again, newest first
        date(2024, 11, 12),
        date(2023, 11, 13),
        date(2022, 11, 13),
        date(2022, 2, 14),
    ]
    assert state(hass, TOTAL) == "10"  # the 8, plus a visit near the one-year mark and an older one


async def test_history_back_through_a_check_in_loads_every_older_year(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    older_visit = CheckIn(datetime(2024, 2, 3, 9, 0), "Example North")

    async def recovered(start: date, end: date) -> list[CheckIn]:
        extra = [older_visit] if start <= older_visit.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)
    assert state(hass, TOTAL) == "unknown"  # not 0: it may be a gap in the data

    # History is back, and the member checks in before the hourly year reload:
    # the last 35 days are the first to show visits.
    mock_client.async_get_check_ins.side_effect = recovered
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)
    assert window_starts(mock_client)[-1] == date(2026, 10, 5)
    await advance(hass, frozen, minutes=11)
    assert window_starts(mock_client)[-5:] == [
        date(2025, 11, 9),
        date(2024, 11, 9),
        date(2023, 11, 10),
        date(2022, 11, 10),
        date(2022, 2, 14),
    ]
    assert state(hass, TOTAL) == "9"


async def test_history_back_for_a_new_member_asks_for_sensible_ranges(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    since = date(2026, 3, 1)  # joined eight months ago
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)
    first_visit = CheckIn(datetime(2026, 11, 7, 18, 50), "Example Central")  # after Home Assistant started

    async def recovered(start: date, end: date) -> list[CheckIn]:
        return [first_visit] if start <= first_visit.local_time.date() <= end else []

    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)
    mock_client.async_get_check_ins.side_effect = recovered
    await advance(hass, frozen, minutes=61)  # the hourly year reload sees it
    await advance(hass, frozen, minutes=11)  # then every year again, back to the start
    windows = [call.args for call in mock_client.async_get_check_ins.await_args_list]
    assert all(start <= end for start, end in windows)
    assert windows[-1] == (since, date(2026, 11, 8))
    assert state(hass, TOTAL) == "1"
    assert state(hass, "sensor.planet_fitness_membership_check_ins_this_year") == "1"  # joined this year
    assert state(hass, EVENT) != "unknown"  # a visit after startup is a new check-in


async def test_club_added_while_running_gets_its_link_once_it_loads(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    clubs = mock_client.async_get_club.side_effect
    failures = [PlanetFitnessError("HTTP 503", status=503)]

    def first_load_fails(club_id: str):
        if club_id == OTHER_ID and failures:
            raise failures.pop()
        return clubs(club_id)

    mock_client.async_get_club.side_effect = first_load_fails
    hass.config_entries.async_add_subentry(
        config_entry,
        ConfigSubentry(
            data=MappingProxyType({CONF_CLUB_ID: OTHER_ID}),
            subentry_type=SUBENTRY_CLUB,
            title="Example North",
            unique_id=OTHER_ID,
        ),
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor.example_north_crowd_level") == "unavailable"

    await advance(hass, frozen, minutes=6)
    assert state(hass, "sensor.example_north_crowd_level") == "41"
    device = dr.async_get(hass).async_get_device_by_identifier((DOMAIN, f"{SUBJECT}_{OTHER_ID}"), config_entry.entry_id)
    assert device.configuration_url == "https://www.planetfitness.com/gyms/example-north-mo"


async def test_empty_years_before_a_failed_one_load_again_once_visits_come_back(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    third_year = date(2023, 11, 10)
    second_year_visit = CheckIn(datetime(2025, 2, 3, 9, 0), "Example North")
    third_year_visit = CheckIn(datetime(2024, 2, 3, 9, 0), "Example North")

    async def outage(start: date, end: date) -> list[CheckIn]:
        if start == third_year:
            raise PlanetFitnessError("HTTP 503", status=503)
        return []

    async def recovered(start: date, end: date) -> list[CheckIn]:
        extra = [c for c in (second_year_visit, third_year_visit) if start <= c.local_time.date() <= end]
        return extra + await good(start, end)

    # The two newest years come back empty, and the third fails.
    mock_client.async_get_check_ins.side_effect = outage
    await setup(hass, config_entry)
    assert window_starts(mock_client) == [date(2025, 11, 9), date(2024, 11, 9), third_year]

    mock_client.async_get_check_ins.side_effect = recovered
    for _ in range(26 * 6):  # a day and a bit, 10 minutes at a time
        await advance(hass, frozen, minutes=10)
    assert state(hass, TOTAL) == "10"  # the 8, plus one in each older year


async def test_visit_after_startup_fires_even_when_history_first_loads_late(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await setup(hass, config_entry)
    assert state(hass, TOTAL) == "unavailable"

    after_startup = CheckIn(datetime(2026, 11, 7, 18, 50), "Example Central")

    async def recovered(start: date, end: date) -> list[CheckIn]:
        extra = [after_startup] if start <= after_startup.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = recovered
    for _ in range(7):
        await advance(hass, frozen, minutes=10)
    assert state(hass, TOTAL) == "9"
    assert hass.states.get(EVENT).attributes["check_in_time"] == "2026-11-07T18:50:00-06:00"


async def test_check_in_never_fires_for_a_visit_more_than_two_days_old(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    for _ in range(5 * 24):  # five days of uptime, so startup isn't what rules it out
        await advance(hass, frozen, hours=1)
    good = mock_client.async_get_check_ins.side_effect
    three_days_old = CheckIn(datetime(2026, 11, 9, 9, 0), "Example Central")

    async def with_late(start: date, end: date) -> list[CheckIn]:
        return [three_days_old, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = with_late
    await advance(hass, frozen, minutes=61)
    assert state(hass, TOTAL) == "9"
    assert state(hass, EVENT) == "unknown"


@pytest.mark.parametrize(
    "case",
    [(date(2026, 11, 12), "unknown"), (date(1900, 1, 1), "8")],  # a presale (no visits yet); a placeholder
)
async def test_odd_membership_start_dates_ask_for_sensible_ranges(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    case: tuple[date, str],
) -> None:
    since, total = case
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)
    await setup(hass, config_entry)
    windows = [call.args for call in mock_client.async_get_check_ins.await_args_list]
    assert all(start <= end for start, end in windows)
    assert windows[-1][0] >= date(1992, 1, 1)
    assert state(hass, TOTAL) == total
    setup_requests = len(windows)
    await advance(hass, frozen, hours=12, minutes=1)  # billing again: no new backfill
    assert mock_client.async_get_check_ins.await_count - setup_requests <= 1


async def test_a_history_outage_is_logged_once_whichever_fetch_hits_it(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    good = mock_client.async_get_check_ins.side_effect
    calls = []

    async def first_works(start: date, end: date) -> list[CheckIn]:
        calls.append(start)
        if len(calls) > 1:  # older years, then the recent check-ins, all fail
            raise PlanetFitnessError("HTTP 500", status=500)
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = first_works
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)
    mock_client.async_get_current_checkin.return_value = {"club": "x"}  # recent history due now
    await advance(hass, frozen, minutes=61)
    await advance(hass, frozen, minutes=61)
    assert len(calls) >= 3
    ours = [r for r in caplog.records if r.name.startswith("custom_components.planet_fitness")]
    warnings = [r.getMessage() for r in ours if r.levelno == logging.WARNING]
    assert len([w for w in warnings if w.startswith("Check-in history")]) == 1

    mock_client.async_get_check_ins.side_effect = good
    for _ in range(3):  # the older years, then the last 35 days
        await advance(hass, frozen, minutes=61)
    assert caplog.text.count("Check-in history is available again") == 1


@pytest.mark.parametrize("older_error", [PlanetFitnessError("HTTP 503", status=503), PlanetFitnessRateLimitError(60)])
async def test_a_new_visit_still_fires_when_an_older_year_fails_in_the_same_load(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    older_error: Exception,
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await setup(hass, config_entry)
    after_startup = CheckIn(datetime(2026, 11, 7, 18, 50), "Example Central")

    async def older_years_fail(start: date, end: date) -> list[CheckIn]:
        if start < date(2025, 1, 1):
            raise older_error
        extra = [after_startup] if start <= after_startup.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = older_years_fail
    for _ in range(7):  # past the hourly retry: the newest year loads, the next fails
        await advance(hass, frozen, minutes=10)
    assert state(hass, TOTAL) == "unknown"  # some years missing: a total would be too low
    assert state(hass, "sensor.planet_fitness_membership_check_ins_this_year") == "8"
    assert hass.states.get(EVENT).attributes["check_in_time"] == "2026-11-07T18:50:00-06:00"


async def test_a_lapsed_member_loads_history_with_two_extra_requests_at_most(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    last_visit = CheckIn(datetime(2025, 3, 1, 9, 0), "Example Central")  # over a year ago

    async def history(start: date, end: date) -> list[CheckIn]:
        return [last_visit] if start <= last_visit.local_time.date() <= end else []

    mock_client.async_get_check_ins.side_effect = history
    await setup(hass, config_entry)
    await advance(hass, frozen, minutes=11)  # an empty newest year looks like a gap: load again
    await advance(hass, frozen, minutes=61)
    assert window_starts(mock_client) == [
        date(2025, 11, 9),
        date(2024, 11, 9),
        date(2025, 11, 9),  # every year again
        date(2024, 11, 9),
        date(2023, 11, 10),
        date(2022, 11, 10),
        date(2022, 2, 14),
        date(2026, 10, 5),  # the hourly recent check-ins; not the year again
    ]
    assert state(hass, TOTAL) == "1"


async def test_a_presale_member_loads_even_if_future_end_dates_are_refused(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    since = date(2026, 11, 12)  # joins next week
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)

    async def strict(start: date, end: date) -> list[CheckIn]:
        if end > date(2026, 11, 7) or start > end:
            raise PlanetFitnessError("HTTP 400", status=400)
        return []

    mock_client.async_get_check_ins.side_effect = strict
    await setup(hass, config_entry)
    windows = [call.args for call in mock_client.async_get_check_ins.await_args_list]
    assert windows == [(date(2026, 11, 8), date(2026, 11, 8)), (date(2026, 11, 7), date(2026, 11, 7))]
    assert state(hass, TOTAL) == "unknown"  # loaded, with no visits yet


async def test_a_visit_repeated_in_one_response_fires_check_in_once(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    fired: list[Event] = []

    @callback
    def record(event: Event) -> None:
        if event.data["entity_id"] == EVENT:
            fired.append(event)

    hass.bus.async_listen("state_changed", record)
    good = mock_client.async_get_check_ins.side_effect
    visit = CheckIn(datetime(2026, 11, 7, 18, 40), "Example Central")

    async def twice(start: date, end: date) -> list[CheckIn]:
        return [visit, visit, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = twice
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)
    assert len(fired) == 1


async def test_an_empty_year_where_visits_are_known_is_tried_again(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    older_visit = CheckIn(datetime(2024, 2, 3, 9, 0), "Example North")
    gap = [False]

    async def history(start: date, end: date) -> list[CheckIn]:
        if gap[0]:
            gap[0] = False  # one empty answer, then it's back
            return []
        extra = [older_visit] if start <= older_visit.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # history loads empty
    mock_client.async_get_check_ins.side_effect = history
    await advance(hass, frozen, minutes=61)  # the year comes back with visits: reload every year
    gap[0] = True  # but the reload's first year comes back empty again
    await advance(hass, frozen, minutes=11)
    assert state(hass, TOTAL) == "unknown"  # not a total missing years
    await advance(hass, frozen, minutes=61)  # tried again
    assert state(hass, TOTAL) == "9"


async def test_this_year_waits_for_january_first_on_new_years_eve(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    frozen.move_to("2026-12-31 18:20:00-06:00")
    good = mock_client.async_get_check_ins.side_effect
    new_years_day = CheckIn(datetime(2026, 1, 1, 9, 0), "Example Central")  # in the second window
    older_fails = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if end < date(2026, 1, 2) and older_fails[0]:
            raise PlanetFitnessError("HTTP 503", status=503)
        extra = [new_years_day] if start <= new_years_day.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    await setup(hass, config_entry)
    this_year = "sensor.planet_fitness_membership_check_ins_this_year"
    assert state(hass, this_year) == "unknown"  # January 1 hasn't loaded
    older_fails[0] = False
    await advance(hass, frozen, minutes=61)
    assert state(hass, this_year) == "8"  # the 7 from November and New Year's Day


async def test_a_visit_that_fired_doesnt_fire_again_after_a_reload_during_a_gap(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    fired: set[str] = set()  # an event's state is when it fired

    @callback
    def record(event: Event) -> None:
        new = event.data["new_state"]
        if event.data["entity_id"] == EVENT and new is not None and new.state not in ("unknown", "unavailable"):
            fired.add(new.state)

    hass.bus.async_listen("state_changed", record)
    good = mock_client.async_get_check_ins.side_effect
    visit = CheckIn(datetime(2026, 11, 7, 18, 25), "Example Central")

    async def with_visit(start: date, end: date) -> list[CheckIn]:
        return [visit, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = with_visit
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)
    assert len(fired) == 1

    # Reloaded while history is empty; then it comes back with the same visit.
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.async_get_check_ins.side_effect = with_visit
    for _ in range(8):
        await advance(hass, frozen, minutes=11)
    assert state(hass, TOTAL) == "9"
    assert len(fired) == 1


async def test_this_year_waits_when_a_check_in_brings_history_back_first(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # history loads empty
    mock_client.async_get_check_ins.side_effect = good
    mock_client.async_get_current_checkin.return_value = {"club": "x"}  # the last 35 days first
    this_year = "sensor.planet_fitness_membership_check_ins_this_year"
    await advance(hass, frozen, minutes=11)
    assert state(hass, "sensor.planet_fitness_membership_check_ins_this_month") == "3"
    assert state(hass, this_year) == "unknown"  # not just the 35 days of it
    await advance(hass, frozen, minutes=11)  # every year again
    assert state(hass, this_year) == "7"


async def test_rate_limit_is_logged_once_per_pause(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_check_ins.side_effect = PlanetFitnessRateLimitError(60)
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    for _ in range(3):
        await advance(hass, frozen, minutes=11)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "Rate limited" in r.getMessage()]
    assert len(warnings) == 1


async def test_a_visit_that_fired_doesnt_fire_again_after_a_reload_while_unavailable(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    fired: set[str] = set()

    @callback
    def record(event: Event) -> None:
        new = event.data["new_state"]
        if event.data["entity_id"] == EVENT and new is not None and new.state not in ("unknown", "unavailable"):
            fired.add(new.state)

    hass.bus.async_listen("state_changed", record)
    good = mock_client.async_get_check_ins.side_effect
    visit = CheckIn(datetime(2026, 11, 7, 18, 25), "Example Central")

    async def with_visit(start: date, end: date) -> list[CheckIn]:
        return [visit, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = with_visit
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)
    assert len(fired) == 1

    # The membership goes unavailable (its check-in status fails), then a reload
    # during a gap, then history comes back with the same visit.
    mock_client.async_get_current_checkin.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await advance(hass, frozen, minutes=11)
    assert state(hass, EVENT) == "unavailable"
    mock_client.async_get_current_checkin.side_effect = None
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.async_get_check_ins.side_effect = with_visit
    for _ in range(8):
        await advance(hass, frozen, minutes=11)
    assert state(hass, TOTAL) == "9"
    assert len(fired) == 1


async def test_visits_before_the_join_date_dont_count_whichever_fetch_brings_them(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    since = date(2026, 3, 1)  # rejoined; history still has a January visit
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)
    await setup(hass, config_entry)
    totals = [state(hass, TOTAL)]
    await advance(hass, frozen, hours=24, minutes=1)  # the daily reload of the last 365 days
    totals.append(state(hass, TOTAL))
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    totals.append(state(hass, TOTAL))
    assert totals == ["6", "6", "6"]


async def test_a_visit_loaded_at_startup_doesnt_fire_when_a_gap_brings_it_back(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    visit = CheckIn(datetime(2026, 11, 7, 18, 18), "Example Central")  # just before startup

    async def with_visit(start: date, end: date) -> list[CheckIn]:
        return [visit, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = with_visit
    await setup(hass, config_entry)  # loaded at startup, so it doesn't fire
    await advance(hass, frozen, minutes=1)

    # Reloaded while history is empty; then it's back, within a visit length of the new start.
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.async_get_check_ins.side_effect = with_visit
    for _ in range(8):
        await advance(hass, frozen, minutes=11)
    assert state(hass, TOTAL) == "9"
    assert state(hass, EVENT) == "unknown"  # never fired


async def test_the_last_check_in_saved_by_an_older_version_doesnt_fire_again(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    fired_at = "2026-11-07T18:00:00.000+00:00"
    attributes = {"event_type": "check_in", "club": "Example Central", "check_in_time": "2026-11-07T18:25:00-06:00"}
    mock_restore_cache_with_extra_data(
        hass,
        ((State(EVENT, fired_at, attributes), {"last_event_type": "check_in", "last_event_attributes": attributes}),),
    )
    await setup(hass, config_entry)
    good = mock_client.async_get_check_ins.side_effect
    visit = CheckIn(datetime(2026, 11, 7, 18, 25), "Example Central")

    async def with_visit(start: date, end: date) -> list[CheckIn]:
        return [visit, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = with_visit
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)
    assert state(hass, TOTAL) == "9"
    assert state(hass, EVENT) == fired_at  # didn't fire again


async def test_week_and_month_dont_dip_when_visits_come_back_through_an_older_year(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    older = CheckIn(datetime(2025, 2, 3, 9, 0), "Example North")  # in the second year

    async def recovered(start: date, end: date) -> list[CheckIn]:
        extra = [older] if start <= older.local_time.date() <= end else []
        return extra + await good(start, end)

    # No history and no join date at startup; both come back by the hourly retry,
    # and the join date sends the load on to the second year first.
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)
    week = "sensor.planet_fitness_membership_check_ins_this_week"
    month = "sensor.planet_fitness_membership_check_ins_this_month"
    last = "sensor.planet_fitness_membership_last_check_in"
    seen = [(state(hass, week), state(hass, month))]
    mock_client.async_get_membership_info.side_effect = None
    mock_client.async_get_check_ins.side_effect = recovered
    await advance(hass, frozen, minutes=61)
    seen.append((state(hass, week), state(hass, month)))
    assert state(hass, last) == "unknown"  # not the older year's visit
    await advance(hass, frozen, minutes=11)  # every year again, newest first
    seen.append((state(hass, week), state(hass, month)))
    assert seen == [("unknown", "unknown"), ("unknown", "unknown"), ("3", "3")]
    assert state(hass, TOTAL) == "9"


async def test_counts_dont_dip_after_a_reload_during_a_gap_with_an_older_year_failing(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    older = CheckIn(datetime(2025, 2, 3, 9, 0), "Example North")

    async def recovered(start: date, end: date) -> list[CheckIn]:
        extra = [older] if start <= older.local_time.date() <= end else []
        return extra + await good(start, end)

    async def gap_and_second_year_fails(start: date, end: date) -> list[CheckIn]:
        if end < date(2026, 11, 1):  # every year but the newest
            raise PlanetFitnessError("HTTP 503", status=503)
        return []

    mock_client.async_get_check_ins.side_effect = recovered
    await setup(hass, config_entry)
    month = "sensor.planet_fitness_membership_check_ins_this_month"
    seen = [(state(hass, month), state(hass, TOTAL))]
    mock_client.async_get_check_ins.side_effect = gap_and_second_year_fails
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.async_get_check_ins.side_effect = recovered
    for _ in range(3):
        await advance(hass, frozen, minutes=61)
        seen.append((state(hass, month), state(hass, TOTAL)))
    assert seen[0] == ("3", "9")
    assert all(s in {("unknown", "unknown"), ("3", "9")} for s in seen)
    assert seen[-1] == ("3", "9")


async def test_this_year_waits_for_the_join_date(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    since = date(2026, 3, 1)  # rejoined; history still has a January visit
    this_year = "sensor.planet_fitness_membership_check_ins_this_year"
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)
    seen = [state(hass, this_year)]
    mock_client.async_get_membership_info.side_effect = None
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)
    await advance(hass, frozen, minutes=61)  # billing tries again
    seen.append(state(hass, this_year))
    assert seen == ["unknown", "6"]  # never 7 (with the past membership's visit), then 6


async def test_a_presale_members_past_visits_dont_count(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    since = date(2026, 11, 12)  # joins next week; history still has visits from before
    this_year = "sensor.planet_fitness_membership_check_ins_this_year"
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)
    await setup(hass, config_entry)
    seen = [(state(hass, TOTAL), state(hass, this_year))]
    for _ in range(3):
        await advance(hass, frozen, minutes=61)  # the last year reloads, with those visits
        seen.append((state(hass, TOTAL), state(hass, this_year)))
    for _ in range(6):
        await advance(hass, frozen, hours=24)  # past the start date
        seen.append((state(hass, TOTAL), state(hass, this_year)))
    assert set(seen) == {("unknown", "unknown")}


async def test_recent_visits_show_while_the_newest_year_keeps_failing(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    older = CheckIn(datetime(2025, 2, 3, 9, 0), "Example North")  # in the second year
    year_fails = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if year_fails[0] and end >= date(2026, 11, 1) and (end - start).days > 40:
            raise PlanetFitnessError("HTTP 503", status=503)
        extra = [older] if start <= older.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # no history and no join date
    mock_client.async_get_membership_info.side_effect = None
    mock_client.async_get_check_ins.side_effect = history  # the older year comes back first
    month = "sensor.planet_fitness_membership_check_ins_this_month"
    seen = []
    for minutes in (61, 11, 11):
        await advance(hass, frozen, minutes=minutes)
        seen.append((state(hass, month), state(hass, TOTAL)))
    assert seen[-1] == ("3", "unknown")  # the last 35 days show; Total waits for every year
    assert all(m in {"unknown", "3"} for m, _ in seen)
    year_fails[0] = False
    await advance(hass, frozen, minutes=61)
    assert (state(hass, month), state(hass, TOTAL)) == ("3", "9")


async def test_this_year_shows_once_the_daily_fetch_has_loaded_the_year(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    flaky = [True]
    year_loaded = [False]

    async def history(start: date, end: date) -> list[CheckIn]:
        if (end - start).days == 364:  # a whole year
            if flaky[0]:
                raise PlanetFitnessError("HTTP 503", status=503)
            year_loaded[0] = True
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # a gap: every year loads empty
    mock_client.async_get_check_ins.side_effect = history
    mock_client.async_get_current_checkin.return_value = {"club": "x"}  # the last 35 days first
    this_year = "sensor.planet_fitness_membership_check_ins_this_year"
    await advance(hass, frozen, minutes=11)
    assert state(hass, this_year) == "unknown"  # only 35 days so far
    for hour in range(30):
        flaky[0] = hour < 20  # reloading the newest year fails for a while
        await advance(hass, frozen, minutes=61)
        if year_loaded[0]:  # the daily fetch of the last year, before the reload's retry
            break
    assert year_loaded[0]
    assert state(hass, this_year) == "7"


async def test_every_count_waits_for_the_join_date(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    since = date(2026, 11, 3)  # rejoined this week; history still has a Nov 1 visit
    week = "sensor.planet_fitness_membership_check_ins_this_week"
    month = "sensor.planet_fitness_membership_check_ins_this_month"
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)
    seen = [(state(hass, week), state(hass, month))]
    mock_client.async_get_membership_info.side_effect = None
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)
    await advance(hass, frozen, minutes=61)  # billing tries again
    seen.append((state(hass, week), state(hass, month)))
    assert seen == [("unknown", "unknown"), ("2", "2")]  # never 3, then 2


async def test_a_visit_before_the_join_date_doesnt_fire_check_in(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    since = date(2026, 11, 12)  # a presale: starts next week
    mock_client.async_get_membership_info.return_value = MembershipInfo(since, since, None)
    await setup(hass, config_entry)
    good = mock_client.async_get_check_ins.side_effect
    visit = CheckIn(datetime(2026, 11, 7, 18, 25), "Example Central")

    async def with_visit(start: date, end: date) -> list[CheckIn]:
        return [visit, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = with_visit
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)
    assert mock_client.async_get_check_ins.await_count  # it was fetched
    assert state(hass, EVENT) == "unknown"  # left out like everywhere else
    assert state(hass, "sensor.planet_fitness_membership_last_check_in") == "unknown"


async def test_membership_info_is_tried_hourly_while_the_join_date_is_unknown(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)
    month = "sensor.planet_fitness_membership_check_ins_this_month"
    for _ in range(8):
        await advance(hass, frozen, minutes=61)
    assert state(hass, month) == "unknown"
    assert mock_client.async_get_membership_info.await_count == 9  # hourly, not after 1, 2, 4… hours
    mock_client.async_get_membership_info.side_effect = None
    await advance(hass, frozen, minutes=61)
    assert state(hass, month) == "3"


async def test_a_check_in_is_fetched_while_the_last_year_keeps_failing(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    visit = CheckIn(datetime(2026, 11, 7, 18, 40), "Example Central")
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # a gap: every year loads empty

    async def year_fails(start: date, end: date) -> list[CheckIn]:
        if (end - start).days > 40:
            raise PlanetFitnessError("HTTP 503", status=503)
        return [visit, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = year_fails
    await advance(hass, frozen, minutes=61)  # the hourly reload of the last year fails: an hour's wait
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)  # checked in: the last 35 days anyway
    assert state(hass, EVENT) != "unknown"  # fired
    assert state(hass, "sensor.planet_fitness_membership_last_check_in") != "unknown"


async def test_a_check_in_tries_history_once_while_it_backs_off(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await advance(hass, frozen, minutes=61)  # the recent fetch fails: an hour's wait
    calls = mock_client.async_get_check_ins.await_count
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)  # checked in: one try anyway
    assert mock_client.async_get_check_ins.await_count == calls + 1
    await advance(hass, frozen, minutes=11)
    await advance(hass, frozen, minutes=11)
    assert mock_client.async_get_check_ins.await_count == calls + 1  # then it waits again


async def test_an_empty_year_where_visits_are_known_doesnt_complete_this_year(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    this_year = "sensor.planet_fitness_membership_check_ins_this_year"
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # a gap at startup
    mock_client.async_get_check_ins.side_effect = good  # back briefly
    mock_client.async_get_current_checkin.return_value = {"club": "x"}  # the last 35 days first
    await advance(hass, frozen, minutes=11)
    mock_client.async_get_check_ins.side_effect = None  # the gap again, for over a day
    mock_client.async_get_check_ins.return_value = []
    seen = []
    for _ in range(26):  # including the daily fetch of the last year, which comes back empty
        await advance(hass, frozen, minutes=61)
        seen.append(state(hass, this_year))
    mock_client.async_get_check_ins.side_effect = good
    for _ in range(8):
        await advance(hass, frozen, minutes=61)
        seen.append(state(hass, this_year))
    assert all(s in {"unknown", "7"} for s in seen)  # never just the 35 days' 6
    assert seen[-1] == "7"


async def test_a_check_in_during_a_history_outage_doesnt_end_it_in_the_log(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    await setup(hass, config_entry)
    good = mock_client.async_get_check_ins.side_effect
    year_fails = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if year_fails[0] and (end - start).days > 40:
            raise PlanetFitnessError("HTTP 503", status=503)
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    await advance(hass, frozen, hours=24, minutes=1)  # the daily reload of the last year fails
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)  # checked in: the last 35 days, which work
    await advance(hass, frozen, minutes=61)  # the year again: still failing
    year_fails[0] = False
    for _ in range(3):
        await advance(hass, frozen, minutes=61)
    logged = [
        r.levelno
        for r in caplog.records
        if r.name.startswith("custom_components.planet_fitness")
        and r.levelno >= logging.INFO
        and "Check-in history" in r.getMessage()
    ]
    assert logged == [logging.WARNING, logging.INFO]  # one warning, one note


async def test_check_ins_coming_and_going_dont_speed_up_history_retries(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await advance(hass, frozen, minutes=61)  # the recent fetch fails: hourly from now
    calls = mock_client.async_get_check_ins.await_count
    for i in range(18):  # three hours of polls, checked in on every other one
        mock_client.async_get_current_checkin.return_value = {"club": "x"} if i % 2 == 0 else None
        await advance(hass, frozen, minutes=10)
    assert mock_client.async_get_check_ins.await_count - calls <= 6  # hourly, plus one check-in try an hour


async def test_membership_retries_dont_drag_fees_and_balance_along(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)
    for _ in range(11):
        await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_membership_info.await_count == 12  # hourly
    assert mock_client.async_get_fee_schedule.await_count == 1  # still every 12 hours
    assert mock_client.async_get_balance.await_count == 1


async def test_recent_visits_keep_coming_while_the_daily_reload_of_the_year_fails(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    await setup(hass, config_entry)
    good = mock_client.async_get_check_ins.side_effect
    year_fails = [True]
    extra: list[CheckIn] = []

    async def history(start: date, end: date) -> list[CheckIn]:
        if year_fails[0] and (end - start).days > 40:
            raise PlanetFitnessError("HTTP 503", status=503)
        return [*extra, *await good(start, end)]

    mock_client.async_get_check_ins.side_effect = history
    await advance(hass, frozen, hours=24, minutes=1)  # the daily reload of the last year fails
    extra.append(CheckIn(datetime(2026, 11, 8, 18, 30), "Example Central"))  # no check-in seen
    await advance(hass, frozen, minutes=61)  # the year again (still failing)
    await advance(hass, frozen, minutes=11)  # the last 35 days, on their usual schedule
    assert state(hass, EVENT) != "unknown"  # fired
    year_fails[0] = False
    for _ in range(3):
        await advance(hass, frozen, minutes=61)
    logged = [
        r.levelno
        for r in caplog.records
        if r.name.startswith("custom_components.planet_fitness")
        and r.levelno >= logging.INFO
        and "Check-in history" in r.getMessage()
    ]
    assert logged == [logging.WARNING, logging.INFO]  # one warning, one note


async def test_total_waits_when_the_join_date_arrives_just_before_a_rate_limit(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    older = CheckIn(datetime(2023, 6, 1, 9, 0), "Example North")  # behind a year with no visits

    async def history(start: date, end: date) -> list[CheckIn]:
        extra = [older] if start <= older.local_time.date() <= end else []
        return extra + await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)  # no join date: history stops at the first empty year
    mock_client.async_get_membership_info.side_effect = None
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessRateLimitError(1800)  # after the join date
    seen = []
    for minutes in (61, 11, 11, 11):
        await advance(hass, frozen, minutes=minutes)
        seen.append(state(hass, TOTAL))
    mock_client.async_get_fee_schedule.side_effect = None
    for _ in range(6):
        await advance(hass, frozen, minutes=11)
        seen.append(state(hass, TOTAL))
    assert all(s in {"unknown", "9"} for s in seen)  # never the 8 before the empty year
    assert seen[-1] == "9"


async def test_history_is_back_soon_after_a_long_outage(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    await setup(hass, config_entry)
    good = mock_client.async_get_check_ins.side_effect
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 503", status=503)
    for _ in range(26):  # over a day, through the daily reload of the year
        await advance(hass, frozen, minutes=61)
    mock_client.async_get_check_ins.side_effect = good
    for _ in range(3):
        await advance(hass, frozen, minutes=61)
    assert "Check-in history is available again" in caplog.text  # not a day later


async def test_an_outage_of_only_the_recent_fetch_logs_one_warning(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    await setup(hass, config_entry)
    good = mock_client.async_get_check_ins.side_effect
    recent_fails = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if recent_fails[0] and (end - start).days < 40:
            raise PlanetFitnessError("HTTP 503", status=503)
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    for _ in range(50):  # two daily reloads of the year, which work
        await advance(hass, frozen, minutes=61)
    recent_fails[0] = False
    for _ in range(2):
        await advance(hass, frozen, minutes=61)
    logged = [
        r.levelno
        for r in caplog.records
        if r.name.startswith("custom_components.planet_fitness")
        and r.levelno >= logging.INFO
        and "Check-in history" in r.getMessage()
    ]
    assert logged == [logging.WARNING, logging.INFO]


async def test_with_no_visits_a_history_outage_still_ends_in_the_log(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # no visits: the year reloads hourly, and goes first
    fail = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if fail[0]:
            fail[0] = False  # a check-in's fetch fails once
            raise PlanetFitnessError("HTTP 503", status=503)
        return []

    mock_client.async_get_check_ins.side_effect = history
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)
    mock_client.async_get_current_checkin.return_value = None
    for _ in range(12):  # polls: the year, and the 35-day retry between year reloads
        await advance(hass, frozen, minutes=11)
    mock_client.async_get_check_ins.side_effect = PlanetFitnessError("HTTP 503", status=503)  # a new outage
    await advance(hass, frozen, minutes=61)
    logged = [
        r.levelno
        for r in caplog.records
        if r.name.startswith("custom_components.planet_fitness")
        and r.levelno >= logging.INFO
        and "Check-in history" in r.getMessage()
    ]
    assert logged == [logging.WARNING, logging.INFO, logging.WARNING]


async def test_a_reload_during_a_rate_limit_pause_waits_for_it(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    with patch("custom_components.planet_fitness.PlanetFitnessClient", return_value=mock_client) as client_class:
        await setup(hass, config_entry)
        client_class.call_args.kwargs["on_pause"](3600)  # rate limited while running
        calls = mock_client.async_get_home_club.await_count
        await hass.config_entries.async_reload(config_entry.entry_id)  # turning on a billing entity, say
        await hass.async_block_till_done()
        assert config_entry.state is ConfigEntryState.SETUP_RETRY
        assert mock_client.async_get_home_club.await_count == calls  # nothing asked during the pause
        for _ in range(3):
            await advance(hass, frozen, minutes=31)
        assert config_entry.state is ConfigEntryState.LOADED


async def test_with_no_visits_an_outage_of_the_35_day_request_is_one_outage_in_the_log(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    mock_client.async_get_check_ins.side_effect = None
    mock_client.async_get_check_ins.return_value = []
    await setup(hass, config_entry)  # no visits yet
    visit = CheckIn(datetime(2026, 11, 7, 18, 25), "Example Central")
    recent_fails = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if recent_fails[0] and (end - start).days < 40:  # only the 35-day request fails
            raise PlanetFitnessError("HTTP 503", status=503)
        return [visit] if start <= visit.local_time.date() <= end else []

    mock_client.async_get_check_ins.side_effect = history
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=11)  # the check-in's try fails
    mock_client.async_get_current_checkin.return_value = None
    for _ in range(8):  # the year brings the visit, the 35-day fetch keeps failing
        await advance(hass, frozen, minutes=61)
    recent_fails[0] = False
    for _ in range(3):
        await advance(hass, frozen, minutes=61)
    logged = [
        r.levelno
        for r in caplog.records
        if r.name.startswith("custom_components.planet_fitness")
        and r.levelno >= logging.INFO
        and "Check-in history" in r.getMessage()
    ]
    assert logged == [logging.WARNING, logging.INFO]


async def test_history_is_back_in_the_log_when_a_429_cuts_the_first_load_short(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    good = mock_client.async_get_check_ins.side_effect
    mode = ["fail"]

    async def history(start: date, end: date) -> list[CheckIn]:
        if mode[0] == "fail":
            raise PlanetFitnessError("HTTP 503", status=503)
        if mode[0] == "429" and end < date(2026, 11, 1):  # the second year
            mode[0] = "ok"
            raise PlanetFitnessRateLimitError(3600)
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    await setup(hass, config_entry)  # the first load fails
    mode[0] = "429"
    await advance(hass, frozen, minutes=61)  # the newest year loads, then a 429
    await advance(hass, frozen, minutes=61)  # after the pause, the older years
    assert "Check-in history is available again" in caplog.text  # not waiting for the next 35 days


async def test_a_rate_limited_history_fetch_waits_an_hour(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_check_ins.side_effect = PlanetFitnessRateLimitError(60)  # again and again
    calls = mock_client.async_get_check_ins.await_count
    for _ in range(18):  # three hours of polls
        await advance(hass, frozen, minutes=10)
    assert mock_client.async_get_check_ins.await_count - calls <= 4  # about hourly, not every poll


async def test_a_rate_limited_fee_schedule_waits_and_doesnt_hold_up_history(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessRateLimitError(60)  # again and again
    await advance(hass, frozen, hours=12, minutes=1)  # billing is due
    fees, history = mock_client.async_get_fee_schedule.await_count, mock_client.async_get_check_ins.await_count
    for _ in range(18):  # three hours of polls
        await advance(hass, frozen, minutes=10)
    assert mock_client.async_get_fee_schedule.await_count - fees <= 3  # backing off, not every poll
    assert mock_client.async_get_check_ins.await_count - history >= 2  # history goes on


async def test_a_short_rate_limit_at_setup_holds_it_up_only_that_long(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    first = [True]

    async def current() -> None:
        if first[0]:
            first[0] = False
            raise PlanetFitnessRateLimitError(30)  # say the crowd profile's 429 paused the client

    mock_client.async_get_current_checkin.side_effect = current
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    for _ in range(12):
        await advance(hass, frozen, seconds=15)
        await hass.async_block_till_done(wait_background_tasks=True)
    assert config_entry.state is ConfigEntryState.LOADED  # minutes, not the account's 10-minute interval


async def test_a_club_held_back_by_another_pause_tries_again_right_after_it(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    good = mock_client.async_get_club.side_effect
    held = [True]

    def club(club_id: str) -> object:
        if held[0]:
            held[0] = False
            raise PlanetFitnessRateLimitError(1, sent=False)  # the account's 429 paused the client
        return good(club_id)

    mock_client.async_get_club.side_effect = club
    await advance(hass, frozen, minutes=5, seconds=1)  # the regular poll is held back
    calls = mock_client.async_get_club.await_count
    await advance(hass, frozen, seconds=3)
    assert mock_client.async_get_club.await_count > calls  # right after the pause, not 5 minutes later


async def test_history_held_back_by_another_pause_isnt_an_outage(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    await setup(hass, config_entry)
    good = mock_client.async_get_check_ins.side_effect
    held = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if held[0]:
            held[0] = False
            raise PlanetFitnessRateLimitError(60, sent=False)  # a club's 429 paused the client
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    await advance(hass, frozen, minutes=61)  # the hourly fetch is held back
    calls = mock_client.async_get_check_ins.await_count
    await advance(hass, frozen, minutes=10)  # after the pause it's still due
    assert mock_client.async_get_check_ins.await_count > calls
    assert "Check-in history unavailable" not in caplog.text


async def test_a_crowd_profile_held_back_by_another_pause_isnt_its_own_outage(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    await setup(hass, config_entry)
    mock_client.async_get_crowd_profile.side_effect = PlanetFitnessRateLimitError(60, sent=False)
    await advance(hass, frozen, hours=24, minutes=1)  # the daily profile is held back
    mock_client.async_get_crowd_profile.side_effect = None
    calls = mock_client.async_get_crowd_profile.await_count
    await advance(hass, frozen, minutes=6)  # the club's next refresh
    assert mock_client.async_get_crowd_profile.await_count > calls  # not an hour later
    assert "Crowd profile" not in caplog.text


async def test_an_added_club_rate_limited_from_the_start_waits_its_interval(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    clubs = mock_client.async_get_club.side_effect
    asked: list[str] = []

    def club(club_id: str) -> object:
        if club_id == OTHER_ID:
            asked.append(club_id)
            raise PlanetFitnessRateLimitError(1)  # again and again
        return clubs(club_id)

    mock_client.async_get_club.side_effect = club
    subentry = ConfigSubentry(
        data=MappingProxyType({CONF_CLUB_ID: OTHER_ID}),
        subentry_type=SUBENTRY_CLUB,
        title="Example North",
        unique_id=OTHER_ID,
    )
    hass.config_entries.async_add_subentry(config_entry, subentry)
    await hass.async_block_till_done()
    first = len(asked)
    for _ in range(60):
        await advance(hass, frozen, seconds=1)
    assert len(asked) == first  # not every 2 seconds, though it has no data yet


async def test_a_check_ins_rate_limited_try_holds_the_35_days_off_for_an_hour(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    mode = ["fail"]

    async def history(start: date, end: date) -> list[CheckIn]:
        if mode[0] == "429":
            mode[0] = "fail"
            raise PlanetFitnessRateLimitError(60)
        raise PlanetFitnessError("HTTP 503", status=503)

    mock_client.async_get_check_ins.side_effect = history
    await advance(hass, frozen, minutes=61)  # the 35-day fetch fails: an hour's backoff
    for _ in range(4):
        await advance(hass, frozen, minutes=10)
    mode[0] = "429"
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=10)  # the check-in's try gets a 429
    calls = mock_client.async_get_check_ins.await_count
    for _ in range(5):
        await advance(hass, frozen, minutes=10)
    assert mock_client.async_get_check_ins.await_count == calls  # nothing more within the hour


async def test_a_check_ins_try_held_back_by_another_pause_runs_after_it(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    await advance(hass, frozen, minutes=61)  # the hourly fetch
    good = mock_client.async_get_check_ins.side_effect
    held = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if held[0]:
            held[0] = False
            raise PlanetFitnessRateLimitError(60, sent=False)  # a club's 429 paused the client
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    await advance(hass, frozen, minutes=10)
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    await advance(hass, frozen, minutes=10)  # checked in: its fetch is held back
    calls = mock_client.async_get_check_ins.await_count
    await advance(hass, frozen, minutes=10)  # after the pause, not at the hourly fetch
    assert mock_client.async_get_check_ins.await_count == calls + 1


async def test_the_first_history_load_retries_right_after_a_short_rate_limit(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    first = [True]

    async def history(start: date, end: date) -> list[CheckIn]:
        if first[0]:
            first[0] = False
            raise PlanetFitnessRateLimitError(5)
        return await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    await setup(hass, config_entry)  # the first load gets a 429
    assert state(hass, TOTAL) == "unavailable"
    await advance(hass, frozen, minutes=11)  # the next poll, after the 5 s
    assert state(hass, TOTAL) == "8"  # not an hour later


async def test_rate_limits_on_the_first_load_retry_after_the_pause_once_then_hourly(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_check_ins.side_effect = PlanetFitnessRateLimitError(5)  # again and again
    await setup(hass, config_entry)
    calls = mock_client.async_get_check_ins.await_count
    await advance(hass, frozen, minutes=11)  # right after the first pause
    assert mock_client.async_get_check_ins.await_count == calls + 1
    for _ in range(5):  # then not at every poll
        await advance(hass, frozen, minutes=10)
    assert mock_client.async_get_check_ins.await_count == calls + 1


async def test_a_fee_schedule_429_storm_backs_off_while_membership_info_retries(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_membership_info.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)  # no join date: membership info alone, hourly
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessRateLimitError(60)  # again and again
    await advance(hass, frozen, hours=12, minutes=1)  # the 12-hour billing run
    fees = mock_client.async_get_fee_schedule.await_count
    for _ in range(12):  # twelve hours
        await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_fee_schedule.await_count - fees <= 3  # 1, 2, 4, 8 h, not every 1-2 h


@pytest.mark.parametrize("which", ["year", "older"])
async def test_the_reload_after_visits_come_back_keeps_a_429_wait(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    which: str,
) -> None:
    good = mock_client.async_get_check_ins.side_effect
    gap = [True]
    limit = ["older" if which == "older" else None]
    big: list[tuple[datetime, str]] = []  # year-sized requests after setup

    async def history(start: date, end: date) -> list[CheckIn]:
        today = date(2026, 11, 7)
        whole_year = (end - start).days > 40
        if limit[0] == "older" and end < today:
            limit[0] = None
            raise PlanetFitnessRateLimitError(60)  # asks a minute; owed at least an hour
        if limit[0] == "year" and whole_year and end >= today:
            limit[0] = None
            raise PlanetFitnessRateLimitError(60)
        if whole_year:
            big.append((datetime.now(), "newest" if end >= today else "older"))
        return [] if gap[0] else await good(start, end)

    mock_client.async_get_check_ins.side_effect = history
    await setup(hass, config_entry)  # a gap: no visits
    if which == "year":
        limit[0] = "year"
        await advance(hass, frozen, minutes=61)  # the hourly reload of the (empty) year gets the 429
    limited_at = datetime.now()
    big.clear()
    gap[0] = False  # the gap ends; a check-in brings the visits back
    mock_client.async_get_current_checkin.return_value = {"club": "x"}
    for _ in range(4):
        await advance(hass, frozen, minutes=10)
    assert state(hass, "sensor.planet_fitness_membership_check_ins_this_month") == "3"  # the visits are back
    soon = [kind for at, kind in big if at - limited_at < timedelta(minutes=55)]
    assert not soon  # the reload waits out the 429's hour


async def test_membership_info_retries_dont_push_billing_back(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    mock_client.async_get_membership_info.side_effect = PlanetFitnessRateLimitError(1)  # again and again
    await setup(hass, config_entry)  # no join date: membership info alone, hourly, between billing runs
    for _ in range(4):  # full runs at 0, 1 and 3 hours, membership alone in between
        await advance(hass, frozen, minutes=61)
    mock_client.async_get_membership_info.side_effect = None
    fees = mock_client.async_get_fee_schedule.await_count
    for _ in range(3):
        await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_fee_schedule.await_count > fees  # the next full run at 7 hours, not 11


async def test_optional_data_outage_is_logged_once_and_its_return(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    frozen: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.planet_fitness")
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessError("HTTP 500", status=500)
    await setup(hass, config_entry)
    await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_fee_schedule.await_count == 2
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "Fee schedule" in r.getMessage()]
    assert len(warnings) == 1

    mock_client.async_get_fee_schedule.side_effect = None
    await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_fee_schedule.await_count == 2  # backing off: two hours this time
    await advance(hass, frozen, minutes=61)
    assert mock_client.async_get_fee_schedule.await_count == 3
    assert "Fee schedule is available again" in caplog.text


def test_calendar_uses_a_calendar_description() -> None:
    # Newer Home Assistant reads calendar-only fields (initial_color) from it. Home
    # Assistant rebuilds description classes, so compare by name.
    assert CalendarEntityDescription.__name__ in {cls.__name__ for cls in type(VISITS).__mro__}
