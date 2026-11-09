"""Async client for the Planet Fitness mobile API (read-only endpoints)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date
import json
import math
import time
from typing import Any
from urllib.parse import quote

import aiohttp

from .auth import jwt_claims, parse_retry_after, post_token, token_from_response
from .const import (
    API_BASE,
    API_HEADERS,
    CLIENT_ID,
    HISTORY_MAX_PAGES,
    HISTORY_PAGE_SIZE,
    RATE_LIMIT_PAUSE,
    REFRESH_SCOPE,
    REQUEST_TIMEOUT,
    TOKEN_REFRESH_MARGIN,
)
from .exceptions import (
    PlanetFitnessAuthError,
    PlanetFitnessConnectionError,
    PlanetFitnessError,
    PlanetFitnessRateLimitError,
)
from .models import Balance, CheckIn, Club, ClubSummary, CrowdProfile, FeeSchedule, MembershipInfo


class PlanetFitnessClient:
    """Talks to api.planetfitness.com with a refreshing bearer token.

    Only read endpoints are implemented. Never add report/checkins (it emails the
    member) or any endpoint that writes.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        token: dict[str, Any],
        on_token_update: Callable[[dict[str, Any]], None] | None = None,
        on_pause: Callable[[float], None] | None = None,
    ) -> None:
        self._session = session
        self._token = dict(token)
        self._on_token_update = on_token_update
        self._on_pause = on_pause  # told the seconds of each pause
        self._lock = asyncio.Lock()
        self._paused_until = 0.0  # after a 429, nothing is sent until then

    @property
    def token(self) -> dict[str, Any]:
        return dict(self._token)

    @property
    def subject(self) -> str | None:
        """The Auth0 user id (`sub`) of the signed-in member."""
        return jwt_claims(self._token.get("access_token", "")).get("sub")

    def _check_paused(self) -> None:
        if (remaining := self._paused_until - time.time()) > 0:
            raise PlanetFitnessRateLimitError(math.ceil(remaining), sent=False)

    def _pause(self, err: PlanetFitnessRateLimitError) -> None:
        wait = err.retry_after or RATE_LIMIT_PAUSE
        self._paused_until = max(self._paused_until, time.time() + wait)  # never shortened
        err.retry_after = math.ceil(self._paused_until - time.time())  # the pause in force
        if self._on_pause:
            self._on_pause(err.retry_after)

    async def _access_token(self, rejected: str | None = None) -> str:
        """A valid access token. Pass the token the API just rejected to force a
        refresh, unless another request has already replaced it."""
        async with self._lock:
            self._check_paused()  # a refresh this waited on may have been rate limited
            current = self._token["access_token"]
            fresh = self._token.get("expires_at", 0) - TOKEN_REFRESH_MARGIN > time.time()
            if fresh and current != rejected:
                return current
            refresh_token = self._token.get("refresh_token")
            if not refresh_token:
                raise PlanetFitnessAuthError("No refresh token; sign in again")
            try:
                body = await post_token(
                    self._session,
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "scope": REFRESH_SCOPE,
                        "client_id": CLIENT_ID,
                    },
                )
            except PlanetFitnessRateLimitError as err:
                self._pause(err)
                raise
            self._token = token_from_response(body, previous=self._token)
            if self._on_token_update:
                self._on_token_update(dict(self._token))
            return self._token["access_token"]

    async def _request(self, method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None) -> Any:
        url = API_BASE + path
        token: str | None = None
        for attempt in (1, 2):
            self._check_paused()
            token = await self._access_token(rejected=token)
            self._check_paused()  # another request may have been rate limited meanwhile
            headers = {**API_HEADERS, "Authorization": f"Bearer {token}"}
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    status = resp.status
                    retry_after = resp.headers.get("Retry-After")
                    text = await resp.text()
            except (aiohttp.ClientError, TimeoutError) as err:
                raise PlanetFitnessConnectionError(f"{method} {path}: {err}") from err
            if status == 401 and attempt == 1:
                continue  # token may have been revoked early; refresh once
            if status == 401:
                raise PlanetFitnessAuthError(f"{method} {path}: HTTP 401", status=401)
            if status == 429:
                err = PlanetFitnessRateLimitError(parse_retry_after(retry_after))
                self._pause(err)
                raise err
            if status >= 400:
                raise PlanetFitnessError(f"{method} {path}: HTTP {status}", status=status)
            if not text.strip():
                return None
            try:
                return json.loads(text)
            except ValueError as err:
                raise PlanetFitnessError(f"{method} {path}: invalid JSON") from err
        raise AssertionError("unreachable")

    async def _get_dict(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        data = await self._request("GET", path, params=params)
        if not isinstance(data, dict):
            raise PlanetFitnessError(f"GET {path}: unexpected response")
        return data

    async def async_get_home_club(self) -> Club:
        data = await self._get_dict("clubs/home-club", {"includeOccupancy": "true"})
        return _parse("home club", Club.from_api, data)

    async def async_get_club(self, club_id: str) -> Club:
        path = f"clubs/{_quote(club_id)}"
        return _parse("club", Club.from_api, await self._get_dict(path, {"includeOccupancy": "true"}))

    async def async_get_crowd_profile(self, club_id: str) -> CrowdProfile:
        data = await self._get_dict(f"clubs/{_quote(club_id)}/capacity")
        return _parse("crowd profile", CrowdProfile.from_api, data)

    async def async_search_clubs(self, latitude: float, longitude: float, limit: int = 10) -> list[ClubSummary]:
        """Clubs nearest a point."""
        params = {"lat": str(latitude), "long": str(longitude), "offset": "0", "limit": str(limit)}
        data = await self._get_dict("clubs", params)
        clubs = [c for c in data.get("clubs") or [] if isinstance(c, dict) and c.get("pfxClubId")]
        return [_parse("club search", ClubSummary.from_api, c) for c in clubs]

    async def async_get_current_checkin(self) -> Any:
        """The active check-in, or None when not checked in (the API returns null).

        Any other non-empty value counts as checked in.
        """
        data = await self._request("GET", "current-checkin")
        return None if data in (None, "", [], {}) else data

    async def async_get_check_ins(self, start: date, end: date) -> list[CheckIn]:
        """Check-ins between two dates (inclusive), newest first."""
        results: list[CheckIn] = []
        for page in range(1, HISTORY_MAX_PAGES + 1):
            data = await self._request(
                "POST",
                "history",
                body={
                    "startDate": start.isoformat(),
                    "endDate": end.isoformat(),
                    "pageNumber": page,
                    "pageSize": HISTORY_PAGE_SIZE,
                    "includeCheckIn": True,
                },
            )
            items = data.get("history") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise PlanetFitnessError("Unexpected history response")
            for item in items:
                if (
                    isinstance(item, dict)
                    and item.get("payloadType") == "CHECK_IN_HISTORY_PAYLOAD"
                    and (check_in := CheckIn.from_api(item)) is not None
                ):
                    results.append(check_in)
            if len(items) < HISTORY_PAGE_SIZE:
                break
        return sorted(results, key=lambda c: c.local_time, reverse=True)

    async def async_get_fee_schedule(self) -> FeeSchedule:
        return _parse("fee schedule", FeeSchedule.from_api, await self._get_dict("billing/v2/fee-schedule"))

    async def async_get_balance(self) -> Balance:
        return _parse("balance", Balance.from_api, await self._get_dict("billing/v2/balance"))

    async def async_get_membership_info(self) -> MembershipInfo:
        return _parse("membership", MembershipInfo.from_api, await self._get_dict("memberships/info"))


def _quote(value: str) -> str:
    return quote(value, safe="")


def _parse[T](what: str, parse: Callable[[dict[str, Any]], T], data: dict[str, Any]) -> T:
    """Turn a response that doesn't have the expected shape into an API error."""
    try:
        return parse(data)
    except (KeyError, TypeError, ValueError, AttributeError) as err:
        raise PlanetFitnessError(f"Unexpected {what} response: {err!r}") from err
