"""Drift-free iterative ranking + fetch planning (Milestones 3 & 4 core).

The engine's one design principle: **preferences define a hard acceptable
region; similarity only reorders candidates inside it.** Liked listings can
never pull results outside your constraints — iteration widens coverage
(more listings seen), never criteria.

This module is pure logic (no I/O, no printing) so it is easy to test;
the CLI in cli.py is the thin imperative shell around it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .feedback import Feedback
from .knn import Query, distance
from .manifest import Manifest
from .models import (NEIGHBORHOOD_COORDS, REGION_POINTS, Listing, Preferences,
                     haversine_km, region_of)
from .quota import QuotaLedger
from .transit import estimate_commute

# How far a listing may be from the neighborhood centroid and still count
# as "in the area" for the hard gate. Generous on purpose: the gate decides
# acceptability; the soft score handles "closer is better".
GATE_RADIUS_KM = 8.0

# Anti-preferences: a listing whose neighborhood the user has explicitly
# excluded is rejected outright; so is one this close to an excluded centroid
# (catches units labeled with a neighboring name but effectively in the zone).
EXCLUDE_RADIUS_KM = 1.5

# Border tolerance for the borough/city filter: a listing this close to a chosen
# region is kept even if it technically classifies to a neighbor — mirroring the
# neighborhood filter's generous "keep near the area" behavior.
BOROUGH_BUFFER_KM = 3.0

# Drift knob: fraction of the soft score that comes from distance-to-prefs
# vs distance-to-liked-seeds. 1.0 ignores likes entirely.
DEFAULT_ALPHA = 0.6


# --------------------------------------------------------------------------
# Stage 1 — the hard gate
# --------------------------------------------------------------------------

def commute_minutes(listing: Listing, plat: float, plon: float) -> int | None:
    """Offline door-to-door transit estimate to a place, or None if the listing
    has no coordinates (in which case time limits treat it as unknown)."""
    if listing.lat is None or listing.lon is None:
        return None
    return estimate_commute(listing.lat, listing.lon, plat, plon).minutes


def _commute_within(plat: float, plon: float, max_min: float) -> Callable[[Listing], bool]:
    def keep(l: Listing) -> bool:
        m = commute_minutes(l, plat, plon)
        return m is None or m <= max_min   # unknown commute ≠ too far
    return keep


def excluded_predicate(prefs: Preferences) -> Callable[[Listing], bool] | None:
    """A predicate that is False for listings the user has anti-preferenced.
    Returns None when no exclusions are set (so callers can skip the check).
    Matches by neighborhood name AND by proximity to the excluded centroid, so
    'no East Village' also catches listings just over its border."""
    excluded = {n.strip().lower() for n in prefs.exclude_neighborhoods if n.strip()}
    if not excluded:
        return None
    ex_coords = [NEIGHBORHOOD_COORDS[n] for n in excluded if n in NEIGHBORHOOD_COORDS]

    def keep(l: Listing) -> bool:
        if l.neighborhood and l.neighborhood.strip().lower() in excluded:
            return False
        if l.lat is not None and l.lon is not None:
            if any(haversine_km(elat, elon, l.lat, l.lon) <= EXCLUDE_RADIUS_KM
                   for elat, elon in ex_coords):
                return False
        return True

    return keep


def acceptable_rules(prefs: Preferences) -> list[tuple[str, Callable[[Listing], bool]]]:
    """The acceptable region as a declarative list of (name, predicate).
    A listing must pass EVERY rule. Rules that don't apply (no floor set,
    no neighborhoods chosen) simply aren't in the list."""
    price_cap = prefs.max_price * (1 + prefs.price_stretch)
    wanted_amenities = {a.lower() for a in prefs.amenities}

    # Guard against None: a malformed stored record (price/beds missing) must
    # not raise mid-gate and blank the whole result set. Unknown ≠ disqualified.
    rules: list[tuple[str, Callable[[Listing], bool]]] = [
        ("price_cap", lambda l: l.price is None or l.price <= price_cap),
        ("beds_floor", lambda l: l.beds is None or l.beds >= prefs.beds),
    ]
    # up-front borough/city filter (before finer neighborhood prefs). Kept
    # deliberately parallel to the neighborhood filter below: it KEEPS a listing
    # when its coords are unknown, when it classifies to a chosen region, or when
    # it's within a small border buffer of one (so a place right on the
    # Brooklyn/Queens line isn't dropped) — generous, unknown ≠ elsewhere.
    if prefs.boroughs:
        wanted_regions = {b.strip().lower() for b in prefs.boroughs if b.strip()}
        wanted_pts = [(la, lo) for reg, la, lo in REGION_POINTS
                      if reg.lower() in wanted_regions]

        def in_region(l: Listing) -> bool:
            if l.lat is None or l.lon is None:
                return True
            r = region_of(l.lat, l.lon)
            if (not r) or r.lower() in wanted_regions:
                return True
            return any(haversine_km(la, lo, l.lat, l.lon) <= BOROUGH_BUFFER_KM
                       for la, lo in wanted_pts)
        rules.append(("borough", in_region))
    if prefs.min_price:
        floor = prefs.min_price * 0.85  # small slack below the floor
        rules.append(("price_floor", lambda l: l.price is None or l.price >= floor))
    if prefs.min_sqft:
        # unknown sqft is NOT a violation — scrapers often omit it
        rules.append(("sqft_floor",
                      lambda l: l.sqft is None or l.sqft >= prefs.min_sqft))
    hood_coords = [NEIGHBORHOOD_COORDS[n.strip().lower()] for n in prefs.neighborhoods
                   if n.strip().lower() in NEIGHBORHOOD_COORDS]
    if hood_coords:
        # "in the area" = within the radius of ANY wanted neighborhood, not the
        # centroid of all of them (else two spread-out picks exclude both)
        rules.append(("location",
                      lambda l: l.lat is None or l.lon is None or
                      any(haversine_km(la, lo, l.lat, l.lon) <= GATE_RADIUS_KM
                          for la, lo in hood_coords)))
    if wanted_amenities:
        # unknown amenities (empty list) ≠ absent amenities; only gate
        # listings that DO report amenities and are missing must-haves
        rules.append(("amenities",
                      lambda l: not l.amenities or
                      wanted_amenities <= {a.lower() for a in l.amenities}))
    keep = excluded_predicate(prefs)
    if keep:
        rules.append(("not_excluded", keep))
    # hard commute-time limits: a listing must be within max_min transit minutes
    # of each place marked "hard" (unknown commute = kept, unknown ≠ too far)
    for place in prefs.places:
        if (place.get("limit") == "hard" and place.get("max_min")
                and place.get("lat") is not None and place.get("lon") is not None):
            rules.append((f"commute<{place.get('name', 'place')}",
                          _commute_within(place["lat"], place["lon"],
                                          float(place["max_min"]))))
    return rules


