"""Tests for fetch-time building verification (verify.py). The building lookup
(GeoSearch → PLUTO → HPD) is injected, so these run with no network."""

from __future__ import annotations

from neighbors.verify import (derived_amenities, inferred_amenities,
                              is_residential, needs_verify, verify_one,
                              verify_store)


def rec(address="422 East 31 Street, Brooklyn, NY 11226",
        lat=40.640426, lon=-73.947494, last_seen=1000.0, **extra):
    return {"listing": {"id": "x", "address": address, "lat": lat, "lon": lon},
            "last_seen": last_seen, **extra}


def lookup_building(units=10, bldgclass="C1", landuse="2", registered=True,
                    lat=40.640430, lon=-73.947500, found=True,
                    numfloors=5, yearbuilt=1960):
    def look(_address, _lat, _lon):
        if not found:
            return {"found": False}
        return {"found": True, "bbl": "3049640016", "bin": "3428876",
                "label": "422 EAST 31 STREET, Brooklyn, NY, USA",
                "lat": lat, "lon": lon, "units": units, "bldgclass": bldgclass,
                "landuse": landuse, "registered": registered,
                "numfloors": numfloors, "yearbuilt": yearbuilt}
    return look


class TestIsResidential:
    def test_units_make_it_residential(self):
        assert is_residential(10, "O4", "5") is True     # units win

    def test_residential_class_or_landuse(self):
        assert is_residential(0, "C1", "") is True        # class C = residential
        assert is_residential(0, "", "02") is True        # landuse 02 = multifamily

    def test_commercial_is_not_residential(self):
        assert is_residential(0, "O4", "05") is False     # office / commercial


class TestDerivedAmenities:
    def test_class_d_is_elevator(self):
        assert derived_amenities("D5") == ["elevator"]

    def test_walkup_and_others_derive_nothing(self):
        assert derived_amenities("C1") == [] and derived_amenities("") == []

    def test_verify_enriches_listing_with_elevator(self):
        r = rec()
        verify_one(r, lookup_building(bldgclass="D5"), now=5.0)
        assert r["listing"]["derived_amenities"] == ["elevator"]

    def test_non_residential_derives_nothing(self):
        r = rec()
        verify_one(r, lookup_building(units=0, bldgclass="O4", landuse="5"), now=5.0)
        assert r["listing"]["derived_amenities"] == []


class TestInferredAmenities:
    def test_old_small_building_infers_nothing(self):
        assert inferred_amenities("C1", numfloors=4, yearbuilt=1960, unitsres=6) == []

    def test_tall_new_large_building_infers_hints(self):
        got = inferred_amenities("D5", numfloors=30, yearbuilt=2018, unitsres=200)
        assert "doorman" in got and "dishwasher" in got and "laundry_in_unit" in got

    def test_verify_stores_inferences_separately_from_facts(self):
        r = rec()
        verify_one(r, lookup_building(bldgclass="D5", numfloors=30,
                                      yearbuilt=2018, units=200), now=5.0)
        assert r["listing"]["derived_amenities"] == ["elevator"]      # fact
        assert "doorman" in r["listing"]["inferred_amenities"]        # guess
        assert "elevator" not in r["listing"]["inferred_amenities"]   # no overlap


class TestVerifyOne:
    def test_ok_for_registered_residential_building(self):
        r = verify_one(rec(), lookup_building(), now=5.0)
        assert r["status"] == "ok" and r["units"] == 10 and r["registered"] is True
        assert r["at"] == 5.0 and r["km_off"] < 0.1

    def test_not_residential_flagged(self):
        r = verify_one(rec(), lookup_building(units=0, bldgclass="O4", landuse="5"),
                       now=5.0)
        assert r["status"] == "not_residential"

    def test_mismatch_when_far_from_listing(self):
        r = verify_one(rec(), lookup_building(lat=40.70, lon=-73.99), now=5.0)
        assert r["status"] == "mismatch" and r["km_off"] > 1.5

    def test_not_found_when_no_building_record(self):
        r = verify_one(rec(), lookup_building(found=False), now=5.0)
        assert r["status"] == "not_found" and r["km_off"] is None

    def test_short_address_skips_lookup(self):
        called = []
        def look(_a, _b, _c):
            called.append(1)
            return {"found": True}
        r = verify_one(rec(address="NYC"), look, now=5.0)
        assert r["status"] == "not_found" and not called


class FakeStore:
    def __init__(self, records):
        self._records = records
    def records(self):
        return self._records


class TestVerifyStore:
    def test_respects_limit_newest_first(self):
        records = {f"k{i}": rec(last_seen=float(i)) for i in range(5)}
        n = verify_store(FakeStore(records), lookup_building(), limit=2, now=9.0)
        assert n == 2
        assert records["k4"].get("verify") and records["k3"].get("verify")
        assert "verify" not in records["k0"]

    def test_skips_until_ttl(self):
        r = rec()
        r["verify"] = {"at": 100.0, "status": "ok"}
        assert needs_verify(r, now=110.0) is False
        assert verify_store(FakeStore({"k": r}), lookup_building(), now=110.0) == 0
