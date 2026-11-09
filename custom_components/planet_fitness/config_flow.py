"""Config flow: sign in with email + emailed code; options; extra clubs."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_LATITUDE, CONF_LOCATION, CONF_LONGITUDE
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession, async_get_clientsession
from homeassistant.helpers.selector import (
    LocationSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
import voluptuous as vol

from .api import (
    ClubSummary,
    EmailCodeLogin,
    LoginError,
    PlanetFitnessAuthError,
    PlanetFitnessClient,
    PlanetFitnessError,
    PlanetFitnessRateLimitError,
)
from .const import (
    CONF_BUSIER_FACTOR,
    CONF_CLUB_ID,
    CONF_CODE,
    CONF_EMAIL,
    CONF_HIGH_THRESHOLD,
    CONF_LOW_THRESHOLD,
    CONF_TOKEN,
    CONF_VISIT_MINUTES,
    CONF_ZIP_CODE,
    DEFAULT_BUSIER_FACTOR,
    DEFAULT_HIGH_THRESHOLD,
    DEFAULT_LOW_THRESHOLD,
    DEFAULT_VISIT_MINUTES,
    DOMAIN,
    SUBENTRY_CLUB,
)
from .zip_codes import parse_zip, zip_location

_LOGGER = logging.getLogger(__name__)

EMAIL_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): TextSelector(TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="email"))}
)
CODE_SCHEMA = vol.Schema({vol.Required(CONF_CODE): TextSelector(TextSelectorConfig(autocomplete="one-time-code"))})


def _error_key(err: Exception) -> str:
    if isinstance(err, LoginError):
        return err.reason if err.reason in ("invalid_email", "invalid_code") else "unexpected_page"
    if isinstance(err, PlanetFitnessRateLimitError):
        return "rate_limited"
    if isinstance(err, PlanetFitnessAuthError):
        return "invalid_auth"
    return "cannot_connect"


class PlanetFitnessConfigFlow(ConfigFlow, domain=DOMAIN):
    """Sign in the way the app does: email, then the code PF emails you."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._login: EmailCodeLogin | None = None
        self._login_session: aiohttp.ClientSession | None = None
        self._token: dict[str, Any] | None = None  # kept if the API check after sign-in fails

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PlanetFitnessOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(cls, config_entry: ConfigEntry) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_CLUB: ClubSubentryFlow}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_email_step("user", user_input)

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_email_step("reauth_confirm", user_input)

    async def _async_email_step(self, step_id: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._email = user_input[CONF_EMAIL].strip()
            self._detach_login_session()
            # A dedicated session: the sign-in page needs its own cookie jar.
            self._login_session = async_create_clientsession(self.hass, auto_cleanup=False)
            self._login = EmailCodeLogin(self._login_session)
            self._token = None
            try:
                await self._login.async_request_code(self._email)
            except PlanetFitnessError as err:
                _LOGGER.debug("Requesting a sign-in code failed: %s", err)
                errors["base"] = _error_key(err)
            else:
                return await self.async_step_code()
        return self._email_form(step_id, errors)

    def _email_form(self, step_id: str, errors: dict[str, str]) -> ConfigFlowResult:
        schema = self.add_suggested_values_to_schema(EMAIL_SCHEMA, {CONF_EMAIL: self._email} if self._email else {})
        return self.async_show_form(step_id=step_id, data_schema=schema, errors=errors)

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None and self._login is not None:
            try:
                if self._token is None:  # a code works once; on a retry, reuse its token
                    self._token = await self._login.async_submit_code(user_input[CONF_CODE])
                client = PlanetFitnessClient(async_get_clientsession(self.hass), self._token)
                await client.async_get_home_club()  # proves the token works for the API
            except PlanetFitnessAuthError:
                # The sign-in itself was rejected; a new code is needed.
                self._token = None
                self._detach_login_session()
                step_id = "reauth_confirm" if self.source == SOURCE_REAUTH else "user"
                return self._email_form(step_id, {"base": "invalid_auth"})
            except PlanetFitnessError as err:
                _LOGGER.debug("Signing in failed: %s", err)
                errors["base"] = _error_key(err)
            else:
                self._detach_login_session()
                return await self._async_finish(client)
        return self.async_show_form(
            step_id="code",
            data_schema=CODE_SCHEMA,
            errors=errors,
            description_placeholders={"email": self._email or ""},
        )

    async def _async_finish(self, client: PlanetFitnessClient) -> ConfigFlowResult:
        if not client.subject:
            return self.async_abort(reason="unexpected_page")
        await self.async_set_unique_id(client.subject)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            entry = self._get_reauth_entry()
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_TOKEN: client.token})
            # The update listener only re-renders; reload so the new sign-in is used.
            self.hass.config_entries.async_schedule_reload(entry.entry_id)
            return self.async_abort(reason="reauth_successful")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title="Planet Fitness", data={CONF_TOKEN: client.token})

    @callback
    def _detach_login_session(self) -> None:
        if self._login_session is not None:
            self._login_session.detach()
            self._login_session = None

    @callback
    def async_remove(self) -> None:
        self._detach_login_session()


