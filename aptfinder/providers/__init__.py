"""Listing providers. Each provider is best-effort: it returns validated
Listings or an empty list — never raises for fetch failures.

Only ToS-clean sources are registered:
  * RentCast — official licensed listings API (commercial use permitted).
  * Housing Connect — NYC's official affordable-housing lottery inventory,
    via NYC Open Data (open by law).

The former StreetEasy and RentHop scrapers were removed: scraping their pages
violates their terms of use, so they are no longer part of this app.
"""

from __future__ import annotations

from .base import Provider
from .rentcast import RentCastProvider
from .housingconnect import HousingConnectProvider

# Live sources only. DemoProvider (providers/demo.py) is intentionally NOT
# registered — it exists solely for the test suite.
PROVIDERS: dict[str, type[Provider]] = {
    "rentcast": RentCastProvider,              # official licensed API (needs key)
    "housingconnect": HousingConnectProvider,  # NYC Open Data (no key needed)
}
