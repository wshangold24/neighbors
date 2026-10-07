"""RentCast provider — the only *official API* source (no scraping).

Docs: https://developers.rentcast.io/reference/rental-listings-long-term
Auth: X-Api-Key header. Free tier = 50 requests/month, so this provider is
aggressively frugal: ONE request per run by default (limit=500 covers far
more than a search needs), and SafeSession's 12h disk cache makes repeat
runs free.

API key resolution (in order):
  1. constructor arg (from --rentcast-key)
  2. RENTCAST_API_KEY environment variable
  3. ~/.aptfinder/rentcast_key file
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from pathlib import Path

from ..models import Listing, Preferences, nearest_neighborhood
from .base import Provider

log = logging.getLogger("aptfinder.rentcast")

KEY_FILE = Path.home() / ".aptfinder" / "rentcast_key"
ENDPOINT = "https://api.rentcast.io/v1/listings/rental/long-term"
MAX_DAYS_OLD = 60          # ignore stale listings
RADIUS_MILES = 2.5         # around chosen-neighborhood centroid


def resolve_api_key(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit.strip()
    env = os.environ.get("RENTCAST_API_KEY", "").strip()
    if env:
        return env
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip() or None
    return None


def save_api_key(key: str) -> None:
    KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_text(key.strip() + "\n")
    KEY_FILE.chmod(0o600)


class RentCastProvider(Provider):
    name = "rentcast"

    def __init__(self, session, api_key: str | None = None):
        super().__init__(session)
        self.api_key = resolve_api_key(api_key)

    def fetch(self, prefs: Preferences, max_pages: int = 1) -> list[Listing]:
        if not self.api_key:
            log.warning(
                "rentcast: no API key. Set RENTCAST_API_KEY, use --rentcast-key, "
                "or write it to %s", KEY_FILE,
            )
            return []

        hi = round(prefs.max_price * (1 + prefs.price_stretch))
        lo = round(prefs.min_price * 0.85) if prefs.min_price else None
        params: dict = {
            "status": "Active",
            "price": f"{lo or '*'}:{hi}",
            "daysOld": f"*:{MAX_DAYS_OLD}",
            "limit": 500,
        }
        coords = prefs.target_coords()
        if coords:
            params.update(latitude=coords[0], longitude=coords[1],
                          radius=RADIUS_MILES if len(prefs.neighborhoods) == 1 else 5)
        else:
            params.update(city="New York", state="NY")

        listings: list[Listing] = []
        # Free tier = 50 req/month: default to a single page unless asked.
        for page in range(max_pages):
            if page:
                params = {**params, "offset": page * 500}
            body = self.session.get(ENDPOINT, params=params,
                                    headers={"X-Api-Key": self.api_key,
                                             "Accept": "application/json"})
            if body is None:
                log.warning("rentcast: request failed (check key/quota)")
                break
            found = self._parse(body)
            listings.extend(found)
            if len(found) < 500:
                break
        return self._validated(listings)

    def _parse(self, body: str) -> list[Listing]:
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.warning("rentcast: non-JSON response")
            return []
        if isinstance(data, dict):  # error payloads are dicts
            log.warning("rentcast: API error: %s", data.get("message", data))
            return []
        out = []
        for d in data:
            try:
                lat, lon = d.get("latitude"), d.get("longitude")
                hood = nearest_neighborhood(lat, lon) if lat and lon else ""
                addr = d.get("formattedAddress") or d.get("addressLine1") or ""
                out.append(Listing(
                    id=f"rc-{d.get('id') or abs(hash(addr))}",
                    source=self.name,
                    # RentCast has no listing page; link a search for the address.
                    url="https://www.google.com/search?q=" +
                        urllib.parse.quote(f'"{addr}" apartment rent'),
                    address=addr,
                    neighborhood=hood,
                    price=int(d["price"]),
                    beds=float(d.get("bedrooms") or 0),
                    baths=float(d.get("bathrooms") or 1),
                    sqft=int(d["squareFootage"]) if d.get("squareFootage") else None,
                    lat=lat,
                    lon=lon,
                    description=f"{d.get('propertyType', '')} · listed {str(d.get('listedDate', ''))[:10]}",
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return out
