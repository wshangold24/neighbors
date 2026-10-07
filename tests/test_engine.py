"""Tests for the engine milestones: manifest coverage (M1), listing store
(M2), gate/rank + feedback (M3), quota ledger (M4), learned weights (M5),
and the engine CLI session end-to-end."""

from __future__ import annotations

import json
import time

from .conftest import run_cli, seed_home

from neighbors.engine import gate, plan_fetch, rank
from neighbors.feedback import Feedback
from neighbors.knn import DEFAULT_WEIGHTS
from neighbors.learn import MIN_LIKES, learn_weights, load_weights, save_weights
from neighbors.manifest import Manifest, covers, prefs_fingerprint, prefs_snapshot
from neighbors.models import Listing, Preferences
from neighbors.quota import MONTHLY_LIMITS, QuotaLedger, current_month
from neighbors.store import Store, identity


def L(**kw) -> Listing:
    base = dict(id=kw.get("id", "t1"), source="test", url="https://x.test/1",
                address="123 Kent Ave #4B", neighborhood="williamsburg",
                price=3000, beds=1, baths=1)
    base.update(kw)
    return Listing(**base)


PREFS = Preferences(max_price=3500, beds=1, neighborhoods=["williamsburg"],
                    price_stretch=0.15, k=10)


# --------------------------------------------------------------------------
# M1 — manifest
# --------------------------------------------------------------------------

class TestManifest:
    def test_fingerprint_stable_and_fetch_relevant_only(self):
        a = Preferences(max_price=3500, neighborhoods=["astoria"], k=5)
        b = Preferences(max_price=3500, neighborhoods=["astoria"], k=99, beds=3)
        assert prefs_fingerprint(a) == prefs_fingerprint(b), \
            "k/beds don't change the fetch — fingerprint must ignore them"
        c = Preferences(max_price=3600, neighborhoods=["astoria"])
        assert prefs_fingerprint(a) != prefs_fingerprint(c)

    def test_covers_price_and_neighborhood_rules(self):
        broad = prefs_snapshot(Preferences(max_price=4000,
                                           neighborhoods=["williamsburg", "greenpoint"]))
        narrow = prefs_snapshot(Preferences(max_price=3500,
                                            neighborhoods=["williamsburg"]))
        citywide = prefs_snapshot(Preferences(max_price=4000, neighborhoods=[]))
        assert covers(broad, narrow)
        assert not covers(narrow, broad), "narrower price cap can't cover broader"
        assert covers(citywide, narrow), "city-wide fetch covers any neighborhood"
        assert not covers(narrow, citywide), "area fetch can't cover city-wide"

    def test_latest_covering_respects_age_and_provider(self, tmp_path):
        m = Manifest(tmp_path / "manifest.jsonl")
        m.record("rentcast", PREFS, listings_found=100)
        assert m.latest_covering("rentcast", PREFS, max_age_s=60)
        assert m.latest_covering("renthop", PREFS, max_age_s=60) is None
        assert m.latest_covering("rentcast", PREFS, max_age_s=-1) is None  # too old

    def test_empty_fetches_never_count_as_coverage(self, tmp_path):
        m = Manifest(tmp_path / "manifest.jsonl")
        m.record("rentcast", PREFS, listings_found=0)
        assert m.latest_covering("rentcast", PREFS, max_age_s=60) is None


# --------------------------------------------------------------------------
# M2 — store
# --------------------------------------------------------------------------

