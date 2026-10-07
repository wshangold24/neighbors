"""Listing → JSON card for the web UI (pure: no I/O, no network).

Everything the single-page app shows about one listing is assembled here:
the trust verdict, deltas vs preferences, commute estimates, nearby stations
and outbound map links. The Api supplies the store record and feedback.
"""

from __future__ import annotations

import json
import time
from urllib.parse import quote_plus

from .feedback import Feedback
from .knn import match_pct
from .models import Listing, Preferences, area_edge_km, nearest_neighborhood, region_of
from .transit import estimate_commute, stations_within

# Both live sources are official, so being listed on one is what "actually
# listed for rent" means; values are how the trust label names them.
OFFICIAL_SOURCES = {
    "rentcast": "RentCast's licensed feed",
    "housingconnect": "NYC's Housing Connect lottery",
}

# A part of the distance above this is "where the listing falls short".
FURTHEST_THRESHOLD = 0.25


def _nyc_query(address: str) -> str:
    return quote_plus(f"{address}, New York, NY")


def maps_links(address: str) -> dict:
    q = _nyc_query(address)
    return {
        "apple": f"https://maps.apple.com/?q={q}",
        "google": f"https://www.google.com/maps/search/?api=1&query={q}",
        # Realtor.com's ToS forbids scraping/embedding, so link out to a
        # site-scoped search instead (its listing is the first result).
        "realtor": ("https://www.google.com/search?q="
                    + quote_plus(f'site:realtor.com "{address}"')),
    }


def commutes(l: Listing, places: list[dict]) -> list[dict]:
    """Per important place: transit-directions links (need only addresses, so
    always present) plus an offline estimate when both ends have coordinates."""
    apt_q = _nyc_query(l.address)
    out = []
    for p in places:
        dest_q = quote_plus(p.get("address", ""))
        entry = {
            "name": p.get("name", "?"),
            # dirflg=r / travelmode=transit → public-transit directions
            "apple": f"https://maps.apple.com/?saddr={apt_q}&daddr={dest_q}&dirflg=r",
            "google": ("https://www.google.com/maps/dir/?api=1"
                       f"&origin={apt_q}&destination={dest_q}&travelmode=transit"),
            "minutes": None, "route": None,
        }
        if l.lat and l.lon and p.get("lat") and p.get("lon"):
            est = estimate_commute(l.lat, l.lon, p["lat"], p["lon"])
            entry.update(minutes=est.minutes, route=est.route,
                         steps=[{"kind": s.kind, "minutes": s.minutes, "label": s.label}
                                for s in est.steps])
        out.append(entry)
    return out


def deltas(l: Listing, prefs: Preferences) -> dict:
    """Raw differences vs preferences, so the UI can say '$180 over budget'."""
    known = {a.lower() for a in l.amenities} | {a.lower() for a in l.derived_amenities}
    has_coords = bool(l.lat and l.lon)
    # distance to the nearest wanted neighborhood's edge (0 if inside it)
    loc_km = (area_edge_km(l.lat, l.lon, prefs.neighborhoods)
              if prefs.neighborhoods and has_coords else None)
    # where the listing actually is: its own neighborhood, the nearest known
    # one, else the borough
    loc_in = ((l.neighborhood or nearest_neighborhood(l.lat, l.lon)
               or region_of(l.lat, l.lon) or None) if has_coords else None)
    return {
        "price": l.price - prefs.max_price,          # >0 = over budget
        "location_km": round(loc_km, 1) if loc_km else None,
        "location_in": loc_in,
        "beds": l.beds - prefs.beds,
        "baths": l.baths - prefs.baths,
        "sqft": (l.sqft - prefs.min_sqft) if l.sqft and prefs.min_sqft else None,
        "sqft_unknown": bool(prefs.min_sqft and not l.sqft),
        "amenities_missing": sorted(set(prefs.amenities) - known) if prefs.amenities else [],
        # no amenity data at all (source or derived) — unknown, not absent
        "amenities_unknown": bool(prefs.amenities and not known),
    }


