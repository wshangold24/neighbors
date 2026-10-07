"""Endpoint logic for the web app, independent of HTTP (unit-testable).

The web app is a second imperative shell over the same pure modules the CLI
uses: engine.rank for ordering, Store for listings, Feedback for verdicts.
State is re-read from disk on every call, so the CLI and the web app can be
used interchangeably without restarting either.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Optional

from .engine import DEFAULT_ALPHA, excluded_predicate, gate, gate_breakdown, rank
from .digest import build_digest
from .feedback import Feedback
from .http import BudgetExceeded, SafeSession
from .knn import DEFAULT_WEIGHTS, Query, distance, knn
from .learn import learn_weights, load_weights, save_weights
from .models import (AMENITIES, NEIGHBORHOOD_COORDS, NEIGHBORHOOD_REGION, REGIONS,
                     Listing, Preferences)
from .providers import PROVIDERS
from .providers.rentcast import resolve_api_key
from .providers.socrata import HPD_REGISTRATIONS, PLUTO, resolve_app_token, soda_get
from .quota import QuotaLedger
from .store import Store
from .transit import estimate_commute
from .verify import needs_verify, verify_one, verify_store
from .views import features_to_amenities, listing_card, trust

log = logging.getLogger("neighbors.web")

# Sort-only ranking penalty per trust level (distance units; never changes a
# listing's match %): verified floats to the top of its tier, flagged sinks.
VERIFY_PRIORITY = {"confirmed": 0.0, "unverified": 0.15, "caution": 0.6}
# User-tunable multiplier on that penalty (Preferences.verify_priority).
VERIFY_STRENGTH = {"off": 0.0, "gentle": 1.0, "strong": 2.0}
# Below this many fresh results, backfill from older listings so a fetch that
# only re-confirmed part of the market can't collapse the page to a handful.
MIN_RESULTS = 12
# Amenities confirmable from ToS-clean data, so the preferences picker can
# mark which selections are data-backed:
#   elevator            — PLUTO building class
#   central_air/parking — RentCast property records (on-demand lookup)
#   income_restricted   — Housing Connect
#   no_fee              — Housing Connect (affordable lotteries are fee-free)
SOURCED_AMENITIES = {"elevator", "central_air", "parking", "income_restricted", "no_fee"}
FETCH_LIMIT_MIN, FETCH_LIMIT_MAX = 100, 1000
RENTCAST_PAGE_SIZE = 500
MAX_VERIFY_BATCH = 50
ALL_TIME_DAYS = 10 ** 6   # an "active window" that admits every stored listing

# Type rules for POST /api/prefs. Anything else in a known field is rejected
# up front, so a bad value can never be persisted and break later requests.
_INT_FIELDS = {"max_price", "min_price", "k", "fetch_limit", "active_window_days"}
_FLOAT_FIELDS = {"beds", "baths", "price_stretch"}
_STR_LIST_FIELDS = {"boroughs", "neighborhoods", "exclude_neighborhoods",
                    "amenities", "sources"}

BuildingLookup = Callable[[str, Optional[float], Optional[float]], dict]


class ErrorResponse(dict):
    """An {"error": message} result that also carries its HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(error=message)
        self.status = status


def _is_number(value) -> bool:
    """A real JSON number: int/float, finite, and not a bool."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _finite(value) -> float | None:
    """`value` as a finite float, or None (bools and non-numbers included)."""
    if isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def clamp_fetch_limit(value) -> int:
    f = _finite(value)
    return int(max(FETCH_LIMIT_MIN, min(FETCH_LIMIT_MAX, f or FETCH_LIMIT_MAX)))


def point_in_poly(lat: float, lon: float, poly: list) -> bool:
    """Ray-casting point-in-polygon. `poly` is a list of (lat, lon) vertices."""
    inside, j = False, len(poly) - 1
    for i in range(len(poly)):
        lati, loni = poly[i]
        latj, lonj = poly[j]
        if ((lati > lat) != (latj > lat)) and \
                (lon < (lonj - loni) * (lat - lati) / (latj - lati) + loni):
            inside = not inside
        j = i
    return inside


def parse_bbox(raw) -> tuple | None:
    """[south, west, north, east] → a tuple of 4 finite floats, else None."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    nums = tuple(_finite(x) for x in raw)
    return None if None in nums else nums