class TestStore:
    def test_upsert_new_then_merge_across_sources(self, tmp_path):
        store = Store(tmp_path / "store.db")
        stats = store.upsert([L(id="rc-1", source="rentcast")])
        assert stats.new == ["rc-1"]
        # same address from another site → merge, not duplicate
        stats = store.upsert([L(id="rh-9", source="renthop",
                                url="https://renthop.test/9")])
        assert len(store) == 1 and stats.updated == 1
        rec = next(iter(store.records().values()))
        assert set(rec["sources"]) == {"rentcast", "renthop"}
        assert rec["listing"]["id"] == "rc-1", "feedback id must stay stable"

    def test_price_history_and_drop_detection(self, tmp_path):
        store = Store(tmp_path / "store.db")
        store.upsert([L(price=3200)], now=1000)
        stats = store.upsert([L(price=3000)], now=2000)
        assert stats.price_drops == {"t1": (3200, 3000)}
        rec = next(iter(store.records().values()))
        assert [p for _, p in rec["price_history"]] == [3200, 3000]
        assert "↓$200" in store.annotations(now=2000)["t1"]

    def test_merge_never_blanks_known_fields(self, tmp_path):
        store = Store(tmp_path / "store.db")
        store.upsert([L(sqft=650)])
        store.upsert([L(sqft=None)])  # scraper without sqft data
        rec = next(iter(store.records().values()))
        assert rec["listing"]["sqft"] == 650

    def test_null_amenities_healed_on_load(self, tmp_path):
        # a record persisted with null list fields (older schema / bad merge)
        # must load back as [] so display and verification don't crash.
        store = Store(tmp_path / "store.db")
        store.upsert([L()])
        rec = next(iter(store.records().values()))
        rec["listing"]["derived_amenities"] = None
        rec["listing"]["amenities"] = None
        store.save()
        reloaded = Store(tmp_path / "store.db")
        got = next(iter(reloaded.records().values()))["listing"]
        assert got["derived_amenities"] == [] and got["amenities"] == []

    def test_merge_never_writes_null_for_missing_old_field(self, tmp_path):
        store = Store(tmp_path / "store.db")
        store.upsert([L()])
        rec = next(iter(store.records().values()))
        rec["listing"].pop("derived_amenities", None)   # old-schema record
        store.upsert([L()])                             # fresh has empty list
        got = next(iter(store.records().values()))["listing"]
        assert got.get("derived_amenities") == []       # not None

    def test_merge_tolerates_old_record_missing_new_fields(self, tmp_path):
        # regression: a record written before a field existed (here we drop
        # derived_amenities to simulate the older schema) must not KeyError when
        # a fresh listing whose value for that field is empty is merged in.
        store = Store(tmp_path / "store.db")
        store.upsert([L()])
        rec = next(iter(store.records().values()))
        rec["listing"].pop("derived_amenities", None)   # old-schema record
        store.upsert([L()])                             # merge must not crash
        assert next(iter(store.records().values()))["listing"]["id"] == L().id

    def test_active_window_excludes_stale(self, tmp_path):
        store = Store(tmp_path / "store.db")
        now = time.time()
        store.upsert([L(id="old", address="1 Old St")], now=now - 30 * 24 * 3600)
        store.upsert([L(id="new", address="2 New St")], now=now)
        assert [l.id for l in store.active(now=now)] == ["new"]

    def test_persistence_roundtrip(self, tmp_path):
        s1 = Store(tmp_path / "store.db")
        s1.upsert([L()])
        s1.save()
        assert len(Store(tmp_path / "store.db")) == 1

    def test_migrates_legacy_json_store(self, tmp_path):
        # a genuine old JSON store next to where the DB will live is imported once
        rec = {"listing": {"id": "rc-1", "address": "5 A St", "source": "rentcast",
                           "url": "u", "neighborhood": "williamsburg", "price": 3000,
                           "beds": 1, "baths": 1}, "first_seen": 1.0, "last_seen": 2.0,
               "price_history": [[1.0, 3000]], "sources": {"rentcast": "u"}}
        (tmp_path / "store.json").write_text(json.dumps({"5 a st": rec}))
        db = Store(tmp_path / "store.db")        # opening the DB migrates the JSON
        assert len(db) == 1 and db.find("rc-1").address == "5 A St"

    def test_identity_normalization(self):
        assert identity(L(address="123 Kent Ave, Apt 4B")) == \
               identity(L(address="123 KENT AVE APT 4B"))


