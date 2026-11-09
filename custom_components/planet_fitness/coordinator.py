"""Data update coordinators for clubs and the member account."""

from __future__ import annotations

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import insights
from .api import (
    Balance,
    CheckIn,
    Club,
    CrowdProfile,
    FeeSchedule,
    MembershipInfo,
    PlanetFitnessAuthError,
    PlanetFitnessClient,
    PlanetFitnessError,
    PlanetFitnessRateLimitError,
)
from .const import (
    ACCOUNT_INTERVAL,
    BILLING_MAX_AGE,
    CLUB_CLOSED_INTERVAL,
    CLUB_INTERVAL,
    CONF_VISIT_MINUTES,
    CROWD_PROFILE_MAX_AGE,
    DEFAULT_RETRY_AFTER,
    DEFAULT_VISIT_MINUTES,
    DOMAIN,
    EARLIEST_START,
    HISTORY_MAX_WINDOWS,
    HISTORY_WINDOW_DAYS,
    NEW_CHECK_IN_MAX_AGE,
    OPTIONAL_RETRY,
    RECENT_HISTORY_DAYS,
    RECENT_HISTORY_MAX_AGE,
    TREND_WINDOW,
    YEAR_HISTORY_MAX_AGE,
)

_LOGGER = logging.getLogger(__name__)

HISTORY = "Check-in history"  # one outage, whether recent or older years fail

# The entities the optional billing calls feed (by key). They start disabled.
FEE_SCHEDULE_KEYS = ("next_payment_amount", "next_payment_date", "monthly_fee", "annual_fee")
BALANCE_KEYS = ("balance_due", "payment_overdue")

type PlanetFitnessConfigEntry = ConfigEntry[PlanetFitnessRuntimeData]


def club_timezone(club: Club | None) -> tzinfo:
    if club and club.timezone and (tz := dt_util.get_time_zone(club.timezone)):
        return tz
    return dt_util.get_default_time_zone()


def retry_after(err: PlanetFitnessRateLimitError) -> timedelta:
    return timedelta(seconds=err.retry_after) if err.retry_after else DEFAULT_RETRY_AFTER


def _update_error(err: PlanetFitnessError, interval: timedelta | None = None) -> Exception:
    """Map an API error to what a coordinator must raise. A rate limit waits as long
    as asked, and, if this coordinator's own request got it, never less than its
    usual `interval`."""
    if isinstance(err, PlanetFitnessAuthError):
        return ConfigEntryAuthFailed(f"Planet Fitness sign-in expired: {err}")
    if isinstance(err, PlanetFitnessRateLimitError):
        # A second to spare: Home Assistant may run a retry up to a second early.
        wait = math.ceil(retry_after(err).total_seconds()) + 1
        if interval is not None and err.sent:  # held back by another's pause: right after it
            wait = max(wait, math.ceil(interval.total_seconds()))
        until = dt_util.as_local(dt_util.utcnow() + timedelta(seconds=wait)).replace(microsecond=0)
        return UpdateFailed(f"Planet Fitness asked to slow down; waiting until {until}", retry_after=wait)
    return UpdateFailed(str(err))


def _after_429(now: datetime, err: PlanetFitnessRateLimitError) -> datetime:
    """When a call that got a 429 is due again: in an hour, or as long as asked if
    that's longer. (The pause is logged once; the call doesn't start an outage.)"""
    return now + max(OPTIONAL_RETRY, retry_after(err))


async def _guard[T](call: Awaitable[T], interval: timedelta | None = None) -> T:
    try:
        return await call
    except PlanetFitnessError as err:
        raise _update_error(err, interval) from err


class _Outage:
    """Optional data that went missing: logs that once (not on every retry) and its
    return, and spaces the retries out, from an hour, doubling, up to `longest`."""

    def __init__(self) -> None:
        self.failures = 0

    def failed(self, what: str, reason: object, longest: timedelta) -> timedelta:
        """Returns how long to wait before trying again."""
        self.failures += 1
        if self.failures == 1:
            _LOGGER.warning("%s unavailable, trying again later: %s", what, reason)
        else:
            _LOGGER.debug("%s still unavailable: %s", what, reason)
        return min(OPTIONAL_RETRY * 2 ** min(self.failures - 1, 8), longest)

    def ok(self, what: str) -> None:
        if self.failures:
            self.failures = 0
            _LOGGER.info("%s is available again", what)


