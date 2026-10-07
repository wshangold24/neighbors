"""Tests for the local web UI: API endpoints against a live server thread,
feedback roundtrips, prefs updates, and the page itself."""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from .conftest import seed_home

from neighbors.feedback import Feedback
from neighbors.models import Preferences
from neighbors.providers.rentcast import resolve_api_key
from neighbors.quota import QuotaLedger
from neighbors.views import features_to_amenities
from neighbors.web import Api, make_server


@pytest.fixture()
def web(tmp_path):
    """Server thread over a seeded ~/.neighbors dir; yields a request helper."""
    seed_home(tmp_path)
    app_dir = tmp_path / ".neighbors"
    server = make_server(app_dir, port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def call(path: str, body: dict | None = None, status: int = 200) -> dict | str:
        """Request `path` (POST if `body` given); assert the HTTP status."""
        req = urllib.request.Request(base + path)
        if body is not None:
            req.data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            resp = urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as e:
            resp = e
        with resp:
            assert resp.status == status, (path, resp.status)
            raw = resp.read().decode()
            return json.loads(raw) if "json" in resp.headers["Content-Type"] else raw

    yield call, app_dir
    server.shutdown()


def _rec(sources, verify=None, age_days=0):
    return {"listing": {"id": "x", "address": "1 Main St", "lat": 40.7, "lon": -73.9},
            "sources": {s: "http://u" for s in sources},
            "last_seen": time.time() - age_days * 86400,
            **({"verify": verify} if verify else {})}


class TestTrustSignal:
    """Api._trust rolls the building-registry check (PLUTO/HPD) and the official-
    listing check into one 'is this a real rental?' verdict."""

    def test_confirmed_needs_residential_building_and_active_official_listing(self, tmp_path):
        t = Api(tmp_path)._trust(
            _rec(["rentcast"], {"status": "ok", "units": 10, "registered": True}),
            Preferences())
        assert t["level"] == "confirmed"
        assert "verified rental" in t["label"] and "10-unit" in t["label"]
        assert "HPD-registered" in t["label"]

    def test_licensed_feed_is_named(self, tmp_path):
        t = Api(tmp_path)._trust(_rec(["rentcast"], {"status": "ok", "units": 4}),
                                 Preferences())
        assert "RentCast's licensed feed" in t["label"]

    def test_housing_connect_is_named(self, tmp_path):
        t = Api(tmp_path)._trust(_rec(["housingconnect"], {"status": "ok", "units": 20}),
                                 Preferences())
        assert "Housing Connect" in t["label"]

    def test_not_residential_is_caution(self, tmp_path):
        t = Api(tmp_path)._trust(_rec(["rentcast"], {"status": "not_residential"}),
                                 Preferences())
        assert t["level"] == "caution" and "isn't a residential building" in t["label"]

    def test_address_mismatch_is_caution(self, tmp_path):
        t = Api(tmp_path)._trust(
            _rec(["rentcast"], {"status": "mismatch", "km_off": 4.2, "units": 5}),
            Preferences())
        assert t["level"] == "caution" and "4.2 km" in t["label"]

    def test_stale_listing_is_caution(self, tmp_path):
        t = Api(tmp_path)._trust(_rec(["rentcast"], {"status": "ok", "units": 3}, age_days=30),
                                 Preferences(active_window_days=7))
        assert t["level"] == "caution" and "rented" in t["label"]

    def test_unverified_until_building_checked(self, tmp_path):
        t = Api(tmp_path)._trust(_rec(["housingconnect"]), Preferences())
        assert t["level"] == "unverified" and "not yet verified" in t["label"]


class TestVerifyEndpoint:
    """/api/verify checks listings against NYC building registries in batches;
    the building lookup is faked here so no network is touched."""

    @staticmethod
    def _fake_ok(budget=40):
        return lambda addr, lat, lon: {
            "found": True, "units": 6, "bldgclass": "C1", "landuse": "2",
            "registered": True, "lat": lat, "lon": lon,
            "bbl": "1", "bin": "1", "label": addr}

    def test_verify_batches_and_reports_remaining(self, tmp_path):
        # verification is scoped to the displayable (in-area, active) listings,
        # not the whole citywide store — that's what keeps "verify all" fast.
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        api._building_verifier = self._fake_ok
        visible = len(api._visible_ids(api._prefs(), api._store()))
        assert 0 < visible < 150          # gate really does narrow the set
        r = api.verify(batch=5)
        assert r["checked"] == 5 and r["total"] == visible
        assert r["remaining"] == visible - 5
        r2 = api.verify(batch=5)          # continues where the first left off
        assert r2["remaining"] == visible - 10

    def test_rentcast_key_save_and_status(self, web):
        call, app_dir = web
        assert call("/api/state")["rentcast_key_set"] is False
        assert call("/api/rentcast-key", {"key": "  test-key-123 "})["set"] is True
        assert resolve_api_key() == "test-key-123"
        assert call("/api/state")["rentcast_key_set"] is True
        # blank clears it
        assert call("/api/rentcast-key", {"key": ""})["set"] is False
        assert call("/api/state")["rentcast_key_set"] is False

    def test_rentcast_key_kept_off_disk_and_out_of_responses(self, web, fake_keyring):
        call, app_dir = web
        r = call("/api/rentcast-key", {"key": "secret-xyz"})
        assert "secret-xyz" not in json.dumps(r)
        assert "secret-xyz" not in json.dumps(call("/api/state"))
        assert fake_keyring.entries[("neighbors", "rentcast-api-key")] == "secret-xyz"
        assert not any("secret-xyz" in p.read_text(errors="ignore")
                       for p in app_dir.rglob("*") if p.is_file() and p.suffix != ".db")

    def test_rentcast_key_without_credential_store_reports_error(self, web):
        import keyring
        from keyring.backends import fail
        call, _ = web
        keyring.set_keyring(fail.Keyring())
        r = call("/api/rentcast-key", {"key": "k"})
        assert "RENTCAST_API_KEY" in r["error"]

    def test_rentcast_key_detected_from_env(self, web, monkeypatch):
        call, app_dir = web
        monkeypatch.setenv("RENTCAST_API_KEY", "env-key-xyz")
        # already available via env → don't ask for it
        assert call("/api/state")["rentcast_key_set"] is True

    def test_blank_stored_key_not_counted(self, web, fake_keyring):
        call, _ = web
        fake_keyring.set_password("neighbors", "rentcast-api-key", "   ")
        assert call("/api/state")["rentcast_key_set"] is False

    def test_setup_dismiss_persists(self, web):
        call, app_dir = web
        assert call("/api/state")["setup_dismissed"] is False
        assert call("/api/setup/dismiss", {})["ok"] is True
        assert (app_dir / "setup.json").exists()
        assert call("/api/state")["setup_dismissed"] is True

    def test_state_reports_verification_counts(self, web):
        call, _ = web
        v = call("/api/state")["verification"]
        assert set(v) >= {"confirmed", "caution", "unverified", "pending"}
        assert v["pending"] >= 1       # nothing verified yet in the seeded store

    def test_verify_single_listing_returns_trust(self, tmp_path):
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        api._building_verifier = self._fake_ok
        lid = api._store().active(window_days=7)[0].id
        r = api.verify(listing_id=lid)
        assert r["checked"] == 1 and r["trust"]["level"] == "confirmed"

    def test_verify_unknown_listing_errors(self, tmp_path):
        seed_home(tmp_path)
        assert "error" in Api(tmp_path / ".neighbors").verify(listing_id="nope")

    def test_results_show_cached_when_all_stale(self, tmp_path):
        # regression: a full store whose listings have all aged past the active
        # window must still show them (flagged stale), not an empty screen.
        from neighbors.store import Store
        seed_home(tmp_path)
        app_dir = tmp_path / ".neighbors"
        store = Store(app_dir / "store.db")
        for r in store.records().values():
            r["last_seen"] = time.time() - 60 * 86400   # 60 days ago
        store.save()
        api = Api(app_dir)
        assert api._store().active(window_days=7) == []   # nothing "fresh"
        res = api.results()
        assert res["stale"] is True
        assert len(res["results"]) > 0                    # cached listings shown

    def test_results_not_stale_when_fresh(self, tmp_path):
        seed_home(tmp_path)
        res = Api(tmp_path / ".neighbors").results()
        assert res["stale"] is False and len(res["results"]) > 0

    def test_empty_results_explain_which_filter(self, tmp_path):
        # when the store is full but every listing is filtered out, results must
        # carry a diagnostic naming the culprit rule (not just return blank).
        import dataclasses, json
        from neighbors.models import Preferences
        seed_home(tmp_path)
        app = tmp_path / ".neighbors"
        prefs = Preferences.from_dict(json.loads((app / "preferences.json").read_text()))
        prefs = dataclasses.replace(prefs, max_price=100)   # impossibly low
        (app / "preferences.json").write_text(json.dumps(prefs.to_dict()))
        res = Api(app).results()
        assert res["results"] == []
        d = res["diagnostic"]
        assert d and d["store"] == 150
        assert d["fails"]["price_cap"] == 150      # names the real reason

    def test_diagnostic_absent_when_results_present(self, tmp_path):
        seed_home(tmp_path)
        assert Api(tmp_path / ".neighbors").results()["diagnostic"] is None

    def test_results_survive_null_amenities_record(self, tmp_path):
        # regression: a listing with derived_amenities=None (persisted null)
        # must not crash results() — it should be healed to [] and shown.
        from neighbors.store import Store
        seed_home(tmp_path)
        app = tmp_path / ".neighbors"
        store = Store(app / "store.db")
        for rec in store.records().values():
            rec["listing"]["derived_amenities"] = None
            rec["listing"]["inferred_amenities"] = None
        store.save()
        res = Api(app).results()               # must not raise
        assert "error" not in res
        assert len(res["results"]) > 0

    def test_save_area_persists_and_constrains(self, tmp_path):
        # "Use in preferences" must save the drawn shape and keep results inside
        # it — not expand to neighborhood-name radii that drag far listings back.
        from neighbors.store import Store
        from neighbors.models import Listing, Preferences
        import json
        app = tmp_path / ".neighbors"; app.mkdir(parents=True)
        (app / "preferences.json").write_text(json.dumps(
            Preferences(max_price=9000, beds=0, k=50).to_dict()))
        st = Store(app / "store.db")
        st.upsert([
            Listing(id="in", source="s", url="u", address="1 A", neighborhood="",
                    price=3000, beds=1.0, baths=1.0, lat=40.715, lon=-73.960),
            Listing(id="out", source="s", url="u", address="2 B", neighborhood="",
                    price=3000, beds=1.0, baths=1.0, lat=40.650, lon=-74.010),
        ])
        st.save()
        api = Api(app)
        bbox = [40.70, -73.98, 40.73, -73.94]        # around "in", excludes "out"
        r = api.save_area(bbox=bbox)
        assert r["ok"] and r["prefs"]["area"] == {"bbox": bbox}
        ids = [x["id"] for x in Api(app).results()["results"]]
        assert "in" in ids and "out" not in ids       # constrained to the shape
        # clearing removes it
        api.save_area()                                # no shape → clear
        assert Api(app)._prefs().area is None
        assert set(x["id"] for x in Api(app).results()["results"]) == {"in", "out"}

    def test_saved_area_survives_unrelated_prefs_save_but_clears_on_location(self, tmp_path):
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        api.save_area(bbox=[40.6, -74.0, 40.8, -73.9])
        assert api._prefs().area is not None
        # saving prefs without touching location keeps the area
        api.update_prefs({"max_price": 4200, "boroughs": [], "neighborhoods": []})
        assert api._prefs().area is not None
        # picking a neighborhood supersedes the drawn area
        api.update_prefs({"neighborhoods": ["williamsburg"]})
        assert api._prefs().area is None

    def test_partial_refresh_does_not_collapse_list(self, tmp_path):
        # regression: a fetch that re-confirms only a slice of the market must
        # not make the rest (recently seen, just aged past the window) vanish.
        from neighbors.store import Store
        seed_home(tmp_path)
        app_dir = tmp_path / ".neighbors"
        api = Api(app_dir)
        full = len(api.results()["results"])       # everything fresh
        assert full > 3

        store = Store(app_dir / "store.db")
        old = time.time() - 10 * 86400
        recs = sorted(store.records().values(),
                      key=lambda r: r.get("last_seen", 0), reverse=True)
        for r in recs:
            r["listing"]["last_seen"] = old
            r["last_seen"] = old               # everything now stale
        for r in recs[:2]:
            r["last_seen"] = time.time()       # a fetch re-saw just two
        store.save()

        res = Api(app_dir).results()
        assert res["stale"] is True            # flagged as including older ones
        assert len(res["results"]) == full     # …but nothing vanished

    def test_features_to_amenities_maps_cooling_and_garage(self, tmp_path):
        body = json.dumps([{"features": {"cooling": True, "garageSpaces": 2}}])
        assert set(features_to_amenities(body)) == {"central_air", "parking"}
        assert features_to_amenities(None) == []
        assert features_to_amenities(json.dumps([{"features": {}}])) == []

    def test_enrich_unknown_listing_errors(self, tmp_path):
        seed_home(tmp_path)
        assert "error" in Api(tmp_path / ".neighbors").enrich("does-not-exist")

    def test_soft_commute_penalty(self, tmp_path):
        from neighbors.models import Listing, NEIGHBORHOOD_COORDS
        api = Api(tmp_path)
        wlat, wlon = NEIGHBORHOOD_COORDS["williamsburg"]
        rlat, rlon = NEIGHBORHOOD_COORDS["riverdale"]
        prefs = Preferences(places=[{"name": "Work", "lat": wlat, "lon": wlon,
                                     "limit": "soft", "max_min": 10}])

        def mk(lat, lon):
            return Listing(id="x", source="t", url="", address="a",
                           neighborhood="n", price=3000, beds=1, baths=1, lat=lat, lon=lon)
        assert api._place_penalty(mk(wlat, wlon), prefs) == 0.0   # at the place
        assert api._place_penalty(mk(rlat, rlon), prefs) > 0.0    # far away

    def test_priority_scales_with_verify_priority(self, tmp_path):
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        store = api._store()
        listings = store.active(window_days=7)
        lid = listings[0].id
        assert api._priority_map(listings, store, Preferences(verify_priority="off")) == {}
        gentle = api._priority_map(listings, store, Preferences(verify_priority="gentle"))
        strong = api._priority_map(listings, store, Preferences(verify_priority="strong"))
        assert gentle[lid] == 0.15 and strong[lid] == 0.30   # unverified × strength


class TestWebApi:
    def test_results_carry_coordinates_for_map(self, web):
        call, _ = web
        r = call("/api/results")["results"][0]
        assert "lat" in r and "lon" in r and r["lat"] and r["lon"]

    def test_results_carry_trust_signal(self, web):
        call, _ = web
        r = call("/api/results")["results"][0]
        assert r["trust"]["level"] in {"confirmed", "caution", "unverified"}
        assert isinstance(r["trust"]["sources"], list) and r["trust"]["label"]

    def test_index_serves_page(self, web):
        call, _ = web
        page = call("/")
        assert "<title>Neighbors</title>" in page

    def test_about_page_served(self, web):
        call, _ = web
        for path in ("/about", "/about/"):
            assert "<title>About · Neighbors</title>" in call(path)
        assert 'href="/about"' in call("/")          # linked from the app header

    def test_state_reports_store_and_quota(self, web):
        call, app_dir = web
        ledger = QuotaLedger(app_dir / "store.db")
        ledger.set_used("rentcast", 4)
        ledger.save()
        state = call("/api/state")
        assert state["store"]["total"] == 150
        assert state["prefs"]["max_price"] == 3500
        assert state["quota"]["rentcast"] == {"used": 4, "limit": 50, "stop_at": 45}

    def test_results_ranked_and_marked_seen(self, web):
        call, app_dir = web
        data = call("/api/results?alpha=1.0")
        results = data["results"]
        assert len(results) == 10
        assert all(r["match"] is not None for r in results)
        matches = [r["match"] for r in results]
        assert matches == sorted(matches, reverse=True)
        # results are remembered for "shown before" flags
        fb = Feedback(app_dir / "store.db")
        assert len(fb.seen) == 10

    def test_feedback_roundtrip_updates_results(self, web):
        call, app_dir = web
        first = call("/api/results")["results"][0]
        r = call("/api/feedback", {"id": first["id"], "verdict": "like"})
        assert r["ok"] and r["liked"] == 1
        ids = {x["id"] for x in call("/api/results")["results"]}
        assert first["id"] not in ids, "liked listings leave the results list"
        call("/api/feedback", {"id": first["id"], "verdict": "clear"})
        fb = Feedback(app_dir / "store.db")
        assert fb.liked == {}

    def test_blend_absent_without_likes_present_after_like(self, web):
        call, _ = web
        data = call("/api/results?alpha=0.6")
        assert data["alpha"] == 0.6
        assert all(r["blend"] is None for r in data["results"]), \
            "no likes yet → nothing to blend"
        call("/api/feedback", {"id": data["results"][0]["id"], "verdict": "like"})
        with_likes = call("/api/results?alpha=0.6")["results"]
        assert all(isinstance(r["blend"]["pref"], float) and
                   isinstance(r["blend"]["seed"], float) for r in with_likes)

    def test_similar_endpoint(self, web):
        call, _ = web
        seed = call("/api/results")["results"][0]
        data = call(f"/api/similar?id={seed['id']}")
        assert data["seed"]["id"] == seed["id"]
        assert data["results"] and all(r["id"] != seed["id"] for r in data["results"])

    def test_liked_endpoint_returns_comparable_shortlist(self, web):
        call, _ = web
        first = call("/api/results")["results"][0]
        call("/api/feedback", {"id": first["id"], "verdict": "like"})
        liked = call("/api/liked")["results"]
        assert len(liked) == 1
        row = liked[0]
        assert row["id"] == first["id"] and row["liked"] is True
        # scored against prefs (the yardstick every card uses) so columns compare
        assert row["match"] is not None
        # compare view needs these fields per column
        assert "commutes" in row and "stations" in row and row["active"] is True
        # still absent from the ranked results — the shortlist is the only place
        ids = {r["id"] for r in call("/api/results")["results"]}
        assert first["id"] not in ids

    def test_liked_newest_first(self, web):
        call, _ = web
        res = call("/api/results")["results"]
        call("/api/feedback", {"id": res[0]["id"], "verdict": "like"})
        call("/api/feedback", {"id": res[1]["id"], "verdict": "like"})
        liked = call("/api/liked")["results"]
        assert [r["id"] for r in liked][:2] == [res[1]["id"], res[0]["id"]]

    def test_exclude_neighborhoods_roundtrip_and_gate(self, web):
        call, _ = web
        # exclude a neighborhood; it must persist and drop those listings
        r = call("/api/prefs", {"exclude_neighborhoods": ["east village"]})
        assert r["ok"] and r["prefs"]["exclude_neighborhoods"] == ["east village"]
        results = call("/api/results")["results"]
        assert all((x["neighborhood"] or "").lower() != "east village" for x in results)

    def test_fetch_limit_roundtrip_and_clamp(self, web):
        call, _ = web
        assert call("/api/prefs", {"fetch_limit": 500})["prefs"]["fetch_limit"] == 500
        assert call("/api/prefs", {"fetch_limit": 99999})["prefs"]["fetch_limit"] == 1000
        assert call("/api/prefs", {"fetch_limit": 1})["prefs"]["fetch_limit"] == 100

    def test_verify_priority_roundtrip_and_default(self, web):
        call, _ = web
        r = call("/api/prefs", {"verify_priority": "strong"})
        assert r["ok"] and r["prefs"]["verify_priority"] == "strong"
        r2 = call("/api/prefs", {"verify_priority": "bogus"})   # invalid → coerced
        assert r2["ok"] and r2["prefs"]["verify_priority"] == "gentle"

    def test_boroughs_roundtrip_and_options(self, web):
        call, _ = web
        assert "Brooklyn" in call("/api/state")["options"]["regions"]
        r = call("/api/prefs", {"boroughs": ["Brooklyn", "Queens"]})
        assert r["ok"] and r["prefs"]["boroughs"] == ["Brooklyn", "Queens"]

    def test_boroughs_reject_unknown(self, web):
        call, _ = web
        assert "error" in call("/api/prefs", {"boroughs": ["Atlantis"]}, status=400)

    def test_state_exposes_neighborhood_region_map(self, web):
        call, _ = web
        nr = call("/api/state")["options"]["neighborhood_region"]
        assert nr["williamsburg"] == "Brooklyn" and nr["astoria"] == "Queens"

    def test_search_this_area_filters_to_bbox(self, tmp_path):
        from neighbors.models import NEIGHBORHOOD_COORDS
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        # a tight box around Williamsburg only
        wlat, wlon = NEIGHBORHOOD_COORDS["williamsburg"]
        bbox = (wlat - 0.02, wlon - 0.02, wlat + 0.02, wlon + 0.02)
        res = api.results(bbox=bbox)["results"]
        assert res, "expected some listings in the box"
        assert all(bbox[0] <= r["lat"] <= bbox[2] and bbox[1] <= r["lon"] <= bbox[3]
                   for r in res)

    def test_search_area_polygon_filters(self, tmp_path):
        from neighbors.models import NEIGHBORHOOD_COORDS
        from neighbors.webapi import point_in_poly
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        wlat, wlon = NEIGHBORHOOD_COORDS["williamsburg"]
        d = 0.06
        # a diamond polygon around Williamsburg
        poly = [(wlat + d, wlon), (wlat, wlon + d), (wlat - d, wlon), (wlat, wlon - d)]
        res = api.results(poly=poly)["results"]
        assert res, "expected some listings inside the shape"
        assert all(point_in_poly(r["lat"], r["lon"], poly) for r in res)
        # it should exclude at least one listing that's outside the diamond
        assert len(res) < len(api.results()["results"])
        # a degenerate polygon (<3 pts) is ignored → full results
        assert len(api.results(poly=[(wlat, wlon)])["results"]) == len(api.results()["results"])

    def test_save_area_writes_neighborhoods_and_clears_boroughs(self, tmp_path):
        from neighbors.models import NEIGHBORHOOD_COORDS
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        api.update_prefs({"boroughs": ["Queens"]})
        wlat, wlon = NEIGHBORHOOD_COORDS["williamsburg"]
        r = api.save_area((wlat - 0.02, wlon - 0.02, wlat + 0.02, wlon + 0.02))
        assert r["ok"] and "williamsburg" in r["neighborhoods"]
        assert r["prefs"]["boroughs"] == []                 # area supersedes boroughs

    def test_exclude_rejects_unknown_neighborhood(self, web):
        call, _ = web
        r = call("/api/prefs", {"exclude_neighborhoods": ["atlantis"]}, status=400)
        assert "error" in r

    def test_wanted_and_excluded_are_mutually_exclusive(self, web):
        call, _ = web
        r = call("/api/prefs", {"neighborhoods": ["williamsburg", "astoria"],
                                "exclude_neighborhoods": ["astoria"]})
        assert r["ok"]
        assert "astoria" not in r["prefs"]["neighborhoods"]
        assert r["prefs"]["exclude_neighborhoods"] == ["astoria"]

    def test_state_includes_canonical_options(self, web):
        call, _ = web
        opts = call("/api/state")["options"]
        assert "williamsburg" in opts["neighborhoods"]
        assert "dishwasher" in opts["amenities"]
        assert opts["sources"] == ["rentcast", "housingconnect"]
        # data-backed amenities are surfaced so the picker can mark them
        assert "elevator" in opts["sourced_amenities"]
        assert "central_air" in opts["sourced_amenities"]

    def test_sources_preference_roundtrip(self, web):
        call, app_dir = web
        r = call("/api/prefs", {"sources": ["housingconnect", "rentcast"]})
        assert r["ok"] and r["prefs"]["sources"] == ["housingconnect", "rentcast"]
        on_disk = json.loads((app_dir / "preferences.json").read_text())
        assert on_disk["sources"] == ["housingconnect", "rentcast"]

    def test_prefs_rejects_unknown_multiselect_values(self, web):
        call, app_dir = web
        r = call("/api/prefs", {"neighborhoods": ["narnia"]}, status=400)
        assert "unknown values: narnia" in r["error"]
        r2 = call("/api/prefs", {"amenities": ["moat"]}, status=400)
        assert "moat" in r2["error"]
        r3 = call("/api/prefs", {"sources": ["zillow"]}, status=400)
        assert "zillow" in r3["error"]
        # nothing was persisted
        on_disk = json.loads((app_dir / "preferences.json").read_text())
        assert on_disk["neighborhoods"] == ["williamsburg", "greenpoint"]

    def test_prefs_update_persists_and_reranks(self, web):
        call, app_dir = web
        r = call("/api/prefs", {"max_price": 2800, "bogus_field": 1})
        assert r["ok"] and r["prefs"]["max_price"] == 2800
        on_disk = json.loads((app_dir / "preferences.json").read_text())
        assert on_disk["max_price"] == 2800
        # hard gate now applies the new cap (plus stretch)
        results = call("/api/results")["results"]
        assert all(x["price"] <= 2800 * 1.15 for x in results)

    def test_unknown_routes_404(self, web):
        call, _ = web
        assert call("/api/nope", status=404) == {"error": "not found"}
        assert "error" in call("/api/nope", {}, status=404)

    def test_source_links_present(self, web):
        call, _ = web
        r = call("/api/results")["results"][0]
        assert r["sources"] and all(u.startswith("http") for u in r["sources"].values())

    def test_deltas_and_weights_for_breakdown_panel(self, web):
        call, _ = web
        state = call("/api/state")
        assert set(state["weights"]) == {"price", "beds", "baths", "location",
                                         "sqft", "amenities"}
        r = call("/api/results")["results"][0]
        d = r["deltas"]
        assert isinstance(d["price"], int)          # signed $ vs max budget
        assert d["location_km"] is None or d["location_km"] >= 0
        assert isinstance(d["amenities_missing"], list)

    def test_nearby_stations_on_cards(self, web):
        call, _ = web
        results = call("/api/results")["results"]
        with_stations = [r for r in results if r["stations"]]
        assert with_stations, "demo listings are in real neighborhoods — some must have stations"
        s = with_stations[0]["stations"][0]
        assert s["lines"] and s["name"] and 1 <= s["walk_min"] <= 10

    def test_maps_links_present(self, web):
        call, _ = web
        r = call("/api/results")["results"][0]
        assert r["maps"]["apple"].startswith("https://maps.apple.com/?q=")
        assert "google.com/maps" in r["maps"]["google"]

    def test_commutes_with_geocoded_place(self, web):
        call, app_dir = web
        # write a place with known coords directly (bypasses live geocoding)
        prefs = json.loads((app_dir / "preferences.json").read_text())
        prefs["places"] = [{"name": "Work", "address": "89 E 42nd St, Manhattan",
                            "lat": 40.7527, "lon": -73.9772}]
        (app_dir / "preferences.json").write_text(json.dumps(prefs))

        r = call("/api/results")["results"][0]
        c = r["commutes"][0]
        assert c["name"] == "Work"
        assert isinstance(c["minutes"], int) and 5 <= c["minutes"] <= 90
        assert c["route"]
        # ordered itinerary: walk in / ride / walk out, each with minutes
        kinds = [s["kind"] for s in c["steps"]]
        assert kinds[0] == "walk" and kinds[-1] == "walk" and "ride" in kinds
        assert sum(s["minutes"] for s in c["steps"]) == c["minutes"]
        assert "dirflg=r" in c["apple"], "Apple link must request transit"
        assert "travelmode=transit" in c["google"]

    def test_place_without_coords_still_gets_direction_links(self, web):
        call, app_dir = web
        prefs = json.loads((app_dir / "preferences.json").read_text())
        prefs["places"] = [{"name": "Gym", "address": "somewhere vague",
                            "lat": None, "lon": None}]
        (app_dir / "preferences.json").write_text(json.dumps(prefs))
        c = call("/api/results")["results"][0]["commutes"][0]
        assert c["minutes"] is None and c["apple"] and c["google"]

    def test_geocode_endpoint_guards_incomplete_input(self, web):
        """Missing street/city must return found=False WITHOUT any network
        call (this also keeps the test suite offline)."""
        call, _ = web
        r = call("/api/geocode?street=abc&city=")
        assert r["found"] is False and "street" in r["error"]
        r2 = call("/api/geocode?street=30%20Hudson%20Street")  # no city
        assert r2["found"] is False

    def test_saving_verified_coords_trusted_without_geocoding(self, web):
        """Places submitted with lat/lon (verified in the UI) are stored
        as-is — no geocode network call happens. Any city works (the whole
        point: a Jersey City work address)."""
        call, app_dir = web
        r = call("/api/prefs", {"places": [
            {"name": "Work", "street": "30 Hudson St", "city": "Jersey City",
             "zip": "07302", "lat": 40.7127, "lon": -74.0134}]})
        assert r["ok"], r
        on_disk = json.loads((app_dir / "preferences.json").read_text())
        assert on_disk["places"][0]["lat"] == 40.7127
        assert on_disk["places"][0]["city"] == "Jersey City"
        assert "Jersey City" in on_disk["places"][0]["address"]

    def test_save_rejected_when_place_cannot_geocode(self, web):
        """Strictness: an address that fails validation must never be saved
        (guard-level failure here, so no network)."""
        call, app_dir = web
        before = (app_dir / "preferences.json").read_text()
        r = call("/api/prefs", {"places": [{"name": "Gym", "street": "x", "city": ""}]},
                 status=400)
        assert "couldn't locate: Gym" in r["error"]
        assert (app_dir / "preferences.json").read_text() == before

    def test_realtor_link_present_and_scoped(self, web):
        call, _ = web
        r = call("/api/results")["results"][0]
        assert "site%3Arealtor.com" in r["maps"]["realtor"]

    def test_saving_places_reuses_existing_coords(self, web):
        call, app_dir = web
        prefs = json.loads((app_dir / "preferences.json").read_text())
        prefs["places"] = [{"name": "Work", "address": "89 E 42nd St",
                            "lat": 40.7527, "lon": -73.9772}]
        (app_dir / "preferences.json").write_text(json.dumps(prefs))
        # resubmit same address (as the UI does) → coords kept, NO geocode call
        r = call("/api/prefs", {"places": [{"name": "Job", "address": "89 E 42nd St"}]})
        assert r["ok"]
        assert r["prefs"]["places"][0]["lat"] == 40.7527
        assert r["prefs"]["places"][0]["name"] == "Job"


class TestRequestRobustness:
    """Malformed requests get a 4xx JSON error — never a 500, a dropped
    connection, or bad data persisted."""

    @staticmethod
    def _raw(app_dir, method: str, path: str, body: bytes = b"",
             headers: dict | None = None) -> tuple[int, dict]:
        import http.client
        server = make_server(app_dir, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            conn.putrequest(method, path)
            for k, v in {"Content-Length": str(len(body)), **(headers or {})}.items():
                conn.putheader(k, v)
            conn.endheaders(body)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read())
        finally:
            server.shutdown()
            server.server_close()

    @pytest.mark.parametrize("query", ["alpha=abc", "alpha=2", "alpha=nan",
                                       "bbox=1,2,3", "bbox=a,b,c,d", "poly=1,2,3,4",
                                       "poly=1,2,3,4,5,6,7"])
    def test_bad_results_query_is_400(self, web, query):
        call, _ = web
        assert "error" in call(f"/api/results?{query}", status=400)

    @pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b"null", b'"x"', b"\xff\xfe"])
    def test_malformed_post_body_is_400(self, tmp_path, body):
        status, data = self._raw(tmp_path, "POST", "/api/prefs", body)
        assert status == 400 and "error" in data

    def test_bad_content_length_is_400(self, tmp_path):
        status, _ = self._raw(tmp_path, "POST", "/api/fetch",
                              headers={"Content-Length": "abc"})
        assert status == 400
        status, _ = self._raw(tmp_path, "POST", "/api/fetch",
                              headers={"Content-Length": "-1"})
        assert status == 400

    def test_oversized_body_is_413(self, tmp_path):
        status, _ = self._raw(tmp_path, "POST", "/api/prefs",
                              headers={"Content-Length": str(10 ** 8)})
        assert status == 413

    def test_wrong_method_is_405(self, web):
        call, _ = web
        call("/api/feedback", status=405)
        call("/api/state", {}, status=405)

    def test_no_file_serving_beyond_the_page(self, web):
        call, _ = web
        for path in ("/../neighbors/cli.py", "/web.html", "/%2e%2e/pyproject.toml",
                     "/api/../../etc/passwd"):
            call(path, status=404)

    def test_feedback_requires_listing_id(self, web):
        call, app_dir = web
        call("/api/feedback", {"verdict": "like"}, status=400)
        call("/api/feedback", {"id": 5, "verdict": "like"}, status=400)
        call("/api/feedback", {"id": "x", "verdict": "love"}, status=400)
        assert Feedback(app_dir / "store.db").liked == {}

    @pytest.mark.parametrize("fields", [{"max_price": "abc"}, {"boroughs": None},
                                        {"beds": True}, {"k": -1}, {"min_sqft": "5"},
                                        {"places": ["work"]}, {"verify_priority": ["x"]},
                                        {"area": "everywhere"}])
    def test_wrongly_typed_prefs_rejected_not_persisted(self, web, fields):
        call, app_dir = web
        before = (app_dir / "preferences.json").read_text()
        if "verify_priority" in fields:   # coerced to the default, not rejected
            assert call("/api/prefs", fields)["prefs"]["verify_priority"] == "gentle"
            return
        assert "error" in call("/api/prefs", fields, status=400)
        assert (app_dir / "preferences.json").read_text() == before
        assert call("/api/results")["results"]       # still serves results

    def test_bad_place_coordinates_fall_back_to_geocode(self, web):
        call, _ = web
        r = call("/api/prefs", {"places": [{"name": "Gym", "street": "1 Main St",
                                            "city": "", "lat": "x", "lon": None}]},
                 status=400)
        assert "couldn't locate: Gym" in r["error"]

    def test_malformed_area_does_not_clear_saved_area(self, web):
        call, app_dir = web
        call("/api/area", {"bbox": [40.6, -74.0, 40.8, -73.9]})
        call("/api/area", {"bbox": ["a", "b", "c", "d"]}, status=400)
        call("/api/area", {"poly": [[1, 2]]}, status=400)
        assert Api(app_dir)._prefs().area == {"bbox": [40.6, -74.0, 40.8, -73.9]}
        assert call("/api/area", {})["cleared"] is True

    def test_saved_junk_area_is_ignored(self, tmp_path):
        seed_home(tmp_path)
        app_dir = tmp_path / ".neighbors"
        prefs = json.loads((app_dir / "preferences.json").read_text())
        prefs["area"] = {"bbox": "nope"}
        (app_dir / "preferences.json").write_text(json.dumps(prefs))
        assert Api(app_dir).results()["results"]

    def test_bad_verify_batch_and_key_types_are_400(self, web):
        call, _ = web
        call("/api/verify", {"batch": "x"}, status=400)
        call("/api/verify", {"batch": 0}, status=400)
        call("/api/rentcast-key", {"key": 123}, status=400)

    def test_unknown_listing_is_404(self, web):
        call, _ = web
        assert "error" in call("/api/similar?id=nope", status=404)
        call("/api/verify", {"id": "nope"}, status=404)
        call("/api/enrich", {"id": "nope"}, status=404)

    def test_fresh_install_dir_is_created(self, tmp_path):
        status, data = self._raw(tmp_path / "new" / ".neighbors", "POST",
                                 "/api/setup/dismiss", b"{}")
        assert status == 200 and data == {"ok": True}

    def test_corrupt_digest_marker_does_not_break_digest(self, web):
        call, app_dir = web
        (app_dir / "digest.json").write_text("[1, 2]")
        assert "error" not in call("/api/digest")


