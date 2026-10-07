"""Housing Connect provider — NYC's official affordable-housing lottery
inventory, via NYC Open Data (open by law, ToS-permitted, no API key).

Two Socrata datasets, joined on Lottery ID:
  * vy5i-a666  "Advertised Lotteries ... by Lottery"  — lottery status/type
  * nibs-na6y  "Advertised Lotteries ... by Building" — per-building address,
    coordinates, bedroom distribution, AMI income bands

We surface only lotteries that are Rental AND currently "Active" (accepting
applications) — real, available inventory you can act on today.

Important honesty note on rent: Housing Connect data has NO rent field —
affordable rents are set per AMI (Area Median Income) band, not published as a
single number in these datasets. So each unit's `price` here is an ESTIMATE
derived from its dominant AMI band and bedroom size (see AMI_BASE_RENT). It's
tagged `income_restricted`, and the estimate is stated plainly in the
description. Treat these as lottery opportunities, not fixed-price leases.
"""

from __future__ import annotations

import logging
import urllib.parse

from ..models import Listing, Preferences, nearest_neighborhood
from .base import Provider
from .socrata import (HOUSING_CONNECT_BUILDING, HOUSING_CONNECT_LOTTERY,
                      resolve_app_token, soda_get, soql_in)

log = logging.getLogger("neighbors.housingconnect")

BOROUGH = {"BK": "Brooklyn", "BX": "Bronx", "MN": "New York",
           "QN": "Queens", "SI": "Staten Island"}

# Representative monthly rent (~1-bedroom) by AMI band — approximate, from
# typical NYC Housing Connect advertised rents. An estimate, clearly labeled.
AMI_BASE_RENT = {
    "ext_low": 500, "very_low": 950, "low": 1500,
    "moderate": 2200, "middle": 2900, "above": 3300,
}
AMI_LABEL = {
    "ext_low": "≤30% AMI", "very_low": "31–50% AMI", "low": "51–80% AMI",
    "moderate": "81–120% AMI", "middle": "121–165% AMI", "above": ">165% AMI",
}
# Monthly delta from the 1-bedroom baseline, by bedroom count.
BED_ADJ = {0: -300, 1: 0, 2: 450, 3: 800, 4: 1200}
# by-building bedroom-distribution columns -> bedroom count
BED_FIELDS = {"unit_distribution_studio": 0, "unit_distribution_1bed": 1,
              "unit_distribution_2bed": 2, "unit_distribution_3bed": 3,
              "unit_distribution_4bed": 4}


def _int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


class HousingConnectProvider(Provider):
    name = "housingconnect"

    def __init__(self, session, app_token: str | None = None):
        super().__init__(session)
        self.app_token = resolve_app_token(app_token)

    def fetch(self, prefs: Preferences, max_pages: int = 1) -> list[Listing]:
        # 1. active rental lotteries → ids + their application deadline
        lotteries = soda_get(self.session, HOUSING_CONNECT_LOTTERY, {
            "$select": "lottery_id,lottery_name,lottery_end_date",
            "$where": "development_type='Rental' AND lottery_status='Active'",
            "$limit": 500,
        }, self.app_token)
        meta = {r["lottery_id"]: r for r in lotteries if r.get("lottery_id")}
        if not meta:
            log.info("housingconnect: no active rental lotteries right now")
            return []

        # 2. buildings for those lotteries
        buildings = soda_get(self.session, HOUSING_CONNECT_BUILDING, {
            "$where": f"lottery_id IN ({soql_in(meta)})",
            "$limit": 2000,
        }, self.app_token)
        return self._validated(self._parse_buildings(buildings, prefs, meta))

    def _parse_buildings(self, rows: list[dict], prefs: Preferences,
                         meta: dict | None = None) -> list[Listing]:
        out = []
        for d in rows:
            try:
                listing = self._one(d, prefs, meta or {})
                if listing:
                    out.append(listing)
            except (KeyError, TypeError, ValueError):
                continue
        return out

    @staticmethod
    def _deadline(raw: str) -> str:
        """'2026-06-02T00:00:00.000' -> 'Jun 2, 2026' (or '' if unparseable)."""
        raw = (raw or "")[:10]
        try:
            from datetime import date
            y, m, day = (int(x) for x in raw.split("-"))
            months = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
            return f"{months[m]} {day}, {y}"
        except (ValueError, IndexError):
            return ""

    def _one(self, d: dict, prefs: Preferences, meta: dict) -> Listing | None:
        house = (d.get("house_number") or "").strip()
        street = (d.get("street_name") or "").strip().title()
        if not house or not street:
            return None
        boro = BOROUGH.get((d.get("borough") or "").strip().upper())
        if not boro:                       # skip "Multiple" / unknown
            return None
        zip_code = (d.get("address_zipcode") or "").strip()
        address = f"{house} {street}, {boro}, NY {zip_code}".strip()

        try:
            lat = float(d["address_latitude"]); lon = float(d["address_longitude"])
        except (KeyError, TypeError, ValueError):
            lat = lon = None

        # available bedroom sizes → pick the one closest to what the user wants
        sizes = [(bed, _int(d.get(f))) for f, bed in BED_FIELDS.items()]
        available = [bed for bed, n in sizes if n > 0]
        if not available:
            return None
        beds = min(available, key=lambda b: (abs(b - prefs.beds), b))

        ami = self._dominant_ami(d)
        rent = max(500, AMI_BASE_RENT[ami] + BED_ADJ.get(beds, 0))
        rent = int(round(rent / 25) * 25)

        hood = (nearest_neighborhood(lat, lon) if lat and lon else "") or boro
        lid = d.get("lottery_id", "")
        m = meta.get(lid, {})
        lottery_name = (m.get("lottery_name") or d.get("lottery_name")
                        or "Housing lottery").strip()
        deadline = self._deadline(m.get("lottery_end_date"))
        when = f"apply by {deadline}" if deadline else "applications open"
        return Listing(
            id=f"hc-{lid}-{d.get('hc_building_id') or d.get('address_buildingidentificationnumber') or house}",
            source=self.name,
            # deep-link into Housing Connect's public site for this lottery
            url=("https://housingconnect.nyc.gov/PublicWeb/search-lotteries?keyword="
                 + urllib.parse.quote(lottery_name)),
            address=address,
            neighborhood=hood,
            price=rent,
            beds=float(beds),
            baths=1.0,
            sqft=None,
            lat=lat, lon=lon,
            amenities=["income_restricted", "no_fee"],
            description=(f"Affordable lottery — {lottery_name} · {AMI_LABEL[ami]} · "
                        f"est. rent ~${rent} (AMI-based, not exact) · {when} on Housing Connect"),
        )

    @staticmethod
    def _dominant_ami(d: dict) -> str:
        """The AMI band with the most units in this building; default 'middle'
        (the most common band overall) when none are populated."""
        best, best_n = "middle", -1
        for band in AMI_BASE_RENT:
            n = _int(d.get(f"applied_income_ami_{band}"))
            if n > best_n:
                best, best_n = band, n
        return best
