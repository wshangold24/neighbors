"""Integration tests for aptfinder.

Covers the real seams between components:
  * SafeSession against a live local HTTP server (rate limiting, caching,
    budget, backoff, block handling) — no external network.
  * Provider parsers fed realistic HTML fixtures through the session layer.
  * Full pipeline: provider → validation → KNN (both search modes).
  * CLI end-to-end as a subprocess with an isolated HOME.

Run:  pytest tests/ -v
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from .conftest import PROMPT_INPUT, make_session, run_cli, seed_home

from aptfinder.http import BudgetExceeded, SafeSession  # noqa: E402
from aptfinder.knn import Query, distance, knn, match_pct  # noqa: E402
from aptfinder.models import Listing, Preferences  # noqa: E402
from aptfinder.providers.demo import DemoProvider  # noqa: E402
from aptfinder.providers.housingconnect import HousingConnectProvider  # noqa: E402
from aptfinder.providers.rentcast import RentCastProvider, resolve_api_key  # noqa: E402


# --------------------------------------------------------------------------
# SafeSession ⇄ real HTTP server
# --------------------------------------------------------------------------

class TestSafeSessionIntegration:
    def test_fetch_and_cache(self, http_server, tmp_path):
        base, handler = http_server
        s = make_session(tmp_path)
        assert s.get(f"{base}/ok") == "<html>hello</html>"
        assert s.get(f"{base}/ok") == "<html>hello</html>"  # cache hit
        assert len(handler.hits["/ok"]) == 1, "second call must not hit network"
        assert s.spent == 1

    def test_cache_persists_across_sessions(self, http_server, tmp_path):
        base, handler = http_server
        make_session(tmp_path).get(f"{base}/ok")
        s2 = make_session(tmp_path, budget=0)  # zero budget: must come from disk
        assert s2.get(f"{base}/ok") == "<html>hello</html>"
        assert len(handler.hits["/ok"]) == 1

    def test_rate_limiting_spaces_requests(self, http_server, tmp_path):
        base, handler = http_server
        s = make_session(tmp_path, min_interval=0.5)
        s.get(f"{base}/ok")
        s.get(f"{base}/ok", params={"page": 2})  # different key → real request
        stamps = handler.hits["/ok"]
        assert len(stamps) == 2
        assert stamps[1] - stamps[0] >= 0.5

    def test_budget_exhaustion_raises(self, http_server, tmp_path):
        base, _ = http_server
        s = make_session(tmp_path, budget=2)
        s.get(f"{base}/ok")
        s.get(f"{base}/ok", params={"page": 2})
        with pytest.raises(BudgetExceeded):
            s.get(f"{base}/ok", params={"page": 3})

    def test_403_gives_up_immediately(self, http_server, tmp_path):
        base, handler = http_server
        s = make_session(tmp_path)
        assert s.get(f"{base}/forbidden") is None
        assert len(handler.hits["/forbidden"]) == 1, "must not retry a block"

    def test_429_backs_off_then_recovers(self, http_server, tmp_path):
        base, handler = http_server
        s = make_session(tmp_path)
        t0 = time.monotonic()
        assert s.get(f"{base}/flaky") == "recovered"
        stamps = handler.hits["/flaky"]
        assert len(stamps) == 2
        assert stamps[1] - stamps[0] >= 1.0, "must honor Retry-After: 1"
        assert time.monotonic() - t0 >= 1.0

    def test_500_retries_then_gives_up(self, http_server, tmp_path):
        base, handler = http_server
        s = make_session(tmp_path, max_retries=2)
        assert s.get(f"{base}/error") is None
        assert len(handler.hits["/error"]) == 2

    def test_failed_responses_not_cached(self, http_server, tmp_path):
        base, handler = http_server
        s = make_session(tmp_path)
        s.get(f"{base}/forbidden")
        s.get(f"{base}/forbidden")
        assert len(handler.hits["/forbidden"]) == 2, "errors must not be cached"


# --------------------------------------------------------------------------
# Providers ⇄ session (fixture HTML through the real fetch path)
# --------------------------------------------------------------------------

class FixtureSession:
    """Duck-typed SafeSession returning canned bodies (None after exhausted)."""

    def __init__(self, bodies: list):
        self.bodies = list(bodies)
        self.calls = []
        self.headers_seen = []

    def get(self, url, params=None, use_cache=True, headers=None):
        self.calls.append((url, params))
        self.headers_seen.append(headers or {})
        return self.bodies.pop(0) if self.bodies else None


# Socrata rows: step 1 = active rental lottery ids, step 2 = buildings.
HC_LOTTERIES = json.dumps([
    {"lottery_id": "3600", "lottery_name": "422 East 31 Street",
     "lottery_end_date": "2026-06-02T00:00:00.000"},
    {"lottery_id": "3601", "lottery_name": "Scattered Site"}])
HC_BUILDINGS = json.dumps([
    {"lottery_id": "3600", "lottery_name": "422 East 31 Street",
     "hc_building_id": "5986", "house_number": "422", "street_name": "EAST 31 STREET",
     "borough": "BK", "address_zipcode": "11226",
     "unit_distribution_1bed": "1", "unit_distribution_2bed": "2",
     "applied_income_ami_middle": "3",
     "address_latitude": "40.640426", "address_longitude": "-73.947494"},
    # "Multiple"-borough rows and address-less rows must be skipped
    {"lottery_id": "3601", "lottery_name": "Scattered Site",
     "house_number": "", "street_name": "", "borough": "Multiple",
     "address_latitude": "40.7", "address_longitude": "-73.9"},
])


class TestHousingConnectIntegration:
    def test_parses_active_rental_buildings(self):
        sess = FixtureSession([HC_LOTTERIES, HC_BUILDINGS])
        listings = HousingConnectProvider(sess).fetch(Preferences(beds=2), max_pages=1)
        assert len(listings) == 1, "the 'Multiple'/address-less row must be skipped"
        l = listings[0]
        assert l.source == "housingconnect"
        assert l.beds == 2                       # closest available size to prefs.beds=2
        assert "income_restricted" in l.amenities and "no_fee" in l.amenities
        assert l.neighborhood and l.lat == 40.640426
        assert 500 <= l.price <= 60_000          # AMI-estimated, still valid
        assert "422" in l.address and "Brooklyn" in l.address
        assert "apply by Jun 2, 2026" in l.description   # deadline surfaced
        assert "housingconnect.nyc.gov" in l.url          # links to the real site

    def test_only_active_rentals_are_queried(self):
        sess = FixtureSession([HC_LOTTERIES, HC_BUILDINGS])
        HousingConnectProvider(sess).fetch(Preferences(), max_pages=1)
        where = sess.calls[0][1]["$where"]
        assert "development_type='Rental'" in where and "lottery_status='Active'" in where

    def test_no_active_lotteries_returns_empty_without_second_call(self):
        sess = FixtureSession([json.dumps([])])   # no active lottery ids
        assert HousingConnectProvider(sess).fetch(Preferences(), max_pages=1) == []
        assert len(sess.calls) == 1, "must not query buildings when no lotteries"


RENTCAST_FIXTURE = json.dumps([
    {"id": "123-Kent-Ave,-Brooklyn,-NY-11249", "formattedAddress": "123 Kent Ave, Apt 4B, Brooklyn, NY 11249",
     "latitude": 40.7085, "longitude": -73.9575, "propertyType": "Apartment",
     "bedrooms": 1, "bathrooms": 1, "squareFootage": 640, "status": "Active",
     "price": 3350, "listedDate": "2026-06-20T00:00:00.000Z", "daysOnMarket": 12},
    {"id": "9-India-St,-Brooklyn,-NY-11222", "formattedAddress": "9 India St, Brooklyn, NY 11222",
     "latitude": 40.7248, "longitude": -73.9442, "propertyType": "Condo",
     "bedrooms": 0, "bathrooms": 1, "status": "Active", "price": 2900},
    {"id": "bad", "formattedAddress": "1 Nowhere Ln", "price": 12,
     "bedrooms": 1, "bathrooms": 1},
])


class TestRentCastProvider:
    def make(self, bodies, key="test-key-123"):
        sess = FixtureSession(bodies)
        p = RentCastProvider(sess, api_key=key)
        return p, sess

    def test_parses_and_validates_listings(self):
        p, _ = self.make([RENTCAST_FIXTURE])
        listings = p.fetch(Preferences(max_price=3500, neighborhoods=["williamsburg"]))
        assert len(listings) == 2, "the $12 listing must fail validation"
        by_price = {l.price: l for l in listings}
        l = by_price[3350]
        assert (l.beds, l.baths, l.sqft) == (1, 1, 640)
        assert l.neighborhood == "Williamsburg"   # reverse-geocoded from coords
        assert by_price[2900].neighborhood == "Greenpoint"
        assert by_price[2900].beds == 0

    def test_sends_api_key_header_and_query_params(self):
        p, sess = self.make([RENTCAST_FIXTURE])
        p.fetch(Preferences(max_price=3000, price_stretch=0.15,
                            neighborhoods=["williamsburg"]))
        assert sess.headers_seen[0]["X-Api-Key"] == "test-key-123"
        url, params = sess.calls[0]
        assert "api.rentcast.io/v1/listings/rental/long-term" in url
        assert params["price"] == "*:3450"
        assert params["status"] == "Active"
        assert "latitude" in params and "radius" in params

    def test_city_fallback_without_neighborhoods(self):
        p, sess = self.make([RENTCAST_FIXTURE])
        p.fetch(Preferences(neighborhoods=[]))
        _, params = sess.calls[0]
        assert params.get("city") == "New York" and params.get("state") == "NY"

    def test_no_api_key_makes_zero_requests(self, monkeypatch, tmp_path):
        monkeypatch.delenv("RENTCAST_API_KEY", raising=False)
        monkeypatch.setattr("aptfinder.providers.rentcast.KEY_FILE",
                            tmp_path / "nokey")
        p, sess = self.make([RENTCAST_FIXTURE], key=None)
        assert p.fetch(Preferences()) == []
        assert sess.calls == [], "must not spend quota without a key"

    def test_api_error_payload_returns_empty(self):
        p, _ = self.make([json.dumps({"status": 401, "message": "Invalid API key"})])
        assert p.fetch(Preferences()) == []

    def test_key_resolution_order(self, monkeypatch, tmp_path):
        keyfile = tmp_path / "rentcast_key"
        keyfile.write_text("file-key\n")
        monkeypatch.setattr("aptfinder.providers.rentcast.KEY_FILE", keyfile)
        monkeypatch.setenv("RENTCAST_API_KEY", "env-key")
        assert resolve_api_key("explicit-key") == "explicit-key"
        assert resolve_api_key(None) == "env-key"
        monkeypatch.delenv("RENTCAST_API_KEY")
        assert resolve_api_key(None) == "file-key"


# --------------------------------------------------------------------------
# Full pipeline: provider → KNN
# --------------------------------------------------------------------------

class TestPipelineIntegration:
    @pytest.fixture()
    def listings(self):
        return DemoProvider(None, n=400, seed=7).fetch(Preferences())

    def test_demo_listings_all_valid(self, listings):
        assert len(listings) > 300
        assert all(l.validate() for l in listings)

    def test_requirements_mode_ranks_sensibly(self, listings):
        prefs = Preferences(max_price=3500, beds=1,
                            neighborhoods=["williamsburg", "greenpoint"],
                            amenities=["laundry_in_unit"], k=10)
        results = knn(Query.from_preferences(prefs), listings, k=prefs.k)
        assert len(results) == 10
        dists = [d for _, d, _ in results]
        assert dists == sorted(dists), "must be nearest-first"
        top = [l for l, _, _ in results[:5]]
        # near-misses allowed, but the top shouldn't blow the budget badly
        assert all(l.price <= 3500 * 1.25 for l in top)
        assert sum(l.beds == 1 for l in top) >= 3

    def test_knn_surfaces_near_misses_that_filters_hide(self, listings):
        """The core feature: a listing slightly over budget must be able to
        outrank an in-budget listing that's wrong on everything else."""
        prefs = Preferences(max_price=3000, beds=2, neighborhoods=["williamsburg"])
        q = Query.from_preferences(prefs)
        near_miss = Listing(id="nm", source="t", url="", address="x",
                            neighborhood="Williamsburg", price=3100, beds=2, baths=1)
        bad_fit = Listing(id="bf", source="t", url="", address="y",
                          neighborhood="Inwood", price=2900, beds=0, baths=1)
        assert distance(q, near_miss) < distance(q, bad_fit)

    def test_seed_mode_finds_similar_and_excludes_seed(self, listings):
        seed = listings[0]
        q = Query.from_listing(seed)
        results = knn(q, listings, k=10, exclude_ids={seed.id})
        assert all(l.id != seed.id for l, _, _ in results)
        top = [l for l, _, _ in results[:5]]
        assert sum(abs(l.price - seed.price) <= seed.price * 0.35 for l in top) >= 3
        assert sum(abs(l.beds - seed.beds) <= 1 for l in top) >= 4
        assert any(l.beds == seed.beds for l in top)

    def test_match_pct_monotonic(self, listings):
        results = knn(Query.from_preferences(Preferences()), listings, k=20)
        pcts = [match_pct(d) for _, d, _ in results]
        assert pcts == sorted(pcts, reverse=True)
        assert all(0 <= p <= 100 for p in pcts)

    def test_listing_validation_rejects_garbage(self):
        good = dict(id="x", source="t", url="", address="1 Main St",
                    neighborhood="soho", price=3000, beds=1, baths=1)
        assert Listing(**good).validate()
        assert not Listing(**{**good, "price": 80}).validate()
        assert not Listing(**{**good, "price": 90_000}).validate()
        assert not Listing(**{**good, "beds": 12}).validate()
        assert not Listing(**{**good, "address": ""}).validate()
        assert not Listing(**{**good, "lat": 34.0, "lon": -118.2}).validate()  # LA