def gate(prefs: Preferences, listings: list[Listing]) -> list[Listing]:
    rules = acceptable_rules(prefs)
    return [l for l in listings if all(rule(l) for _, rule in rules)]


def gate_breakdown(prefs: Preferences, listings: list[Listing]) -> dict:
    """Diagnostic: how many listings each hard rule eliminates (counted
    independently, so totals can overlap). Lets the UI explain an empty result
    set — 'all 900 are over your price cap' — instead of a blank screen."""
    rules = acceptable_rules(prefs)
    return {name: sum(1 for l in listings if not rule(l)) for name, rule in rules}


# --------------------------------------------------------------------------
# Stage 2 — the soft score (only ever applied inside the gate)
# --------------------------------------------------------------------------

@dataclass
class Ranked:
    """One ranked result with its full score breakdown, so any UI can show
    not just the order but WHY: per-feature distances (parts) and how much
    of the blended score came from preferences vs liked seeds."""
    listing: Listing
    score: float
    parts: dict[str, float]  # per-feature distance vs preferences
    d_pref: float            # distance to the preference query
    d_seed: float | None     # min distance to liked seeds (None if no likes)


def rank(prefs: Preferences, listings: list[Listing], feedback: Feedback,
         alpha: float = DEFAULT_ALPHA, weights: dict[str, float] | None = None,
         k: int | None = None, priority: dict[str, float] | None = None) -> list[Ranked]:
    """Two-stage ranking: hard gate, then blended KNN score.

    score = alpha * dist_to_prefs + (1 - alpha) * min(dist_to_liked_seeds)

    Liked/hidden listings themselves are excluded from results (you already
    know about them); likes act only as seeds.

    `priority` is an optional {listing_id: penalty} map (penalty in distance
    units, ≥0). It's ADDED to the score only for ordering and top-k selection —
    the stored Ranked.score (and thus the displayed match %) is left untouched.
    This is how verification priority nudges verified listings up without
    distorting how well a listing actually matches your preferences.
    """
    k = k or prefs.k
    pri = priority or {}
    known = set(feedback.liked) | set(feedback.hidden)
    pool = [l for l in gate(prefs, listings) if l.id not in known]

    pref_q = Query.from_preferences(prefs)
    seeds = [Query.from_listing(l) for l in listings if l.id in feedback.liked]

    def score(l: Listing) -> Ranked:
        d_pref, parts = distance(pref_q, l, weights, explain=True)
        if not seeds:
            return Ranked(l, d_pref, parts, d_pref, None)
        d_seed = min(distance(s, l, weights) for s in seeds)
        return Ranked(l, alpha * d_pref + (1 - alpha) * d_seed, parts, d_pref, d_seed)

    return sorted((score(l) for l in pool),
                  key=lambda r: r.score + pri.get(r.listing.id, 0.0))[:k]


# --------------------------------------------------------------------------
# Fetch planning (Milestone 4 wiring)
# --------------------------------------------------------------------------

@dataclass
class FetchDecision:
    provider: str
    action: str   # "fetch" | "skip"
    reason: str


def plan_fetch(prefs: Preferences, sources: list[str], manifest: Manifest,
               quota: QuotaLedger, coverage_ttl_s: float,
               ignore_coverage: bool = False) -> list[FetchDecision]:
    """Decide per provider whether the network is needed. Order of checks:
    1. a fresh covering fetch exists → its listings are already in the store
    2. quota refuses → skip (never silently burn the monthly budget)
    3. otherwise fetch

    `ignore_coverage` overrides step 1. The caller sets it when the store has
    no *active* listings to show — otherwise a covering-fetch record whose
    listings have since aged past the active window would keep suppressing the
    fetch that would refresh them, leaving the app stuck showing nothing.
    """
    decisions = []
    for name in sources:
        covering = None if ignore_coverage else manifest.latest_covering(
            name, prefs, coverage_ttl_s)
        if covering:
            age_h = max(0, int(time.time() - covering["fetched_at"]) // 3600)
            decisions.append(FetchDecision(
                name, "skip",
                f"covered by fetch {age_h}h ago ({covering['listings_found']} listings already in store)"))
            continue
        allowed, why = quota.allow(name)
        if not allowed:
            decisions.append(FetchDecision(name, "skip", why))
            continue
        decisions.append(FetchDecision(name, "fetch", why or "no covering fetch on record"))
    return decisions
