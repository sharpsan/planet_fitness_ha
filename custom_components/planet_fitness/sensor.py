"""Sensors for clubs (crowd) and the membership (check-ins, billing)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.typing import StateType
from homeassistant.util import dt as dt_util

from . import insights
from .const import CONF_HIGH_THRESHOLD, CONF_LOW_THRESHOLD, CURRENCY
from .coordinator import AccountData, ClubCoordinator, ClubData, PlanetFitnessConfigEntry
from .entity import PlanetFitnessAccountEntity, PlanetFitnessClubEntity, add_club_entities, options

PARALLEL_UPDATES = 0
type Value = StateType | date | datetime


@dataclass(frozen=True, kw_only=True)
class ClubSensorDescription(SensorEntityDescription):
    value_fn: Callable[[ClubData, Mapping[str, float], datetime], Value]
    attrs_fn: Callable[[ClubData, datetime], dict[str, Any]] | None = None
    time_sensitive: bool = False


def _max_capacity(d: ClubData) -> int | None:
    return d.profile.max_capacity if d.profile and d.profile.max_capacity else None


def _typical(d: ClubData, now: datetime) -> float | None:
    return insights.typical_now(d.profile, now.astimezone(d.tz)) if d.profile else None


def _quietest(d: ClubData, now: datetime) -> datetime | None:
    return insights.quietest_time_today(d.profile, d.club, now.astimezone(d.tz), d.tz) if d.profile else None


def _open_state(d: ClubData, now: datetime) -> insights.OpenState | None:
    return insights.open_state(d.club, now, d.tz)


CLUB_SENSORS: tuple[ClubSensorDescription, ...] = (
    ClubSensorDescription(
        key="crowd_level",
        translation_key="crowd_level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, o, now: d.club.occupancy_percentage,
    ),
    ClubSensorDescription(
        key="estimated_people",
        translation_key="estimated_people",
        native_unit_of_measurement="people",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, o, now: insights.estimated_people(d.club.occupancy_percentage, _max_capacity(d)),
    ),
    ClubSensorDescription(
        key="capacity",
        translation_key="capacity",
        native_unit_of_measurement="people",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d, o, now: _max_capacity(d),
    ),
    ClubSensorDescription(
        key="crowd_status",
        translation_key="crowd_status",
        device_class=SensorDeviceClass.ENUM,
        options=["low", "moderate", "high"],
        value_fn=lambda d, o, now: insights.crowd_status(
            d.club.occupancy_percentage, o[CONF_LOW_THRESHOLD], o[CONF_HIGH_THRESHOLD]
        ),
    ),
    ClubSensorDescription(
        key="typical_crowd",
        translation_key="typical_crowd",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d, o, now: _typical(d, now),
        attrs_fn=lambda d, now: (
            {"today": insights.day_profile(d.profile, now.astimezone(d.tz).weekday())} if d.profile else {}
        ),
        time_sensitive=True,
    ),
    ClubSensorDescription(
        key="quietest_time_today",
        translation_key="quietest_time_today",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d, o, now: _quietest(d, now),
        time_sensitive=True,
    ),
    ClubSensorDescription(
        key="crowd_trend",
        translation_key="crowd_trend",
        device_class=SensorDeviceClass.ENUM,
        options=["rising", "steady", "falling"],
        value_fn=lambda d, o, now: insights.crowd_trend(d.samples, now),
    ),
    ClubSensorDescription(
        key="next_closing",
        translation_key="next_closing",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d, o, now: s.next_close if (s := _open_state(d, now)) else None,
        time_sensitive=True,
    ),
    ClubSensorDescription(
        key="next_opening",
        translation_key="next_opening",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d, o, now: s.next_open if (s := _open_state(d, now)) else None,
        time_sensitive=True,
    ),
)


@dataclass(frozen=True, kw_only=True)
class AccountSensorDescription(SensorEntityDescription):
    value_fn: Callable[[AccountData, insights.CheckInStats, Any], Value]
    attrs_fn: Callable[[AccountData, insights.CheckInStats, Any], dict[str, Any]] | None = None
    needs_history: bool = True
    needs_all_years: bool = False
    needs_this_year: bool = False  # from January 1 (the newest year starts later on Dec 30-31)


def _next_annual(month: int | None, day: int | None, today: date) -> date | None:
    if not month or not day:
        return None
    for year in (today.year, today.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            return None
        if candidate >= today:
            return candidate
    return None


ACCOUNT_SENSORS: tuple[AccountSensorDescription, ...] = (
    AccountSensorDescription(
        key="last_check_in",
        translation_key="last_check_in",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda d, s, tz: s.last.local_time.replace(tzinfo=tz) if s.last else None,
        attrs_fn=lambda d, s, tz: {"club": s.last.club_name} if s.last else {},
    ),
    AccountSensorDescription(
        key="check_ins_this_week",
        translation_key="check_ins_this_week",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d, s, tz: s.this_week,
    ),
    AccountSensorDescription(
        key="check_ins_this_month",
        translation_key="check_ins_this_month",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d, s, tz: s.this_month,
    ),
    AccountSensorDescription(
        key="check_ins_this_year",
        translation_key="check_ins_this_year",
        state_class=SensorStateClass.TOTAL_INCREASING,
        needs_this_year=True,
        value_fn=lambda d, s, tz: s.this_year,
    ),
    AccountSensorDescription(
        key="total_check_ins",
        translation_key="total_check_ins",
        state_class=SensorStateClass.TOTAL_INCREASING,
        needs_all_years=True,
        value_fn=lambda d, s, tz: s.total,
    ),
    AccountSensorDescription(
        key="weekly_streak",
        translation_key="weekly_streak",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d, s, tz: s.weekly_streak,
    ),
    AccountSensorDescription(
        key="member_since",
        translation_key="member_since",
        device_class=SensorDeviceClass.DATE,
        entity_registry_enabled_default=False,
        needs_history=False,
        value_fn=lambda d, s, tz: d.membership.since if d.membership else None,
    ),
    AccountSensorDescription(
        key="next_payment_amount",
        translation_key="next_payment_amount",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        needs_history=False,
        value_fn=lambda d, s, tz: d.fee_schedule.next_amount if d.fee_schedule else None,
    ),
    AccountSensorDescription(
        key="next_payment_date",
        translation_key="next_payment_date",
        device_class=SensorDeviceClass.DATE,
        entity_registry_enabled_default=False,
        needs_history=False,
        value_fn=lambda d, s, tz: d.fee_schedule.next_date if d.fee_schedule else None,
    ),
    AccountSensorDescription(
        key="monthly_fee",
        translation_key="monthly_fee",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        needs_history=False,
        value_fn=lambda d, s, tz: d.fee_schedule.monthly_amount if d.fee_schedule else None,
        attrs_fn=lambda d, s, tz: {"billing_day": d.fee_schedule.monthly_day} if d.fee_schedule else {},
    ),
    AccountSensorDescription(
        key="annual_fee",
        translation_key="annual_fee",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        needs_history=False,
        value_fn=lambda d, s, tz: d.fee_schedule.annual_amount if d.fee_schedule else None,
        attrs_fn=lambda d, s, tz: (
            {
                "next_billing_date": _next_annual(
                    d.fee_schedule.annual_month, d.fee_schedule.annual_day, dt_util.now(tz).date()
                )
            }
            if d.fee_schedule
            else {}
        ),
    ),
    AccountSensorDescription(
        key="balance_due",
        translation_key="balance_due",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement=CURRENCY,
        suggested_display_precision=2,
        entity_registry_enabled_default=False,
        needs_history=False,
        value_fn=lambda d, s, tz: d.balance.payable if d.balance else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlanetFitnessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    add_club_entities(entry, async_add_entities, lambda c: [ClubSensor(c, description) for description in CLUB_SENSORS])
    account = entry.runtime_data.account
    async_add_entities(AccountSensor(account, description) for description in ACCOUNT_SENSORS)


class ClubSensor(PlanetFitnessClubEntity, SensorEntity):
    entity_description: ClubSensorDescription
    _unrecorded_attributes = frozenset({"today"})

    def __init__(self, coordinator: ClubCoordinator, description: ClubSensorDescription) -> None:
        super().__init__(coordinator, description)
        self._time_sensitive = description.time_sensitive

    @property
    def native_value(self) -> Value:
        return self.entity_description.value_fn(
            self.coordinator.data, options(self.coordinator.config_entry), dt_util.utcnow()
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, dt_util.utcnow())


class AccountSensor(PlanetFitnessAccountEntity, SensorEntity):
    entity_description: AccountSensorDescription

    def _stats(self) -> insights.CheckInStats:
        today = dt_util.now(self.coordinator.tz_getter()).date()
        return insights.check_in_stats(self.coordinator.data.check_ins, today)

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        return not self.entity_description.needs_history or self.coordinator.data.history_loaded

    @property
    def native_value(self) -> Value:
        description = self.entity_description
        data = self.coordinator.data
        tz = self.coordinator.tz_getter()
        new_year = dt_util.now(tz).date().replace(month=1, day=1)
        if description.state_class is SensorStateClass.TOTAL_INCREASING and (
            not data.check_ins
            or data.membership is None  # visits before the join date are left out
            or data.membership.since is None
            or (description.needs_all_years and not data.all_loaded)
            or (
                description.needs_this_year
                and not data.all_loaded  # complete back to the join date, even if that's this year
                and (data.loaded_from is None or data.loaded_from > new_year)
            )
        ):
            # No visits at all may be a gap in Planet Fitness's data, a total from some
            # of the years is too low, and without the join date a past membership's
            # visits may be in. Unknown, not a number that drops later, so long-term
            # statistics don't count visits again.
            return None
        return description.value_fn(data, self._stats(), tz)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data, self._stats(), self.coordinator.tz_getter())