class TestBuildingVerifier:
    def test_exhausted_budget_leaves_listings_unverified(self, tmp_path):
        """Regression: running out of request budget must not read as 'no
        building record' (which flags a listing as caution for 30 days)."""
        from neighbors.http import BudgetExceeded
        seed_home(tmp_path)
        api = Api(tmp_path / ".neighbors")
        real = Api._building_verifier
        with pytest.raises(BudgetExceeded):   # budget 0: no network is touched
            real(api, budget=0)("100 Bedford Ave, Brooklyn", None, None)

        api._building_verifier = lambda budget=40: real(api, budget=0)
        r = api.verify(batch=5)
        assert r["checked"] == 0 and r["remaining"] == r["total"] > 0
        assert api._verification_summary(api._prefs(), api._store())["caution"] == 0
        lid = api._store().active(window_days=7)[0].id
        assert api.verify(listing_id=lid).status == 429
        assert "verify" not in next(r for r in api._store().records().values()
                                    if r["listing"]["id"] == lid)

    def test_registry_ids_are_digits_only(self):
        from neighbors.webapi import _registry_id
        assert _registry_id("1000010001") == "1000010001"
        assert _registry_id(3001234) == "3001234"
        for junk in (None, "", "1' OR '1'='1", "12.5", "abc"):
            assert _registry_id(junk) is None


class TestConcurrency:
    def test_parallel_likes_and_results_lose_no_feedback(self, web):
        """Feedback is saved whole; concurrent writers must not clobber
        each other's likes."""
        from concurrent.futures import ThreadPoolExecutor
        call, app_dir = web
        ids = [x["id"] for x in call("/api/results")["results"]][:6]
        with ThreadPoolExecutor(8) as pool:
            jobs = [pool.submit(call, "/api/feedback", {"id": i, "verdict": "hide"})
                    for i in ids]
            jobs += [pool.submit(call, "/api/results") for _ in range(4)]
            for j in jobs:
                j.result()
        assert set(Feedback(app_dir / "store.db").hidden) == set(ids)
