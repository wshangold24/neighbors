"""Local web UI (stdlib only — no new dependencies).

The web app is a second imperative shell over the same pure modules the CLI
uses: engine.rank for ordering, Store for listings, Feedback for verdicts.
It exists because browsing is a visual task — cards, one-click like/hide,
and direct links to source listings beat typing `like 3` in a terminal.

Routes (all JSON unless noted):
  GET  /                    the single-page app (web.html)
  GET  /api/state           prefs + store stats + quota + weights source
  GET  /api/results         ranked results (?alpha=0.6)
  GET  /api/similar?id=X    seed mode: neighbors of one listing
  GET  /api/liked           the shortlist: liked listings for compare view
  POST /api/feedback        {"id": ..., "verdict": "like"|"hide"|"clear"}
  POST /api/prefs           partial Preferences fields to update
  POST /api/fetch           run a live fetch (plan/quota-respecting)
  POST /api/verify          verify one batch of listings vs NYC registries

State is re-read from disk on every request, so the CLI and the web app can
be used interchangeably without restarting either.
"""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

from .engine import DEFAULT_ALPHA, excluded_predicate, gate_breakdown, rank
from .knn import DEFAULT_WEIGHTS, haversine_km
from .transit import estimate_commute, stations_within
from .feedback import Feedback
from .knn import Query, distance, knn, match_pct
from .learn import learn_weights, load_weights, save_weights
from .models import (AMENITIES, NEIGHBORHOOD_COORDS, NEIGHBORHOOD_REGION, REGIONS,
                     Listing, Preferences, area_edge_km, nearest_neighborhood,
                     region_of)
from .providers import PROVIDERS
from .quota import QuotaLedger
from .store import Store

HTML_FILE = Path(__file__).with_name("web.html")

# Source reputability. Both live sources are authoritative/official: RentCast
# is a licensed listings API, Housing Connect is NYC's official affordable-
# housing lottery. Each maps to how we phrase "actually listed for rent".
OFFICIAL_SOURCES = {
    "rentcast": "RentCast's licensed feed",
    "housingconnect": "NYC's Housing Connect lottery",
}

# How much each trust level is nudged in ranking (distance units, added only
# for ordering — never changes a listing's match %). Verified listings get no
# penalty (float to the top of their match tier); not-yet-checked a small one;
# a red flag (not residential / mismatch / stale) a large one.
VERIFY_PRIORITY = {"confirmed": 0.0, "unverified": 0.15, "caution": 0.6}
# User-tunable multiplier on that penalty (Preferences.verify_priority).
VERIFY_STRENGTH = {"off": 0.0, "gentle": 1.0, "strong": 2.0}
# Below this many fresh results, backfill from older listings so a fetch that
# only re-confirmed part of the market can't collapse the page to a handful.
MIN_RESULTS = 12

# Amenities the app can actually confirm from ToS-clean data (vs. best-effort
# ones that depend on a listing source publishing them). Surfaced to the UI so
# the preferences picker can tell the user which selections are data-backed:
#   elevator            — PLUTO building class
#   central_air/parking — RentCast property records (on-demand lookup)
#   income_restricted   — Housing Connect
#   no_fee              — Housing Connect (affordable lotteries are fee-free)
SOURCED_AMENITIES = {"elevator", "central_air", "parking",
                     "income_restricted", "no_fee"}


def _point_in_poly(lat: float, lon: float, poly: list) -> bool:
    """Ray-casting point-in-polygon. `poly` is a list of (lat, lon) vertices."""
    inside, n, j = False, len(poly), len(poly) - 1
    for i in range(n):
        lati, loni = poly[i]
        latj, lonj = poly[j]
        if ((lati > lat) != (latj > lat)) and \
                (lon < (lonj - loni) * (lat - lati) / (latj - lati) + loni):
            inside = not inside
        j = i
    return inside


