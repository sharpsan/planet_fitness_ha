"""Config flow: email -> code sign-in, reauth, options, add-club subentry."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER, ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.planet_fitness.api import LoginError, PlanetFitnessAuthError, PlanetFitnessConnectionError
from custom_components.planet_fitness.const import (
    CONF_BUSIER_FACTOR,
    CONF_CLUB_ID,
    CONF_HIGH_THRESHOLD,
    CONF_LOW_THRESHOLD,
    CONF_TOKEN,
    CONF_VISIT_MINUTES,
    CONF_ZIP_CODE,
    DOMAIN,
    SUBENTRY_CLUB,
)

from .conftest import OTHER_ID, SUBJECT, build_client_mock, make_token


@pytest.fixture
def mock_login() -> Generator[MagicMock]:
    login = MagicMock()
    login.async_request_code = AsyncMock()
    login.async_submit_code = AsyncMock(return_value=make_token())
    with patch("custom_components.planet_fitness.config_flow.EmailCodeLogin", return_value=login):
        yield login


@pytest.fixture
def flow_client() -> Generator[MagicMock]:
    client = build_client_mock()
    with patch("custom_components.planet_fitness.config_flow.PlanetFitnessClient", return_value=client):
        yield client


async def test_sign_in(
    hass: HomeAssistant, mock_login: MagicMock, flow_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": " member@example.com "})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "code"
    assert result["description_placeholders"] == {"email": "member@example.com"}
    mock_login.async_request_code.assert_awaited_once_with("member@example.com")

    with patch("custom_components.planet_fitness.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Planet Fitness"
    assert result["result"].unique_id == SUBJECT
    assert set(result["data"][CONF_TOKEN]) == {"access_token", "refresh_token", "expires_at"}
    assert "email" not in result["data"]
    assert "closes the Home Assistant aiohttp session" not in caplog.text


async def test_bad_email_then_bad_code(hass: HomeAssistant, mock_login: MagicMock, flow_client: MagicMock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    mock_login.async_request_code.side_effect = LoginError("invalid_email")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "nope"})
    assert result["errors"] == {"base": "invalid_email"}

    mock_login.async_request_code.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    mock_login.async_submit_code.side_effect = LoginError("invalid_code")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "000000"})
    assert result["step_id"] == "code" and result["errors"] == {"base": "invalid_code"}

    mock_login.async_submit_code.side_effect = None
    with patch("custom_components.planet_fitness.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_cannot_connect(hass: HomeAssistant, mock_login: MagicMock) -> None:
    mock_login.async_request_code.side_effect = PlanetFitnessConnectionError("down")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_already_configured(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_login: MagicMock, flow_client: MagicMock
) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_reauth(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_login: MagicMock, flow_client: MagicMock
) -> None:
    config_entry.add_to_hass(hass)
    new_token = make_token()
    new_token["refresh_token"] = "refresh-new"
    flow_client.token = new_token
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    with patch("custom_components.planet_fitness.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN]["refresh_token"] == "refresh-new"


@pytest.mark.usefixtures("mock_login")
async def test_reauth_on_a_running_entry_reloads_it_with_the_new_sign_in(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_client: MagicMock,
    flow_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    setups = mock_client.async_get_home_club.await_count
    new_token = make_token()
    new_token["refresh_token"] = "refresh-new"
    flow_client.token = new_token

    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_TOKEN]["refresh_token"] == "refresh-new"
    assert mock_client.async_get_home_club.await_count > setups  # reloaded with it
    assert "Detected that custom integration" not in caplog.text


async def test_reauth_wrong_account(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_login: MagicMock, flow_client: MagicMock
) -> None:
    config_entry.add_to_hass(hass)
    flow_client.subject = "email|someone-else"
    result = await config_entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "other@example.com"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_account"


async def test_options(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    bad = {CONF_LOW_THRESHOLD: 60, CONF_HIGH_THRESHOLD: 40, CONF_BUSIER_FACTOR: 1.5, CONF_VISIT_MINUTES: 75}
    result = await hass.config_entries.options.async_configure(result["flow_id"], bad)
    assert result["errors"] == {CONF_HIGH_THRESHOLD: "high_not_above_low"}
    good = {**bad, CONF_LOW_THRESHOLD: 20}
    result = await hass.config_entries.options.async_configure(result["flow_id"], good)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == good


async def test_add_club_subentry(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_CLUB), context={"source": SOURCE_USER}
    )
    assert result["step_id"] == "user"
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"location": {"latitude": 39.05, "longitude": -94.51}}
    )
    assert result["step_id"] == "pick"
    mock_client.async_search_clubs.assert_awaited_once_with(39.05, -94.51)
    labels = [o["label"] for o in result["data_schema"].schema[CONF_CLUB_ID].config["options"]]
    assert labels == [
        "Example North - Northtown, MO (2.1 mi)",
        "Example South - Southville, MO (8.0 mi)",
    ]  # home club hidden

    result = await hass.config_entries.subentries.async_configure(result["flow_id"], {CONF_CLUB_ID: OTHER_ID})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    assert hass.states.get("sensor.example_north_crowd_level").state == "41"
    entity_entry = er.async_get(hass).async_get("sensor.example_north_crowd_level")
    assert entity_entry.config_subentry_id == next(iter(config_entry.subentries))
    device = dr.async_get(hass).async_get(entity_entry.device_id)
    assert device.configuration_url == "https://www.planetfitness.com/gyms/example-north-mo"  # without a reload


async def test_subentry_created_at_setup(hass: HomeAssistant, mock_client: MagicMock) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SUBJECT,
        data={CONF_TOKEN: make_token()},
        subentries_data=[
            ConfigSubentryData(
                data={CONF_CLUB_ID: OTHER_ID}, subentry_type=SUBENTRY_CLUB, title="Example North", unique_id=OTHER_ID
            )
        ],
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.example_north_open") is not None


async def test_code_retry_reuses_the_sign_in(
    hass: HomeAssistant, mock_login: MagicMock, flow_client: MagicMock
) -> None:
    home = flow_client.async_get_home_club.return_value
    flow_client.async_get_home_club.side_effect = [PlanetFitnessConnectionError("down"), home]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["errors"] == {"base": "cannot_connect"}
    with patch("custom_components.planet_fitness.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_login.async_submit_code.await_count == 1  # a code works once


async def test_add_club_needs_a_loaded_entry(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_CLUB), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "not_loaded"


async def test_add_club_errors(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    location = {"location": {"latitude": 39.0, "longitude": -94.5}}

    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_CLUB), context={"source": SOURCE_USER}
    )
    mock_client.async_search_clubs.side_effect = PlanetFitnessConnectionError("down")
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], location)
    assert result["errors"] == {"base": "cannot_connect"}

    home_only = [c for c in (await build_client_mock().async_search_clubs()) if c.name == "Example Central"]
    mock_client.async_search_clubs.side_effect = None
    mock_client.async_search_clubs.return_value = home_only
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], location)
    assert result["errors"] == {"base": "no_clubs"}  # the home club isn't offered


async def test_add_club_by_zip_code(hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_CLUB), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ZIP_CODE: "64105-1234", "location": {"latitude": 1.0, "longitude": 2.0}}
    )
    assert result["step_id"] == "pick"
    mock_client.async_search_clubs.assert_awaited_once_with(39.11, -94.59)  # the ZIP wins over the pin


@pytest.mark.parametrize(("zip_code", "error"), [("6410", "invalid_zip"), ("00000", "unknown_zip")])
async def test_add_club_zip_code_errors(
    hass: HomeAssistant, config_entry: MockConfigEntry, mock_client: MagicMock, zip_code: str, error: str
) -> None:
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.subentries.async_init(
        (config_entry.entry_id, SUBENTRY_CLUB), context={"source": SOURCE_USER}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {CONF_ZIP_CODE: zip_code, "location": {"latitude": 39.0, "longitude": -94.5}}
    )
    assert result["errors"] == {CONF_ZIP_CODE: error}
    mock_client.async_search_clubs.assert_not_awaited()
    zip_key = next(k for k in result["data_schema"].schema if k == CONF_ZIP_CODE)
    assert zip_key.description["suggested_value"] == zip_code  # kept for fixing


async def test_rejected_sign_in_starts_over_at_email(
    hass: HomeAssistant, mock_login: MagicMock, flow_client: MagicMock
) -> None:
    home = flow_client.async_get_home_club.return_value
    flow_client.async_get_home_club.side_effect = [PlanetFitnessAuthError("rejected"), home]
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "123456"})
    assert result["step_id"] == "user" and result["errors"] == {"base": "invalid_auth"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"email": "member@example.com"})
    assert result["step_id"] == "code"
    with patch("custom_components.planet_fitness.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"code": "654321"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_login.async_submit_code.await_count == 2  # the new code was used
