"""Data models for listings and search preferences."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Local copy so models has no dependency on knn (which imports models)."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# Rough centroids for common NYC neighborhoods (lat, lon).
# Used to geocode preferences and listings that lack coordinates.
NEIGHBORHOOD_COORDS: dict[str, tuple[float, float]] = {
    # Manhattan
    "financial district": (40.7075, -74.0113),
    "tribeca": (40.7163, -74.0086),
    "soho": (40.7233, -74.0030),
    "lower east side": (40.7150, -73.9843),
    "east village": (40.7265, -73.9815),
    "west village": (40.7358, -74.0036),
    "greenwich village": (40.7336, -73.9992),
    "chelsea": (40.7465, -74.0014),
    "gramercy": (40.7368, -73.9845),
    "murray hill": (40.7478, -73.9756),
    "midtown": (40.7549, -73.9840),
    "hell's kitchen": (40.7638, -73.9918),
    "upper east side": (40.7736, -73.9566),
    "upper west side": (40.7870, -73.9754),
    "harlem": (40.8116, -73.9465),
    "east harlem": (40.7957, -73.9389),
    "washington heights": (40.8417, -73.9394),
    "inwood": (40.8677, -73.9212),
    # Brooklyn
    "williamsburg": (40.7081, -73.9571),
    "greenpoint": (40.7245, -73.9440),
    "bushwick": (40.6944, -73.9213),
    "bedford-stuyvesant": (40.6872, -73.9418),
    "bed-stuy": (40.6872, -73.9418),
    "crown heights": (40.6694, -73.9422),
    "prospect heights": (40.6774, -73.9668),
    "park slope": (40.6710, -73.9814),
    "gowanus": (40.6734, -73.9899),
    "carroll gardens": (40.6795, -73.9991),
    "cobble hill": (40.6862, -73.9962),
    "boerum hill": (40.6848, -73.9843),
    "brooklyn heights": (40.6959, -73.9936),
    "dumbo": (40.7033, -73.9881),
    "fort greene": (40.6892, -73.9740),
    "clinton hill": (40.6897, -73.9661),
    "downtown brooklyn": (40.6930, -73.9857),
    "sunset park": (40.6454, -74.0121),
    "bay ridge": (40.6262, -74.0299),
    "flatbush": (40.6415, -73.9594),
    "ditmas park": (40.6415, -73.9666),
    "ridgewood": (40.7043, -73.9018),
    # Queens
    "astoria": (40.7644, -73.9235),
    "long island city": (40.7447, -73.9485),
    "sunnyside": (40.7434, -73.9196),
    "woodside": (40.7454, -73.9053),
    "jackson heights": (40.7557, -73.8831),
    "forest hills": (40.7185, -73.8458),
    "flushing": (40.7675, -73.8331),
    "rego park": (40.7264, -73.8625),
    # Bronx
    "mott haven": (40.8091, -73.9229),
    "riverdale": (40.8898, -73.9122),
    "fordham": (40.8592, -73.8986),
    "concourse": (40.8317, -73.9222),
}


def _compute_radii() -> dict[str, float]:
    """Approximate each neighborhood as a DISK, not a point, so a listing that's
    inside the neighborhood but off-center still counts as "in the area".

    Radius = half the distance to the nearest other neighborhood centroid
    (so adjacent disks meet around the midpoint — a rough Voronoi tiling),
    clamped to a sane [0.7, 2.5] km. Dense Manhattan gets small disks; sparse
    outer-borough neighborhoods get larger ones — derived from the data we
    already have, no boundary dataset needed."""
    radii: dict[str, float] = {}
    items = list(NEIGHBORHOOD_COORDS.items())
    for name, (la, lo) in items:
        nearest = min((_haversine_km(la, lo, ola, olo)
                       for other, (ola, olo) in items if other != name), default=3.0)
        radii[name] = max(0.7, min(2.5, nearest * 0.5))
    return radii


# name -> disk radius (km) representing the neighborhood's extent
NEIGHBORHOOD_RADIUS = _compute_radii()

# Optional PRECISE boundaries: {neighborhood: [[lat, lon], ...]} — the outer ring
# of the NYC NTA polygon that contains the neighborhood's centroid. Generated on
# demand by `scripts/build_neighborhoods.py` (free, from NYC Open Data) and NOT
# required: when the file is absent, or a neighborhood has no polygon, location
# falls back to the disk model above. So the app runs identically out of the box
# and simply gets more accurate boundaries if the file is present.
_POLY_FILE = Path(__file__).with_name("neighborhoods.json")
NEIGHBORHOOD_POLYGONS: dict[str, list] = {}
if _POLY_FILE.exists():
    try:
        NEIGHBORHOOD_POLYGONS = {k.strip().lower(): v
                                 for k, v in json.loads(_POLY_FILE.read_text()).items()
                                 if isinstance(v, list) and len(v) >= 3}
    except (json.JSONDecodeError, OSError, TypeError):
        NEIGHBORHOOD_POLYGONS = {}


def _point_in_ring(lat: float, lon: float, ring: list) -> bool:
    """Ray-casting point-in-polygon over a ring of [lat, lon] vertices."""
    inside, n, j = False, len(ring), len(ring) - 1
    for i in range(n):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if ((yi > lat) != (yj > lat)) and \
                (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _dist_to_ring_km(lat: float, lon: float, ring: list) -> float:
    """Shortest distance (km) from a point to a polygon's boundary, via a local
    planar projection (accurate to ~metres at neighborhood scale, and fast)."""
    kx = 111.32 * math.cos(math.radians(lat))   # km per ° lon at this latitude
    ky = 110.57                                  # km per ° lat
    px, py = lon * kx, lat * ky
    best = float("inf")
    n = len(ring)
    for i in range(n):
        y1, x1 = ring[i]
        y2, x2 = ring[(i + 1) % n]
        ax, ay, bx, by = x1 * kx, y1 * ky, x2 * kx, y2 * ky
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            d = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
            d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        best = min(best, d)
    return best


def area_edge_km(lat: float, lon: float, neighborhoods) -> Optional[float]:
    """Distance (km) from a point to the nearest wanted neighborhood's EDGE —
    0.0 when the point is INSIDE one of them. This is what "how far from your
    area" should mean: a listing in the neighborhood is at distance 0, not
    "1.4 km from the center".

    Uses the precise NTA polygon when available (inside → 0, else distance to the
    boundary); otherwise the disk approximation. None if no known neighborhood."""
    best = None
    for n in neighborhoods:
        key = n.strip().lower()
        poly = NEIGHBORHOOD_POLYGONS.get(key)
        if poly:
            edge = 0.0 if _point_in_ring(lat, lon, poly) else _dist_to_ring_km(lat, lon, poly)
        elif key in NEIGHBORHOOD_COORDS:
            cla, clo = NEIGHBORHOOD_COORDS[key]
            edge = max(0.0, _haversine_km(cla, clo, lat, lon) - NEIGHBORHOOD_RADIUS.get(key, 1.2))
        else:
            continue
        best = edge if best is None else min(best, edge)
    return best


# Coarser than neighborhoods: the borough / city a listing sits in. Jersey City
# and Hoboken are included because PATH puts them in commuting range. Used as an
# up-front "where" filter before the finer neighborhood preferences.
REGIONS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island",
           "Jersey City", "Hoboken"]

# Which borough/city each known neighborhood belongs to — lets us classify a
# listing by its NEAREST reference point (many points per borough), which is far
# more accurate near the waterfront than a single centroid per borough.
_NB_REGION = {
    "Manhattan": ["financial district", "tribeca", "soho", "lower east side",
                  "east village", "west village", "greenwich village", "chelsea",
                  "gramercy", "murray hill", "midtown", "hell's kitchen",
                  "upper east side", "upper west side", "harlem", "east harlem",
                  "washington heights", "inwood"],
    "Brooklyn": ["williamsburg", "greenpoint", "bushwick", "bedford-stuyvesant",
                 "bed-stuy", "crown heights", "prospect heights", "park slope",
                 "gowanus", "carroll gardens", "cobble hill", "boerum hill",
                 "brooklyn heights", "dumbo", "fort greene", "clinton hill",
                 "downtown brooklyn", "sunset park", "bay ridge", "flatbush",
                 "ditmas park"],
    "Queens": ["ridgewood", "astoria", "long island city", "sunnyside", "woodside",
               "jackson heights", "forest hills", "flushing", "rego park"],
    "Bronx": ["mott haven", "riverdale", "fordham", "concourse"],
}
# (region, lat, lon) reference points: every known neighborhood, plus anchors
# for boroughs/cities that have no neighborhoods in the table above.
REGION_POINTS: list[tuple[str, float, float]] = [
    (region, *NEIGHBORHOOD_COORDS[n])
    for region, names in _NB_REGION.items() for n in names
    if n in NEIGHBORHOOD_COORDS
] + [
    ("Staten Island", 40.5795, -74.1502), ("Staten Island", 40.6300, -74.0900),
    ("Jersey City", 40.7178, -74.0431), ("Jersey City", 40.7050, -74.0680),
    ("Hoboken", 40.7439, -74.0324),
]


# neighborhood -> borough/city (for the UI: selecting boroughs filters which
# neighborhoods are offered)
NEIGHBORHOOD_REGION = {n: region for region, names in _NB_REGION.items()
                       for n in names if n in NEIGHBORHOOD_COORDS}


def region_of(lat: float, lon: float, max_deg: float = 0.30) -> str:
    """Borough / city for a coordinate, by nearest reference point; '' if the
    point isn't within ~33 km of any (i.e. outside the NYC/NJ metro we cover)."""
    best, best_d = "", max_deg ** 2
    for region, rlat, rlon in REGION_POINTS:
        d = (rlat - lat) ** 2 + (rlon - lon) ** 2
        if d < best_d:
            best, best_d = region, d
    return best


