"""Constants for the Planet Fitness integration."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final

DOMAIN: Final = "planet_fitness"

CONF_TOKEN: Final = "token"
CONF_EMAIL: Final = "email"
CONF_CODE: Final = "code"
CONF_CLUB_ID: Final = "club_id"
CONF_ZIP_CODE: Final = "zip_code"

SUBENTRY_CLUB: Final = "club"

# Options
CONF_LOW_THRESHOLD: Final = "low_threshold"
CONF_HIGH_THRESHOLD: Final = "high_threshold"
CONF_BUSIER_FACTOR: Final = "busier_factor"
CONF_VISIT_MINUTES: Final = "visit_minutes"
DEFAULT_LOW_THRESHOLD: Final = 25
DEFAULT_HIGH_THRESHOLD: Final = 50
DEFAULT_BUSIER_FACTOR: Final = 1.25
DEFAULT_VISIT_MINUTES: Final = 60

# Polling. Keep these gentle: this is a private API used through the member's account.
CLUB_INTERVAL: Final = timedelta(minutes=5)
CLUB_CLOSED_INTERVAL: Final = timedelta(minutes=30)
CROWD_PROFILE_MAX_AGE: Final = timedelta(hours=24)
ACCOUNT_INTERVAL: Final = timedelta(minutes=10)  # current check-in
RECENT_HISTORY_MAX_AGE: Final = timedelta(hours=1)  # last RECENT_HISTORY_DAYS
YEAR_HISTORY_MAX_AGE: Final = timedelta(hours=24)  # last HISTORY_WINDOW_DAYS
BILLING_MAX_AGE: Final = timedelta(hours=12)
RECENT_HISTORY_DAYS: Final = 35
HISTORY_WINDOW_DAYS: Final = 365
EARLIEST_START: Final = date(1992, 1, 1)  # Planet Fitness opened in 1992
HISTORY_MAX_WINDOWS: Final = 40  # years back to the membership start
OPTIONAL_RETRY: Final = timedelta(hours=1)  # first retry of optional data; doubles while it keeps failing
DEFAULT_RETRY_AFTER: Final = timedelta(minutes=15)  # a 429 without Retry-After
TREND_WINDOW: Final = timedelta(minutes=45)
TREND_MIN_SPAN: Final = timedelta(minutes=15)
NEW_CHECK_IN_MAX_AGE: Final = timedelta(days=2)

CURRENCY: Final = "USD"