# --------------------------------------------------------------------------
# M3 — gate, rank, feedback
# --------------------------------------------------------------------------

class TestGateAndRank:
    def pool(self):
        return [
            L(id="good", price=3200),
            L(id="over", price=4200),                      # over cap+stretch
            L(id="studio", price=3000, beds=0),            # below beds floor
            L(id="far", price=3000, neighborhood="riverdale",
              lat=40.8898, lon=-73.9122),                  # outside gate radius
            L(id="cheap", price=2400, address="9 Other St"),
        ]

    def fb(self, tmp_path, liked=(), hidden=()):
        f = Feedback(tmp_path / "store.db")
        for i in liked:
            f.like(i)
        for i in hidden:
            f.hide(i)
        return f

    def test_gate_enforces_hard_bounds(self):
        ids = {l.id for l in gate(PREFS, self.pool())}
        assert ids == {"good", "cheap"}

    def test_anti_preference_excludes_by_name(self):
        prefs = Preferences(max_price=4000, beds=1,
                            exclude_neighborhoods=["east village"])
        keep = L(id="wburg", neighborhood="williamsburg", lat=40.7081, lon=-73.9571)
        drop = L(id="ev", neighborhood="east village", lat=40.7265, lon=-73.9815)
        ids = {l.id for l in gate(prefs, [keep, drop])}
        assert ids == {"wburg"}

    def test_priority_reorders_without_changing_match(self, tmp_path):
        """A verification penalty should sink a listing in the ORDER but leave
        its score/match untouched (penalty is sort-only)."""
        fb = self.fb(tmp_path)
        pool = self.pool()
        base = rank(PREFS, pool, fb)
        top = base[0].listing.id
        # heavily penalize the current winner → it must drop, but every
        # listing's stored score is unchanged
        pen = {top: 5.0}
        with_pen = rank(PREFS, pool, fb, priority=pen)
        assert with_pen[0].listing.id != top
        base_scores = {r.listing.id: r.score for r in base}
        for r in with_pen:
            assert r.score == base_scores[r.listing.id]

    def test_region_classification(self):
        from neighbors.models import region_of, NEIGHBORHOOD_COORDS
        assert region_of(*NEIGHBORHOOD_COORDS["williamsburg"]) == "Brooklyn"
        assert region_of(*NEIGHBORHOOD_COORDS["astoria"]) == "Queens"
        assert region_of(*NEIGHBORHOOD_COORDS["financial district"]) == "Manhattan"
        assert region_of(40.7178, -74.0431) == "Jersey City"
        assert region_of(0.0, 0.0) == ""            # outside the metro

    def test_borough_filter_gate(self):
        from neighbors.models import NEIGHBORHOOD_COORDS
        prefs = Preferences(max_price=9000, beds=1, boroughs=["Brooklyn"])
        bk = L(id="bk", **dict(zip(("lat", "lon"), NEIGHBORHOOD_COORDS["williamsburg"])))
        qn = L(id="qn", **dict(zip(("lat", "lon"), NEIGHBORHOOD_COORDS["astoria"])),
               neighborhood="astoria")
        no_coords = L(id="nc", lat=None, lon=None)
        ids = {x.id for x in gate(prefs, [bk, qn, no_coords])}
        assert ids == {"bk", "nc"}                  # Queens excluded; unknown kept

    def test_borough_filter_keeps_border_listings(self):
        """Like the neighborhood filter, the borough gate keeps a listing right
        on the line — within the border buffer of the chosen borough."""
        from neighbors.models import NEIGHBORHOOD_COORDS
        prefs = Preferences(max_price=9000, beds=1, boroughs=["Brooklyn"])
        # ~1 km north of Greenpoint (Brooklyn), toward the Queens line
        glat, glon = NEIGHBORHOOD_COORDS["greenpoint"]
        border = L(id="edge", lat=glat + 0.009, lon=glon)
        assert {x.id for x in gate(prefs, [border])} == {"edge"}

    def test_hard_commute_limit_excludes_far_listings(self):
        from neighbors.models import NEIGHBORHOOD_COORDS
        wlat, wlon = NEIGHBORHOOD_COORDS["williamsburg"]
        rlat, rlon = NEIGHBORHOOD_COORDS["riverdale"]
        prefs = Preferences(max_price=9000, beds=1, places=[
            {"name": "Work", "lat": wlat, "lon": wlon, "limit": "hard", "max_min": 30}])
        near = L(id="near", lat=wlat, lon=wlon)
        far = L(id="far", lat=rlat, lon=rlon, neighborhood="riverdale")
        ids = {x.id for x in gate(prefs, [near, far])}
        assert "near" in ids and "far" not in ids

    def test_commute_limit_keeps_listings_with_unknown_coords(self):
        prefs = Preferences(max_price=9000, beds=1, places=[
            {"name": "Work", "lat": 40.71, "lon": -73.95, "limit": "hard", "max_min": 10}])
        no_coords = L(id="nc", lat=None, lon=None)
        assert {x.id for x in gate(prefs, [no_coords])} == {"nc"}   # unknown ≠ too far

    def test_soft_commute_limit_does_not_gate(self):
        from neighbors.models import NEIGHBORHOOD_COORDS
        rlat, rlon = NEIGHBORHOOD_COORDS["riverdale"]
        prefs = Preferences(max_price=9000, beds=1, places=[
            {"name": "Work", "lat": 40.71, "lon": -73.95, "limit": "soft", "max_min": 5}])
        far = L(id="far", lat=rlat, lon=rlon, neighborhood="riverdale")
        assert {x.id for x in gate(prefs, [far])} == {"far"}   # soft never excludes

    def test_anti_preference_excludes_by_proximity(self):
        # labeled generically but sitting on the East Village centroid → still out
        prefs = Preferences(max_price=4000, beds=1,
                            exclude_neighborhoods=["east village"])
        near = L(id="near", neighborhood="", lat=40.7266, lon=-73.9816)
        assert gate(prefs, [near]) == []

    def test_unknown_amenities_not_gated_out(self):
        prefs = Preferences(max_price=3500, beds=1, amenities=["dishwasher"])
        no_data = L(amenities=[])                     # unknown ≠ absent
        missing = L(amenities=["gym"])                # known and missing
        having = L(amenities=["dishwasher", "gym"])
        ids = {l.id for l in gate(prefs, [no_data, missing, having])}
        assert no_data.id in ids  # all share id t1… give distinct ids
        assert {l.id for l in gate(prefs, [
            L(id="nodata", amenities=[]),
            L(id="missing", amenities=["gym"]),
            L(id="has", amenities=["dishwasher"]),
        ])} == {"nodata", "has"}

    def test_derived_amenities_do_not_trigger_amenity_gate(self):
        """Derived amenities must never flip the gate into strict mode: a
        listing with derived elevator but no source amenity data must still pass
        when the user wants a different amenity (unknown ≠ absent)."""
        prefs = Preferences(max_price=4000, beds=1, amenities=["dishwasher"])
        l = L(id="d", amenities=[], derived_amenities=["elevator"])
        assert {x.id for x in gate(prefs, [l])} == {"d"}

    def test_seeds_reorder_but_never_escape_gate(self, tmp_path):
        """The drift guarantee: liking an expensive listing must not pull
        over-budget listings into results."""
        fb = self.fb(tmp_path, liked=["good"])
        results = rank(PREFS, self.pool(), fb, alpha=0.1)  # heavy seed influence
        ids = {r.listing.id for r in results}
        assert "over" not in ids and "studio" not in ids and "far" not in ids

    def test_hidden_and_liked_excluded_from_results(self, tmp_path):
        fb = self.fb(tmp_path, liked=["good"], hidden=["cheap"])
        ids = {r.listing.id for r in rank(PREFS, self.pool(), fb)}
        assert "good" not in ids and "cheap" not in ids

    def test_alpha_one_ignores_seeds(self, tmp_path):
        pool = self.pool()
        no_fb = self.fb(tmp_path)
        with_likes = self.fb(tmp_path, liked=["far"])  # far is outside gate anyway
        a = [r.listing.id for r in rank(PREFS, pool, no_fb, alpha=1.0)]
        b = [r.listing.id for r in rank(PREFS, pool, with_likes, alpha=1.0)]
        assert a == b

    def test_rank_exposes_blend_components(self, tmp_path):
        pool = self.pool()
        # no likes: pure preference score, no seed component
        for r in rank(PREFS, pool, self.fb(tmp_path)):
            assert r.d_seed is None and r.score == r.d_pref
        # with a like: score is exactly the documented alpha blend
        alpha = 0.6
        for r in rank(PREFS, pool, self.fb(tmp_path, liked=["good"]), alpha=alpha):
            assert r.d_seed is not None
            assert abs(r.score - (alpha * r.d_pref + (1 - alpha) * r.d_seed)) < 1e-9

    def test_like_overrides_hide(self, tmp_path):
        fb = self.fb(tmp_path, hidden=["x"])
        fb.like("x")
        assert "x" in fb.liked and "x" not in fb.hidden

    def test_feedback_persists(self, tmp_path):
        self.fb(tmp_path, liked=["a"], hidden=["b"]).save()
        fb2 = Feedback(tmp_path / "store.db")
        assert "a" in fb2.liked and "b" in fb2.hidden


