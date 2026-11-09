"""Typed models for Planet Fitness API responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

WEEKDAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY")


def _minutes(value: Any) -> int | None:
    """Parse "HH:MM" into minutes after midnight ("24:00" -> 1440)."""
    if not isinstance(value, str) or ":" not in value:
        return None
    hours, _, minutes = value.partition(":")
    try:
        total = int(hours) * 60 + int(minutes[:2])
    except ValueError:
        return None
    return total if 0 <= total <= 1440 else None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class DayHours:
    """Regular hours for one weekday (1 = Monday ... 7 = Sunday)."""

    day_of_week: int
    opens: int | None  # minutes after midnight
    closes: int | None  # minutes after midnight; 1440 = midnight at day end
    status: str | None

    @property
    def closed(self) -> bool:
        return self.opens is None or self.closes is None or (self.status or "").lower() == "closed"

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DayHours | None:
        try:
            day = int(data["dayOfWeek"])
        except KeyError, TypeError, ValueError:
            return None
        if not 1 <= day <= 7:
            return None
        opens, closes, status = _minutes(data.get("opens")), _minutes(data.get("closes")), data.get("status")
        if opens is None and closes is None and "24" in str(status or ""):
            opens, closes = 0, 1440
        return cls(day, opens, closes, status)


@dataclass(frozen=True, slots=True)
class HolidayDate:
    """Hours override for one calendar day."""

    day: date
    opens: int | None
    closes: int | None
    status: str | None
    action: str | None

    @property
    def closed(self) -> bool:
        return (self.status or "").lower() == "closed"

    @property
    def has_hours(self) -> bool:
        return self.opens is not None and self.closes is not None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> HolidayDate | None:
        day = _date(data.get("date"))
        if day is None:
            return None
        return cls(
            day, _minutes(data.get("opens")), _minutes(data.get("closes")), data.get("status"), data.get("action")
        )


@dataclass(frozen=True, slots=True)
class Holiday:
    """A holiday with its human-readable lines and per-day overrides."""

    name: str
    lines: tuple[str, ...]
    dates: tuple[HolidayDate, ...]

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Holiday:
        lines = tuple(str(data[k]) for k in ("line1", "line2", "line3") if data.get(k))
        dates = tuple(d for d in (HolidayDate.from_api(x) for x in data.get("dates") or []) if d)
        return cls(str(data.get("holidayName") or ""), lines, dates)


@dataclass(frozen=True, slots=True)
class Club:
    """A club, as returned by clubs/{id} or clubs/home-club."""

    club_id: str  # pfx:clubs:<uuid>
    pf_club_id: str | None
    name: str
    status: str | None
    slug: str | None
    timezone: str | None
    city: str | None
    state: str | None
    latitude: float | None
    longitude: float | None
    occupancy_percentage: int | None
    hours_description: str | None
    hours: tuple[DayHours, ...]
    holidays: tuple[Holiday, ...]

    @property
    def operating(self) -> bool:
        """False for clubs that are not operating at all (presale, closed...)."""
        return (self.status or "OPEN").upper().startswith("OPEN")

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Club:
        location = data.get("location") or {}
        address = location.get("address") or {}
        hours = data.get("hours") or {}
        content = data.get("content") or {}
        occupancy = data.get("occupancyPercentage")
        return cls(
            club_id=str(data["pfxClubId"]),
            pf_club_id=data.get("pfClubId"),
            name=str(data.get("name") or "Planet Fitness"),
            status=data.get("status"),
            slug=data.get("slug"),
            timezone=data.get("timezone"),
            city=address.get("city"),
            state=address.get("stateCode"),
            latitude=_float(location.get("latitude")),
            longitude=_float(location.get("longitude")),
            occupancy_percentage=int(occupancy) if isinstance(occupancy, (int, float)) else None,
            hours_description=hours.get("description"),
            hours=tuple(h for h in (DayHours.from_api(x) for x in hours.get("hours") or []) if h),
            holidays=tuple(Holiday.from_api(x) for x in content.get("holidayHours") or []),
        )


@dataclass(frozen=True, slots=True)
class ClubSummary:
    """A club search result."""

    club_id: str
    name: str
    city: str | None
    state: str | None
    distance_m: float | None  # meters from the search point

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> ClubSummary:
        address = (data.get("location") or {}).get("address") or {}
        return cls(
            club_id=str(data["pfxClubId"]),
            name=str(data.get("name") or "Planet Fitness"),
            city=address.get("city"),
            state=address.get("stateCode"),
            distance_m=_float(data.get("distance")),
        )


@dataclass(frozen=True, slots=True)
class CrowdProfile:
    """Typical visits per weekday/hour (club local time) and the club's capacity."""

    max_capacity: int
    visits: dict[tuple[int, int], float] = field(default_factory=dict)  # (weekday 0=Mon, hour)

    def typical_percent(self, weekday: int, hour: int) -> float | None:
        if self.max_capacity <= 0 or (weekday, hour) not in self.visits:
            return None
        return self.visits[(weekday, hour)] / self.max_capacity * 100

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CrowdProfile:
        visits: dict[tuple[int, int], float] = {}
        for item in data.get("crowdHistory") or []:
            day = str(item.get("weekDay", "")).upper()
            hour, value = item.get("hourOfDay"), _float(item.get("visits"))
            if day in WEEKDAYS and isinstance(hour, int) and 0 <= hour <= 23 and value is not None:
                visits[(WEEKDAYS.index(day), hour)] = value
        return cls(int(data.get("maxCapacity") or 0), visits)