class PlanetFitnessOptionsFlow(OptionsFlow):
    """Crowd thresholds and calendar visit length."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_LOW_THRESHOLD] >= user_input[CONF_HIGH_THRESHOLD]:
                errors[CONF_HIGH_THRESHOLD] = "high_not_above_low"
            else:
                return self.async_create_entry(data=user_input)
        percent = NumberSelectorConfig(min=1, max=100, step=1, unit_of_measurement="%", mode=NumberSelectorMode.BOX)
        schema = vol.Schema(
            {
                vol.Required(CONF_LOW_THRESHOLD, default=DEFAULT_LOW_THRESHOLD): NumberSelector(percent),
                vol.Required(CONF_HIGH_THRESHOLD, default=DEFAULT_HIGH_THRESHOLD): NumberSelector(percent),
                vol.Required(CONF_BUSIER_FACTOR, default=DEFAULT_BUSIER_FACTOR): NumberSelector(
                    NumberSelectorConfig(min=1.0, max=3.0, step=0.05, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_VISIT_MINUTES, default=DEFAULT_VISIT_MINUTES): NumberSelector(
                    NumberSelectorConfig(
                        min=15, max=240, step=5, unit_of_measurement="min", mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or self.config_entry.options),
            errors=errors,
        )


def _club_label(club: ClubSummary) -> str:
    place = ", ".join(p for p in (club.city, club.state) if p)
    distance = f" ({club.distance_m / 1609.344:.1f} mi)" if club.distance_m is not None else ""
    return f"{club.name}{f' - {place}' if place else ''}{distance}"


class ClubSubentryFlow(ConfigSubentryFlow):
    """Track another club's crowd meter."""

    def __init__(self) -> None:
        self._results: list[ClubSummary] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        entry = self._get_entry()
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        errors: dict[str, str] = {}
        if user_input is not None:
            point = await self._search_point(user_input, errors)
            if point is not None:
                runtime = entry.runtime_data
                try:
                    results = await runtime.client.async_search_clubs(*point)
                except PlanetFitnessError as err:
                    _LOGGER.debug("Club search failed: %s", err)
                    errors["base"] = _error_key(err)
                else:
                    taken = {runtime.home_club_id} | {s.data.get(CONF_CLUB_ID) for s in entry.subentries.values()}
                    self._results = [r for r in results if r.club_id not in taken]
                    if self._results:
                        return await self.async_step_pick()
                    errors["base"] = "no_clubs"
        home = {CONF_LATITUDE: self.hass.config.latitude, CONF_LONGITUDE: self.hass.config.longitude}
        schema = vol.Schema(
            {
                vol.Optional(CONF_ZIP_CODE): TextSelector(TextSelectorConfig(autocomplete="postal-code")),
                vol.Required(CONF_LOCATION, default=home): LocationSelector(),
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
        )

    async def _search_point(self, user_input: dict[str, Any], errors: dict[str, str]) -> tuple[float, float] | None:
        """Where to search: the ZIP code if one was entered, else the map pin."""
        text = user_input.get(CONF_ZIP_CODE, "").strip()
        if not text:
            location = user_input[CONF_LOCATION]
            return location[CONF_LATITUDE], location[CONF_LONGITUDE]
        zip_code = parse_zip(text)
        point = await self.hass.async_add_executor_job(zip_location, zip_code) if zip_code else None
        if point is None:
            errors[CONF_ZIP_CODE] = "unknown_zip" if zip_code else "invalid_zip"
        return point

    async def async_step_pick(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        if user_input is not None:
            club = next(r for r in self._results if r.club_id == user_input[CONF_CLUB_ID])
            return self.async_create_entry(title=club.name, data={CONF_CLUB_ID: club.club_id}, unique_id=club.club_id)
        options = [SelectOptionDict(value=r.club_id, label=_club_label(r)) for r in self._results]
        return self.async_show_form(
            step_id="pick",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_CLUB_ID): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )
