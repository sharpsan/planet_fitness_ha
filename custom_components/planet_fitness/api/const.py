"""Constants for the Planet Fitness mobile API."""

from __future__ import annotations

AUTH_DOMAIN = "https://login.planetfitness.com"
CLIENT_ID = "V90DU2UBXm6sbkpk0rj08i0ol3rQey06"
REDIRECT_URI = "com.planetfitness.pfmobileauth://callback"
AUDIENCE = "https://*.api.planetfitness.com"
LOGIN_SCOPE = "openid offline_access"
REFRESH_SCOPE = "openid offline_access"
APP_VERSION = "10.0.5"

# Extra /authorize parameters the Android app sends.
AUTHORIZE_EXTRAS = {
    "connection": "email",
    "ui_locales": "en",
    "ext-countryCode": "US",
    "ext-appVersion": APP_VERSION,
}

API_BASE = "https://api.planetfitness.com/mobile/"
API_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "pfx-mobile",
    "Accept-Version": "25",
    "Mobile-App-Version": APP_VERSION,
    "Mobile-App-Platform": "Android",
    "mobile-app-preview-mode": "false",
    "Accept-Language": "en-US",
    "countryCode": "US",
}
AUTH_HEADERS = {"Accept": "application/json"}

REQUEST_TIMEOUT = 20  # seconds
TOKEN_REFRESH_MARGIN = 120  # refresh this many seconds before expiry
RATE_LIMIT_PAUSE = 900  # seconds to wait after a 429 without Retry-After
HISTORY_PAGE_SIZE = 500  # same page size as the app
HISTORY_MAX_PAGES = 5