@dataclass(frozen=True, slots=True)
class CheckIn:
    """One club visit. `local_time` is the club's wall-clock time (naive)."""

    local_time: datetime
    club_name: str | None

    @property
    def key(self) -> str:
        return self.local_time.isoformat()

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CheckIn | None:
        # dateTime/historyDate end in "Z" but are local time, same as localTimestamp.
        raw = data.get("localTimestamp") or data.get("dateTime") or data.get("historyDate")
        if not isinstance(raw, str):
            return None
        try:
            parsed = datetime.fromisoformat(raw.rstrip("Z"))
        except ValueError:
            return None
        return cls(parsed.replace(tzinfo=None, microsecond=0), data.get("clubName"))


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """billing/v2/fee-schedule."""

    monthly_amount: float | None
    monthly_day: int | None
    annual_amount: float | None
    annual_month: int | None
    annual_day: int | None
    next_amount: float | None
    next_date: date | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> FeeSchedule:
        monthly = data.get("monthlyFee") or {}
        annual = data.get("annualFee") or {}
        nxt = data.get("nextPayment") or {}
        return cls(
            monthly_amount=_float(monthly.get("amount")),
            monthly_day=monthly.get("billingDay"),
            annual_amount=_float(annual.get("amount")),
            annual_month=annual.get("billingMonth"),
            annual_day=annual.get("billingDay"),
            next_amount=_float(nxt.get("amount")),
            next_date=_date(nxt.get("date")),
        )


@dataclass(frozen=True, slots=True)
class Balance:
    """billing/v2/balance."""

    payable: float | None
    processing: float | None
    overdue: bool
    in_draft_period: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Balance:
        return cls(
            payable=_float(data.get("payableBalance")),
            processing=_float(data.get("processingBalance")),
            overdue=bool(data.get("isOverdue")),
            in_draft_period=bool(data.get("inDraftPeriod")),
        )


@dataclass(frozen=True, slots=True)
class MembershipInfo:
    """memberships/info."""

    since: date | None
    begin: date | None
    expiration: date | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> MembershipInfo:
        return cls(_date(data.get("sinceDate")), _date(data.get("beginDate")), _date(data.get("expirationDate")))