def nearest_neighborhood(lat: float, lon: float, max_deg: float = 0.02) -> str:
    """Reverse-map coordinates to the closest known neighborhood centroid
    (≈2 km cutoff). Returns '' if nothing is close enough."""
    best, best_d = "", max_deg ** 2
    for name, (nlat, nlon) in NEIGHBORHOOD_COORDS.items():
        d = (nlat - lat) ** 2 + (nlon - lon) ** 2
        if d < best_d:
            best, best_d = name.title(), d
    return best


AMENITIES = [
    "laundry_in_unit",
    "laundry_in_building",
    "dishwasher",
    "doorman",
    "elevator",
    "gym",
    "outdoor_space",
    "roof_deck",
    "pets_allowed",
    "no_fee",
    "central_air",
    "parking",             # garage/parking (from RentCast property records)
    "storage",
    "income_restricted",   # affordable / income-capped (e.g. Housing Connect)
]


@dataclass
class Listing:
    id: str
    source: str                    # provider name, e.g. "rentcast", "housingconnect"
    url: str
    address: str
    neighborhood: str
    price: int                     # monthly rent, USD
    beds: float                    # 0 = studio
    baths: float
    sqft: Optional[int] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    amenities: list[str] = field(default_factory=list)
    # Three-tier amenity model (see docs/METHOD.md):
    #   amenities          — reported by the listing source (the complete set;
    #                        this is what the hard gate reasons about)
    #   derived_amenities  — high-confidence FACTS from authoritative lookups
    #                        (elevator from PLUTO class; central_air/parking from
    #                        RentCast property records). Credit the soft score,
    #                        gate-safe (never flip the gate to strict mode).
    #   inferred_amenities — LOW-confidence GUESSES from building size/age
    #                        (e.g. "likely doorman" for a 20-floor tower).
    #                        Display-only: never gate, never score.
    derived_amenities: list[str] = field(default_factory=list)
    inferred_amenities: list[str] = field(default_factory=list)
    description: str = ""

    def __post_init__(self):
        # Heal list fields that were persisted as null (older records, or a
        # merge that fell back to a missing key) — a None here is not iterable
        # and would crash display/scoring. Unknown amenities = empty list.
        if self.amenities is None:
            self.amenities = []
        if self.derived_amenities is None:
            self.derived_amenities = []
        if self.inferred_amenities is None:
            self.inferred_amenities = []
        # Fill coordinates from neighborhood centroid only when BOTH are
        # missing — never overwrite a provided coordinate.
        if self.lat is None and self.lon is None and self.neighborhood:
            coords = NEIGHBORHOOD_COORDS.get(self.neighborhood.strip().lower())
            if coords:
                self.lat, self.lon = coords

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Listing":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def validate(self) -> bool:
        """Sanity checks — reject garbage rows from scrapers."""
        if not self.id or not self.address:
            return False
        if not (500 <= self.price <= 60_000):
            return False
        if not (0 <= self.beds <= 8) or not (0 <= self.baths <= 8):
            return False
        if self.sqft is not None and not (100 <= self.sqft <= 12_000):
            return False
        if self.lat is not None and not (40.4 <= self.lat <= 41.0):
            return False
        if self.lon is not None and not (-74.3 <= self.lon <= -73.6):
            return False
        return True


