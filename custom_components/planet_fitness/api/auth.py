"""Sign-in: email + emailed code through PF's hosted Universal Login page.

This mirrors what the Planet Fitness app does in its browser tab:
GET /authorize -> identifier form (email) -> passwordless-email-challenge form
(code) -> /authorize/resume -> redirect to the app callback with ?code=, then a
PKCE authorization-code exchange.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import email.utils
import hashlib
from html.parser import HTMLParser
import json
import math
import secrets
import time
from typing import Any
import urllib.parse

import aiohttp

from .const import (
    AUDIENCE,
    AUTH_DOMAIN,
    AUTH_HEADERS,
    AUTHORIZE_EXTRAS,
    CLIENT_ID,
    LOGIN_SCOPE,
    REDIRECT_URI,
    REQUEST_TIMEOUT,
)
from .exceptions import (
    LoginError,
    PlanetFitnessAuthError,
    PlanetFitnessConnectionError,
    PlanetFitnessError,
    PlanetFitnessRateLimitError,
)

MAX_RETRY_AFTER = 86400  # seconds; a longer wait is capped at this


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header: a number of seconds, or an HTTP date,
    up to a day. Anything else counts as missing."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        pass
    else:
        return min(max(seconds, 0.0), MAX_RETRY_AFTER) if not math.isnan(seconds) else None
    try:
        when = email.utils.parsedate_to_datetime(value)
    except TypeError, ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return min(max((when - datetime.now(UTC)).total_seconds(), 0.0), MAX_RETRY_AFTER)


PAGE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; planet_fitness-homeassistant)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US",
}
MAX_REDIRECTS = 6


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_claims(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying it (used only for `sub`)."""
    try:
        payload = token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except IndexError, ValueError:
        return {}