class _Backoff:
    """One schedule's own retry delays: from an hour, doubling, up to `longest`."""

    def __init__(self) -> None:
        self.failures = 0

    def next(self, longest: timedelta) -> timedelta:
        self.failures += 1
        return min(OPTIONAL_RETRY * 2 ** min(self.failures - 1, 8), longest)


def club_device_id(entry: ConfigEntry, club_id: str) -> str:
    """Scoped to the account, so two members tracking the same club don't collide."""
    return f"{entry.unique_id}_{club_id}"


def club_url(slug: str | None) -> str | None:
    """The club's page on planetfitness.com."""
    return f"https://www.planetfitness.com/gyms/{slug}" if slug else None


# --------------------------------------------------------------------------- #
# Clubs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ClubData:
    club: Club
    profile: CrowdProfile | None
    samples: tuple[tuple[datetime, int], ...]

    @property
    def tz(self) -> tzinfo:
        return club_timezone(self.club)


class ClubCoordinator(DataUpdateCoordinator[ClubData]):
    """Polls one club's occupancy, and its crowd profile once a day."""

    config_entry: PlanetFitnessConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PlanetFitnessConfigEntry,
        client: PlanetFitnessClient,
        club_id: str,
        *,
        initial: Club | None = None,
        name: str | None = None,
        subentry_id: str | None = None,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} club {club_id}", update_interval=CLUB_INTERVAL
        )
        self.client = client
        self.club_id = club_id
        self.subentry_id = subentry_id
        self.display_name = initial.name if initial else (name or "Planet Fitness")
        self._initial = initial
        self._profile: CrowdProfile | None = None
        self._profile_next: datetime | None = None
        self._profile_outage = _Outage()
        self._samples: deque[tuple[datetime, int]] = deque()
        self._linked = False

    async def _async_update_data(self) -> ClubData:
        now = dt_util.utcnow()
        if self._initial is not None:
            club, self._initial = self._initial, None
        else:
            club = await _guard(self.client.async_get_club(self.club_id), self.update_interval)
        self.display_name = club.name
        if not self._linked:
            self._linked = self._link_device(club)
        await self._refresh_profile(club, now)
        if club.occupancy_percentage is not None:
            self._samples.append((now, club.occupancy_percentage))
        while self._samples and now - self._samples[0][0] > TREND_WINDOW:
            self._samples.popleft()
        self._schedule_next(club, now)
        return ClubData(club, self._profile, tuple(self._samples))

    def _link_device(self, club: Club) -> bool:
        """Link the device to the club's page. A club added while running gets its
        device before it has data. False until the device exists.
        """
        identifier = (DOMAIN, club_device_id(self.config_entry, self.club_id))
        registry = dr.async_get(self.hass)
        for device in dr.async_entries_for_config_entry(registry, self.config_entry.entry_id):
            if identifier in device.identifiers:
                url = club_url(club.slug)
                if url and device.configuration_url != url:
                    registry.async_update_device(device.id, configuration_url=url)
                return True
        return False

    async def _refresh_profile(self, club: Club, now: datetime) -> None:
        """The crowd profile is optional: a failure backs off without failing the update,
        and keeps the last good profile."""
        if self._profile_next is not None and now < self._profile_next:
            return
        what = f"Crowd profile for {club.name}"
        try:
            profile = await self.client.async_get_crowd_profile(self.club_id)
        except PlanetFitnessAuthError as err:
            raise _update_error(err) from err
        except PlanetFitnessRateLimitError as err:
            if not err.sent:
                return  # another request's pause: due again after it
            wait = self._profile_outage.failed(what, "rate limited", CROWD_PROFILE_MAX_AGE)
            self._profile_next = now + max(wait, retry_after(err))
            return
        except PlanetFitnessError as err:
            self._profile_next = now + self._profile_outage.failed(what, err, CROWD_PROFILE_MAX_AGE)
            return
        had_visits = self._profile is not None and bool(self._profile.visits)
        if profile.max_capacity <= 0 or (had_visits and not profile.visits):
            # Planet Fitness sometimes leaves out the capacity (seen overnight).
            # Keep the last good profile.
            reason = "the response had no capacity" if profile.max_capacity <= 0 else "no crowd data"
            self._profile_next = now + self._profile_outage.failed(what, reason, CROWD_PROFILE_MAX_AGE)
            return
        self._profile = profile
        if not profile.visits:
            # A capacity but no crowd history yet (a new club?): use it, and check back.
            self._profile_next = now + self._profile_outage.failed(what, "no crowd data", CROWD_PROFILE_MAX_AGE)
            return
        self._profile_next = now + CROWD_PROFILE_MAX_AGE
        self._profile_outage.ok(what)

    def _schedule_next(self, club: Club, now: datetime) -> None:
        state = insights.open_state(club, now, club_timezone(club))
        interval = CLUB_INTERVAL
        if state is not None and not state.is_open:
            interval = CLUB_CLOSED_INTERVAL
            if state.next_open is not None:
                interval = max(CLUB_INTERVAL, min(interval, state.next_open - now))
        self.update_interval = interval


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AccountData:
    check_ins: tuple[CheckIn, ...]  # newest first
    history_loaded: bool  # the newest year
    all_loaded: bool  # every year back to the join date
    loaded_from: date | None  # everything from this day through today has loaded
    current_checkin: Any  # None when not checked in
    fee_schedule: FeeSchedule | None
    balance: Balance | None
    membership: MembershipInfo | None
    new_check_ins: tuple[CheckIn, ...]  # appeared since the previous refresh


