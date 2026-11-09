"""Setup, entity states, the check-in event, token persistence and reauth."""

from __future__ import annotations

from datetime import timedelta
import json
from unittest.mock import MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.planet_fitness.api import CheckIn, PlanetFitnessAuthError, PlanetFitnessError
from custom_components.planet_fitness.const import CONF_CLUB_ID, CONF_TOKEN, DOMAIN, SUBENTRY_CLUB

from .conftest import HOME_ID, OTHER_ID, SUBJECT, check_ins, make_token

BILLING_ENTITIES = (
    "sensor.planet_fitness_membership_member_since",
    "sensor.planet_fitness_membership_next_payment_amount",
    "sensor.planet_fitness_membership_next_payment_date",
    "sensor.planet_fitness_membership_monthly_fee",
    "sensor.planet_fitness_membership_annual_fee",
    "sensor.planet_fitness_membership_balance_due",
    "binary_sensor.planet_fitness_membership_payment_overdue",
)

NOW = "2026-11-07 18:20:00-06:00"  # Saturday evening, Central time


async def setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture(autouse=True)
def frozen(freezer: FrozenDateTimeFactory) -> FrozenDateTimeFactory:
    freezer.move_to(NOW)
    return freezer


async def test_club_entities(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    await hass.config.async_set_time_zone("America/Chicago")
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    def state(entity_id: str) -> str:
        return hass.states.get(entity_id).state

    assert state("sensor.example_central_crowd_level") == "23"
    assert state("sensor.example_central_estimated_people") == "69"
    assert state("sensor.example_central_capacity") == "300"
    assert state("sensor.example_central_crowd_status") == "low"
    typical = hass.states.get("sensor.example_central_typical_crowd_now")
    assert float(typical.state) > 15 and len(typical.attributes["today"]) == 24
    assert state("binary_sensor.example_central_busier_than_usual") == "on"  # 23% vs ~15% typical
    open_state = hass.states.get("binary_sensor.example_central_open")
    assert open_state.state == "on"
    assert open_state.attributes["upcoming_holidays"][0]["name"] == "Thanksgiving"
    assert state("sensor.example_central_next_closing") == "2026-11-26T02:00:00+00:00"  # Nov 25 8 PM CST
    assert state("sensor.example_central_next_opening") == "2026-11-27T12:00:00+00:00"
    assert state("sensor.example_central_quietest_time_today").startswith("2026-11-0")
    assert state("sensor.example_central_crowd_trend") == "unknown"  # needs 15+ min of samples


async def test_account_entities(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    await setup(hass, config_entry)

    def state(entity_id: str) -> str:
        return hass.states.get(entity_id).state

    assert state("sensor.planet_fitness_membership_last_check_in") == "2026-11-05T12:30:00+00:00"  # 6:30 AM CST
    assert hass.states.get("sensor.planet_fitness_membership_last_check_in").attributes["club"] == "Example Central"
    assert state("sensor.planet_fitness_membership_check_ins_this_week") == "3"
    assert state("sensor.planet_fitness_membership_check_ins_this_month") == "3"
    assert state("sensor.planet_fitness_membership_check_ins_this_year") == "7"
    assert state("sensor.planet_fitness_membership_total_check_ins") == "8"
    assert state("sensor.planet_fitness_membership_weekly_streak") == "3"
    assert state("binary_sensor.planet_fitness_membership_checked_in") == "off"
    assert state("binary_sensor.planet_fitness_membership_checked_in_today") == "off"
    assert state("calendar.planet_fitness_membership_visits") == "off"

    # Membership and billing (disabled by default; tests turn every entity on).
    assert state("sensor.planet_fitness_membership_member_since") == "2022-02-14"
    assert state("sensor.planet_fitness_membership_next_payment_amount") == "15.0"
    assert state("sensor.planet_fitness_membership_next_payment_date") == "2026-12-05"
    assert state("sensor.planet_fitness_membership_monthly_fee") == "15.0"
    assert hass.states.get("sensor.planet_fitness_membership_monthly_fee").attributes["billing_day"] == 5
    assert state("sensor.planet_fitness_membership_annual_fee") == "49.0"
    annual = hass.states.get("sensor.planet_fitness_membership_annual_fee").attributes
    assert str(annual["next_billing_date"]) == "2027-03-15"
    assert state("sensor.planet_fitness_membership_balance_due") == "0.0"
    assert state("binary_sensor.planet_fitness_membership_payment_overdue") == "off"


@pytest.mark.entity_defaults
async def test_membership_and_billing_entities_start_disabled(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(hass, config_entry)
    registry = er.async_get(hass)
    for entity_id in BILLING_ENTITIES:
        entry = registry.async_get(entity_id)
        assert entry is not None and entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, entity_id
        assert hass.states.get(entity_id) is None


async def test_history_windows_back_to_member_since(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    await setup(hass, config_entry)
    windows = [(c.args[0].isoformat(), c.args[1].isoformat()) for c in mock_client.async_get_check_ins.await_args_list]
    assert windows[0] == ("2025-11-09", "2026-11-08")  # 365 days ending tomorrow
    assert windows[-1][0] == "2022-02-14"  # stops at the membership start date
    assert len(windows) == 5


async def test_check_in_event_fires_for_new_visit_only(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, frozen: FrozenDateTimeFactory
) -> None:
    await setup(hass, config_entry)
    event_id = "event.planet_fitness_membership_check_in"
    assert hass.states.get(event_id).state == "unknown"  # history at startup doesn't fire

    new_visit = CheckIn(check_ins()[0].local_time.replace(day=7, hour=18, minute=5, second=0), "Example Central")
    mock_client.async_get_current_checkin.return_value = {"club": "x"}

    async def with_new(start, end):
        return [new_visit, *[c for c in check_ins() if start <= c.local_time.date() <= end]]

    mock_client.async_get_check_ins.side_effect = with_new
    frozen.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()

    event = hass.states.get(event_id)
    assert event.state != "unknown"
    assert event.attributes["event_type"] == "check_in"
    assert event.attributes["club"] == "Example Central"
    assert event.attributes["check_in_time"] == "2026-11-07T18:05:00-06:00"
    assert hass.states.get("binary_sensor.planet_fitness_membership_checked_in").state == "on"
    assert hass.states.get("binary_sensor.planet_fitness_membership_checked_in_today").state == "on"
    assert hass.states.get("calendar.planet_fitness_membership_visits").state == "on"

    # A token save re-runs the listeners with the same data; the event must not fire again.
    fired_at = event.state
    frozen.tick(timedelta(minutes=1))
    token = {**config_entry.data[CONF_TOKEN], "access_token": "rotated"}
    hass.config_entries.async_update_entry(config_entry, data={**config_entry.data, CONF_TOKEN: token})
    await hass.async_block_till_done()
    assert hass.states.get(event_id).state == fired_at


async def test_stale_club_device_removed(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    config_entry.add_to_hass(hass)
    registry = dr.async_get(hass)
    old = registry.async_get_or_create(
        config_entry_id=config_entry.entry_id, identifiers={(DOMAIN, f"{SUBJECT}_pfx:clubs:old-home")}
    )
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get(old.id) is None
    assert registry.async_get_device_by_identifier((DOMAIN, f"{SUBJECT}_{HOME_ID}"), config_entry.entry_id) is not None
    assert registry.async_get_device_by_identifier((DOMAIN, SUBJECT), config_entry.entry_id) is not None


async def test_token_refresh_saved_without_reload(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    with patch("custom_components.planet_fitness.PlanetFitnessClient", return_value=mock_client) as factory:
        await setup(hass, config_entry)
    on_token_update = factory.call_args.args[2]
    new = {"access_token": "a2", "refresh_token": "r2", "expires_at": 1}
    on_token_update(new)
    await hass.async_block_till_done()
    assert config_entry.data[CONF_TOKEN] == new
    assert config_entry.state is ConfigEntryState.LOADED
    assert factory.call_count == 1  # not reloaded


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.async_get_home_club.side_effect = PlanetFitnessAuthError("expired")
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_api_down_retries_setup(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.async_get_home_club.side_effect = PlanetFitnessError("HTTP 503", status=503)
    await setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_billing_failure_does_not_break_account(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock
) -> None:
    mock_client.async_get_fee_schedule.side_effect = PlanetFitnessError("HTTP 403", status=403)
    await setup(hass, config_entry)
    assert hass.states.get("sensor.planet_fitness_membership_total_check_ins").state == "8"


async def test_diagnostics_redacts_token(
    hass: HomeAssistant, mock_client: MagicMock, hass_client: ClientSessionGenerator
) -> None:
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SUBJECT,
        data={CONF_TOKEN: make_token()},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_CLUB_ID: OTHER_ID}, subentry_type=SUBENTRY_CLUB, title="Example North", unique_id=OTHER_ID
            )
        ],
    )
    mock_client.async_get_current_checkin.return_value = {"club": "Example Central", "since": 1}
    await setup(hass, config_entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    assert diag["account"]["current_checkin_shape"] == {"club": "str", "since": "int"}
    assert diag["entry"]["data"][CONF_TOKEN] == "**REDACTED**"
    text = json.dumps(diag)
    assert "Example Central" not in text and "Example North" not in text
    assert "Example City" not in text and "Northtown" not in text and "39.0" not in text
    assert diag["account"]["check_in_count"] == 8
    assert diag["account"]["all_loaded"] is True
    assert "Closes at 8:00 PM" not in text and "Open 24/7" not in text  # free text from the club
    assert "America/Chicago" not in text  # some time zones are a county
    assert "Easter" in text and '"2026-04-04"' in text and "__type" not in text  # holidays stay readable
    assert "check_ins" not in diag["account"]


async def test_unload(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    await setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
