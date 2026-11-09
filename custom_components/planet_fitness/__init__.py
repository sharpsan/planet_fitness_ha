"""The Planet Fitness integration."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .api import PlanetFitnessAuthError, PlanetFitnessClient, PlanetFitnessError, PlanetFitnessRateLimitError
from .const import CONF_CLUB_ID, CONF_TOKEN, DOMAIN, SUBENTRY_CLUB
from .coordinator import (
    AccountCoordinator,
    ClubCoordinator,
    PlanetFitnessConfigEntry,
    PlanetFitnessRuntimeData,
    club_device_id,
    club_timezone,
    retry_after,
)
from .entity import membership_device_id

# When setup may call the API again after a 429, per entry. Home Assistant
# retries setup within about a minute, which would ignore Retry-After.
SETUP_PAUSED_UNTIL: HassKey[dict[str, datetime]] = HassKey(f"{DOMAIN}_setup_paused_until")

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CALENDAR, Platform.EVENT, Platform.SENSOR]


def _club_subentry_ids(entry: PlanetFitnessConfigEntry) -> frozenset[str]:
    return frozenset(s.subentry_id for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CLUB)


async def async_setup_entry(hass: HomeAssistant, entry: PlanetFitnessConfigEntry) -> bool:
    """Set up Planet Fitness from a config entry."""
    paused = hass.data.setdefault(SETUP_PAUSED_UNTIL, {})

    def _waiting() -> ConfigEntryNotReady:
        local = dt_util.as_local(paused[entry.entry_id]).replace(microsecond=0)
        return ConfigEntryNotReady(f"Planet Fitness asked to slow down; waiting until {local}")

    if (until := paused.get(entry.entry_id)) and dt_util.utcnow() < until:
        raise _waiting()

    def _pause(wait: timedelta) -> None:
        until = dt_util.utcnow() + wait
        paused[entry.entry_id] = max(paused.get(entry.entry_id, until), until)  # never shortened

    @callback
    def _save_token(token: dict[str, Any]) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: token})

    client = PlanetFitnessClient(
        async_get_clientsession(hass),
        entry.data[CONF_TOKEN],
        _save_token,
        # Any pause, also while running: a reload (turning on a billing entity,
        # signing in again) waits for it too.
        on_pause=lambda seconds: _pause(timedelta(seconds=seconds)),
    )
    try:
        home = await client.async_get_home_club()
    except PlanetFitnessAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except PlanetFitnessRateLimitError as err:
        _pause(retry_after(err))
        raise _waiting() from err
    except PlanetFitnessError as err:
        raise ConfigEntryNotReady(str(err)) from err

    home_coordinator = ClubCoordinator(hass, entry, client, home.club_id, initial=home)
    clubs = {home.club_id: home_coordinator}
    for subentry in entry.subentries.values():
        club_id = subentry.data.get(CONF_CLUB_ID)
        if subentry.subentry_type != SUBENTRY_CLUB or not club_id or club_id in clubs:
            continue
        clubs[club_id] = ClubCoordinator(
            hass, entry, client, club_id, name=subentry.title, subentry_id=subentry.subentry_id
        )

    account = AccountCoordinator(
        hass,
        entry,
        client,
        lambda: club_timezone(home_coordinator.data.club if home_coordinator.data else home),
    )

    await home_coordinator.async_config_entry_first_refresh()
    for coordinator in clubs.values():
        if coordinator is not home_coordinator:
            await coordinator.async_refresh()  # an extra club failing shouldn't block setup
    try:
        await account.async_config_entry_first_refresh()
    except ConfigEntryNotReady as err:
        if wait := getattr(err.__cause__, "retry_after", None):
            _pause(timedelta(seconds=wait))
        raise

    entry.runtime_data = PlanetFitnessRuntimeData(
        client=client,
        home_club_id=home.club_id,
        clubs=clubs,
        account=account,
        club_subentry_ids=_club_subentry_ids(entry),
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_remove_stale_devices(hass, entry, clubs)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


@callback
def _async_remove_stale_devices(hass: HomeAssistant, entry: PlanetFitnessConfigEntry, club_ids: Iterable[str]) -> None:
    """Drop devices this entry no longer provides, such as an old home club."""
    expected = {(DOMAIN, membership_device_id(entry))} | {(DOMAIN, club_device_id(entry, c)) for c in club_ids}
    registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
        if not device.identifiers & expected:
            registry.async_remove_device(device.id)


async def _async_update_listener(hass: HomeAssistant, entry: PlanetFitnessConfigEntry) -> None:
    """Start or stop clubs that were added or removed, then re-render (options, token saves)."""
    data = entry.runtime_data
    if _club_subentry_ids(entry) != data.club_subentry_ids:
        _async_sync_clubs(hass, entry)
    for coordinator in (*data.clubs.values(), data.account):
        coordinator.async_update_listeners()


@callback
def _async_sync_clubs(hass: HomeAssistant, entry: PlanetFitnessConfigEntry) -> None:
    """Match the club coordinators to the club subentries without a reload.

    No awaits, so a second update can't interleave; first refreshes run in the
    background (cancelled on unload). Home Assistant removes a deleted
    subentry's devices and entities itself.
    """
    if entry.state is not ConfigEntryState.LOADED:
        return
    data = entry.runtime_data
    wanted = {s.subentry_id: s for s in entry.subentries.values() if s.subentry_type == SUBENTRY_CLUB}
    for club_id, coordinator in list(data.clubs.items()):
        if coordinator.subentry_id and coordinator.subentry_id not in wanted:
            del data.clubs[club_id]
            entry.async_create_background_task(hass, coordinator.async_shutdown(), f"{DOMAIN} stop {club_id}")
    for subentry_id, subentry in wanted.items():
        club_id = subentry.data.get(CONF_CLUB_ID)
        if subentry_id in data.club_subentry_ids or not club_id or club_id in data.clubs:
            continue
        coordinator = ClubCoordinator(hass, entry, data.client, club_id, name=subentry.title, subentry_id=subentry_id)
        data.clubs[club_id] = coordinator
        for add in data.club_entity_adders:
            add(coordinator)
        entry.async_create_background_task(hass, coordinator.async_refresh(), f"{DOMAIN} start {club_id}")
    data.club_subentry_ids = frozenset(wanted)


async def async_unload_entry(hass: HomeAssistant, entry: PlanetFitnessConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