class Api:
    """All endpoint logic, independent of HTTP plumbing (unit-testable)."""

    def __init__(self, app_dir: Path):
        self.dir = app_dir

    # fresh objects per request: cheap at this scale, and it means the web
    # app and CLI never fight over stale in-memory state
    def _prefs(self) -> Preferences:
        f = self.dir / "preferences.json"
        if f.exists():
            try:
                return Preferences.from_dict(json.loads(f.read_text()))
            except (json.JSONDecodeError, TypeError):
                pass
        return Preferences()

    def _store(self) -> Store:
        return Store(self.dir / "store.db")

    def _feedback(self) -> Feedback:
        return Feedback(self.dir / "store.db")

    def _weights(self):
        return load_weights(self.dir / "weights.json")

    # ---------- serialization ----------

    @staticmethod
    def _maps_links(address: str) -> dict:
        q = quote_plus(f"{address}, New York, NY")
        return {
            "apple": f"https://maps.apple.com/?q={q}",
            "google": f"https://www.google.com/maps/search/?api=1&query={q}",
            # Realtor.com's ToS forbids scraping/embedding, so we link out —
            # a site-scoped search reproduces "look the address up, realtor
            # is the first result" in one click.
            "realtor": ("https://www.google.com/search?q="
                        + quote_plus(f'site:realtor.com "{address}"')),
        }

    @staticmethod
    def _commutes(l: Listing, places: list[dict]) -> list[dict]:
        """Per important place: offline transit estimate + directions links.
        Estimates need coordinates on both ends; the directions links (the
        exact answer) only need addresses, so they're always present."""
        out = []
        for p in places:
            apt_q = quote_plus(f"{l.address}, New York, NY")
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
                             steps=[{"kind": s.kind, "minutes": s.minutes,
                                     "label": s.label} for s in est.steps])
            out.append(entry)
        return out

    @staticmethod
    def _deltas(l: Listing, prefs: Preferences) -> dict:
        """Raw differences vs preferences, so the UI can say '$180 over
        budget' instead of a bare number."""
        # everything we know the listing has: source-reported + derived
        known = ({a.lower() for a in l.amenities} |
                 {a.lower() for a in l.derived_amenities})
        # distance to the nearest wanted neighborhood's EDGE (0 if inside it)
        loc_km = (area_edge_km(l.lat, l.lon, prefs.neighborhoods)
                  if prefs.neighborhoods and l.lat and l.lon else None)
        # where the listing ACTUALLY is (so "1.4 km away" gains a place-name):
        # its own neighborhood, else the nearest known one, else the borough
        loc_in = None
        if l.lat and l.lon:
            loc_in = (l.neighborhood or nearest_neighborhood(l.lat, l.lon)
                      or region_of(l.lat, l.lon) or None)
        return {
            "price": l.price - prefs.max_price,          # >0 = over budget
            "location_km": round(loc_km, 1) if loc_km else None,
            "location_in": loc_in,
            "beds": l.beds - prefs.beds,
            "baths": l.baths - prefs.baths,
            "sqft": ((l.sqft - prefs.min_sqft)
                     if l.sqft and prefs.min_sqft else None),
            "sqft_unknown": bool(prefs.min_sqft and not l.sqft),
            "amenities_missing": sorted(set(prefs.amenities) - known)
                if prefs.amenities else [],
            # no amenity data at all (source OR derived) — unknown, not absent
            "amenities_unknown": bool(prefs.amenities and not known),
        }

    @staticmethod
    def _trust(rec: dict | None, prefs: Preferences, now: float | None = None) -> dict:
        """Is this a *real rental*? Combines two independent checks:
          1. real registered residential dwelling — verify.py resolved the
             address to a residential building in NYC's PLUTO/HPD registries,
             near where it was listed (cached on the record at fetch time)
          2. actually listed for rent — present on an official source (RentCast's
             licensed feed / NYC Housing Connect) and seen in a recent fetch
        Green ('confirmed') requires BOTH. Amber ('caution') flags a red flag —
        the address isn't a residential building, resolves somewhere else, or the
        listing has gone stale. Grey ('unverified') means the building check
        hasn't run yet. Recomputed fresh each read, so 'stale' is always current."""
        now = now or time.time()
        v = (rec or {}).get("verify") or {}
        sources = sorted((rec or {}).get("sources", {}).keys()) if rec else []
        official = [s for s in sources if s in OFFICIAL_SOURCES]
        last_seen = (rec or {}).get("last_seen", now)
        active = (now - last_seen) <= prefs.active_window_days * 86400
        addr = v.get("status")   # ok | not_residential | mismatch | not_found | None
        km, units, registered = v.get("km_off"), v.get("units"), v.get("registered")

        def listed_phrase() -> str:
            if official:
                names = [OFFICIAL_SOURCES[s] for s in official]
                extra = (f" + {len(sources) - len(official)} more"
                         if len(sources) > len(official) else "")
                return "on " + " & ".join(names) + extra
            return f"listed on {sources[0]}" if sources else "listed"

        def building_phrase() -> str:
            bits = []
            if units:
                bits.append(f"{units}-unit residential building")
            else:
                bits.append("residential building")
            if registered:
                bits.append("HPD-registered")
            return ", ".join(bits)

        if addr == "not_residential":
            level, label = "caution", "that address isn't a residential building — likely not a real rental"
        elif addr == "not_found":
            level, label = "caution", "no NYC building record for this address — treat with caution"
        elif addr == "mismatch":
            level = "caution"
            label = f"address resolves {km} km from where it's listed — verify before contacting"
        elif not active:
            level, label = "caution", "not seen in recent fetches — may already be rented"
        elif addr == "ok":
            level = "confirmed"
            label = f"verified rental — {building_phrase()}, {listed_phrase()}"
        else:
            level = "unverified"
            label = f"{listed_phrase()} · building not yet verified"

        return {"level": level, "label": label, "address": addr, "km_off": km,
                "units": units, "registered": registered,
                "sources": sources, "official": official, "active": active}

    def _listing_json(self, l: Listing, store: Store, feedback: Feedback,
                      prefs: Preferences,
                      dist: float | None = None, parts: dict | None = None,
                      blend: dict | None = None) -> dict:
        rec = next((r for r in store.records().values()
                    if r["listing"]["id"] == l.id), None)
        furthest = (max(parts, key=parts.get) if parts and max(parts.values()) > 0.25
                    else None)
        return {
            "id": l.id, "address": l.address, "neighborhood": l.neighborhood,
            "lat": l.lat, "lon": l.lon,   # needed by the map view
            "price": l.price, "beds": l.beds, "baths": l.baths, "sqft": l.sqft,
            # display combines source amenities with derived ones (e.g. elevator
            # from building records); derived_amenities lets the UI tag them
            "amenities": l.amenities + [a for a in l.derived_amenities
                                        if a.lower() not in {x.lower() for x in l.amenities}],
            "derived_amenities": l.derived_amenities,
            # advisory guesses (never gate/score) — UI shows them as "likely"
            "inferred_amenities": l.inferred_amenities, "url": l.url,
            # has this listing's property record already been looked up?
            "enriched": bool(rec.get("enriched_at")) if rec else False,
            "description": l.description,
            "match": match_pct(dist) if dist is not None else None,
            "parts": {k: round(v, 3) for k, v in (parts or {}).items()},
            "furthest": furthest,
            "sources": rec["sources"] if rec else {l.source: l.url},
            "price_history": [[int(t), p] for t, p in rec["price_history"]] if rec else [],
            # is this a real rental? real address (verify.py) + actively listed
            # on reputable sources; see Api._trust
            "trust": self._trust(rec, prefs),
            "flags": store.annotations().get(l.id, ""),
            "liked": l.id in feedback.liked,
            "hidden": l.id in feedback.hidden,
            "seen": feedback.seen.get(l.id, 0),
            # how much of the blended score came from prefs vs liked seeds —
            # lets the UI draw the per-card contribution bar
            "blend": blend,
            "maps": self._maps_links(l.address),
            "deltas": self._deltas(l, prefs),
            "commutes": self._commutes(l, prefs.places),
            # subway/PATH stations within a 10-minute walk, nearest first
            "stations": [{"lines": s.lines_str, "name": s.name,
                          "walk_min": s.walk_min}
                         for s in (stations_within(l.lat, l.lon)
                                   if l.lat and l.lon else [])],
        }

    # ---------- endpoints ----------

    def state(self) -> dict:
        prefs, store = self._prefs(), self._store()
        return {
            "prefs": prefs.to_dict(),
            # active ranking weights — the UI uses them to order the
            # compatibility rows by (importance × deficit) and to show
            # importance markers
            "weights": self._weights() or DEFAULT_WEIGHTS,
            # canonical choice lists for the UI's multi-selects — these values
            # feed provider API calls, so the UI must offer only valid ones
            "options": {"regions": list(REGIONS),
                        "neighborhoods": sorted(NEIGHBORHOOD_COORDS),
                        # neighborhood -> borough, so the picker can filter
                        "neighborhood_region": NEIGHBORHOOD_REGION,
                        "amenities": list(AMENITIES),
                        # which amenities we can actually confirm from data
                        "sourced_amenities": sorted(SOURCED_AMENITIES),
                        "sources": list(PROVIDERS)},
            "store": {"total": len(store),
                      "active": len(store.active(window_days=prefs.active_window_days))},
            "quota": QuotaLedger(self.dir / "store.db").summary(),
            "weights_learned": self._weights() is not None,
            "rentcast_key_set": self._rentcast_key_present(),
            "setup_dismissed": (self.dir / "setup.json").exists(),
            "feedback": {"liked": len(self._feedback().liked),
                         "hidden": len(self._feedback().hidden)},
            # verification status across active listings — powers the Verify
            # button's count and any "verified only" filtering the UI wants
            "verification": self._verification_summary(prefs, store),
        }

    def _visible_ids(self, prefs: Preferences, store: Store) -> set:
        """Ids of listings that can actually surface to the user — active (or,
        if nothing's fresh, the whole store) and passing the hard gate. Scoping
        verification to this set keeps it fast: we don't check the hundreds of
        out-of-area addresses a citywide fetch dumps in the store."""
        from .engine import gate
        active = (store.active(window_days=prefs.active_window_days)
                  or store.active(window_days=10 ** 6))
        return {l.id for l in gate(prefs, active)}

    def _verification_summary(self, prefs: Preferences, store: Store) -> dict:
        """Counts of displayable listings by trust level, plus how many still
        need a (first-time or stale) building check — that last number is what
        the Verify button acts on."""
        from .verify import needs_verify
        now = time.time()
        visible_ids = self._visible_ids(prefs, store)
        counts = {"confirmed": 0, "caution": 0, "unverified": 0, "pending": 0}
        for rec in store.records().values():
            if rec["listing"]["id"] not in visible_ids:
                continue
            counts[self._trust(rec, prefs, now)["level"]] += 1
            if needs_verify(rec, now):
                counts["pending"] += 1
        return counts

    @staticmethod
    def _place_penalty(l: Listing, prefs: Preferences) -> float:
        """Soft commute-time penalty: each 'soft' place adds to the ranking
        penalty when the listing's transit time to it exceeds max_min. Reorders
        only (never changes match %); 'hard' places are handled by the gate."""
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

    def _priority_map(self, listings, store: Store, prefs: Preferences) -> dict:
        """Per-listing sort-only ranking penalty = verification status (verified
        floats up, flagged sinks; scaled by verify_priority) + soft commute-time
        limits. Never changes a listing's match %."""
        strength = VERIFY_STRENGTH.get(prefs.verify_priority, 1.0)
        recs = {r["listing"]["id"]: r for r in store.records().values()}
        now = time.time()
        out = {}
        for l in listings:
            pen = self._place_penalty(l, prefs)
            if strength:
                pen += strength * VERIFY_PRIORITY.get(
                    self._trust(recs.get(l.id), prefs, now)["level"], 0.15)
            if pen:
                out[l.id] = pen
        return out

    def digest(self) -> dict:
        """What's new since the digest was last acknowledged (or 7 days back on
        first run). Powers the 'what's new' banner."""
        from .digest import build_digest
        marker = self.dir / "digest.json"
        since = 0.0
        if marker.exists():
            try:
                since = float(json.loads(marker.read_text()).get("acked_at", 0))
            except (json.JSONDecodeError, TypeError, ValueError):
                since = 0.0
        if not since:
            since = time.time() - 7 * 86400
        return build_digest(self._store(), self._feedback(), since)

    def ack_digest(self) -> dict:
        (self.dir / "digest.json").write_text(json.dumps({"acked_at": time.time()}))
        return {"ok": True}

    def dismiss_setup(self) -> dict:
        (self.dir / "setup.json").write_text(json.dumps({"dismissed": True}))
        return {"ok": True}

    def _rentcast_key_file(self) -> Path:
        return self.dir / "rentcast_key"

    def _rentcast_key_present(self) -> bool:
        """True if a RentCast key already exists where we'd look for one — a
        non-empty saved key file OR the RENTCAST_API_KEY env var. Mirrors the
        provider's resolve order so we never ask for a key that's already set."""
        import os
        f = self._rentcast_key_file()
        try:
            if f.exists() and f.read_text().strip():
                return True
        except OSError:
            pass
        return bool(os.environ.get("RENTCAST_API_KEY", "").strip())

    def save_rentcast_key(self, key: str) -> dict:
        """Store (or clear, if blank) the RentCast API key on disk — the in-app
        way to enable market-rate listings, so no terminal/env var is needed.
        Written to ~/.aptfinder/rentcast_key with 0600 permissions (readable only
        by this user account) and created that way from the start, so it's never
        briefly world-readable. It lives in the user's home dir (never the repo,
        never sent back to the browser) and persists across launches."""
        import os
        key = (key or "").strip()
        f = self._rentcast_key_file()
        if not key:
            f.unlink(missing_ok=True)
            return {"ok": True, "set": False}
        f.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(key + "\n")
        try:
            f.chmod(0o600)   # tighten even if the file pre-existed
        except OSError:
            pass
        return {"ok": True, "set": True}

    @staticmethod
    def _area_predicate(bbox: tuple | None, poly: list | None):
        """A lat,lon -> bool test for a drawn search area: a freehand polygon (3+
        points) if given, else a rectangle bbox, else None."""
        if poly and len(poly) >= 3:
            return lambda la, lo: _point_in_poly(la, lo, poly)
        if bbox:
            s_lat, w_lon, n_lat, e_lon = bbox
            return lambda la, lo: s_lat <= la <= n_lat and w_lon <= lo <= e_lon
        return None

    def results(self, alpha: float | None = None,
                bbox: tuple | None = None, poly: list | None = None) -> dict:
        prefs, store, feedback = self._prefs(), self._store(), self._feedback()
        alpha = alpha if alpha is not None else DEFAULT_ALPHA
        # A transient drawn area (bbox/poly from the request) wins; otherwise a
        # saved area in preferences ("Use in preferences") acts as the location.
        area = self._area_predicate(bbox, poly)
        if area is None and prefs.area:
            area = self._area_predicate(prefs.area.get("bbox"), prefs.area.get("poly"))

        def rank_within(listings):
            # "Search this area" — a drawn rectangle/shape REPLACES the saved
            # location (boroughs/neighborhoods). Everything else (price, beds,
            # amenities, exclusions, commute limits) still applies. This is a
            # search, not a save: prefs change only via the save-area button.
            p = prefs
            if area:
                from dataclasses import replace
                listings = [l for l in listings if l.lat is not None
                            and l.lon is not None and area(l.lat, l.lon)]
                p = replace(prefs, boroughs=[], neighborhoods=[])
            r = rank(p, listings, feedback, alpha=alpha, weights=self._weights(),
                     priority=self._priority_map(listings, store, p))
            return r, p

        # Fresh set first. If it's thin — because a fetch only re-confirmed a
        # slice of the market, so most listings just aged past the active
        # window — widen to the whole store so recently-seen listings don't
        # vanish on refresh. Only adopt the wider set if it actually shows more,
        # and flag it stale so the UI can say "includes older listings".
        active = store.active(window_days=prefs.active_window_days)
        ranked, used_prefs = rank_within(active)
        stale = False
        if len(ranked) < MIN_RESULTS and len(store) > len(active):
            ranked_all, prefs_all = rank_within(store.active(window_days=10 ** 6))
            if len(ranked_all) > len(ranked):
                ranked, used_prefs, stale = ranked_all, prefs_all, True
        prefs = used_prefs
        # If we still have nothing to show but the store isn't empty, the hard
        # filters are eliminating everything — tell the UI why (which rule, how
        # many) so the empty state can be actionable instead of blank.
        diagnostic = None
        if not ranked and len(store):
            all_listings = store.active(window_days=10 ** 6)
            fails = {k: v for k, v in gate_breakdown(prefs, all_listings).items() if v}
            diagnostic = {"store": len(store), "shown": 0, "fails": fails,
                          "prefs": {"max_price": prefs.max_price, "beds": prefs.beds,
                                    "neighborhoods": prefs.neighborhoods,
                                    "amenities": prefs.amenities}}
        feedback.mark_seen([r.listing.id for r in ranked])
        feedback.save()
        return {"alpha": alpha, "stale": stale, "diagnostic": diagnostic, "results": [
            self._listing_json(
                r.listing, store, feedback, prefs, r.score, r.parts,
                blend=None if r.d_seed is None else {
                    "pref": round(alpha * r.d_pref, 4),
                    "seed": round((1 - alpha) * r.d_seed, 4),
                })
            for r in ranked]}

    def save_area(self, bbox: tuple | None = None, poly: list | None = None) -> dict:
        """Commit the drawn area to preferences AS the location — the precise
        shape, persisted, replacing borough/neighborhood filters. This is the
        'Use in preferences' half of Search-this-area. Previously we converted
        the shape to neighborhood names, but the gate then re-expanded each name
        to an 8 km radius, dragging far-away listings back in; saving the shape
        itself keeps results inside what the user actually drew.

        We also record which named neighborhoods the shape covers, purely for a
        friendly label ('saved 3 neighborhoods'); they don't drive filtering."""
        pred = self._area_predicate(bbox, poly)
        if not pred:
            # called with no shape → clear any saved area (the "Clear" action)
            prefs = self._prefs().to_dict()
            prefs["area"] = None
            updated = Preferences.from_dict(prefs)
            (self.dir / "preferences.json").write_text(json.dumps(updated.to_dict(), indent=2))
            return {"ok": True, "cleared": True, "prefs": updated.to_dict()}
        area = {"poly": poly} if (poly and len(poly) >= 3) else {"bbox": list(bbox)}
        names = sorted(n for n, (la, lo) in NEIGHBORHOOD_COORDS.items() if pred(la, lo))
        prefs = self._prefs().to_dict()
        prefs["area"] = area
        prefs["neighborhoods"] = []   # the shape is the location now
        prefs["boroughs"] = []
        updated = Preferences.from_dict(prefs)
        (self.dir / "preferences.json").write_text(json.dumps(updated.to_dict(), indent=2))
        return {"ok": True, "neighborhoods": names, "area": area,
                "prefs": updated.to_dict()}

    def liked(self) -> dict:
        """The shortlist: every liked listing, fully serialized for the compare
        view. Liked listings are excluded from ranked results by design, so the
        UI can't assemble this itself. Match % is scored against preferences
        (the same yardstick every card uses), so columns stay comparable.
        Newest likes first; listings no longer in the store are skipped."""
        prefs, store, feedback = self._prefs(), self._store(), self._feedback()
        weights = self._weights() or DEFAULT_WEIGHTS
        pref_q = Query.from_preferences(prefs)
        active_ids = {l.id for l in store.active(window_days=prefs.active_window_days)}
        out = []
        # feedback.liked maps id -> timestamp; show most recently liked first
        for lid in sorted(feedback.liked, key=lambda i: feedback.liked[i], reverse=True):
            l = store.find(lid)
            if l is None:
                continue
            d, parts = distance(pref_q, l, weights, explain=True)
            rec = self._listing_json(l, store, feedback, prefs, d, parts)
            rec["active"] = l.id in active_ids  # UI can flag listings that have aged out
            out.append(rec)
        return {"results": out}

    def similar(self, listing_id: str) -> dict:
        prefs, store, feedback = self._prefs(), self._store(), self._feedback()
        seed = store.find(listing_id)
        if seed is None:
            return {"error": f"unknown listing {listing_id}"}
        listings = store.active(window_days=prefs.active_window_days)
        # anti-preferences are hard: excluded neighborhoods never appear, even
        # among "similar" neighbors of a liked listing
        keep = excluded_predicate(prefs)
        if keep:
            listings = [l for l in listings if keep(l)]
        q = Query.from_listing(seed, max_price=prefs.max_price * (1 + prefs.price_stretch))
        ranked = knn(q, listings, k=prefs.k, weights=self._weights(),
                     exclude_ids={seed.id})
        return {"seed": self._listing_json(seed, store, feedback, prefs),
                "results": [self._listing_json(l, store, feedback, prefs, d, parts)
                            for l, d, parts in ranked]}

    def set_feedback(self, listing_id: str, verdict: str) -> dict:
        feedback = self._feedback()
        if verdict == "like":
            feedback.like(listing_id)
        elif verdict == "hide":
            feedback.hide(listing_id)
        elif verdict == "clear":
            feedback.liked.pop(listing_id, None)
            feedback.hidden.pop(listing_id, None)
        else:
            return {"error": f"unknown verdict '{verdict}'"}
        feedback.save()
        self._relearn(feedback)
        return {"ok": True, "liked": len(feedback.liked), "hidden": len(feedback.hidden)}

    def _relearn(self, feedback: Feedback) -> None:
        store = self._store()
        liked = [l for l in (store.find(i) for i in feedback.liked) if l]
        learned = learn_weights(self._prefs(), liked)
        if learned:
            save_weights(self.dir / "weights.json", learned)

    def geocode(self, street: str = "", city: str = "", zip_code: str = "") -> dict:
        """Segmented address → resolved location via OpenStreetMap Nominatim
        (structured search: any city works — Jersey City, Hoboken, NYC…).
        Returns {"found": True, "address": <canonical>, "lat", "lon"} or
        found=False with a reason. Guards run BEFORE any network call.
        Rate-limited and cached by SafeSession; identifies itself per
        Nominatim's usage policy."""
        street, city, zip_code = (s.strip() for s in (street, city, zip_code))
        if len(street) < 5 or not city:
            return {"found": False, "error": "need a street address and a city"}
        from .http import SafeSession
        session = SafeSession(self.dir / "cache", budget=5, min_interval=1.2)
        params = {"format": "json", "limit": 1, "country": "USA",
                  "street": street, "city": city}
        if zip_code:
            params["postalcode"] = zip_code
        body = session.get(
            "https://nominatim.openstreetmap.org/search", params=params,
            headers={"User-Agent": "aptfinder/0.2 (personal local apartment search)"})
        try:
            hit = json.loads(body)[0]
            return {"found": True, "address": hit.get("display_name", street),
                    "lat": float(hit["lat"]), "lon": float(hit["lon"])}
        except (TypeError, json.JSONDecodeError, IndexError, KeyError, ValueError):
            logging.getLogger("aptfinder.web").warning(
                "geocode failed: %s / %s / %s", street, city, zip_code)
            return {"found": False,
                    "error": "no match — check the street number and city"}

    @staticmethod
    def _place_display(p: dict) -> str:
        """One-line address from segments (or the legacy free-form field)."""
        if p.get("street"):
            return ", ".join(x for x in
                             (p["street"], p.get("city", ""), p.get("zip", "")) if x)
        return p.get("address", "")

    def _resolve_places(self, submitted: list, existing: list[dict]) -> tuple[list[dict], list[str]]:
        """Returns (places, failures). Order of trust: coordinates verified in
        the UI > coordinates from an unchanged existing address > a fresh
        geocode last. Failures are collected so the save can be REJECTED —
        an address must be known-valid when preferences are saved."""
        known = {self._place_display(p).lower(): p for p in existing}
        out, failures = [], []
        for p in submitted:
            name = str(p.get("name", "")).strip()
            display = self._place_display(p).strip()
            if not name or not display:
                continue
            # per-place commute-time limit: off / soft / hard, in transit minutes
            limit = p.get("limit") if p.get("limit") in ("soft", "hard") else "off"
            try:
                max_min = float(p["max_min"]) if p.get("max_min") else None
            except (TypeError, ValueError):
                max_min = None
            record = {"name": name, "street": p.get("street", ""),
                      "city": p.get("city", ""), "zip": p.get("zip", ""),
                      "address": display, "limit": limit, "max_min": max_min}
            if p.get("lat") is not None and p.get("lon") is not None:
                out.append({**record, "lat": float(p["lat"]), "lon": float(p["lon"])})
                continue
            prior = known.get(display.lower())
            if prior and prior.get("lat") is not None:
                out.append({**record, "lat": prior["lat"], "lon": prior["lon"]})
                continue
            hit = self.geocode(p.get("street", display), p.get("city", ""),
                               p.get("zip", ""))
            if not hit["found"]:
                failures.append(f"{name} ({hit['error']})")
                continue
            out.append({**record, "address": hit["address"],
                        "lat": hit["lat"], "lon": hit["lon"]})
        return out, failures

    def update_prefs(self, fields: dict) -> dict:
        prefs = self._prefs()
        current = prefs.to_dict()
        known = {k: v for k, v in fields.items() if k in current}
        current.update(known)
        # Choosing a named location (a borough or neighborhood) supersedes a
        # previously-drawn saved area — otherwise the old shape would keep
        # overriding the new pick. Empty selections leave any saved area intact.
        if known.get("boroughs") or known.get("neighborhoods"):
            current["area"] = None
        # Multi-select fields feed provider API calls — reject unknown values
        # outright rather than sending a bad query later.
        region_names = {r.lower() for r in REGIONS}
        unknown = ([b for b in current["boroughs"] if b.lower() not in region_names] +
                   [h for h in current["neighborhoods"]
                    if h.lower() not in NEIGHBORHOOD_COORDS] +
                   [h for h in current["exclude_neighborhoods"]
                    if h.lower() not in NEIGHBORHOOD_COORDS] +
                   [a for a in current["amenities"] if a not in AMENITIES] +
                   [s for s in current["sources"] if s not in PROVIDERS])
        if unknown:
            return {"error": f"unknown values: {', '.join(unknown)}"}
        if current.get("verify_priority") not in VERIFY_STRENGTH:
            current["verify_priority"] = "gentle"
        try:
            current["fetch_limit"] = max(100, min(1000, int(current.get("fetch_limit") or 1000)))
        except (TypeError, ValueError):
            current["fetch_limit"] = 1000
        # a neighborhood can't be both wanted and excluded
        current["neighborhoods"] = [h for h in current["neighborhoods"]
                                    if h.lower() not in
                                    {e.lower() for e in current["exclude_neighborhoods"]}]
        if "places" in known:
            places, failures = self._resolve_places(known["places"], prefs.places)
            if failures:
                # strict by design: an invalid address must never be saved
                return {"error": "couldn't locate: " + "; ".join(failures)}
            current["places"] = places
        updated = Preferences.from_dict(current)
        (self.dir / "preferences.json").write_text(json.dumps(updated.to_dict(), indent=2))
        return {"ok": True, "prefs": updated.to_dict()}

    def _building_verifier(self, budget: int = 40):
        """A 'listing -> building facts' lookup over NYC's authoritative
        registries, used by verify.py to confirm a real residential dwelling:

          address --(NYC GeoSearch)--> BBL + BIN + canonical coords
                BBL --(PLUTO)--> residential units, class, land use
                BIN --(HPD)--> registered?

        One shared, rate-limited, cached session; degrades to found=False on any
        error (a listing simply stays 'unverified')."""
        from .http import SafeSession
        from .providers.socrata import (HPD_REGISTRATIONS, PLUTO,
                                         resolve_app_token, soda_get)
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
                bbl, bin_ = pad.get("bbl"), pad.get("bin")
            except Exception:
                return {"found": False}

            out = {"found": True, "bbl": bbl, "bin": bin_,
                   "label": props.get("label", address), "lat": glat, "lon": glon,
                   "units": None, "bldgclass": "", "landuse": "",
                   "numfloors": None, "yearbuilt": None, "registered": None}
            if bbl:
                rows = soda_get(session, PLUTO,
                                {"$where": f"bbl={int(float(bbl))}",
                                 "$select": "unitsres,bldgclass,landuse,numfloors,yearbuilt",
                                 "$limit": 1}, token)
                if rows:
                    r = rows[0]
                    try:
                        out["units"] = int(float(r.get("unitsres") or 0))
                    except (TypeError, ValueError):
                        out["units"] = None
                    out["bldgclass"] = r.get("bldgclass", "")
                    out["landuse"] = r.get("landuse", "")
                    out["numfloors"] = r.get("numfloors")
                    out["yearbuilt"] = r.get("yearbuilt")
            if bin_:
                reg = soda_get(session, HPD_REGISTRATIONS,
                               {"$where": f"bin='{bin_}'",
                                "$select": "registrationid", "$limit": 1}, token)
                out["registered"] = bool(reg)
            return out

        return lookup

    @staticmethod
    def _features_to_amenities(body: str | None) -> list[str]:
        """Map a RentCast property record's `features` object to our amenity
        vocabulary. RentCast exposes structural features, so realistically this
        yields central_air (cooling) and parking (garage) — treated as FACTS."""
        if not body:
            return []
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return []
        records = data if isinstance(data, list) else [data]
        out: list[str] = []
        for pr in records:
            f = (pr or {}).get("features") or {}
            if f.get("cooling"):
                out.append("central_air")
            try:
                spaces = int(f.get("garageSpaces") or 0)
            except (TypeError, ValueError):
                spaces = 0
            if f.get("garage") or spaces > 0:
                out.append("parking")
        seen, uniq = set(), []
        for a in out:
            if a not in seen:
                seen.add(a)
                uniq.append(a)
        return uniq

    def enrich(self, listing_id: str) -> dict:
        """On-demand: look up one listing's property record on RentCast and add
        any amenities it reveals as FACTS (into derived_amenities). Spends one
        RentCast API call — quota-checked and only counted on a real network hit
        — so the UI can offer it explicitly per-listing rather than for the pool."""
        from .http import SafeSession
        from .providers.rentcast import resolve_api_key
        store = self._store()
        rec = next((r for r in store.records().values()
                    if r["listing"]["id"] == listing_id), None)
        if rec is None:
            return {"error": f"unknown listing {listing_id}"}
        if rec["listing"].get("source") != "rentcast":
            return {"error": "amenity lookup is only available for RentCast listings"}
        key = resolve_api_key()
        if not key:
            return {"error": "no RentCast API key configured"}
        ledger = QuotaLedger(self.dir / "store.db")
        allowed, why = ledger.allow("rentcast")
        if not allowed:
            return {"error": why}

        session = SafeSession(self.dir / "cache", budget=3, min_interval=1.0)
        before = session.spent
        body = session.get("https://api.rentcast.io/v1/properties",
                           params={"address": rec["listing"]["address"]},
                           headers={"X-Api-Key": key, "Accept": "application/json"})
        if session.spent > before:      # count only a real (non-cached) call
            ledger.record("rentcast", 1)
            ledger.save()

        listing = rec["listing"]
        derived = listing.setdefault("derived_amenities", [])
        have = ({a.lower() for a in listing.get("amenities", [])} |
                {a.lower() for a in derived})
        added = [a for a in self._features_to_amenities(body) if a.lower() not in have]
        derived.extend(added)
        rec["enriched_at"] = time.time()
        store.save()
        return {"ok": True, "added": added, "derived_amenities": derived,
                "quota": ledger.summary().get("rentcast")}

    def fetch(self) -> dict:
        # Lazy import: cli imports web for the `web` command; importing cli at
        # module load would be a circular import.
        from .cli import fetch_listings
        from .verify import verify_store
        prefs, store = self._prefs(), self._store()
        sources = list(prefs.sources) or list(PROVIDERS)  # empty = all
        limit = max(100, min(1000, int(prefs.fetch_limit or 1000)))
        pages = 1 if limit <= 500 else 2   # RentCast returns up to 500 per page
        # nothing fresh to show → don't let a stale coverage record skip the fetch
        no_active = not store.active(window_days=prefs.active_window_days)
        fetched = fetch_listings(prefs, sources, budget=25, min_interval=4.0,
                                 max_pages=pages, ignore_coverage=no_active)
        if len(fetched) > limit:
            # keep the unique affordable inventory, cap the rest to the limit
            hc = [l for l in fetched if l.source == "housingconnect"]
            other = [l for l in fetched if l.source != "housingconnect"]
            fetched = (hc + other)[:limit]
        stats = store.upsert(fetched)
        # confirm the listings that can actually surface (in-area, active),
        # bounded per fetch so we never breach the open-data rate limit
        verified = verify_store(store, self._building_verifier(), limit=8,
                                only_ids=self._visible_ids(prefs, store))
        store.save()
        return {"ok": True, "fetched": len(fetched), "new": len(stats.new),
                "price_drops": len(stats.price_drops), "verified": verified,
                "store_total": len(store)}

    def verify(self, batch: int = 20, listing_id: str | None = None) -> dict:
        """Verify listings against NYC's building registries.

        With `listing_id`: verify just that one listing now (the per-card
        'verify this' action), returning its fresh trust verdict.

        Otherwise: verify one BATCH that still needs checking and report how many
        remain. The UI calls this in a loop to verify *everything* with a
        progress readout — chunking keeps each request short and the open-data
        rate limit happy, instead of one multi-minute request."""
        from .verify import needs_verify, verify_one, verify_store
        prefs, store = self._prefs(), self._store()

        if listing_id:
            rec = next((r for r in store.records().values()
                        if r["listing"]["id"] == listing_id), None)
            if rec is None:
                return {"error": f"unknown listing {listing_id}"}
            rec["verify"] = verify_one(rec, self._building_verifier(budget=12),
                                       time.time())
            store.save()
            return {"ok": True, "checked": 1, "trust": self._trust(rec, prefs)}

        visible = self._visible_ids(prefs, store)
        checked = verify_store(store, self._building_verifier(budget=batch * 4 + 10),
                               limit=batch, only_ids=visible)
        store.save()
        now = time.time()
        # only count the listings that can actually appear — verifying the whole
        # citywide store would make this crawl and never reach zero
        remaining = sum(1 for r in store.records().values()
                        if r["listing"]["id"] in visible and needs_verify(r, now))
        return {"ok": True, "checked": checked, "remaining": remaining,
                "total": len(visible)}


