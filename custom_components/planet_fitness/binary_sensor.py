"""Binary sensors: club open / busier than usual; checked in / today / overdue."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import insights
from .const import CONF_BUSIER_FACTOR
from .coordinator import AccountCoordinator, AccountData, ClubCoordinator, ClubData, PlanetFitnessConfigEntry
from .entity import PlanetFitnessAccountEntity, PlanetFitnessClubEntity, add_club_entities, options

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class ClubBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[ClubData, dict[str, float], datetime], bool | None]
    attrs_fn: Callable[[ClubData, datetime], dict[str, Any]] | None = None


def _open(d: ClubData, now: datetime) -> bool | None:
    state = insights.open_state(d.club, now, d.tz)
    return state.is_open if state else None


def _open_attrs(d: ClubData, now: datetime) -> dict[str, Any]:
    today = now.astimezone(d.tz).date()
    return {
        "hours": d.club.hours_description,
        "upcoming_holidays": [
            {"name": h.name, "details": list(h.lines)} for h in insights.upcoming_holidays(d.club, today)
        ],
    }


def _busier(d: ClubData, o: dict[str, float], now: datetime) -> bool | None:
    typical = insights.typical_now(d.profile, now.astimezone(d.tz)) if d.profile else None
    return insights.busier_than_usual(d.club.occupancy_percentage, typical, o[CONF_BUSIER_FACTOR])


CLUB_BINARY_SENSORS: tuple[ClubBinaryDescription, ...] = (
    ClubBinaryDescription(
        key="open",
        translation_key="open",
        value_fn=lambda d, o, now: _open(d, now),
        attrs_fn=_open_attrs,
    ),
    ClubBinaryDescription(
        key="busier_than_usual",
        translation_key="busier_than_usual",
        value_fn=_busier,
    ),
)


@dataclass(frozen=True, kw_only=True)
class AccountBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[AccountData, Any], bool | None]
    needs_history: bool = False


ACCOUNT_BINARY_SENSORS: tuple[AccountBinaryDescription, ...] = (
    AccountBinaryDescription(
        key="checked_in",
        translation_key="checked_in",
        value_fn=lambda d, tz: d.current_checkin is not None,
    ),
    AccountBinaryDescription(
        key="checked_in_today",
        translation_key="checked_in_today",
        needs_history=True,
        value_fn=lambda d, tz: any(c.local_time.date() == dt_util.now(tz).date() for c in d.check_ins),
    ),
    AccountBinaryDescription(
        key="payment_overdue",
        translation_key="payment_overdue",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_registry_enabled_default=False,
        value_fn=lambda d, tz: d.balance.overdue if d.balance else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlanetFitnessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    add_club_entities(entry, async_add_entities, lambda c: [ClubBinarySensor(c, d) for d in CLUB_BINARY_SENSORS])
    account = entry.runtime_data.account
    async_add_entities(AccountBinarySensor(account, d) for d in ACCOUNT_BINARY_SENSORS)


class ClubBinarySensor(PlanetFitnessClubEntity, BinarySensorEntity):
    entity_description: ClubBinaryDescription
    _time_sensitive = True

    def __init__(self, coordinator: ClubCoordinator, description: ClubBinaryDescription) -> None:
        super().__init__(coordinator, description)

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(
            self.coordinator.data, options(self.coordinator.config_entry), dt_util.utcnow()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, dt_util.utcnow())


class AccountBinarySensor(PlanetFitnessAccountEntity, BinarySensorEntity):
    entity_description: AccountBinaryDescription

    def __init__(self, coordinator: AccountCoordinator, description: AccountBinaryDescription) -> None:
        super().__init__(coordinator, description)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not self.entity_description.needs_history or self.coordinator.data.history_loaded

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data, self.coordinator.tz_getter())
