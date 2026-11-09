"""Check-in event: fires when a new visit shows up in the history."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.event import EventEntity, EventEntityDescription, EventExtraStoredData
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .coordinator import AccountCoordinator, PlanetFitnessConfigEntry
from .entity import PlanetFitnessAccountEntity

PARALLEL_UPDATES = 0

CHECK_IN = EventEntityDescription(key="check_in", translation_key="check_in", event_types=["check_in"])


@dataclass
class CheckInStoredData(EventExtraStoredData):
    """The event's stored data, plus the newest visit known."""

    known_until: str | None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PlanetFitnessConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([CheckInEvent(entry.runtime_data.account, CHECK_IN)])


class CheckInEvent(PlanetFitnessAccountEntity, EventEntity):
    """Fires `check_in` once per new visit (never for history loaded at startup)."""

    def __init__(self, coordinator: AccountCoordinator, description: EventEntityDescription) -> None:
        super().__init__(coordinator, description)
        # Listeners also re-run without new data (an options change, a token
        # save), so remember which visits have fired.
        self._fired: set[str] = set()
        # The newest visit known, fired or loaded, kept across restarts and reloads:
        # a visit that shows up again (after a gap in the history, say) doesn't fire.
        self._known_until: datetime | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        extra = await self.async_get_last_extra_data()
        stored = extra.as_dict() if extra else {}
        # An older version saved only the last visit that fired.
        when = stored.get("known_until") or (stored.get("last_event_attributes") or {}).get("check_in_time")
        self._known_until = dt_util.parse_datetime(when) if isinstance(when, str) else None
        self._note_known()  # history loaded at startup

    @property
    def extra_restore_state_data(self) -> EventExtraStoredData:
        base = super().extra_restore_state_data
        known = self._known_until.isoformat() if self._known_until else None
        return CheckInStoredData(base.last_event_type, base.last_event_attributes, known)

    @callback
    def _note_known(self) -> None:
        data = self.coordinator.data
        if data and data.check_ins:  # newest first
            newest = data.check_ins[0].local_time.replace(tzinfo=self.coordinator.tz_getter())
            if self._known_until is None or newest > self._known_until:
                self._known_until = newest

    @callback
    def _handle_coordinator_update(self) -> None:
        data = self.coordinator.data
        tz = self.coordinator.tz_getter()
        known = self._known_until
        new = [
            v
            for v in (data.new_check_ins if data else ())
            if v.key not in self._fired and (known is None or v.local_time.replace(tzinfo=tz) > known)
        ]
        self._note_known()
        if not new:
            super()._handle_coordinator_update()
            return
        for visit in new:
            self._fired.add(visit.key)
            when = visit.local_time.replace(tzinfo=tz)
            if self._known_until is None or when > self._known_until:
                self._known_until = when
            self._trigger_event(
                "check_in",
                {"club": visit.club_name, "check_in_time": visit.local_time.replace(tzinfo=tz).isoformat()},
            )
            self.async_write_ha_state()