def parse_poly(raw) -> list | None:
    """[[lat, lon], ...] with 3+ vertices → a list of float pairs, else None."""
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        return None
    pts = []
    for p in raw:
        if not isinstance(p, (list, tuple)) or len(p) != 2:
            return None
        lat, lon = _finite(p[0]), _finite(p[1])
        if lat is None or lon is None:
            return None
        pts.append((lat, lon))
    return pts


def area_predicate(bbox=None, poly=None) -> Callable[[float, float], bool] | None:
    """A (lat, lon) -> bool test for a drawn search area: the polygon if valid,
    else the bbox if valid, else None. Tolerates junk (e.g. a hand-edited
    preferences file) by ignoring it."""
    pts = parse_poly(poly)
    if pts:
        return lambda la, lo: point_in_poly(la, lo, pts)
    box = parse_bbox(bbox)
    if box:
        s_lat, w_lon, n_lat, e_lon = box
        return lambda la, lo: s_lat <= la <= n_lat and w_lon <= lo <= e_lon
    return None


def _registry_id(value) -> str | None:
    """A BBL/BIN as a plain digit string, else None — the only form ever
    interpolated into a SoQL query."""
    s = str(value or "").strip()
    return s if s.isdigit() else None


def _record(store: Store, listing_id: str) -> dict | None:
    return next((r for r in store.records().values()
                 if r["listing"]["id"] == listing_id), None)


def _validate_pref_fields(fields: dict) -> str | None:
    """Return why `fields` (already restricted to known Preferences fields)
    can't be saved, or None if every value has the right type."""
    for name, value in fields.items():
        if name in _INT_FIELDS or name in _FLOAT_FIELDS:
            if not (_is_number(value) and value >= 0):
                return f"{name} must be a non-negative number"
        elif name == "min_sqft":
            if value is not None and not (_is_number(value) and value >= 0):
                return "min_sqft must be a non-negative number or null"
        elif name in _STR_LIST_FIELDS:
            if not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
                return f"{name} must be a list of strings"
        elif name == "places":
            if not (isinstance(value, list) and all(isinstance(p, dict) for p in value)):
                return "places must be a list of objects"
        elif name == "area":
            if value is not None and not isinstance(value, dict):
                return "area must be an object or null"
    return None


def _place_display(p: dict) -> str:
    """One-line address from segments (or the legacy free-form field)."""
    if p.get("street"):
        return ", ".join(str(x) for x in (p["street"], p.get("city"), p.get("zip")) if x)
    return str(p.get("address") or "")