# --------------------------------------------------------------------------
# M4 — quota
# --------------------------------------------------------------------------

class TestQuota:
    def test_record_and_refuse_at_limit(self, tmp_path):
        ledger = QuotaLedger(tmp_path / "store.db")
        assert ledger.allow("rentcast")[0]
        ledger.record("rentcast", MONTHLY_LIMITS["rentcast"])
        allowed, reason = ledger.allow("rentcast")
        assert not allowed and "quota" in reason

    def test_unlimited_providers_always_allowed(self, tmp_path):
        ledger = QuotaLedger(tmp_path / "store.db")
        ledger.record("streeteasy", 10_000)
        assert ledger.allow("streeteasy")[0]

    def test_counts_are_per_month(self, tmp_path):
        ledger = QuotaLedger(tmp_path / "store.db")
        ledger.record("rentcast", 3)
        assert ledger.spent("rentcast") == 3
        assert ledger.spent("rentcast", month="1999-01") == 0

    def test_persistence(self, tmp_path):
        ledger = QuotaLedger(tmp_path / "store.db")
        ledger.record("rentcast", 7)
        ledger.save()
        assert QuotaLedger(tmp_path / "store.db").spent("rentcast") == 7

    def test_plan_fetch_skips_covered_and_over_quota(self, tmp_path):
        manifest = Manifest(tmp_path / "manifest.jsonl")
        ledger = QuotaLedger(tmp_path / "store.db")
        manifest.record("renthop", PREFS, listings_found=50)
        ledger.record("rentcast", MONTHLY_LIMITS["rentcast"])
        plan = {d.provider: d for d in plan_fetch(
            PREFS, ["rentcast", "renthop", "streeteasy"], manifest, ledger, 3600)}
        assert plan["renthop"].action == "skip" and "covered" in plan["renthop"].reason
        assert plan["rentcast"].action == "skip" and "quota" in plan["rentcast"].reason
        assert plan["streeteasy"].action == "fetch"

    def test_plan_fetch_ignore_coverage_forces_refresh(self, tmp_path):
        # a fresh covering fetch would normally skip; ignore_coverage overrides
        # it (the "store has nothing active to show" escape hatch) while quota
        # limits are still respected.
        manifest = Manifest(tmp_path / "manifest.jsonl")
        ledger = QuotaLedger(tmp_path / "store.db")
        manifest.record("renthop", PREFS, listings_found=50)
        plan = {d.provider: d for d in plan_fetch(
            PREFS, ["renthop"], manifest, ledger, 3600, ignore_coverage=True)}
        assert plan["renthop"].action == "fetch"


