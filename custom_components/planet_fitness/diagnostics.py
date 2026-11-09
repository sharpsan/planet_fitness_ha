"""Diagnostics. Tokens, visit history, billing amounts, and club names and locations are left out."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, time
from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from .api import Club
from .const import CONF_TOKEN
from .coordinator import PlanetFitnessConfigEntry

TO_REDACT = {CONF_TOKEN}
# Where the member's clubs are says roughly where they live: the time zone too (some
# are a county), and the hours description, free text from the club.
CLUB_REDACT = (
    "club_id",
    "pf_club_id",
    "name",
    "slug",
    "timezone",
    "city",
    "state",
    "latitude",
    "longitude",
    "hours_description",
)


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: PlanetFitnessConfigEntry) -> dict[str, Any]:
    runtime = entry.runtime_data
    account = runtime.account.data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
            "subentries": [{"type": s.subentry_type} for s in entry.subentries.values()],  # titles are club names
        },
        "clubs": [
            {
                "home": club_id == runtime.home_club_id,
                "last_update_success": coordinator.last_update_success,
                "club": _club(coordinator.data.club) if coordinator.data else None,
                "max_capacity": coordinator.data.profile.max_capacity
                if coordinator.data and coordinator.data.profile
                else None,
                "profile_hours": len(coordinator.data.profile.visits)
                if coordinator.data and coordinator.data.profile
                else 0,
                "trend_samples": len(coordinator.data.samples) if coordinator.data else 0,
            }
            for club_id, coordinator in runtime.clubs.items()
        ],
        "account": {
            "last_update_success": runtime.account.last_update_success,
            "history_loaded": account.history_loaded if account else False,
            "all_loaded": account.all_loaded if account else False,
            "check_in_count": len(account.check_ins) if account else 0,
            "checked_in": account.current_checkin is not None if account else None,
            "current_checkin_shape": _shape(account.current_checkin) if account else None,
            "has_fee_schedule": bool(account and account.fee_schedule),
            "has_balance": bool(account and account.balance),
            "has_membership_info": bool(account and account.membership),
        },
    }


def _club(club: Club) -> dict[str, Any]:
    """The club with where it is left out. Holidays keep their names (Thanksgiving…)
    but not their notes, which are free text."""
    data = _plain(asdict(club))
    for key in CLUB_REDACT:
        if data.get(key) is not None:
            data[key] = REDACTED
    for holiday in data["holidays"]:
        holiday["lines"] = REDACTED
    return data


def _plain(value: Any) -> Any:
    """JSON-friendly: lists for tuples, ISO strings for dates and times."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (date, time)):
        return value.isoformat()
    return value


def _shape(value: Any) -> Any:
    """Keys and types only, so an unseen response's shape can be shared safely."""
    if isinstance(value, dict):
        return {k: _shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shape(value[0])] if value else []
    return type(value).__name__