# --------------------------------------------------------------------------
# CLI end-to-end (subprocess, isolated HOME)
# --------------------------------------------------------------------------

class TestCliEndToEnd:
    def test_demo_is_not_a_cli_source(self, tmp_path):
        """Demo data must be unreachable from the CLI (test-only)."""
        r = run_cli(["--sources", "demo"], tmp_path, stdin=PROMPT_INPUT)
        assert r.returncode == 1
        assert "unknown provider 'demo'" in r.stdout
        assert "demo data" not in r.stdout.lower()

    def test_prompting_saves_prefs_and_empty_fetch_fails_cleanly(self, tmp_path):
        # 'bogus' source: prompts run, no network, zero listings → clean exit 1.
        r = run_cli(["--sources", "bogus"], tmp_path, stdin=PROMPT_INPUT)
        assert r.returncode == 1
        assert "unknown provider" in r.stdout
        assert "No active listings available." in r.stdout
        prefs = json.loads((tmp_path / ".aptfinder" / "preferences.json").read_text())
        assert prefs["max_price"] == 3500
        assert prefs["neighborhoods"] == ["williamsburg", "greenpoint"]

    def test_cached_search_and_seed_mode(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["--cached"], tmp_path)
        assert r.returncode == 0, r.stderr
        assert "no network" in r.stdout
        assert "%]" in r.stdout and "$" in r.stdout

        r2 = run_cli(["--cached", "--like", "1"], tmp_path)
        assert r2.returncode == 0, r2.stderr
        assert "Similar to:" in r2.stdout

    def test_saved_prefs_reused_without_prompting(self, tmp_path):
        seed_home(tmp_path)
        r = run_cli(["--cached"], tmp_path)  # no stdin: must not prompt
        assert r.returncode == 0, r.stderr
        assert "Using saved preferences" in r.stdout

    def test_cached_without_prior_fetch_fails_cleanly(self, tmp_path):
        seed_home(tmp_path, with_listings=False)
        r = run_cli(["--cached"], tmp_path)
        assert r.returncode == 1
        assert "run once without --cached" in r.stdout

    def test_prefs_only_mode_updates_and_exits(self, tmp_path):
        from aptfinder.store import Store
        seed_home(tmp_path)
        store_path = tmp_path / ".aptfinder" / "store.db"
        listings_before = Store(store_path).records()

        # Change only max price; Enter (blank) keeps every other current value.
        stdin = "4200\n" + "\n" * 9
        r = run_cli(["--prefs"], tmp_path, stdin=stdin)
        assert r.returncode == 0, r.stderr
        assert "Preferences saved" in r.stdout
        assert "Fetching" not in r.stdout and "%]" not in r.stdout, \
            "prefs-only mode must not fetch or search"

        prefs = json.loads((tmp_path / ".aptfinder" / "preferences.json").read_text())
        assert prefs["max_price"] == 4200                            # updated
        assert prefs["neighborhoods"] == ["williamsburg", "greenpoint"]  # kept
        assert prefs["amenities"] == ["laundry_in_unit"]                 # kept
        assert Store(store_path).records() == listings_before   # store untouched

    def test_prefs_rejects_unknown_neighborhood_then_accepts(self, tmp_path):
        """Multi-select fields feed API calls — typos must be re-asked, never
        silently saved."""
        stdin = ("3500\n0\n1\n1\n"
                 "willyburg\n"                  # rejected → re-asked
                 "williamsburg\n"
                 "none\n0\n10\n15\n"
                 "zillow\n"                     # unknown source → re-asked
                 "housingconnect\n")
        r = run_cli(["--prefs"], tmp_path, stdin=stdin)
        assert r.returncode == 0, r.stderr
        assert "not recognized: willyburg" in r.stdout
        assert "not recognized: zillow" in r.stdout
        prefs = json.loads((tmp_path / ".aptfinder" / "preferences.json").read_text())
        assert prefs["neighborhoods"] == ["williamsburg"]
        assert prefs["sources"] == ["housingconnect"]

    def test_sources_resolution_precedence(self):
        """--sources flag > saved preference > all registered providers."""
        from argparse import Namespace
        from aptfinder.cli import resolve_sources
        from aptfinder.providers import PROVIDERS
        prefs = Preferences(sources=["housingconnect"])
        assert resolve_sources(Namespace(sources="rentcast"), prefs) == ["rentcast"]
        assert resolve_sources(Namespace(sources=None), prefs) == ["housingconnect"]
        assert resolve_sources(Namespace(sources=None), Preferences()) == list(PROVIDERS)

    def test_prefs_only_mode_works_without_existing_prefs(self, tmp_path):
        r = run_cli(["--prefs"], tmp_path, stdin=PROMPT_INPUT)
        assert r.returncode == 0, r.stderr
        prefs = json.loads((tmp_path / ".aptfinder" / "preferences.json").read_text())
        assert prefs["max_price"] == 3500
        assert not (tmp_path / ".aptfinder" / "store.db").exists()
