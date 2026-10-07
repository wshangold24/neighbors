"""Weighted K-nearest-neighbors over apartment listings.

Why KNN instead of hard filters: strict filters hide near-misses ($3,550 when
your cap is $3,500; Greenpoint when you searched Williamsburg). Distance-based
ranking surfaces them, ordered by how close they are to what you asked for —
the same effect as a site's "similar listings" module.

Feature space (all components scaled to ~[0, 1] before weighting):
  price       directional (preference queries): free anywhere within budget,
              hard penalty over max, mild penalty under the floor;
              symmetric (seed queries): |Δprice| + over-budget penalty
  beds        |Δ| / 2, asymmetric: fewer than wanted costs extra
  baths       fewer than wanted penalized; extra baths free in directional mode
  location    km to the nearest wanted-neighborhood EDGE / 8 (0 if inside it;
              each neighborhood is a disk, not a point)
  sqft        below min penalized; extra sqft free in directional mode
  amenities   1 − (matched wanted amenities / wanted amenities)

Pure stdlib — no numpy/sklearn needed at this scale (linear scan).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import Listing, Preferences, area_edge_km, haversine_km

DEFAULT_WEIGHTS = {
    "price": 3.0,
    "beds": 2.5,
    "baths": 1.0,
    "location": 2.0,
    "sqft": 0.8,
    "amenities": 1.5,
}


# Part for a feature whose value is missing on either side: a mild penalty,
# never worst-case (invariant U — unknown ≠ absent).
UNKNOWN_PART = 0.4


@dataclass
class Query:
    """A point in feature space. Built from Preferences or a seed Listing.
    price/beds/baths may be None only for a seed listing with missing data."""
    price: float | None
    beds: float | None
    baths: float | None
    lat: float | None
    lon: float | None
    sqft: float | None
    amenities: set[str] = field(default_factory=set)
    # Wanted neighborhood names. Location distance is to the nearest one's AREA
    # edge (0 if the listing is inside it) — precise NTA polygon if available,
    # else a disk (see models.area_edge_km). Empty for seed queries, which fall
    # back to lat/lon below.
    neighborhoods: list[str] = field(default_factory=list)
    max_price: float | None = None   # soft budget line (asymmetric penalty)
    min_price: float | None = None   # optional floor (directional mode only)
    # directional=True (preference queries): limits are one-sided —
    # cheaper than max, more baths, and more sqft are all FREE, not "far".
    # directional=False (seed queries): differences are symmetric, because
    # "similar to this listing" means similar in both directions.
    directional: bool = False

    @classmethod
    def from_preferences(cls, p: Preferences) -> "Query":
        coords = p.target_coords()
        # Aim slightly below max so "at budget" isn't treated as far away.
        target_price = p.max_price * 0.92 if not p.min_price else (p.min_price + p.max_price) / 2
        return cls(
            price=target_price,
            beds=p.beds,
            baths=p.baths,
            lat=coords[0] if coords else None,
            lon=coords[1] if coords else None,
            sqft=float(p.min_sqft) if p.min_sqft else None,
            amenities={a.lower() for a in p.amenities},
            neighborhoods=list(p.neighborhoods),
            max_price=float(p.max_price),
            min_price=float(p.min_price) if p.min_price else None,
            directional=True,
        )

    @classmethod
    def from_listing(cls, l: Listing, max_price: float | None = None) -> "Query":
        return cls(
            price=float(l.price) if l.price is not None else None,
            beds=l.beds,
            baths=l.baths,
            lat=l.lat,
            lon=l.lon,
            sqft=float(l.sqft) if l.sqft else None,
            amenities={a.lower() for a in l.amenities},
            max_price=max_price,
        )


def distance(q: Query, l: Listing, weights: dict[str, float] | None = None,
             explain: bool = False):
    """Weighted distance between query point and listing.
    Returns float, or (float, dict of per-feature contributions) if explain."""
    w = weights or DEFAULT_WEIGHTS
    parts: dict[str, float] = {}

    # price
    if l.price is None or q.price is None:
        parts["price"] = UNKNOWN_PART
    else:
        price_scale = max(q.price * 0.35, 500.0)
        if q.directional:
            # One-sided: anywhere within [min_price, max_price] costs nothing.
            # Over budget is penalized hard; under the floor mildly.
            d_price = 0.0
            if q.max_price and l.price > q.max_price:
                d_price = 2.5 * (l.price - q.max_price) / price_scale
            elif q.min_price and l.price < q.min_price:
                d_price = (q.min_price - l.price) / price_scale
        else:
            # Symmetric (seed mode) with extra asymmetric penalty above budget.
            d_price = abs(l.price - q.price) / price_scale
            if q.max_price and l.price > q.max_price:
                d_price += 2.0 * (l.price - q.max_price) / price_scale
        parts["price"] = min(d_price, 3.0)

    # beds: wanting 2 and getting 1 hurts more than getting 3
    if l.beds is None or q.beds is None:
        parts["beds"] = UNKNOWN_PART
    else:
        d_beds = (q.beds - l.beds) if l.beds < q.beds else 0.5 * (l.beds - q.beds)
        parts["beds"] = min(d_beds / 2.0, 1.5)

    # baths: fewer than wanted always hurts; extra baths are free in
    # directional mode (a bathroom floor, not a target)
    if l.baths is None or q.baths is None:
        parts["baths"] = UNKNOWN_PART
    else:
        if l.baths < q.baths:
            d_baths = q.baths - l.baths
        else:
            d_baths = 0.0 if q.directional else 0.3 * (l.baths - q.baths)
        parts["baths"] = min(d_baths / 2.0, 1.0)

    # location — distance to the nearest wanted neighborhood's AREA edge (0 if
    # the listing is inside it), using precise polygons when available else disks
    near = area_edge_km(l.lat, l.lon, q.neighborhoods) if (
        q.neighborhoods and l.lat is not None and l.lon is not None) else None
    if near is not None:
        parts["location"] = min(near / 8.0, 1.5)
    elif q.lat is not None and l.lat is not None and q.lon is not None and l.lon is not None:
        parts["location"] = min(haversine_km(q.lat, q.lon, l.lat, l.lon) / 8.0, 1.5)
    else:
        parts["location"] = UNKNOWN_PART

    # sqft (only if both sides known); min_sqft is a floor in directional
    # mode, so bigger is free
    if q.sqft and l.sqft:
        if l.sqft < q.sqft:
            d_sqft = q.sqft - l.sqft
        else:
            d_sqft = 0.0 if q.directional else 0.3 * (l.sqft - q.sqft)
        parts["sqft"] = min(d_sqft / 600.0, 1.0)
    elif q.sqft:
        parts["sqft"] = 0.3
    else:
        parts["sqft"] = 0.0

    # amenities: fraction of wanted amenities missing — BUT a listing that
    # reports no amenity data at all is "unknown", not "missing everything".
    # Most rental sources don't publish structured renter amenities, so scoring
    # every such listing as a total miss (1.0) would uniformly crater match %
    # and make the amenities row always red. Instead give unknowns a mild
    # penalty — consistent with the hard gate (unknown ≠ absent) and with how
    # sqft/location treat missing data. Only listings that DO report amenities
    # are penalized for a wanted one being genuinely absent.
    if q.amenities:
        provider = {a.lower() for a in l.amenities}
        derived = {a.lower() for a in l.derived_amenities}
        if provider:
            # the source reported amenities → absence is meaningful; a derived
            # amenity (e.g. elevator from building records) also counts as present
            matched = len(q.amenities & (provider | derived))
            parts["amenities"] = 1.0 - matched / len(q.amenities)
        else:
            # no source amenity data → unknown (mild), but each wanted amenity we
            # can positively confirm from building records pulls it toward 0
            confirmed = len(q.amenities & derived)
            parts["amenities"] = UNKNOWN_PART * (1.0 - confirmed / len(q.amenities))
    else:
        parts["amenities"] = 0.0

    total = math.sqrt(sum(w[k] * parts[k] ** 2 for k in parts))
    return (total, parts) if explain else total


def knn(query: Query, listings: list[Listing], k: int = 15,
        weights: dict[str, float] | None = None,
        exclude_ids: set[str] | None = None) -> list[tuple[Listing, float, dict]]:
    """Return top-k (listing, distance, per-feature parts), nearest first."""
    exclude_ids = exclude_ids or set()
    scored = [(l, *distance(query, l, weights, explain=True))
              for l in listings if l.id not in exclude_ids]
    scored.sort(key=lambda t: t[1])
    return scored[:k]


def match_pct(dist: float) -> int:
    """Human-friendly 0–100 similarity score from a distance."""
    return max(0, min(100, round(100 * math.exp(-dist / 1.4))))