def token_from_response(body: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the stored token dict. The id_token (name/email) is not kept."""
    previous = previous or {}
    return {
        "access_token": body["access_token"],
        # Auth0 may rotate refresh tokens; keep the newest one.
        "refresh_token": body.get("refresh_token") or previous.get("refresh_token"),
        "expires_at": time.time() + int(body.get("expires_in", 3600)),
    }


class _Form:
    def __init__(self, action: str | None, method: str) -> None:
        self.action = action
        self.method = method
        self.inputs: list[dict[str, str]] = []
        self.buttons: list[dict[str, str]] = []

    @property
    def field_names(self) -> set[str]:
        return {i["name"] for i in self.inputs if i.get("name")}

    def button(self, value: str) -> dict[str, str] | None:
        return next((b for b in self.buttons if b.get("name") and b.get("value") == value), None)


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[_Form] = []
        self._open: _Form | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: (v or "") for k, v in attrs}
        if tag == "form":
            self._open = _Form(a.get("action") or None, (a.get("method") or "GET").upper())
            self.forms.append(self._open)
        elif self._open is None or "disabled" in a:
            return
        elif tag == "input":
            if a.get("type", "").lower() in ("checkbox", "radio") and "checked" not in a:
                return
            self._open.inputs.append(a)
        elif tag == "button":
            self._open.buttons.append(a)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._open = None


def _forms(html: str) -> list[_Form]:
    parser = _FormParser()
    parser.feed(html)
    return [f for f in parser.forms if f.method == "POST"]


def _find_form(html: str, field: str, button_value: str = "default") -> _Form | None:
    for form in _forms(html):
        if field in form.field_names and form.button(button_value):
            return form
    return None


class EmailCodeLogin:
    """One sign-in attempt. Give it a session with its own cookie jar."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._verifier = _b64url(secrets.token_bytes(48))
        self._state = _b64url(secrets.token_bytes(24))
        self._page_url: str | None = None
        self._page_html: str | None = None

    def _authorize_url(self) -> str:
        params = {
            "scope": LOGIN_SCOPE,
            "audience": AUDIENCE,
            **AUTHORIZE_EXTRAS,
            "response_type": "code",
            "code_challenge": _b64url(hashlib.sha256(self._verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "state": self._state,
            "nonce": _b64url(secrets.token_bytes(24)),
        }
        return f"{AUTH_DOMAIN}/authorize?{urllib.parse.urlencode(params, quote_via=urllib.parse.quote)}"

    async def _fetch(
        self, url: str, data: dict[str, str] | None = None, referer: str | None = None
    ) -> tuple[int, str, str]:
        """Request and follow http(s) redirects. Returns (status, final_url, html).

        A redirect to a non-http URL (the app callback) is returned as final_url.
        """
        headers = dict(PAGE_HEADERS)
        if referer:
            headers["Referer"] = referer
        method = "GET"
        if data is not None:
            method = "POST"
            headers["Origin"] = AUTH_DOMAIN
        try:
            for _ in range(MAX_REDIRECTS):
                async with self._session.request(
                    method,
                    url,
                    data=data,
                    headers=headers,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    status = resp.status
                    location = resp.headers.get("Location")
                    html = await resp.text()
                if status == 429:
                    raise PlanetFitnessRateLimitError
                if status >= 500:
                    raise PlanetFitnessError(f"Sign-in page returned HTTP {status}", status=status)
                if not (300 <= status < 400 and location):
                    return status, url, html
                url = urllib.parse.urljoin(url, location)
                if not url.startswith(("http://", "https://")):
                    return status, url, ""
                method, data = "GET", None
                headers.pop("Origin", None)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise PlanetFitnessConnectionError(str(err)) from err
        raise PlanetFitnessError("Too many redirects on the sign-in page")

    async def _submit(self, form: _Form, values: dict[str, str], button_value: str = "default") -> tuple[int, str, str]:
        assert self._page_url is not None
        data = {i["name"]: i.get("value", "") for i in form.inputs if i.get("name") and i.get("type") != "password"}
        data.update(values)
        button = form.button(button_value)
        if button:
            data[button["name"]] = button.get("value", "")
        action = urllib.parse.urljoin(self._page_url, form.action) if form.action else self._page_url
        return await self._fetch(action, data, referer=self._page_url)

    async def async_request_code(self, email: str) -> None:
        """Open the sign-in page and submit the email; PF then emails a code."""
        status, url, html = await self._fetch(self._authorize_url())
        form = _find_form(html, "username")
        if form is None:
            raise LoginError("unexpected_page", f"No email form at {url.split('?')[0]} (HTTP {status})")
        self._page_url, self._page_html = url, html
        status, url, html = await self._submit(form, {"username": email.strip()})
        if _find_form(html, "code") is None:
            # Still on the email form: Auth0 rejected the address.
            reason = "invalid_email" if _find_form(html, "username") else "unexpected_page"
            raise LoginError(reason, f"No code form at {url.split('?')[0]} (HTTP {status})")
        self._page_url, self._page_html = url, html

    def _challenge_form(self) -> _Form:
        form = _find_form(self._page_html or "", "code")
        if form is None:
            raise LoginError("unexpected_page", "No code form; request a code first")
        return form

    async def async_submit_code(self, code: str) -> dict[str, Any]:
        """Submit the emailed code and exchange the result for tokens."""
        form = self._challenge_form()
        status, url, html = await self._submit(form, {"code": code.strip().replace(" ", "")})
        if not url.startswith(REDIRECT_URI):
            if _find_form(html, "code") is not None:
                self._page_url, self._page_html = url, html  # wrong/expired code; can retry
                raise LoginError("invalid_code")
            raise LoginError("unexpected_page", f"Ended at {url.split('?')[0]} (HTTP {status})")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if "error" in query:
            raise LoginError("unexpected_page", f"{query['error'][0]}: {query.get('error_description', [''])[0]}")
        if query.get("state", [None])[0] != self._state:
            raise LoginError("unexpected_page", "State mismatch on the callback")
        return await self._exchange(query["code"][0])

    async def _exchange(self, code: str) -> dict[str, Any]:
        form = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": self._verifier,
            "redirect_uri": REDIRECT_URI,
        }
        body = await post_token(self._session, form)
        return token_from_response(body)


async def post_token(session: aiohttp.ClientSession, form: dict[str, str]) -> dict[str, Any]:
    """POST /oauth/token and return the JSON body, mapping errors to exceptions."""
    try:
        async with session.post(
            f"{AUTH_DOMAIN}/oauth/token",
            data=form,
            headers=AUTH_HEADERS,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            status = resp.status
            retry_after = resp.headers.get("Retry-After")
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = None
    except (aiohttp.ClientError, TimeoutError) as err:
        raise PlanetFitnessConnectionError(str(err)) from err
    if status == 200 and isinstance(body, dict) and "access_token" in body:
        return body
    error = body.get("error") if isinstance(body, dict) else None
    if status == 429:
        raise PlanetFitnessRateLimitError(parse_retry_after(retry_after))
    if status in (400, 401, 403):
        raise PlanetFitnessAuthError(f"Token request rejected: {error}", status=status)
    raise PlanetFitnessError(f"Token request failed: HTTP {status} {error or ''}".strip(), status=status)