@dataclass
class Preferences:
    max_price: int = 4000
    min_price: int = 0
    beds: float = 1                # desired bedrooms (0 = studio)
    baths: float = 1
    # Up-front "where" filter: only these boroughs/cities (empty = anywhere).
    # Coarser than neighborhoods and applied first (a hard gate).
    boroughs: list[str] = field(default_factory=list)
    neighborhoods: list[str] = field(default_factory=list)
    # Hard exclusions ("regardless of anything else"): listings in — or right
    # next to — these neighborhoods are dropped before ranking, no matter how
    # well they'd otherwise score. Enforced in engine.gate and similar search.
    exclude_neighborhoods: list[str] = field(default_factory=list)
    amenities: list[str] = field(default_factory=list)
    min_sqft: Optional[int] = None
    # How far past hard limits KNN may explore (this is the whole point:
    # find near-misses the strict filters would hide).
    price_stretch: float = 0.15    # allow results up to 15% over max_price
    k: int = 15                    # number of neighbors to return
    # Max listings to pull in one fetch (RentCast returns ≤500/page, so ≤500 is
    # one API call, more is two). Capped at 1000.
    fetch_limit: int = 1000
    # Listings unseen by any fetch for this many days are treated as rented:
    # excluded from ranking, kept in the store as market reference.
    active_window_days: int = 7
    # Which providers to fetch from. Empty = all registered providers.
    # Validated against the provider registry in cli/web (models can't import
    # providers — that would be circular).
    sources: list[str] = field(default_factory=list)
    # How strongly verification status reorders results: "off" (ignore),
    # "gentle" (default nudge), or "strong" (push unverified/flagged down hard).
    verify_priority: str = "gentle"
    # Important places (work, roommate's office, friends' apartments) used
    # for per-listing commute estimates and transit-directions links.
    # Shape: {"name": str, "address": str, "lat": float|None, "lon": float|None}
    # — lat/lon filled by geocoding when a place is saved in the web UI.
    places: list[dict] = field(default_factory=list)
    # A drawn map area saved as the location ("Use in preferences"). When set it
    # REPLACES boroughs/neighborhoods as the where-filter — the precise shape,
    # not a name expanded back to an 8 km radius. Shape: {"bbox":[s,w,n,e]} or
    # {"poly":[[lat,lon],...]}. None = use boroughs/neighborhoods instead.
    area: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Preferences":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def target_coords(self) -> Optional[tuple[float, float]]:
        """Centroid of chosen neighborhoods."""
        pts = [
            NEIGHBORHOOD_COORDS[n.strip().lower()]
            for n in self.neighborhoods
            if n.strip().lower() in NEIGHBORHOOD_COORDS
        ]
        if not pts:
            return None
        return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