# --------------------------------------------------------------------------
# M5 — learned weights
# --------------------------------------------------------------------------

class TestLearn:
    def liked_clustered_on_location(self, n=MIN_LIKES + 2):
        # all likes in williamsburg (location agreement), prices scattered
        return [L(id=f"l{i}", address=f"{i} Kent Ave",
                  price=2500 + i * 150) for i in range(n)]

    def test_below_min_likes_returns_none(self):
        assert learn_weights(PREFS, self.liked_clustered_on_location(MIN_LIKES - 1)) is None

    def test_agreement_raises_relative_weight(self):
        learned = learn_weights(PREFS, self.liked_clustered_on_location())
        assert learned is not None
        # likes agree on location, scatter on price → location must gain
        # weight share relative to price compared with the defaults
        assert (learned["location"] / learned["price"]) > \
               (DEFAULT_WEIGHTS["location"] / DEFAULT_WEIGHTS["price"])

    def test_total_weight_mass_comparable(self):
        learned = learn_weights(PREFS, self.liked_clustered_on_location())
        assert abs(sum(learned.values()) - sum(DEFAULT_WEIGHTS.values())) \
               < 0.25 * sum(DEFAULT_WEIGHTS.values())

    def test_weights_roundtrip_and_reject_malformed(self, tmp_path):
        p = tmp_path / "weights.json"
        save_weights(p, DEFAULT_WEIGHTS)
        assert load_weights(p) == DEFAULT_WEIGHTS
        p.write_text(json.dumps({"price": 1.0}))  # incomplete set
        assert load_weights(p) is None


