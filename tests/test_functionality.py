"""Functional tests for the desired behavior of each component.

These pin down the *intended semantics* (asymmetric penalties, geo math,
validation rules, preference persistence) rather than component wiring —
see test_integration.py for the seams.

Run:  pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from neighbors.knn import (  # noqa: E402
    DEFAULT_WEIGHTS, Query, distance, haversine_km, knn, match_pct,
)
from neighbors.models import Listing, Preferences, NEIGHBORHOOD_COORDS  # noqa: E402


def L(**kw) -> Listing:
    base = dict(id="t1", source="test", url="", address="1 Test St",
                neighborhood="williamsburg", price=3000, beds=1, baths=1)
    base.update(kw)
    return Listing(**base)


def Q(**kw) -> Query:
    lat, lon = NEIGHBORHOOD_COORDS["williamsburg"]
    base = dict(price=3000, beds=1, baths=1, lat=lat, lon=lon, sqft=None,
                amenities=set(), max_price=3000)
    base.update(kw)
    return Query(**base)


class TestGeoMath:
    def test_haversine_known_distance(self):
        # Williamsburg → Astoria is roughly 7 km
        d = haversine_km(40.7081, -73.9571, 40.7644, -73.9235)
        assert 6.0 < d < 8.0

    def test_haversine_zero(self):
        assert haversine_km(40.7, -73.9, 40.7, -73.9) == 0

    def test_neighborhood_coords_fill_in(self):
        l = L(lat=None, lon=None, neighborhood="Greenpoint")
        assert l.lat is not None and abs(l.lat - 40.7245) < 0.01


class TestDistanceSemantics:
    def test_perfect_match_is_near_zero(self):
        assert distance(Q(), L()) < 0.35

    def test_over_budget_hurts_more_than_under(self):
        """Asymmetry: $300 over budget must rank worse than $300 under."""
        over, under = L(price=3300), L(price=2700)
        assert distance(Q(), over) > distance(Q(), under)

    def test_fewer_beds_hurts_more_than_extra(self):
        q = Q(beds=2)
        assert distance(q, L(beds=1)) > distance(q, L(beds=3))

    # ---- directional semantics (preference queries) ----

    def test_directional_cheaper_than_max_is_free(self):
        """Any price within budget is equally acceptable — being well under
        the cap must cost nothing."""
        q = Q(directional=True, max_price=3500, price=3500 * 0.92)
        _, cheap = distance(q, L(price=2400), explain=True)
        _, at_cap = distance(q, L(price=3450), explain=True)
        assert cheap["price"] == 0.0
        assert at_cap["price"] == 0.0
        _, over = distance(q, L(price=3800), explain=True)
        assert over["price"] > 0.0

    def test_directional_price_floor_still_applies(self):
        q = Q(directional=True, max_price=3500, min_price=2500)
        _, below_floor = distance(q, L(price=2000), explain=True)
        _, in_range = distance(q, L(price=2600), explain=True)
        assert below_floor["price"] > in_range["price"] == 0.0

    def test_directional_extra_baths_are_free(self):
        q = Q(directional=True, baths=1)
        _, more = distance(q, L(baths=2), explain=True)
        _, fewer = distance(q, L(baths=0.5), explain=True)
        assert more["baths"] == 0.0
        assert fewer["baths"] > 0.0

    def test_directional_extra_sqft_is_free(self):
        q = Q(directional=True, sqft=700.0)
        _, bigger = distance(q, L(sqft=1100), explain=True)
        _, smaller = distance(q, L(sqft=450), explain=True)
        assert bigger["sqft"] == 0.0
        assert smaller["sqft"] > 0.0

    def test_seed_mode_stays_symmetric(self):
        """Seed queries (directional=False): a much cheaper/bigger listing is
        NOT 'similar' — symmetric distance must still apply."""
        q = Q(directional=False, price=3000, max_price=None)
        _, cheap = distance(q, L(price=1800), explain=True)
        assert cheap["price"] > 0.0
        q2 = Q(directional=False, baths=1, sqft=700.0)
        _, r = distance(q2, L(baths=3, sqft=1500), explain=True)
        assert r["baths"] > 0.0 and r["sqft"] > 0.0

    def test_from_preferences_is_directional(self):
        from neighbors.models import Preferences
        from neighbors.knn import Query
        q = Query.from_preferences(Preferences(max_price=3000, min_price=2000))
        assert q.directional and q.max_price == 3000 and q.min_price == 2000

    def test_from_listing_is_not_directional(self):
        from neighbors.knn import Query
        assert Query.from_listing(L()).directional is False

    def test_closer_location_ranks_higher(self):
        gp = NEIGHBORHOOD_COORDS["greenpoint"]
        fh = NEIGHBORHOOD_COORDS["forest hills"]
        near = L(lat=gp[0], lon=gp[1], neighborhood="")
        far = L(lat=fh[0], lon=fh[1], neighborhood="")
        assert distance(Q(), near) < distance(Q(), far)

    def test_amenity_overlap_reduces_distance(self):
        q = Q(amenities={"dishwasher", "laundry_in_unit"})
        both = L(amenities=["dishwasher", "laundry_in_unit"])
        one = L(amenities=["dishwasher"])          # reports amenities, missing one
        assert distance(q, both) < distance(q, one)

    def test_unknown_amenities_treated_as_mild_not_absent(self):
        """A listing with NO amenity data is 'unknown', not 'missing all' —
        it must not be scored worse than one that reports amenities but is
        genuinely missing a wanted one (unknown ≠ absent, like the hard gate)."""
        q = Q(amenities={"dishwasher", "laundry_in_unit"})
        both = L(amenities=["dishwasher", "laundry_in_unit"])
        missing_one = L(amenities=["dishwasher"])
        no_data = L(amenities=[])
        assert distance(q, both) < distance(q, no_data) < distance(q, missing_one)

    def test_location_uses_nearest_wanted_neighborhood(self):
        """A listing IN a wanted neighborhood must score ~0 on location even
        when a second, far-away neighborhood is also wanted (nearest, not the
        centroid of the two)."""
        from neighbors.knn import Query
        from neighbors.models import Preferences, NEIGHBORHOOD_COORDS
        # want Financial District AND Astoria (across the city from each other)
        q = Query.from_preferences(Preferences(
            max_price=4000, beds=1, neighborhoods=["financial district", "astoria"]))
        flat, flon = NEIGHBORHOOD_COORDS["financial district"]
        fidi = L(lat=flat, lon=flon, neighborhood="financial district")
        _, parts = distance(q, fidi, explain=True)
        assert parts["location"] < 0.1        # right in a wanted area → basically 0

    def test_location_inside_neighborhood_but_off_center_scores_zero(self):
        """A listing inside a wanted neighborhood's disk — but not at its exact
        centroid — must still score 0 on location (neighborhood is an area)."""
        from neighbors.knn import Query
        from neighbors.models import Preferences, NEIGHBORHOOD_COORDS, NEIGHBORHOOD_RADIUS
        q = Query.from_preferences(Preferences(
            max_price=4000, beds=1, neighborhoods=["williamsburg"]))
        wlat, wlon = NEIGHBORHOOD_COORDS["williamsburg"]
        # nudge ~0.5 km north — still well within the neighborhood disk
        off = L(lat=wlat + 0.0045, lon=wlon, neighborhood="williamsburg")
        _, parts = distance(q, off, explain=True)
        assert NEIGHBORHOOD_RADIUS["williamsburg"] >= 0.7
        assert parts["location"] == 0.0

    def test_precise_polygon_used_when_available(self, monkeypatch):
        """When a neighborhood has a boundary polygon, location is 0 inside it
        and a real distance outside — overriding the disk approximation."""
        from neighbors import models
        # ~1 km square around (40.70, -73.95) for the 'williamsburg' key
        square = [[40.695, -73.955], [40.705, -73.955],
                  [40.705, -73.945], [40.695, -73.945]]
        monkeypatch.setattr(models, "NEIGHBORHOOD_POLYGONS", {"williamsburg": square})
        assert models.area_edge_km(40.70, -73.95, ["williamsburg"]) == 0.0   # inside
        assert models.area_edge_km(40.75, -73.95, ["williamsburg"]) > 1.0    # outside

    def test_location_falls_back_to_disk_without_polygon(self):
        """No polygon file → the disk model still gives 0 inside a neighborhood."""
        from neighbors.models import area_edge_km, NEIGHBORHOOD_COORDS, NEIGHBORHOOD_POLYGONS
        assert "williamsburg" not in NEIGHBORHOOD_POLYGONS   # none bundled by default
        wlat, wlon = NEIGHBORHOOD_COORDS["williamsburg"]
        assert area_edge_km(wlat, wlon, ["williamsburg"]) == 0.0

    def test_derived_amenity_credits_score(self):
        """A building we KNOW has an elevator (derived from PLUTO) should beat
        one with no amenity data, and be fully credited for that wanted amenity."""
        q = Q(amenities={"elevator"})
        elevator_bldg = L(amenities=[], derived_amenities=["elevator"])
        unknown = L(amenities=[], derived_amenities=[])
        assert distance(q, elevator_bldg) < distance(q, unknown)
        _, parts = distance(q, elevator_bldg, explain=True)
        assert parts["amenities"] == 0.0

    def test_inferred_amenities_never_affect_score(self):
        """Inferences are advisory only — they must not change the distance."""
        q = Q(amenities={"doorman"})
        guess = L(amenities=[], derived_amenities=[], inferred_amenities=["doorman"])
        plain = L(amenities=[], derived_amenities=[], inferred_amenities=[])
        assert distance(q, guess) == distance(q, plain)

    def test_unknown_sqft_penalized_less_than_bad_sqft(self):
        q = Q(sqft=800.0)
        unknown = L(sqft=None)
        way_small = L(sqft=300)
        assert distance(q, unknown) < distance(q, way_small)

    def test_distance_components_bounded(self):
        """A wildly wrong listing must not produce inf/NaN."""
        q = Q()
        awful = L(price=59_000, beds=8, baths=8, neighborhood="riverdale",
                  lat=None, lon=None)
        d, parts = distance(q, awful, explain=True)
        assert d == d and d != float("inf")  # not NaN, not inf
        assert all(v >= 0 for v in parts.values())
        assert set(parts) == set(DEFAULT_WEIGHTS)


class TestKnnBehavior:
    def make_pool(self):
        return [
            L(id="exact", price=2900, beds=1),
            L(id="pricey", price=3400, beds=1),
            L(id="studio", price=2900, beds=0),
            L(id="far", price=2900, beds=1, neighborhood="riverdale",
              lat=None, lon=None),
        ]

    def test_k_limits_results_and_orders_them(self):
        pool = self.make_pool()
        res = knn(Q(price=2900), pool, k=2)
        assert len(res) == 2
        assert res[0][1] <= res[1][1]
        assert res[0][0].id == "exact"

    def test_exclude_ids(self):
        res = knn(Q(), self.make_pool(), k=10, exclude_ids={"exact"})
        assert "exact" not in {l.id for l, _, _ in res}

    def test_seed_query_reproduces_seed_profile(self):
        seed = L(id="seed", price=3200, beds=2, sqft=900,
                 amenities=["gym", "doorman"])
        q = Query.from_listing(seed)
        assert (q.price, q.beds, q.sqft) == (3200, 2, 900)
        assert q.amenities == {"gym", "doorman"}
        assert distance(q, seed) < 0.01

    def test_match_pct_range_and_monotonicity(self):
        assert match_pct(0) == 100
        assert match_pct(10) <= 5
        assert match_pct(0.5) > match_pct(1.0) > match_pct(2.0)


class TestPreferences:
    def test_roundtrip_serialization(self):
        p = Preferences(max_price=3500, beds=2, neighborhoods=["astoria"],
                        amenities=["gym"], min_sqft=700, k=8, price_stretch=0.2)
        assert Preferences.from_dict(p.to_dict()) == p

    def test_from_dict_ignores_unknown_keys(self):
        p = Preferences.from_dict({"max_price": 2000, "bogus_key": 1})
        assert p.max_price == 2000

    def test_target_coords_averages_neighborhoods(self):
        p = Preferences(neighborhoods=["williamsburg", "greenpoint"])
        lat, lon = p.target_coords()
        w, g = NEIGHBORHOOD_COORDS["williamsburg"], NEIGHBORHOOD_COORDS["greenpoint"]
        assert lat == pytest.approx((w[0] + g[0]) / 2)
        assert lon == pytest.approx((w[1] + g[1]) / 2)

    def test_target_coords_none_when_unknown(self):
        assert Preferences(neighborhoods=["narnia"]).target_coords() is None

    def test_query_from_prefs_uses_soft_budget(self):
        q = Query.from_preferences(Preferences(max_price=3000))
        assert q.max_price == 3000
        assert q.price < 3000  # aims slightly under budget


class TestListingValidation:
    @pytest.mark.parametrize("field,value", [
        ("price", 80), ("price", 90_000),
        ("beds", -1), ("beds", 12), ("baths", 9),
        ("sqft", 50), ("sqft", 50_000),
        ("lat", 34.05), ("lon", -118.24),   # not NYC
        ("address", ""), ("id", ""),
    ])
    def test_rejects_out_of_range(self, field, value):
        assert not L(**{field: value}).validate()

    def test_accepts_reasonable_listing(self):
        assert L().validate()
        assert L(beds=0, sqft=None, lat=None, lon=None).validate()

    def test_rejects_missing_core_numbers_instead_of_raising(self):
        for field in ("price", "beds", "baths"):
            assert not L(**{field: None}).validate()


class TestMissingData:
    def test_distance_tolerates_missing_price_beds_baths(self):
        # a malformed stored record passes the gate (unknown ≠ disqualified),
        # so the soft score must not raise on it either — mild penalty instead
        q = Query.from_preferences(Preferences(max_price=3000))
        for field in ("price", "beds", "baths"):
            d, parts = distance(q, L(**{field: None}), explain=True)
            assert parts[field] == pytest.approx(0.4)
            assert d > 0

    def test_seed_with_missing_price_does_not_raise(self):
        seed = Query.from_listing(L(price=None))
        assert seed.price is None
        _, parts = distance(seed, L(), explain=True)
        assert parts["price"] == pytest.approx(0.4)

    def test_nearest_neighborhood_display_name_keeps_apostrophe(self):
        from neighbors.models import nearest_neighborhood
        lat, lon = NEIGHBORHOOD_COORDS["hell's kitchen"]
        assert nearest_neighborhood(lat, lon) == "Hell's Kitchen"
        lat, lon = NEIGHBORHOOD_COORDS["bedford-stuyvesant"]
        assert nearest_neighborhood(lat, lon) == "Bedford-Stuyvesant"
