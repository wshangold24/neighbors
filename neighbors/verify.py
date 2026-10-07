"""Independent, fetch-time verification that a listing is a *real registered
residential dwelling* — not a phantom or mis-located address.

We don't take a provider's address on faith. After a fetch we resolve each
listing against NYC's authoritative building registries (open data, ToS-clean):

  address --(NYC GeoSearch)--> BBL + BIN + canonical coords
        BBL --(PLUTO)--> residential units, building class, land use
        BIN --(HPD registrations)--> is the building registered as required?

The verdict recorded per listing:
  * "ok"              — resolves to a residential building (PLUTO units/class),
                        near where it was listed; HPD registration is a bonus
  * "not_residential" — resolves to a real lot, but not a home (commercial/
                        vacant) — a strong "not a real rental" signal
  * "mismatch"        — resolves far from where the provider placed it (suspect)
  * "not_found"       — no NYC building record for the address at all (suspect)

The lookup is INJECTED (a callable address,lat,lon -> facts dict), so this
module is pure logic and unit-testable with zero network. Results are stamped
and cached on the store record, rechecked only after VERIFY_TTL_S, and bounded
per fetch to respect rate limits.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from .knn import haversine_km

# Resolved coords this far from the provider's coords → flag mismatch.
MISMATCH_KM = 1.5
# Re-verify a listing at most this often (buildings don't move).
VERIFY_TTL_S = 30 * 24 * 3600

# PLUTO land-use codes that are residential (01 1&2-family, 02 multifamily
# walk-up, 03 multifamily elevator, 04 mixed residential/commercial).
RESIDENTIAL_LANDUSE = {"01", "02", "03", "04"}
# PLUTO building-class first letters that are residential.
RESIDENTIAL_BLDG_CLASS = {"A", "B", "C", "D", "R", "S"}

# address, lat, lon -> {
#   "found": bool,               # GeoSearch matched a NYC address
#   "bbl": str, "bin": str, "label": str,
#   "lat": float, "lon": float,  # canonical coords (for the mismatch check)
#   "units": int|None, "bldgclass": str, "landuse": str,   # PLUTO
#   "numfloors": str|None, "yearbuilt": str|None,          # PLUTO
#   "registered": bool|None,     # HPD
# }
# Optional, not `float | None`: an alias is evaluated at runtime (Python 3.9).
BuildingLookup = Callable[[str, Optional[float], Optional[float]], dict]


def _to_int(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def derived_amenities(bldgclass: str) -> list[str]:
    """FACTS: amenities we can assert with high confidence from a PLUTO building
    class. Class 'D' = elevator apartment buildings — the one renter amenity a
    building's class lets us state outright. (Walk-ups are class 'C'; we don't
    infer a negative from that, to stay conservative.)"""
    return ["elevator"] if (bldgclass or "").upper().startswith("D") else []


def inferred_amenities(bldgclass: str, numfloors, yearbuilt, unitsres) -> list[str]:
    """GUESSES: lower-confidence amenities suggested by a building's size and
    age. These are advisory hints only — never used to gate or score — because
    they're correlations, not confirmations. A tall building usually has a
    doorman; a newly-built one usually has in-unit laundry and a dishwasher;
    a large one usually has building laundry — but any given building may not.

    Deliberately conservative thresholds, and we never repeat a derived fact."""
    nf, yb, ur = _to_int(numfloors), _to_int(yearbuilt), _to_int(unitsres)
    out: list[str] = []
    if nf >= 20:
        out.append("doorman")               # tall towers are usually staffed
    if ur >= 20:
        out.append("laundry_in_building")   # large buildings usually have it
    if yb >= 2000:
        out += ["dishwasher", "central_air"]  # modern construction standard
    if yb >= 2010:
        out.append("laundry_in_unit")
    if yb >= 2010 and ur >= 50:
        out += ["gym", "roof_deck"]          # large new developments
    return list(dict.fromkeys(out))


def is_residential(units, bldgclass: str, landuse: str) -> bool:
    if _to_int(units) >= 1:
        return True
    if (landuse or "").zfill(2) in RESIDENTIAL_LANDUSE:
        return True
    return (bldgclass or "").upper()[:1] in RESIDENTIAL_BLDG_CLASS


def verify_one(rec: dict, lookup: BuildingLookup, now: float) -> dict:
    """Verify one store record against the building registries. An address
    the lookup can't resolve is a 'not_found' result, not an error; exceptions
    raised by `lookup` itself (e.g. BudgetExceeded) propagate, so the record
    stays unverified instead of being stamped suspect for VERIFY_TTL_S."""
    listing = rec.get("listing", {})
    address = (listing.get("address") or "").strip()
    result: dict = {"at": now, "checked": address}
    if len(address) < 6:
        return {**result, "status": "not_found", "km_off": None}

    f = lookup(address, listing.get("lat"), listing.get("lon")) or {}
    if not f.get("found"):
        return {**result, "status": "not_found", "km_off": None}

    residential = is_residential(f.get("units"), f.get("bldgclass"), f.get("landuse"))
    # enrich the listing from the building record (kept separate from provider-
    # reported amenities — see the Listing model). Facts credit ranking and are
    # gate-safe; inferences are advisory display-only hints.
    facts = derived_amenities(f.get("bldgclass")) if residential else []
    guesses = (inferred_amenities(f.get("bldgclass"), f.get("numfloors"),
                                  f.get("yearbuilt"), f.get("units"))
               if residential else [])
    # existing derived facts (e.g. a prior RentCast enrichment) are preserved
    prior = [a for a in listing.get("derived_amenities") or [] if a not in facts]
    listing["derived_amenities"] = facts + prior
    listing["inferred_amenities"] = [a for a in guesses
                                     if a not in listing["derived_amenities"]]

    base = {**result, "bbl": f.get("bbl"), "bin": f.get("bin"),
            "units": f.get("units"), "bldgclass": f.get("bldgclass"),
            "numfloors": f.get("numfloors"), "yearbuilt": f.get("yearbuilt"),
            "registered": f.get("registered"), "resolved": f.get("label", address)}

    # distance between where it's listed and where it actually resolves
    km = None
    lat, lon = listing.get("lat"), listing.get("lon")
    if None not in (lat, lon, f.get("lat"), f.get("lon")):
        km = round(haversine_km(lat, lon, f["lat"], f["lon"]), 2)
    base["km_off"] = km

    if not residential:
        return {**base, "status": "not_residential"}
    if km is not None and km > MISMATCH_KM:
        return {**base, "status": "mismatch"}
    return {**base, "status": "ok"}


def needs_verify(rec: dict, now: float) -> bool:
    v = rec.get("verify")
    return not v or (now - v.get("at", 0)) > VERIFY_TTL_S


def verify_store(store, lookup: BuildingLookup, limit: int = 8,
                 now: float | None = None,
                 only_ids: set[str] | None = None) -> int:
    """Verify up to `limit` unverified (or stale) records in place. Returns how
    many were checked. Newest-seen first, so freshly-fetched listings get
    verified before old ones re-checked. Caller is responsible for store.save().

    `only_ids` restricts verification to those listing ids — used to verify just
    the listings that can actually be shown (in-area, active), instead of every
    address in the store. A citywide fetch stores hundreds the user will never
    see; verifying all of them is what made 'verify all' crawl."""
    now = time.time() if now is None else now
    checked = 0
    for rec in sorted(store.records().values(),
                      key=lambda r: r.get("last_seen", 0), reverse=True):
        if checked >= limit:
            break
        if only_ids is not None and rec["listing"]["id"] not in only_ids:
            continue
        if not needs_verify(rec, now):
            continue
        rec["verify"] = verify_one(rec, lookup, now)
        checked += 1
    return checked