# --------------------------------------------------------------------------
# CLI end-to-end for the new commands
# --------------------------------------------------------------------------

class TestEngineCli:
    def test_engine_session_like_and_persist(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["engine", "--cached"], tmp_path, stdin="like 1\nhide 2\nq\n")
        assert r.returncode == 0, r.stderr
        assert "engine>" in r.stdout
        fb = Feedback(tmp_path / ".neighbors" / "store.db")
        assert len(fb.liked) == 1 and len(fb.hidden) == 1

    def test_engine_rerank_after_feedback(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["engine", "--cached"], tmp_path, stdin="like 1\nr\nq\n")
        assert r.returncode == 0, r.stderr
        # after re-rank the liked listing is excluded from results
        assert r.stdout.count("Session saved: 1 liked") == 1

    def test_engine_plan_dry_run_spends_nothing(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["engine", "--plan"], tmp_path)
        assert r.returncode == 0, r.stderr
        assert "Fetch plan (nothing executed)" in r.stdout
        assert "rentcast" in r.stdout
        assert QuotaLedger(tmp_path / ".neighbors" / "store.db").spent("rentcast") == 0

    def test_cache_status_and_quota_commands(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["cache-status"], tmp_path)
        assert r.returncode == 0 and "Store: 150 listings" in r.stdout
        r2 = run_cli(["quota"], tmp_path)
        assert r2.returncode == 0 and "rentcast" in r2.stdout

    def test_weights_command_shows_defaults_and_resets(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["weights"], tmp_path)
        assert r.returncode == 0 and "defaults" in r.stdout
        (tmp_path / ".neighbors" / "weights.json").write_text(
            json.dumps(DEFAULT_WEIGHTS))
        r2 = run_cli(["weights", "--reset-weights"], tmp_path)
        assert r2.returncode == 0
        assert not (tmp_path / ".neighbors" / "weights.json").exists()