class AccountCoordinator(DataUpdateCoordinator[AccountData]):
    """The current check-in every 10 minutes; history and billing on their own schedules.

    Only the current check-in is required. History and billing back off on errors
    without failing the update, and a 429 pauses every request. The fee schedule
    and balance are only fetched while one of their entities is on.
    """

    config_entry: PlanetFitnessConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: PlanetFitnessConfigEntry,
        client: PlanetFitnessClient,
        tz_getter: Callable[[], tzinfo],
    ) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=f"{DOMAIN} account", update_interval=ACCOUNT_INTERVAL)
        self.client = client
        self.tz_getter = tz_getter
        # Visits are merged in and never dropped: a short or empty response can't
        # erase history (a deleted check-in disappears after a restart).
        self._check_ins: dict[str, CheckIn] = {}
        self._history_loaded = False  # the newest year has loaded
        self._all_loaded = False  # every year back to the membership start has loaded
        self._backfill_end: date | None = None  # end of the next older year to load
        self._next_backfill: datetime | None = None  # set after an older year fails
        self._next_year: datetime | None = None
        self._next_recent: datetime | None = None
        self._next_history_try: datetime | None = None  # set after a failure
        self._history_due = False  # a check-in started; fetch recent history now
        self._reload_all = False  # visits came back after history loaded empty
        self._unannounced: list[CheckIn] = []  # new visits not yet passed on
        self._rate_limited = False  # logged, until a refresh gets through
        self._loaded_from: date | None = None
        self._next_billing: datetime | None = None
        self._next_membership: datetime | None = None  # membership info alone, while it fails
        self._side_trip_for: datetime | None = None  # the backoff a check-in already cut into
        self._first_load_limited = False  # the first load got a 429 already
        self._history_429_until: datetime | None = None  # the year's or older years' 429 wait
        self._year_failing = False  # the last year's reload failed; it retries on its own
        self._year_backoff = _Backoff()
        self._backfill_backoff = _Backoff()  # older years
        self._billing_backoff = _Backoff()  # after a 429
        self._recent_failing = False  # the last 35 days (or the first load) failed
        self._paused_until: datetime | None = None  # after a 429
        self._was_checked_in = False
        self._future_end_ok = True
        self._fee: FeeSchedule | None = None
        self._balance: Balance | None = None
        self._membership: MembershipInfo | None = None
        self._outages: dict[str, _Outage] = {}
        self._started = dt_util.utcnow()

    def _outage(self, what: str) -> _Outage:
        return self._outages.setdefault(what, _Outage())

    async def _async_update_data(self) -> AccountData:
        now = dt_util.utcnow()
        today = dt_util.now(self.tz_getter()).date()
        # Setup's first refresh waits only as asked; later ones never sooner than usual.
        current = await _guard(self.client.async_get_current_checkin(), self.update_interval if self.data else None)
        checked_in = current is not None
        if checked_in and not self._was_checked_in:
            self._history_due = True
        self._was_checked_in = checked_in

        if self._paused_until is None or now >= self._paused_until:
            try:
                await self._refresh_billing(now)
                await self._refresh_history(now, today)
            except PlanetFitnessAuthError as err:
                raise _update_error(err) from err
            except PlanetFitnessRateLimitError as err:
                self._paused_until = dt_util.utcnow() + retry_after(err)
                log = _LOGGER.debug if self._rate_limited else _LOGGER.warning
                self._rate_limited = True
                log(
                    "Rate limited; pausing requests to Planet Fitness until %s",
                    dt_util.as_local(self._paused_until).replace(microsecond=0),
                )
            else:
                self._rate_limited = False

        since = self._membership.since if self._membership else None
        new = tuple(
            sorted(
                (c for c in self._unannounced if since is None or c.local_time.date() >= since),
                key=lambda c: c.local_time,
            )
        )
        self._unannounced.clear()
        visits = sorted(self._check_ins.values(), key=lambda c: c.local_time, reverse=True)
        if self._loaded_from is None:
            # Nothing loaded through today: visits that came back through an older
            # year wait for the newest one, so the counts don't dip.
            visits = []
        else:
            # And nothing before the join date (a past membership, or a presale's
            # start): the full load stops there but the yearly reloads don't.
            floor = max(self._loaded_from, since) if since else self._loaded_from
            visits = [c for c in visits if c.local_time.date() >= floor]
        return AccountData(
            check_ins=tuple(visits),
            history_loaded=self._history_loaded,
            # Without the join date, history stops at the first year with no visits.
            all_loaded=self._history_loaded
            and self._all_loaded
            and self._membership is not None
            and self._membership.since is not None,
            loaded_from=self._loaded_from,
            current_checkin=current,
            fee_schedule=self._fee,
            balance=self._balance,
            membership=self._membership,
            new_check_ins=new,
        )

    async def _refresh_billing(self, now: datetime) -> None:
        due = self._next_billing is None or now >= self._next_billing
        retry = self._next_membership is not None and now >= self._next_membership
        if not due and not retry:
            return
        calls: list[tuple[str, str, Callable[[], Awaitable[Any]]]] = [
            # Always: history goes back to the membership's start date.
            ("_membership", "Membership info", self.client.async_get_membership_info),
        ]
        if due and self._in_use(FEE_SCHEDULE_KEYS):
            calls.append(("_fee", "Fee schedule", self.client.async_get_fee_schedule))
        if due and self._in_use(BALANCE_KEYS):
            calls.append(("_balance", "Balance", self.client.async_get_balance))
        wait = BILLING_MAX_AGE
        next_membership = None
        limited: PlanetFitnessRateLimitError | None = None
        try:
            for attr, what, call in calls:
                try:
                    setattr(self, attr, await call())
                except PlanetFitnessAuthError:
                    raise
                except PlanetFitnessError as err:
                    needed = attr == "_membership" and (self._membership is None or self._membership.since is None)
                    if isinstance(err, PlanetFitnessRateLimitError):
                        if not err.sent:
                            raise  # another request's pause: due again after it
                        limited = err
                        if due:  # the run (not a membership-only retry) waits 1, 2, 4… hours, or as asked
                            wait = min(wait, max(self._billing_backoff.next(BILLING_MAX_AGE), retry_after(err)))
                        if needed:
                            next_membership = _after_429(now, err)
                        break
                    if needed:
                        # The counts wait for the join date: try it alone hourly until it's known.
                        next_membership = now + self._outage(what).failed(what, err, OPTIONAL_RETRY)
                    else:
                        wait = min(wait, self._outage(what).failed(what, err, BILLING_MAX_AGE))
                else:
                    self._outage(what).ok(what)
        finally:
            # Even if a later call is rate limited: without the start date history
            # stopped early, so go on (and Total waits) as soon as it's known.
            since = self._membership.since if self._membership else None
            if self._all_loaded and since and self._backfill_end and since <= self._backfill_end:
                self._all_loaded = False
        self._next_membership = next_membership
        if due:
            self._next_billing = now + wait
        if due and not limited:
            self._billing_backoff.failures = 0
        if limited:
            raise limited  # pause; history goes on after it

    def _in_use(self, keys: tuple[str, ...]) -> bool:
        """False once all these entities are registered and disabled.

        Checked on every billing refresh (turning one on also reloads the entry, so
        its data loads right away). Until they're registered, on a new install, they
        count as in use.
        """
        ids = {f"{self.config_entry.unique_id}_{key}" for key in keys}
        registry = er.async_get(self.hass)
        known = [
            e for e in er.async_entries_for_config_entry(registry, self.config_entry.entry_id) if e.unique_id in ids
        ]
        return len(known) < len(ids) or any(not e.disabled for e in known)

    async def _refresh_history(self, now: datetime, today: date) -> None:
        """Every year once, then the last year daily and the last 35 days hourly."""
        backing_off = self._next_history_try is not None and now < self._next_history_try
        side_trip = (  # a check-in during a backoff: the last 35 days, once per backoff
            backing_off and self._history_due and self._history_loaded and self._side_trip_for != self._next_history_try
        )
        if backing_off and not side_trip:
            return
        recent = False  # the last 35 days loaded (or the first load, which has them)
        turn = (self._history_due, self._side_trip_for, self._history_loaded)  # if held back
        limited: PlanetFitnessRateLimitError | None = None  # scheduled, to pass on
        try:
            if side_trip:
                self._side_trip_for = self._next_history_try
                self._history_due = False
                await self._fetch_recent(today, RECENT_HISTORY_DAYS)
                self._next_recent = now + RECENT_HISTORY_MAX_AGE
            elif not self._all_loaded and (
                not self._history_loaded or self._next_backfill is None or now >= self._next_backfill
            ):
                recent = not self._history_loaded
                limited = await self._refresh_backfill(now, today)
            elif self._next_year is None or now >= self._next_year:
                limited = await self._refresh_year(now, today)
            elif self._history_due or self._next_recent is None or now >= self._next_recent:
                self._history_due = False  # a check-in's one try, even if it fails
                await self._fetch_recent(today, RECENT_HISTORY_DAYS)
                self._next_recent = now + RECENT_HISTORY_MAX_AGE
                recent = True
            else:
                return
        except PlanetFitnessAuthError:
            raise
        except PlanetFitnessError as err:
            if isinstance(err, PlanetFitnessRateLimitError):
                self._history_rate_limited(now, err, turn)
                raise
            if not side_trip:  # a side trip leaves the backoff as it was
                wait = self._outage(HISTORY).failed(HISTORY, err, RECENT_HISTORY_MAX_AGE)
                self._next_history_try = now + wait
                self._recent_failing = True
            return
        if not side_trip:  # which leaves the backoff and the log to the fetch that failed
            self._next_history_try = None
            self._recent_failing = self._recent_failing and not recent
            if self._next_backfill is None and not self._year_failing and not self._recent_failing:
                self._outage(HISTORY).ok(HISTORY)  # nothing still failing
        if self._reload_all:
            self._start_reload(now)
        if limited:
            raise limited

    def _history_rate_limited(
        self, now: datetime, err: PlanetFitnessRateLimitError, turn: tuple[bool, datetime | None, bool]
    ) -> None:
        """A 429 on the first load, the last 35 days or a side trip (or any history
        request held back by another's pause)."""
        due, side_trip_for, loaded = turn
        if not err.sent:  # another request's pause: a check-in's try waits for it
            # unless the first load, which has the last 35 days, just came in
            self._history_due = due and (loaded or not self._history_loaded)
            self._side_trip_for = side_trip_for
        elif self._history_loaded or self._first_load_limited:
            # an hour, or as asked, with no check-in try in it
            self._next_history_try = self._side_trip_for = _after_429(now, err)
        else:  # the first load's first 429: again when the pause ends
            self._first_load_limited = True

    async def _refresh_backfill(self, now: datetime, today: date) -> PlanetFitnessRateLimitError | None:
        """Every year back to the join date. The first load's failure is history's
        own (it raises); after it, older years retry on their own backoff. Returns
        a rate limit, once scheduled, to pass on."""
        try:
            await self._backfill(today, now)
        except PlanetFitnessAuthError:
            raise
        except PlanetFitnessError as err:
            if not self._history_loaded or (isinstance(err, PlanetFitnessRateLimitError) and not err.sent):
                raise
            if isinstance(err, PlanetFitnessRateLimitError):
                self._next_backfill = now + max(self._backfill_backoff.next(YEAR_HISTORY_MAX_AGE), retry_after(err))
                self._history_429_until = max(self._history_429_until or now, self._next_backfill)
                return err
            # The newest year is in; try the older ones again later.
            self._outage(HISTORY).failed(HISTORY, err, YEAR_HISTORY_MAX_AGE)
            self._next_backfill = now + self._backfill_backoff.next(YEAR_HISTORY_MAX_AGE)
            return None
        self._next_backfill = None
        self._backfill_backoff.failures = 0
        return None

    async def _refresh_year(self, now: datetime, today: date) -> PlanetFitnessRateLimitError | None:
        """The last year, daily (hourly while history is empty). A failure retries it
        on its own backoff, so the last 35 days keep their hourly schedule. Returns a
        rate limit, once scheduled, to pass on."""
        if self._check_ins:
            self._future_end_ok = True  # re-check once a day
        try:
            await self._fetch_recent(today, HISTORY_WINDOW_DAYS)
        except PlanetFitnessAuthError:
            raise
        except PlanetFitnessError as err:
            if isinstance(err, PlanetFitnessRateLimitError):
                if not err.sent:
                    raise  # another request's pause: due again after it
                self._next_year = now + max(self._year_backoff.next(self._year_max_age()), retry_after(err))
                self._history_429_until = max(self._history_429_until or now, self._next_year)
                return err
            self._outage(HISTORY).failed(HISTORY, err, self._year_max_age())
            self._next_year = now + self._year_backoff.next(self._year_max_age())
            self._year_failing = True
            return None
        self._next_year = now + self._year_max_age()
        if not self._recent_failing:  # else that retries and ends its own failure
            self._next_recent = now + RECENT_HISTORY_MAX_AGE
        self._year_failing = False
        self._year_backoff.failures = 0
        return None

    def _start_reload(self, now: datetime) -> None:
        """History loaded empty (Planet Fitness sometimes returns nothing for a while)
        and now has visits: load every year again, newest first."""
        self._reload_all = False
        self._all_loaded = False
        self._backfill_end = None
        # A 429 wait the year or older years still owe holds the reload too.
        until = self._history_429_until
        self._next_backfill = until if until is not None and until > now else None
        self._next_year = now + self._year_max_age()  # the reload covers it
        self._year_failing = False
        self._year_backoff.failures = 0
        self._backfill_backoff.failures = 0

    def _year_max_age(self) -> timedelta:
        """Reload the last year daily, or hourly while no visits have come back."""
        return YEAR_HISTORY_MAX_AGE if self._check_ins else RECENT_HISTORY_MAX_AGE

    def _end(self, today: date) -> date:
        """Tomorrow, so today's visits are in range, unless the API rejects that."""
        return today + timedelta(days=1) if self._future_end_ok else today

    def _merge(self, items: list[CheckIn], start: date, end: date, today: date) -> None:
        """Add visits to the cache, and the new ones to `_unannounced`. New means no
        more than 2 days old and, on the first load, since startup; after it, also one
        that began up to a visit length before startup (under way, and showed up late)."""
        tz = self.tz_getter()
        started = self._started.astimezone(tz).replace(tzinfo=None)
        if self._history_loaded:
            started -= timedelta(minutes=self.config_entry.options.get(CONF_VISIT_MINUTES, DEFAULT_VISIT_MINUTES))
        cutoff = max(dt_util.now(tz).replace(tzinfo=None) - NEW_CHECK_IN_MAX_AGE, started)
        fresh = {c.key: c for c in items if c.key not in self._check_ins and c.local_time >= cutoff}
        self._unannounced += fresh.values()
        if items and not self._check_ins and self._history_loaded:
            # The first visits since history loaded empty, so the empty years may
            # have been a gap in Planet Fitness's data.
            self._reload_all = True
            # What loaded empty may have been the gap: only this range is loaded,
            # and only if it reaches today.
            self._loaded_from = start if end >= today else None
        self._check_ins.update((c.key, c) for c in items)

    async def _fetch_recent(self, today: date, days: int) -> None:
        end = self._end(today)
        start = end - timedelta(days=days - 1)
        items = await self._fetch(start, end, today)
        self._merge(items, start, end, today)
        # Everything from start through today is in (even while a reload of the newest
        # year keeps failing), unless it came back empty where visits are known: a gap.
        if items or not any(start <= c.local_time.date() <= end for c in self._check_ins.values()):
            self._loaded_from = min(start, self._loaded_from) if self._loaded_from else start
        self._history_due = False

    async def _backfill(self, today: date, now: datetime) -> None:
        """365-day windows, newest first, back to the membership start (or the first
        empty year). Each window is kept as it loads, so a failure resumes there."""
        since = self._membership.since if self._membership else None
        window_end = self._backfill_end or self._end(today)
        if since is not None:
            # A start date in the future (a presale?) or before Planet Fitness opened
            # would make a window that ends before it starts, or no end.
            since = min(max(since, EARLIEST_START), window_end)
        for _ in range(HISTORY_MAX_WINDOWS):
            window_start = window_end - timedelta(days=HISTORY_WINDOW_DAYS - 1)
            if since is not None and window_start < since:
                window_start = since
            items = await self._fetch(window_start, window_end, today)
            if not items and any(window_start <= c.local_time.date() <= window_end for c in self._check_ins.values()):
                # Visits are known in this range, so it's a gap in Planet Fitness's data.
                raise PlanetFitnessError("history came back empty for a range with visits")
            self._merge(items, window_start, window_end, today)
            if not self._reload_all:
                self._loaded_from = min(window_start, self._loaded_from or window_start)
            if not self._history_loaded:  # the newest year is in
                self._history_loaded = True
                self._recent_failing = False  # it has the last 35 days
                self._history_due = False
                self._next_year = now + self._year_max_age()
                self._next_recent = now + RECENT_HISTORY_MAX_AGE
            window_end = window_start - timedelta(days=1)
            self._backfill_end = window_end
            if self._reload_all:
                break  # newer years came back empty; start again from the newest
            if since is not None and window_start <= since:
                self._backfill_end = None  # reached the start
                break
            if since is None and not items:
                break
        self._all_loaded = True

    async def _fetch(self, start: date, end: date, today: date) -> list[CheckIn]:
        try:
            return await self.client.async_get_check_ins(start, end)
        except PlanetFitnessError as err:
            if err.status != 400 or end <= today:
                raise
            # Maybe the API rejects future end dates. Only believe it if today works.
            items = await self.client.async_get_check_ins(min(start, today), today)
            self._future_end_ok = False
            _LOGGER.debug("History rejected a future end date; using today until tomorrow")
            return items


# --------------------------------------------------------------------------- #
# Runtime data
# --------------------------------------------------------------------------- #


@dataclass
class PlanetFitnessRuntimeData:
    client: PlanetFitnessClient
    home_club_id: str
    clubs: dict[str, ClubCoordinator]
    account: AccountCoordinator
    club_subentry_ids: frozenset[str]
    # One per platform with club entities; each adds a club's entities.
    club_entity_adders: list[Callable[[ClubCoordinator], None]] = field(default_factory=list)
