"""Base entities."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_BUSIER_FACTOR,
    CONF_HIGH_THRESHOLD,
    CONF_LOW_THRESHOLD,
    CONF_VISIT_MINUTES,
    DEFAULT_BUSIER_FACTOR,
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_VISIT_MINUTES,
    DOMAIN,
)
from .coordinator import AccountCoordinator, ClubCoordinator, PlanetFitnessConfigEntry, club_device_id, club_url

MANUFACTURER = "Planet Fitness"


def membership_device_id(entry: PlanetFitnessConfigEntry) -> str:
    return str(entry.unique_id)


def options(entry: PlanetFitnessConfigEntry) -> dict[str, float]:
    """Options with defaults applied."""
    opts = entry.options
    return {
        CONF_LOW_THRESHOLD: float(opts.get(CONF_LOW_THRESHOLD, DEFAULT_LOW_THRESHOLD)),
        CONF_HIGH_THRESHOLD: float(opts.get(CONF_HIGH_THRESHOLD, DEFAULT_HIGH_THRESHOLD)),
        CONF_BUSIER_FACTOR: float(opts.get(CONF_BUSIER_FACTOR, DEFAULT_BUSIER_FACTOR)),
        CONF_VISIT_MINUTES: float(opts.get(CONF_VISIT_MINUTES, DEFAULT_VISIT_MINUTES)),
    }


class PlanetFitnessClubEntity(CoordinatorEntity[ClubCoordinator]):
    """An entity on a club device."""

    _attr_has_entity_name = True
    # Re-evaluate on a clock (typical-now, open/closed...) between polls.
    _time_sensitive = False

    def __init__(self, coordinator: ClubCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        scope = club_device_id(coordinator.config_entry, coordinator.club_id)
        self._attr_unique_id = f"{scope}_{description.key}"
        club = coordinator.data.club if coordinator.data else None
        slug = club.slug if club else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, scope)},
            name=coordinator.display_name,
            manufacturer=MANUFACTURER,
            model="Club",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url=club_url(slug),
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._time_sensitive:
            self.async_on_remove(async_track_time_change(self.hass, self._async_tick, minute="/15", second=1))

    @callback
    def _async_tick(self, _now: datetime) -> None:
        if self.available:
            self.async_write_ha_state()


class PlanetFitnessAccountEntity(CoordinatorEntity[AccountCoordinator]):
    """An entity on the membership device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: AccountCoordinator, description: EntityDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        unique = coordinator.config_entry.unique_id
        self._attr_unique_id = f"{unique}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, membership_device_id(coordinator.config_entry))},
            translation_key="membership",
            manufacturer=MANUFACTURER,
            model="Membership",
            entry_type=DeviceEntryType.SERVICE,
        )


def add_club_entities(
    entry: PlanetFitnessConfigEntry,
    async_add_entities: Callable[..., None],
    build: Callable[[ClubCoordinator], list[Any]],
) -> None:
    """Add each club's entities now, and any club added later.

    An added club's entities are attached to its config subentry.
    """

    @callback
    def add(coordinator: ClubCoordinator) -> None:
        if coordinator.subentry_id:
            async_add_entities(build(coordinator), config_subentry_id=coordinator.subentry_id)
        else:
            async_add_entities(build(coordinator))

    for coordinator in entry.runtime_data.clubs.values():
        add(coordinator)
    entry.runtime_data.club_entity_adders.append(add)
