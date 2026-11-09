"""Shared fixtures. All data is synthetic (tests/fixtures), never real API responses."""

from __future__ import annotations

import base64
from collections.abc import Generator
from datetime import date
import json
from pathlib import Path
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.planet_fitness.api import (
    Balance,
    CheckIn,
    Club,
    ClubSummary,
    CrowdProfile,
    FeeSchedule,
    MembershipInfo,
)
from custom_components.planet_fitness.const import CONF_TOKEN, DOMAIN

FIXTURES = Path(__file__).parent / "fixtures"
SUBJECT = "email|test-member"
HOME_ID = "pfx:clubs:00000000-0000-4000-8000-000000000001"
OTHER_ID = "pfx:clubs:00000000-0000-4000-8000-000000000002"


def load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def fake_jwt(sub: str = SUBJECT) -> str:
    def enc(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{enc({'alg': 'none'})}.{enc({'sub': sub, 'aud': ['https://*.api.planetfitness.com']})}.sig"


def make_token(sub: str = SUBJECT, expires_in: int = 86400) -> dict[str, Any]:
    return {"access_token": fake_jwt(sub), "refresh_token": "refresh-1", "expires_at": time.time() + expires_in}


def check_ins() -> list[CheckIn]:
    items = [CheckIn.from_api(i) for i in load("history")["history"] if i["payloadType"] == "CHECK_IN_HISTORY_PAYLOAD"]
    return [c for c in items if c is not None]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Load custom_components/ in every test."""


@pytest.fixture(autouse=True)
def all_entities_enabled(request: pytest.FixtureRequest) -> Generator[None]:
    """Every entity on, so every endpoint is called and every entity is checked.

    Mark a test `entity_defaults` to get what a real install gets instead.
    """
    if request.node.get_closest_marker("entity_defaults"):
        yield
        return
    with patch(
        "homeassistant.helpers.entity.Entity.entity_registry_enabled_default",
        new_callable=PropertyMock,
        return_value=True,
    ):
        yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, title="Planet Fitness", unique_id=SUBJECT, data={CONF_TOKEN: make_token()})


def build_client_mock(subject: str = SUBJECT) -> MagicMock:
    client = MagicMock()
    client.subject = subject
    client.token = make_token(subject)
    client.async_get_home_club = AsyncMock(return_value=Club.from_api(load("home_club")))
    other = Club.from_api(load("club_other"))
    home = Club.from_api(load("home_club"))
    client.async_get_club = AsyncMock(side_effect=lambda club_id: home if club_id == HOME_ID else other)
    client.async_get_crowd_profile = AsyncMock(return_value=CrowdProfile.from_api(load("club_capacity")))
    client.async_search_clubs = AsyncMock(return_value=[ClubSummary.from_api(c) for c in load("club_search")["clubs"]])
    client.async_get_current_checkin = AsyncMock(return_value=None)

    async def _check_ins(start: date, end: date) -> list[CheckIn]:
        return [c for c in check_ins() if start <= c.local_time.date() <= end]

    client.async_get_check_ins = AsyncMock(side_effect=_check_ins)
    client.async_get_fee_schedule = AsyncMock(return_value=FeeSchedule.from_api(load("fee_schedule")))
    client.async_get_balance = AsyncMock(return_value=Balance.from_api(load("balance")))
    client.async_get_membership_info = AsyncMock(return_value=MembershipInfo.from_api(load("memberships_info")))
    return client


@pytest.fixture
def mock_client() -> Generator[MagicMock]:
    """Patch the API client used by setup."""
    client = build_client_mock()
    with patch("custom_components.planet_fitness.PlanetFitnessClient", return_value=client):
        yield client
