#!/usr/bin/env python3
"""Generate precise neighborhood boundaries for the compatibility score.

Run this ONCE (it needs network) to produce `aptfinder/neighborhoods.json`:

    python scripts/build_neighborhoods.py

For every neighborhood the app knows about (`NEIGHBORHOOD_COORDS`), it asks NYC
Open Data which **NTA (Neighborhood Tabulation Area)** polygon contains that
neighborhood's centroid, simplifies the polygon, and writes its outer ring as
`{neighborhood: [[lat, lon], ...]}`. At runtime `models.area_edge_km` uses these
polygons (inside → location score 0; else distance to the boundary) and falls
back to the disk approximation for any neighborhood without one — so this file
is entirely optional and never required for the app to run.

Data: NYC Open Data "2020 Neighborhood Tabulation Areas" (dataset 9nt8-h7nd),
free and open. Stdlib only (urllib + json) — nothing to install. An optional
free app token (NYC_APP_TOKEN env) lifts the rate limit.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from aptfinder.models import NEIGHBORHOOD_COORDS  # noqa: E402

DATASET = "9nt8-h7nd"
ENDPOINT = f"https://data.cityofnewyork.us/resource/{DATASET}.json"
SIMPLIFY_DEG = 0.0006          # ~66 m server-side simplification
RDP_EPS_DEG = 0.00025          # extra client-side thinning
OUT = Path(__file__).resolve().parent.parent / "aptfinder" / "neighborhoods.json"


def _get(lat: float, lon: float) -> dict | None:
    params = {
        "$select": f"simplify(the_geom,{SIMPLIFY_DEG}) AS geom",
        "$where": f"intersects(the_geom,'POINT({lon} {lat})')",
        "$limit": 1,
    }
    url = ENDPOINT + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    token = os.environ.get("NYC_APP_TOKEN", "").strip()
    if token:
        req.add_header("X-App-Token", token)
    with urllib.request.urlopen(req, timeout=30) as r:
        rows = json.loads(r.read())
    return (rows[0].get("geom") if rows else None)


def _largest_outer_ring(geom: dict) -> list | None:
    """Outer ring (as [[lon,lat],...]) of the biggest polygon in the geometry."""
    if not geom:
        return None
    if geom["type"] == "Polygon":
        return geom["coordinates"][0]
    if geom["type"] == "MultiPolygon":
        rings = [poly[0] for poly in geom["coordinates"]]
        return max(rings, key=len) if rings else None
    return None


def _rdp(pts: list, eps: float) -> list:
    """Ramer–Douglas–Peucker on [lat,lon] points (planar; good enough here)."""
    if len(pts) < 3:
        return pts
    (y1, x1), (y2, x2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    denom = (dx * dx + dy * dy) ** 0.5 or 1e-12
    idx, dmax = 0, 0.0
    for i in range(1, len(pts) - 1):
        y0, x0 = pts[i]
        d = abs(dy * (x0 - x1) - dx * (y0 - y1)) / denom
        if d > dmax:
            idx, dmax = i, d
    if dmax <= eps:
        return [pts[0], pts[-1]]
    return _rdp(pts[:idx + 1], eps)[:-1] + _rdp(pts[idx:], eps)


def main() -> None:
    out: dict[str, list] = {}
    total = len(NEIGHBORHOOD_COORDS)
    for i, (name, (lat, lon)) in enumerate(sorted(NEIGHBORHOOD_COORDS.items()), 1):
        try:
            geom = _get(lat, lon)
            ring = _largest_outer_ring(geom)
            if not ring:
                print(f"  [{i}/{total}] {name}: no NTA found — will use disk fallback")
                continue
            latlon = _rdp([[pt[1], pt[0]] for pt in ring], RDP_EPS_DEG)   # [lon,lat]->[lat,lon]
            out[name] = [[round(a, 6), round(b, 6)] for a, b in latlon]
            print(f"  [{i}/{total}] {name}: {len(out[name])} points")
        except Exception as e:                                   # noqa: BLE001
            print(f"  [{i}/{total}] {name}: failed ({e}) — will use disk fallback")
        time.sleep(0.3)                                          # be polite to the API

    OUT.write_text(json.dumps(out, separators=(",", ":")))
    size_kb = OUT.stat().st_size / 1024
    print(f"\nWrote {len(out)}/{total} neighborhood boundaries to {OUT} ({size_kb:.0f} KB)")


if __name__ == "__main__":
    main()
