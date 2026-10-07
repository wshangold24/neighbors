"""NYC Open Data (Socrata SODA) access — free, open by law, ToS-permitted.

Two consumers:
  * HousingConnectProvider — affordable-lottery inventory.
  * verify.py's building lookup — PLUTO + HPD building-registry verification.

An app token lifts the shared anonymous rate pool (register a free one at
data.cityofnewyork.us; up to ~1,000 req/hour). It's optional — anonymous
access works, just with a smaller shared budget. Resolution order:
  1. explicit argument
  2. NYC_APP_TOKEN environment variable
  3. ~/.aptfinder/nyc_app_token file

All requests go through SafeSession (rate-limited, cached, budgeted).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("aptfinder.socrata")

DOMAIN = "https://data.cityofnewyork.us"
TOKEN_FILE = Path.home() / ".aptfinder" / "nyc_app_token"

# Dataset ids used across the app (documented here so the magic strings live
# in exactly one place).
PLUTO = "64uk-42ks"          # Primary Land Use Tax Lot Output (building facts)
HPD_REGISTRATIONS = "tesw-yqqr"   # HPD building registrations
HOUSING_CONNECT_LOTTERY = "vy5i-a666"    # Advertised lotteries, by lottery
HOUSING_CONNECT_BUILDING = "nibs-na6y"   # Advertised lotteries, by building


def resolve_app_token(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit.strip()
    env = os.environ.get("NYC_APP_TOKEN", "").strip()
    if env:
        return env
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip() or None
    return None


def soda_get(session, dataset: str, params: dict,
             app_token: str | None = None) -> list[dict]:
    """Fetch rows from a Socrata dataset as a list of dicts. Returns [] on any
    failure (network, non-JSON, error payload) — callers degrade gracefully."""
    url = f"{DOMAIN}/resource/{dataset}.json"
    headers = {"Accept": "application/json"}
    token = resolve_app_token(app_token)
    if token:
        headers["X-App-Token"] = token
    body = session.get(url, params=params, headers=headers)
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("socrata: non-JSON response from %s", dataset)
        return []
    if isinstance(data, dict):   # Socrata errors come back as a dict
        log.warning("socrata: error from %s: %s", dataset, data.get("message", data))
        return []
    return data


def soql_in(values) -> str:
    """Render a SoQL IN(...) list of quoted strings: ['1','2'] -> \"'1','2'\"."""
    return ",".join("'" + str(v).replace("'", "") + "'" for v in values)
