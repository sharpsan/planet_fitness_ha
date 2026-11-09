"""Calendar of gym visits (from check-in history)."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.components.calendar import CalendarEntity, CalendarEntityDescription, CalendarEvent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import CheckIn
from .const import CONF_VISIT_MINUTES
from .coordinator import PlanetFitnessConfigEntry
from .entity import PlanetFitnessAccountEntity, options

PARALLEL_UPDATES = 0

VISITS = CalendarEntityDescription(key="visits", translation_key="visits")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlanetFitnessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([VisitsCalendar(entry.runtime_data.account, VISITS)])


class VisitsCalendar(PlanetFitnessAccountEntity, CalendarEntity):
    """Each check-in as an event lasting the configured visit length."""

    def _to_event(self, visit: CheckIn) -> CalendarEvent:
        start = visit.local_time.replace(tzinfo=self.coordinator.tz_getter())
        minutes = options(self.coordinator.config_entry)[CONF_VISIT_MINUTES]
        return CalendarEvent(
            start=start,
            end=start + timedelta(minutes=minutes),
            summary=visit.club_name or "Planet Fitness",
            uid=visit.key,
        )

    @property
    def event(self) -> CalendarEvent | None:
        """The visit in progress, if any."""
        now = dt_util.utcnow()
        for visit in self.coordinator.data.check_ins:
            event = self._to_event(visit)
            if event.start_datetime_local <= now < event.end_datetime_local:
                return event
            if event.end_datetime_local <= now:
                break  # newest first; the rest are older
        return None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        events = (self._to_event(v) for v in self.coordinator.data.check_ins)
        return sorted(
            (e for e in events if e.end_datetime_local > start_date and e.start_datetime_local < end_date),
            key=lambda e: e.start_datetime_local,
        )