def make_handler(api: Api):
    """Bind the Api to an http.server handler. Routing is a declarative
    table; the handler just decodes/encodes JSON around it."""

    def _bbox(query: dict):
        raw = query.get("bbox", [None])[0]
        if not raw:
            return None
        try:
            parts = tuple(float(x) for x in raw.split(","))
            return parts if len(parts) == 4 else None
        except ValueError:
            return None

    def _poly(query: dict):
        raw = query.get("poly", [None])[0]
        if not raw:
            return None
        try:
            nums = [float(x) for x in raw.split(",")]
        except ValueError:
            return None
        pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums) - 1, 2)]
        return pts if len(pts) >= 3 else None

    def get_routes(path: str, query: dict):
        return {
            "/api/state": lambda: api.state(),
            "/api/results": lambda: api.results(
                alpha=float(query["alpha"][0]) if "alpha" in query else None,
                bbox=_bbox(query), poly=_poly(query)),
            "/api/similar": lambda: api.similar(query.get("id", [""])[0]),
            "/api/liked": lambda: api.liked(),
            "/api/digest": lambda: api.digest(),
            "/api/geocode": lambda: api.geocode(
                street=query.get("street", [""])[0],
                city=query.get("city", [""])[0],
                zip_code=query.get("zip", [""])[0]),
        }.get(path)

    def post_routes(path: str, body: dict):
        return {
            "/api/feedback": lambda: api.set_feedback(body.get("id", ""),
                                                      body.get("verdict", "")),
            "/api/prefs": lambda: api.update_prefs(body),
            "/api/fetch": lambda: api.fetch(),
            "/api/verify": lambda: api.verify(
                batch=int(body.get("batch", 20)),
                listing_id=body.get("id") or None),
            "/api/enrich": lambda: api.enrich(body.get("id", "")),
            "/api/digest/ack": lambda: api.ack_digest(),
            "/api/rentcast-key": lambda: api.save_rentcast_key(body.get("key", "")),
            "/api/setup/dismiss": lambda: api.dismiss_setup(),
            "/api/area": lambda: api.save_area(
                bbox=tuple(body["bbox"]) if isinstance(body.get("bbox"), list)
                and len(body["bbox"]) == 4 else None,
                poly=[tuple(p) for p in body["poly"]]
                if isinstance(body.get("poly"), list) and len(body.get("poly", [])) >= 3
                else None),
        }.get(path)

    # The client hanging up mid-response (navigating away, a fetch aborted by a
    # page reload, a long request the browser gave up on) surfaces as these —
    # expected, not a server fault, and nothing can be sent back over a dead
    # socket, so we swallow them everywhere instead of logging a scary traceback.
    DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal quiet
            pass

        def handle(self):
            # top-level guard so a disconnect can never bubble into a traceback
            try:
                super().handle()
            except DISCONNECTS:
                pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # the page and API are always dynamic — never let the browser
                # serve a stale cached copy (that's why a restart didn't seem to
                # take effect: the old single-page app was still cached)
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(body)
            except DISCONNECTS:
                pass   # client is gone; there's nothing to send

        def _send_json(self, data: dict, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, HTML_FILE.read_bytes(), "text/html; charset=utf-8")
                return
            route = get_routes(url.path, parse_qs(url.query))
            if route is None:
                self._send_json({"error": "not found"}, 404)
                return
            try:
                self._send_json(route())
            except DISCONNECTS:
                pass   # client hung up while we were working
            except Exception as e:  # surface real errors to the UI, don't 500-loop
                logging.getLogger("aptfinder").exception("GET %s failed", url.path)
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                self._send_json({"error": "invalid JSON"}, 400)
                return
            except DISCONNECTS:
                return
            route = post_routes(url.path, body)
            if route is None:
                self._send_json({"error": "not found"}, 404)
                return
            try:
                self._send_json(route())
            except DISCONNECTS:
                pass
            except Exception as e:
                logging.getLogger("aptfinder").exception("POST %s failed", url.path)
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    return Handler


def make_server(app_dir: Path, port: int = 0,
                host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Bind (port 0 = pick a free one). Caller runs serve_forever()."""
    return ThreadingHTTPServer((host, port), make_handler(Api(app_dir)))


def serve(app_dir: Path, port: int, open_browser: bool = True,
          host: str = "127.0.0.1") -> None:
    server = make_server(app_dir, port, host)
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    url = f"http://{shown}:{server.server_port}/"
    print(f"  aptfinder web UI: {url}  (Ctrl-C to stop)")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
