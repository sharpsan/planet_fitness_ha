"""API client and sign-in flow against mocked HTTP."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
import email.utils
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import pytest
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker, AiohttpClientMockResponse

from custom_components.planet_fitness.api import (
    EmailCodeLogin,
    LoginError,
    PlanetFitnessAuthError,
    PlanetFitnessClient,
    PlanetFitnessError,
    PlanetFitnessRateLimitError,
    auth,
)
from custom_components.planet_fitness.api.auth import parse_retry_after
from custom_components.planet_fitness.api.const import API_BASE, AUTH_DOMAIN, REDIRECT_URI

from .conftest import HOME_ID, fake_jwt, load, make_token

TOKEN_URL = f"{AUTH_DOMAIN}/oauth/token"
CLUB_URL = f"{API_BASE}clubs/pfx%3Aclubs%3A00000000-0000-4000-8000-000000000001"


def client_for(
    hass: HomeAssistant, token: dict[str, Any] | None = None, saved: list | None = None
) -> PlanetFitnessClient:
    return PlanetFitnessClient(
        async_get_clientsession(hass), token or make_token(), saved.append if saved is not None else None
    )


async def test_get_club_parses_and_sends_app_headers(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CLUB_URL, json=load("home_club"))
    club = await client_for(hass).async_get_club(HOME_ID)
    assert club.club_id == HOME_ID
    assert club.occupancy_percentage == 23
    assert club.timezone == "America/Chicago"
    assert len(club.hours) == 7 and club.hours[0].closes == 1440
    assert club.holidays[1].dates[1].closed
    _, url, _, headers = aioclient_mock.mock_calls[0]
    assert url.query["includeOccupancy"] == "true"
    assert headers["Authorization"].startswith("Bearer ")
    assert headers["User-Agent"] == "pfx-mobile" and headers["Accept-Version"] == "25"


async def test_expired_token_is_refreshed_and_saved(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN_URL, json={"access_token": fake_jwt(), "refresh_token": "refresh-2", "expires_in": 86400})
    aioclient_mock.get(CLUB_URL, json=load("home_club"))
    saved: list[dict[str, Any]] = []
    client = client_for(hass, {**make_token(), "expires_at": time.time() - 10}, saved)
    await client.async_get_club(HOME_ID)
    assert saved and saved[0]["refresh_token"] == "refresh-2"
    form = aioclient_mock.mock_calls[0][2]
    assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "refresh-1"


async def test_401_refreshes_once_then_retries(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CLUB_URL, status=401)
    aioclient_mock.post(TOKEN_URL, json={"access_token": fake_jwt(), "expires_in": 86400})
    with pytest.raises(PlanetFitnessAuthError):
        await client_for(hass).async_get_club(HOME_ID)
    assert [c[0] for c in aioclient_mock.mock_calls] == ["GET", "POST", "GET"]


async def test_refresh_rejected_is_auth_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN_URL, status=403, json={"error": "invalid_grant"})
    with pytest.raises(PlanetFitnessAuthError):
        await client_for(hass, {**make_token(), "expires_at": 0}).async_get_club(HOME_ID)


async def test_rate_limit_and_server_errors(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CLUB_URL, status=429, headers={"Retry-After": "120"})
    with pytest.raises(PlanetFitnessRateLimitError) as err:
        await client_for(hass).async_get_club(HOME_ID)
    assert err.value.retry_after == 120
    aioclient_mock.clear_requests()
    aioclient_mock.get(CLUB_URL, status=503)
    with pytest.raises(PlanetFitnessError) as err2:
        await client_for(hass).async_get_club(HOME_ID)
    assert err2.value.status == 503


async def test_malformed_response_is_an_api_error(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CLUB_URL, json={"name": "no id"})
    with pytest.raises(PlanetFitnessError, match="Unexpected club response"):
        await client_for(hass).async_get_club(HOME_ID)


async def test_already_replaced_token_is_not_refreshed_again(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    client = client_for(hass)
    assert await client._access_token(rejected="an-older-token") == client.token["access_token"]
    assert aioclient_mock.call_count == 0


async def test_current_checkin_of_any_shape_counts(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_BASE}current-checkin", json=[{"club": "x"}])
    assert await client_for(hass).async_get_current_checkin() == [{"club": "x"}]


async def test_rate_limit_pauses_every_call(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CLUB_URL, status=429, headers={"Retry-After": "120"})
    aioclient_mock.get(f"{API_BASE}current-checkin", text="null")
    client = client_for(hass)
    with pytest.raises(PlanetFitnessRateLimitError):
        await client.async_get_club(HOME_ID)
    with pytest.raises(PlanetFitnessRateLimitError) as err:
        await client.async_get_current_checkin()
    assert 0 < (err.value.retry_after or 0) <= 120
    assert aioclient_mock.call_count == 1  # the second call never left


async def test_a_rate_limit_is_reported(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(CLUB_URL, status=429, headers={"Retry-After": "120"})
    paused: list[float] = []
    client = PlanetFitnessClient(async_get_clientsession(hass), make_token(), on_pause=paused.append)
    with pytest.raises(PlanetFitnessRateLimitError):
        await client.async_get_club(HOME_ID)
    assert paused == [120]


async def test_rate_limited_refresh_pauses_every_call(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(TOKEN_URL, status=429, headers={"Retry-After": "300"})
    paused: list[float] = []
    client = PlanetFitnessClient(
        async_get_clientsession(hass), {**make_token(), "expires_at": 0}, on_pause=paused.append
    )
    with pytest.raises(PlanetFitnessRateLimitError):
        await client.async_get_club(HOME_ID)
    with pytest.raises(PlanetFitnessRateLimitError):
        await client.async_get_club(HOME_ID)
    assert aioclient_mock.call_count == 1  # the refresh wasn't retried at once
    assert paused == [300]  # as long as asked


async def test_calls_waiting_on_a_rate_limited_token_refresh_dont_send(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    paused: list[float] = []
    client = PlanetFitnessClient(
        async_get_clientsession(hass), {**make_token(), "expires_at": 0}, on_pause=paused.append
    )

    async def slow_429(method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        for _ in range(5):
            await asyncio.sleep(0)  # the others queue up on the refresh meanwhile
        return AiohttpClientMockResponse(method, url, status=429, headers={"Retry-After": "300"})

    aioclient_mock.post(TOKEN_URL, side_effect=slow_429)
    results = await asyncio.gather(
        client.async_get_club(HOME_ID),
        client.async_get_current_checkin(),
        client.async_get_crowd_profile(HOME_ID),
        return_exceptions=True,
    )
    assert all(isinstance(r, PlanetFitnessRateLimitError) for r in results)
    assert aioclient_mock.call_count == 1  # one refresh; the others waited and didn't send
    assert paused == [300]


async def test_a_shorter_rate_limit_doesnt_shorten_a_pause(hass: HomeAssistant) -> None:
    paused: list[float] = []
    client = PlanetFitnessClient(async_get_clientsession(hass), make_token(), on_pause=paused.append)
    client._pause(PlanetFitnessRateLimitError(300))
    client._pause(PlanetFitnessRateLimitError(30))  # an overlapping request's 429
    with pytest.raises(PlanetFitnessRateLimitError) as err:
        client._check_paused()
    assert (err.value.retry_after or 0) > 290
    assert not err.value.sent  # held back, not answered
    later = PlanetFitnessRateLimitError(30)
    client._pause(later)
    assert (later.retry_after or 0) > 290  # carries the pause in force
    assert paused[-1] > 290


def test_retry_after_in_seconds_or_as_a_date() -> None:
    assert parse_retry_after("120") == 120
    later = email.utils.format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 115 < (parse_retry_after(later) or 0) <= 120
    assert parse_retry_after("soon") is None
    assert parse_retry_after("nan") is None  # the default pause instead
    for huge in ("inf", "1e12", "90000"):
        assert parse_retry_after(huge) == 86400  # a day at most
    assert parse_retry_after(None) is None


async def test_current_checkin_null(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_BASE}current-checkin", text="null")
    assert await client_for(hass).async_get_current_checkin() is None


async def test_history_requests_check_ins_and_parses_local_time(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{API_BASE}history", json=load("history"))
    visits = await client_for(hass).async_get_check_ins(date(2025, 11, 8), date(2026, 11, 8))
    body = aioclient_mock.mock_calls[0][2]
    assert body["includeCheckIn"] is True and body["pageNumber"] == 1 and body["pageSize"] == 500
    assert len(visits) == 8  # the activity item is skipped
    assert visits[0].local_time == datetime(2026, 11, 5, 6, 30)  # "Z" ignored: it's local time
    assert visits[0].club_name == "Example Central"


@pytest.mark.parametrize("body", [{"history": None}, {}, None])
async def test_malformed_history_is_an_error_not_no_visits(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, body: object
) -> None:
    if body is None:
        aioclient_mock.post(f"{API_BASE}history", text="")
    else:
        aioclient_mock.post(f"{API_BASE}history", json=body)
    with pytest.raises(PlanetFitnessError):
        await client_for(hass).async_get_check_ins(date(2026, 1, 1), date(2026, 11, 8))


async def test_club_search_distance_in_meters(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{API_BASE}clubs", json=load("club_search"))
    results = await client_for(hass).async_search_clubs(39.0, -94.5)
    assert [round((r.distance_m or 0) / 1609.344, 1) for r in results] == [0.0, 2.1, 8.0]


# ---- sign-in page ----------------------------------------------------------

IDENTIFIER = f"{AUTH_DOMAIN}/u/login/identifier"
CHALLENGE = f"{AUTH_DOMAIN}/u/login/passwordless-email-challenge"
RESUME = f"{AUTH_DOMAIN}/authorize/resume"

EMAIL_PAGE = """<html><body><form method="POST">
<input type="hidden" name="state" value="S1"><input type="text" name="username" value="">
<input type="password" value=""><input type="hidden" name="js-available" value="false">
<button type="submit" name="action" value="default">Continue</button></form></body></html>"""
CODE_PAGE = """<html><body>
<form method="POST"><input type="hidden" name="state" value="S1"><input type="text" name="code" value="">
<button type="submit" name="action" value="default">Continue</button></form>
<form method="POST"><input type="hidden" name="state" value="S1">
<button type="submit" name="action" value="resend">Resend</button></form></body></html>"""


def mock_login_pages(aioclient_mock: AiohttpClientMocker, login: EmailCodeLogin, code_ok: bool = True) -> None:
    aioclient_mock.get(f"{AUTH_DOMAIN}/authorize", status=302, headers={"Location": "/u/login/identifier?state=S1"})
    aioclient_mock.get(IDENTIFIER, text=EMAIL_PAGE)
    aioclient_mock.post(IDENTIFIER, status=302, headers={"Location": "/u/login/passwordless-email-challenge?state=S1"})
    aioclient_mock.get(CHALLENGE, text=CODE_PAGE)
    if code_ok:
        aioclient_mock.post(CHALLENGE, status=302, headers={"Location": "/authorize/resume?state=S1"})
        callback = f"{REDIRECT_URI}?code=AUTHCODE&state={login._state}"
        aioclient_mock.get(RESUME, status=302, headers={"Location": callback})
    else:
        aioclient_mock.post(CHALLENGE, status=400, text=CODE_PAGE)
    aioclient_mock.post(TOKEN_URL, json={"access_token": fake_jwt(), "refresh_token": "r", "expires_in": 86400})


async def test_email_code_login(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    login = EmailCodeLogin(async_get_clientsession(hass))
    mock_login_pages(aioclient_mock, login)
    await login.async_request_code("member@example.com")
    token = await login.async_submit_code(" 123 456 ")
    assert token["refresh_token"] == "r"
    calls = [(c[0], c[1].path) for c in aioclient_mock.mock_calls]
    assert calls == [
        ("GET", "/authorize"),
        ("GET", "/u/login/identifier"),
        ("POST", "/u/login/identifier"),
        ("GET", "/u/login/passwordless-email-challenge"),
        ("POST", "/u/login/passwordless-email-challenge"),
        ("GET", "/authorize/resume"),
        ("POST", "/oauth/token"),
    ]
    assert aioclient_mock.mock_calls[2][2] == {
        "state": "S1",
        "username": "member@example.com",
        "js-available": "false",
        "action": "default",
    }
    assert aioclient_mock.mock_calls[4][2] == {"state": "S1", "code": "123456", "action": "default"}
    exchange = aioclient_mock.mock_calls[6][2]
    assert exchange["grant_type"] == "authorization_code" and exchange["code"] == "AUTHCODE"
    assert exchange["redirect_uri"] == REDIRECT_URI and exchange["code_verifier"] == login._verifier


async def test_wrong_code(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    login = EmailCodeLogin(async_get_clientsession(hass))
    mock_login_pages(aioclient_mock, login, code_ok=False)
    await login.async_request_code("member@example.com")
    with pytest.raises(LoginError) as err:
        await login.async_submit_code("000000")
    assert err.value.reason == "invalid_code"


async def test_rejected_email(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    login = EmailCodeLogin(async_get_clientsession(hass))
    aioclient_mock.get(f"{AUTH_DOMAIN}/authorize", status=302, headers={"Location": "/u/login/identifier?state=S1"})
    aioclient_mock.get(IDENTIFIER, text=EMAIL_PAGE)
    aioclient_mock.post(IDENTIFIER, status=400, text=EMAIL_PAGE)
    with pytest.raises(LoginError) as err:
        await login.async_request_code("not-an-email")
    assert err.value.reason == "invalid_email"


async def test_changed_sign_in_page(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    login = EmailCodeLogin(async_get_clientsession(hass))
    aioclient_mock.get(f"{AUTH_DOMAIN}/authorize", text="<html>something new</html>")
    with pytest.raises(LoginError) as err:
        await login.async_request_code("member@example.com")
    assert err.value.reason == "unexpected_page"


def test_form_parser_submits_like_a_browser() -> None:
    page = """<form method="POST"><input type="hidden" name="state" value="S1"><input name="code">
    <input type="checkbox" name="remember"><input type="checkbox" name="agree" checked>
    <input name="old" disabled><button type="submit" name="action" value="default">Go</button></form>
    <input type="hidden" name="stray" value="x">"""
    form = auth._find_form(page, "code")
    assert form is not None
    assert form.field_names == {"state", "code", "agree"}