class Api:
    """All endpoint logic. Each public method returns a JSON-able dict; a
    failure is an ErrorResponse (still a dict) carrying its HTTP status."""

    def __init__(self, app_dir: Path):
        self.dir = Path(app_dir)
        # Feedback and preferences are saved whole, so concurrent requests
        # serialize their read-modify-write; store writers (fetch, verify,
        # enrich) do the same on a separate lock so a slow fetch never blocks
        # a like.
        self._state_lock = threading.Lock()
        self._store_lock = threading.Lock()

    # ---------- local state ----------

    def _read_json(self, name: str) -> dict | None:
        try:
            data = json.loads((self.dir / name).read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _write_json(self, name: str, data: dict) -> None:
        """Atomic write, so a concurrent reader never sees a torn file."""
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{name}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.dir / name)

    def _prefs(self) -> Preferences:
        data = self._read_json("preferences.json")
        if data is not None:
            try:
                return Preferences.from_dict(data)
            except TypeError:
                pass
        return Preferences()

    def _save_prefs(self, fields: dict) -> dict:
        prefs = Preferences.from_dict(fields).to_dict()
        self._write_json("preferences.json", prefs)
        return prefs

    def _store(self) -> Store:
        return Store(self.dir / "store.db")

    def _feedback(self) -> Feedback:
        return Feedback(self.dir / "store.db")

    def _weights(self) -> dict | None:
        return load_weights(self.dir / "weights.json")

    def _rentcast_key_present(self) -> bool:
        """A non-empty saved key file or RENTCAST_API_KEY — the provider's own
        resolve order, so we never ask for a key that's already set."""
        try:
            if (self.dir / "rentcast_key").read_text().strip():
                return True
        except OSError:
            pass
        return bool(os.environ.get("RENTCAST_API_KEY", "").strip())

    # ---------- ranking helpers ----------

    _trust = staticmethod(trust)

    def _cards(self, store: Store, feedback: Feedback, prefs: Preferences):
        """A listing -> JSON card serializer bound to this request's state."""
        recs: dict[str, dict] = {}
        for r in store.records().values():
            recs.setdefault(r["listing"]["id"], r)
        flags = store.annotations()

        def card(l: Listing, dist: float | None = None, parts: dict | None = None,
                 blend: dict | None = None) -> dict:
            return listing_card(l, recs.get(l.id), prefs, feedback, flags.get(l.id, ""),
                                dist, parts, blend)
        return card

    def _visible_ids(self, prefs: Preferences, store: Store) -> set[str]:
        """Ids of listings that can actually surface — active (or, if nothing
        is fresh, the whole store) and passing the hard gate. Verification is
        scoped to this set so it never crawls a whole citywide fetch."""
        active = (store.active(window_days=prefs.active_window_days)
                  or store.active(window_days=ALL_TIME_DAYS))
        return {l.id for l in gate(prefs, active)}

    def _verification_summary(self, prefs: Preferences, store: Store) -> dict:
        """Visible listings by trust level, plus how many still need a (first
        or stale) building check — the count the Verify button acts on."""
        now = time.time()
        visible = self._visible_ids(prefs, store)
        counts = {"confirmed": 0, "caution": 0, "unverified": 0, "pending": 0}
        for rec in store.records().values():
            if rec["listing"]["id"] in visible:
                counts[trust(rec, prefs, now)["level"]] += 1
                if needs_verify(rec, now):
                    counts["pending"] += 1
        return counts

    @staticmethod
    def _place_penalty(l: Listing, prefs: Preferences) -> float:
        """Soft commute limits: each 'soft' place adds up to one unit of sort
        penalty as transit time exceeds its max_min ('hard' ones are gated)."""
        if l.lat is None or l.lon is None:
            return 0.0
        pen = 0.0
        for place in prefs.places:
            if (place.get("limit") == "soft" and place.get("max_min")
                    and place.get("lat") is not None and place.get("lon") is not None):
                m = estimate_commute(l.lat, l.lon, place["lat"], place["lon"]).minutes
                over = m - float(place["max_min"])
                if over > 0:
                    pen += min(over / 20.0, 1.0)   # ~20 min over = a full unit
        return pen

    def _priority_map(self, listings: list[Listing], store: Store,
                      prefs: Preferences) -> dict[str, float]:
        """Per-listing sort-only penalty: verification status (scaled by
        verify_priority) plus soft commute limits."""
        strength = VERIFY_STRENGTH.get(prefs.verify_priority, 1.0)
        recs = {r["listing"]["id"]: r for r in store.records().values()}
        now = time.time()
        out = {}
        for l in listings:
            pen = self._place_penalty(l, prefs)
            if strength:
                level = trust(recs.get(l.id), prefs, now)["level"]
                pen += strength * VERIFY_PRIORITY.get(level, 0.15)
            if pen:
                out[l.id] = pen
        return out

    def _relearn(self, feedback: Feedback) -> None:
        store = self._store()
        liked = [l for l in (store.find(i) for i in feedback.liked) if l]
        learned = learn_weights(self._prefs(), liked)
        if learned:
            save_weights(self.dir / "weights.json", learned)

    # ---------- read endpoints ----------

    def state(self) -> dict:
        prefs, store, feedback = self._prefs(), self._store(), self._feedback()
        learned = self._weights()
        return {
            "prefs": prefs.to_dict(),
            # active ranking weights: the UI orders compatibility rows by
            # importance × deficit
            "weights": learned or DEFAULT_WEIGHTS,
            # canonical choices for the multi-selects — these values feed
            # provider API calls, so the UI must offer only valid ones
            "options": {"regions": list(REGIONS),
                        "neighborhoods": sorted(NEIGHBORHOOD_COORDS),
                        "neighborhood_region": NEIGHBORHOOD_REGION,
                        "amenities": list(AMENITIES),
                        "sourced_amenities": sorted(SOURCED_AMENITIES),
                        "sources": list(PROVIDERS)},
            "store": {"total": len(store),
                      "active": len(store.active(window_days=prefs.active_window_days))},
            "quota": QuotaLedger(self.dir / "store.db").summary(),
            "weights_learned": learned is not None,
            "rentcast_key_set": self._rentcast_key_present(),
            "setup_dismissed": (self.dir / "setup.json").exists(),
            "feedback": {"liked": len(feedback.liked), "hidden": len(feedback.hidden)},
            "verification": self._verification_summary(prefs, store),
        }

    def results(self, alpha: float | None = None,
                bbox: tuple | None = None, poly: list | None = None) -> dict:
        """Ranked results. A drawn area (bbox/poly) replaces the location
        filters for this search only; otherwise a saved area does."""
        prefs, store, feedback = self._prefs(), self._store(), self._feedback()
        alpha = DEFAULT_ALPHA if alpha is None else alpha
        area = area_predicate(bbox, poly)
        if area is None and isinstance(prefs.area, dict):
            area = area_predicate(prefs.area.get("bbox"), prefs.area.get("poly"))
        weights = self._weights()

        def rank_within(listings: list[Listing]):
            p = prefs
            if area:
                listings = [l for l in listings if l.lat is not None
                            and l.lon is not None and area(l.lat, l.lon)]
                p = replace(prefs, boroughs=[], neighborhoods=[])
            ranked = rank(p, listings, feedback, alpha=alpha, weights=weights,
                          priority=self._priority_map(listings, store, p))
            return ranked, p

        # If the fresh set is thin (a fetch re-confirmed only a slice, so most
        # listings just aged out), widen to the whole store — but only adopt
        # it if it shows more, and flag it stale so the UI can say so.
        active = store.active(window_days=prefs.active_window_days)
        ranked, used_prefs = rank_within(active)
        stale = False
        if len(ranked) < MIN_RESULTS and len(store) > len(active):
            wider, wider_prefs = rank_within(store.active(window_days=ALL_TIME_DAYS))
            if len(wider) > len(ranked):
                ranked, used_prefs, stale = wider, wider_prefs, True

        # Nothing to show from a non-empty store: say which hard filter is
        # eliminating everything, so the empty state is actionable.
        diagnostic = None
        if not ranked and len(store):
            everything = store.active(window_days=ALL_TIME_DAYS)
            fails = {k: v for k, v in gate_breakdown(used_prefs, everything).items() if v}
            diagnostic = {"store": len(store), "shown": 0, "fails": fails,
                          "prefs": {"max_price": used_prefs.max_price,
                                    "beds": used_prefs.beds,
                                    "neighborhoods": used_prefs.neighborhoods,
                                    "amenities": used_prefs.amenities}}

        with self._state_lock:
            feedback = self._feedback()
            feedback.mark_seen([r.listing.id for r in ranked])
            feedback.save()
        card = self._cards(store, feedback, used_prefs)
        return {"alpha": alpha, "stale": stale, "diagnostic": diagnostic, "results": [
            card(r.listing, r.score, r.parts,
                 blend=None if r.d_seed is None else {
                     "pref": round(alpha * r.d_pref, 4),
                     "seed": round((1 - alpha) * r.d_seed, 4)})
            for r in ranked]}

    def liked(self) -> dict:
        """The shortlist for the compare view, newest like first. Liked
        listings are excluded from ranked results, so the UI can't build this
        itself. Match % is against preferences, so columns stay comparable;
        listings no longer in the store are skipped."""
        prefs, store, feedback = self._prefs(), self._store(), self._feedback()
        weights = self._weights() or DEFAULT_WEIGHTS
        pref_q = Query.from_preferences(prefs)
        active_ids = {l.id for l in store.active(window_days=prefs.active_window_days)}
        card = self._cards(store, feedback, prefs)
        out = []
        for lid in sorted(feedback.liked, key=feedback.liked.get, reverse=True):
            l = store.find(lid)
            if l is None:
                continue
            d, parts = distance(pref_q, l, weights, explain=True)
            out.append({**card(l, d, parts), "active": l.id in active_ids})
        return {"results": out}

    def similar(self, listing_id: str) -> dict:
        """Seed mode: the nearest neighbors of one listing."""
        prefs, store, feedback = self._prefs(), self._store(), self._feedback()
        seed = store.find(listing_id)
        if seed is None:
            return ErrorResponse(f"unknown listing {listing_id}", 404)
        listings = store.active(window_days=prefs.active_window_days)
        # excluded neighborhoods are hard, even among a liked listing's neighbors
        keep = excluded_predicate(prefs)
        if keep:
            listings = [l for l in listings if keep(l)]
        q = Query.from_listing(seed, max_price=prefs.max_price * (1 + prefs.price_stretch))
        ranked = knn(q, listings, k=prefs.k, weights=self._weights(), exclude_ids={seed.id})
        card = self._cards(store, feedback, prefs)
        return {"seed": card(seed),
                "results": [card(l, d, parts) for l, d, parts in ranked]}

    def digest(self) -> dict:
        """What's new since the digest was last acknowledged (or the past
        week on first run) — the 'what's new' banner."""
        since = _finite((self._read_json("digest.json") or {}).get("acked_at")) or 0.0
        if not since:
            since = time.time() - 7 * 86400
        return build_digest(self._store(), self._feedback(), since)

    def geocode(self, street: str = "", city: str = "", zip_code: str = "") -> dict:
        """Segmented address → location via OpenStreetMap Nominatim (structured
        search, so any city works). Returns {"found": True, "address", "lat",
        "lon"} or found=False with an error. Input guards run before any
        network call; the session rate-limits, caches and identifies itself
        per Nominatim's usage policy."""
        street, city, zip_code = (str(s or "").strip() for s in (street, city, zip_code))
        if len(street) < 5 or not city:
            return {"found": False, "error": "need a street address and a city"}
        session = SafeSession(self.dir / "cache", budget=5, min_interval=1.2)
        params = {"format": "json", "limit": 1, "country": "USA",
                  "street": street, "city": city}
        if zip_code:
            params["postalcode"] = zip_code
        body = session.get(
            "https://nominatim.openstreetmap.org/search", params=params,
            headers={"User-Agent": "neighbors/0.2 (personal local apartment search)"})
        try:
            hit = json.loads(body)[0]
            return {"found": True, "address": hit.get("display_name", street),
                    "lat": float(hit["lat"]), "lon": float(hit["lon"])}
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            log.warning("geocode failed: %s / %s / %s", street, city, zip_code)
            return {"found": False, "error": "no match — check the street number and city"}

    # ---------- write endpoints ----------

    def set_feedback(self, listing_id: str, verdict: str) -> dict:
        if not isinstance(listing_id, str) or not listing_id:
            return ErrorResponse("missing listing id")
        if verdict not in ("like", "hide", "clear"):
            return ErrorResponse(f"unknown verdict '{verdict}'")
        with self._state_lock:
            feedback = self._feedback()
            if verdict == "like":
                feedback.like(listing_id)
            elif verdict == "hide":
                feedback.hide(listing_id)
            else:
                feedback.liked.pop(listing_id, None)
                feedback.hidden.pop(listing_id, None)
            feedback.save()
            self._relearn(feedback)
        return {"ok": True, "liked": len(feedback.liked), "hidden": len(feedback.hidden)}

    def _resolve_places(self, submitted: list[dict],
                        existing: list[dict]) -> tuple[list[dict], list[str]]:
        """Returns (places, failures). Coordinates are trusted in order: sent
        by the UI (verified there) > from an unchanged existing address > a
        fresh geocode. Any failure rejects the whole save — an address must be
        known-valid when preferences are saved."""
        known = {_place_display(p).lower(): p for p in existing}
        out, failures = [], []
        for p in submitted:
            name = str(p.get("name") or "").strip()
            display = _place_display(p).strip()
            if not name or not display:
                continue
            limit = p.get("limit") if p.get("limit") in ("soft", "hard") else "off"
            max_min = _finite(p.get("max_min")) if p.get("max_min") else None
            record = {"name": name, "street": str(p.get("street") or ""),
                      "city": str(p.get("city") or ""), "zip": str(p.get("zip") or ""),
                      "address": display, "limit": limit, "max_min": max_min}
            lat, lon = _finite(p.get("lat")), _finite(p.get("lon"))
            if lat is not None and lon is not None:
                out.append({**record, "lat": lat, "lon": lon})
                continue
            prior = known.get(display.lower())
            if prior and prior.get("lat") is not None:
                out.append({**record, "lat": prior["lat"], "lon": prior["lon"]})
                continue
            hit = self.geocode(record["street"] or display, record["city"], record["zip"])
            if not hit["found"]:
                failures.append(f"{name} ({hit['error']})")
                continue
            out.append({**record, "address": hit["address"],
                        "lat": hit["lat"], "lon": hit["lon"]})
        return out, failures

    def update_prefs(self, fields: dict) -> dict:
        """Merge a partial update into preferences. Unknown keys are ignored;
        wrongly-typed or unrecognized multi-select values reject the save."""
        with self._state_lock:
            prefs = self._prefs()
            current = prefs.to_dict()
            known = {k: v for k, v in fields.items() if k in current}
            problem = _validate_pref_fields(known)
            if problem:
                return ErrorResponse(problem)
            for name in _INT_FIELDS & known.keys():
                known[name] = int(known[name])
            current.update(known)
            # picking a named location supersedes a drawn saved area; empty
            # selections leave it intact
            if known.get("boroughs") or known.get("neighborhoods"):
                current["area"] = None
            # multi-selects feed provider API calls: reject unknown values
            # rather than send a bad query later
            region_names = {r.lower() for r in REGIONS}
            unknown = ([b for b in current["boroughs"] if b.lower() not in region_names] +
                       [h for h in current["neighborhoods"] + current["exclude_neighborhoods"]
                        if h.lower() not in NEIGHBORHOOD_COORDS] +
                       [a for a in current["amenities"] if a not in AMENITIES] +
                       [s for s in current["sources"] if s not in PROVIDERS])
            if unknown:
                return ErrorResponse(f"unknown values: {', '.join(unknown)}")
            if not isinstance(current.get("verify_priority"), str) \
                    or current["verify_priority"] not in VERIFY_STRENGTH:
                current["verify_priority"] = "gentle"
            current["fetch_limit"] = clamp_fetch_limit(current.get("fetch_limit"))
            # a neighborhood can't be both wanted and excluded
            excluded = {e.lower() for e in current["exclude_neighborhoods"]}
            current["neighborhoods"] = [h for h in current["neighborhoods"]
                                        if h.lower() not in excluded]
            if "places" in known:
                places, failures = self._resolve_places(known["places"], prefs.places)
                if failures:
                    return ErrorResponse("couldn't locate: " + "; ".join(failures))
                current["places"] = places
            return {"ok": True, "prefs": self._save_prefs(current)}

    def save_area(self, bbox=None, poly=None) -> dict:
        """Commit a drawn area as the location ('Use in preferences'): the
        precise shape replaces borough/neighborhood filters, so results stay
        inside what was drawn. The covered neighborhoods are returned only for
        a friendly label. With no shape, clears any saved area."""
        pred = area_predicate(bbox, poly)
        with self._state_lock:
            current = self._prefs().to_dict()
            if pred is None:
                current["area"] = None
                return {"ok": True, "cleared": True, "prefs": self._save_prefs(current)}
            pts = parse_poly(poly)
            area = ({"poly": [list(p) for p in pts]} if pts
                    else {"bbox": list(parse_bbox(bbox))})
            current.update(area=area, neighborhoods=[], boroughs=[])
            prefs = self._save_prefs(current)
        names = sorted(n for n, (la, lo) in NEIGHBORHOOD_COORDS.items() if pred(la, lo))
        return {"ok": True, "neighborhoods": names, "area": area, "prefs": prefs}

    def ack_digest(self) -> dict:
        self._write_json("digest.json", {"acked_at": time.time()})
        return {"ok": True}

    def dismiss_setup(self) -> dict:
        self._write_json("setup.json", {"dismissed": True})
        return {"ok": True}

    def save_rentcast_key(self, key: str) -> dict:
        """Save (or clear, if blank) the RentCast API key — the in-app way to
        enable market-rate listings. Created owner-only (0600) from the start,
        so it's never briefly world-readable, and never sent back to the UI."""
        key = (key or "").strip()
        f = self.dir / "rentcast_key"
        if not key:
            f.unlink(missing_ok=True)
            return {"ok": True, "set": False}
        f.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        f.chmod(0o600)   # tighten a file that pre-existed with looser bits
        return {"ok": True, "set": True}

    # ---------- network-backed endpoints ----------

    def _building_verifier(self, budget: int = 40) -> BuildingLookup:
        """A building-facts lookup over NYC's registries, used by verify.py:

          address --(NYC GeoSearch)--> BBL + BIN + canonical coords
              BBL --(PLUTO)--> residential units, class, land use
              BIN --(HPD)--> registered?

        One shared rate-limited, cached session. A failed lookup is
        found=False; running out of request budget raises BudgetExceeded
        instead, since "couldn't ask" must not read as "no such building"."""
        session = SafeSession(self.dir / "cache", budget=budget, min_interval=0.5)
        token = resolve_app_token()

        def lookup(address: str, lat, lon) -> dict:
            address = (address or "").strip()
            if len(address) < 6:
                return {"found": False}
            try:
                body = session.get("https://geosearch.planninglabs.nyc/v2/search",
                                   params={"text": address, "size": 1})
                feat = (json.loads(body) or {}).get("features") or []
                if not feat:
                    return {"found": False}
                props = feat[0]["properties"]
                glon, glat = feat[0]["geometry"]["coordinates"]
                pad = (props.get("addendum") or {}).get("pad") or {}
            except BudgetExceeded:
                raise
            except Exception:
                return {"found": False}
            bbl, bin_ = _registry_id(pad.get("bbl")), _registry_id(pad.get("bin"))

            out = {"found": True, "bbl": bbl, "bin": bin_,
                   "label": props.get("label", address), "lat": glat, "lon": glon,
                   "units": None, "bldgclass": "", "landuse": "",
                   "numfloors": None, "yearbuilt": None, "registered": None}
            if bbl:
                rows = soda_get(session, PLUTO,
                                {"$where": f"bbl={bbl}",
                                 "$select": "unitsres,bldgclass,landuse,numfloors,yearbuilt",
                                 "$limit": 1}, token)
                if rows:
                    r = rows[0]
                    units = _finite(r.get("unitsres") or 0)
                    out.update(units=None if units is None else int(units),
                               bldgclass=r.get("bldgclass", ""),
                               landuse=r.get("landuse", ""),
                               numfloors=r.get("numfloors"),
                               yearbuilt=r.get("yearbuilt"))
            if bin_:
                reg = soda_get(session, HPD_REGISTRATIONS,
                               {"$where": f"bin='{bin_}'",
                                "$select": "registrationid", "$limit": 1}, token)
                out["registered"] = bool(reg)
            return out

        return lookup

    def _verify_batch(self, store: Store, only_ids: set[str], limit: int,
                      budget: int = 40) -> int:
        """verify_store, ending early if the request budget runs out: the
        listing being checked then stays unverified rather than flagged.
        Returns how many listings were checked."""
        lookup = self._building_verifier(budget=budget)
        start = time.time()
        pending = [r for r in store.records().values()
                   if r["listing"]["id"] in only_ids and needs_verify(r, start)]
        try:
            return verify_store(store, lookup, limit=limit, only_ids=only_ids)
        except BudgetExceeded:
            log.info("verification stopped: request budget exhausted")
            return sum(1 for r in pending if not needs_verify(r, start))

    def enrich(self, listing_id: str) -> dict:
        """Look up one RentCast listing's property record and add the amenities
        it reveals as facts (derived_amenities). Spends one quota-checked
        RentCast call — counted only on a real network hit — so the UI offers
        it per listing rather than for the whole pool."""
        with self._store_lock:
            store = self._store()
            rec = _record(store, listing_id)
            if rec is None:
                return ErrorResponse(f"unknown listing {listing_id}", 404)
            listing = rec["listing"]
            if listing.get("source") != "rentcast":
                return ErrorResponse("amenity lookup is only available for RentCast listings")
            key = resolve_api_key()
            if not key:
                return ErrorResponse("no RentCast API key configured")
            ledger = QuotaLedger(self.dir / "store.db")
            allowed, why = ledger.allow("rentcast")
            if not allowed:
                return ErrorResponse(why, 429)

            session = SafeSession(self.dir / "cache", budget=3, min_interval=1.0)
            body = session.get("https://api.rentcast.io/v1/properties",
                               params={"address": listing["address"]},
                               headers={"X-Api-Key": key, "Accept": "application/json"})
            if session.spent:      # count only a real (non-cached) call
                ledger.record("rentcast", 1)
                ledger.save()

            derived = listing.setdefault("derived_amenities", [])
            have = {a.lower() for a in listing.get("amenities", []) + derived}
            added = [a for a in features_to_amenities(body) if a.lower() not in have]
            derived.extend(added)
            rec["enriched_at"] = time.time()
            store.save()
        return {"ok": True, "added": added, "derived_amenities": derived,
                "quota": ledger.summary().get("rentcast")}

    def fetch(self) -> dict:
        """Run a live, plan- and quota-respecting fetch, merge it into the
        store, and verify a bounded number of the newly visible listings."""
        # resolved at call time: cli imports this module (and tests patch it)
        from . import cli
        with self._store_lock:
            prefs, store = self._prefs(), self._store()
            limit = clamp_fetch_limit(prefs.fetch_limit)
            # nothing fresh to show → don't let a stale coverage record skip it
            no_active = not store.active(window_days=prefs.active_window_days)
            fetched = cli.fetch_listings(
                prefs, list(prefs.sources) or list(PROVIDERS), budget=25,
                min_interval=4.0, max_pages=1 if limit <= RENTCAST_PAGE_SIZE else 2,
                ignore_coverage=no_active)
            if len(fetched) > limit:
                # keep all the (unique) affordable inventory; cap the rest
                hc = [l for l in fetched if l.source == "housingconnect"]
                other = [l for l in fetched if l.source != "housingconnect"]
                fetched = (hc + other)[:limit]
            stats = store.upsert(fetched)
            # bounded per fetch so we never breach the open-data rate limit
            verified = self._verify_batch(store, self._visible_ids(prefs, store), limit=8)
            store.save()
        return {"ok": True, "fetched": len(fetched), "new": len(stats.new),
                "price_drops": len(stats.price_drops), "verified": verified,
                "store_total": len(store)}

    def verify(self, batch: int = 20, listing_id: str | None = None) -> dict:
        """Verify listings against NYC's building registries.

        With `listing_id`, verify just that listing and return its fresh trust
        verdict. Otherwise verify one batch of visible listings that still
        need it and report how many remain — the UI loops on this for a
        progress readout instead of one multi-minute request."""
        batch = max(1, min(int(batch), MAX_VERIFY_BATCH))
        with self._store_lock:
            prefs, store = self._prefs(), self._store()
            if listing_id:
                rec = _record(store, listing_id)
                if rec is None:
                    return ErrorResponse(f"unknown listing {listing_id}", 404)
                try:
                    rec["verify"] = verify_one(rec, self._building_verifier(budget=12),
                                               time.time())
                except BudgetExceeded:
                    return ErrorResponse("request budget exhausted — try again shortly", 429)
                store.save()
                return {"ok": True, "checked": 1, "trust": trust(rec, prefs)}

            visible = self._visible_ids(prefs, store)
            checked = self._verify_batch(store, visible, limit=batch,
                                         budget=batch * 4 + 10)
            store.save()
        now = time.time()
        remaining = sum(1 for r in store.records().values()
                        if r["listing"]["id"] in visible and needs_verify(r, now))
        return {"ok": True, "checked": checked, "remaining": remaining,
                "total": len(visible)}