def trust(rec: dict | None, prefs: Preferences, now: float | None = None) -> dict:
    """Is this a real rental? Combines two independent checks:

    1. a registered residential dwelling — verify.py resolved the address to a
       residential building in NYC's PLUTO/HPD registries, near where it was
       listed (cached on the record);
    2. actually for rent — on an official source and seen in a recent fetch.

    'confirmed' needs both; 'caution' means a red flag (not residential, not
    found, resolves elsewhere, or stale); 'unverified' means the building check
    hasn't run. Recomputed on every read, so staleness is always current.
    """
    now = now or time.time()
    rec = rec or {}
    v = rec.get("verify") or {}
    sources = sorted(rec.get("sources", {}))
    official = [s for s in sources if s in OFFICIAL_SOURCES]
    active = (now - rec.get("last_seen", now)) <= prefs.active_window_days * 86400
    addr = v.get("status")   # ok | not_residential | mismatch | not_found | None
    km, units, registered = v.get("km_off"), v.get("units"), v.get("registered")

    if official:
        listed = "on " + " & ".join(OFFICIAL_SOURCES[s] for s in official)
        if len(sources) > len(official):
            listed += f" + {len(sources) - len(official)} more"
    else:
        listed = f"listed on {sources[0]}" if sources else "listed"

    if addr == "not_residential":
        level, label = "caution", "that address isn't a residential building — likely not a real rental"
    elif addr == "not_found":
        level, label = "caution", "no NYC building record for this address — treat with caution"
    elif addr == "mismatch":
        level, label = "caution", f"address resolves {km} km from where it's listed — verify before contacting"
    elif not active:
        level, label = "caution", "not seen in recent fetches — may already be rented"
    elif addr == "ok":
        building = f"{units}-unit residential building" if units else "residential building"
        if registered:
            building += ", HPD-registered"
        level, label = "confirmed", f"verified rental — {building}, {listed}"
    else:
        level, label = "unverified", f"{listed} · building not yet verified"

    return {"level": level, "label": label, "address": addr, "km_off": km,
            "units": units, "registered": registered,
            "sources": sources, "official": official, "active": active}


def listing_card(l: Listing, rec: dict | None, prefs: Preferences, feedback: Feedback,
                 flag: str = "", dist: float | None = None, parts: dict | None = None,
                 blend: dict | None = None) -> dict:
    """The full JSON card for one listing. `dist`/`parts` come from ranking
    (absent for a seed listing); `blend` splits the score into prefs vs likes."""
    own = {a.lower() for a in l.amenities}
    furthest = (max(parts, key=parts.get)
                if parts and max(parts.values()) > FURTHEST_THRESHOLD else None)
    return {
        "id": l.id, "address": l.address, "neighborhood": l.neighborhood,
        "lat": l.lat, "lon": l.lon,
        "price": l.price, "beds": l.beds, "baths": l.baths, "sqft": l.sqft,
        # source amenities plus derived facts (e.g. elevator from building
        # records); derived_amenities lets the UI tag which are which
        "amenities": l.amenities + [a for a in l.derived_amenities if a.lower() not in own],
        "derived_amenities": l.derived_amenities,
        # advisory guesses (never gate or score) — shown as "likely"
        "inferred_amenities": l.inferred_amenities, "url": l.url,
        "enriched": bool(rec and rec.get("enriched_at")),
        "description": l.description,
        "match": match_pct(dist) if dist is not None else None,
        "parts": {k: round(v, 3) for k, v in (parts or {}).items()},
        "furthest": furthest,
        "sources": rec["sources"] if rec else {l.source: l.url},
        "price_history": [[int(t), p] for t, p in rec["price_history"]] if rec else [],
        "trust": trust(rec, prefs),
        "flags": flag,
        "liked": l.id in feedback.liked,
        "hidden": l.id in feedback.hidden,
        "seen": feedback.seen.get(l.id, 0),
        "blend": blend,
        "maps": maps_links(l.address),
        "deltas": deltas(l, prefs),
        "commutes": commutes(l, prefs.places),
        # subway/PATH stations within a 10-minute walk, nearest first
        "stations": [{"lines": s.lines_str, "name": s.name, "walk_min": s.walk_min}
                     for s in (stations_within(l.lat, l.lon) if l.lat and l.lon else [])],
    }


def features_to_amenities(body: str | None) -> list[str]:
    """Map a RentCast property record's `features` to our amenity vocabulary.
    Only structural features exist there, so this yields at most central_air
    (cooling) and parking (garage), in that order, deduplicated."""
    if not body:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    found: dict[str, None] = {}   # insertion-ordered set
    for record in data if isinstance(data, list) else [data]:
        f = record.get("features") if isinstance(record, dict) else None
        if not isinstance(f, dict):
            continue
        if f.get("cooling"):
            found["central_air"] = None
        try:
            spaces = int(f.get("garageSpaces") or 0)
        except (TypeError, ValueError):
            spaces = 0
        if f.get("garage") or spaces > 0:
            found["parking"] = None
    return list(found)
