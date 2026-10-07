"""Tests for offline transit estimates and the commute/maps API fields."""

from __future__ import annotations

import json

from .conftest import seed_home  # noqa: F401 (used via web fixture pattern)

from aptfinder.transit import (
    STATIONS, estimate_commute, nearest_station, stations_within,
)

# well-known coordinates for sanity anchors
WILLIAMSBURG = (40.7140, -73.9585)   # near Bedford Av L
MIDTOWN = (40.7527, -73.9772)        # Grand Central
GREENPOINT = (40.7245, -73.9440)
JERSEY_CITY = (40.7190, -74.0440)    # near Grove St PATH
FAR_ROCKAWAY_ISH = (40.590, -73.800) # no station in our table


class TestStations:
    def test_dataset_shape(self):
        for name, lines, lat, lon in STATIONS:
            assert name and lines
            assert 40.4 <= lat <= 41.0 and -74.3 <= lon <= -73.6, name

    def test_nearest_station_finds_bedford(self):
        st = nearest_station(*WILLIAMSBURG)
        assert st is not None and "L" in st.lines
        assert st.walk_min <= 10

    def test_nearest_station_none_when_too_far(self):
        assert nearest_station(*FAR_ROCKAWAY_ISH) is None

    def test_stations_within_ten_minute_walk(self):
        near = stations_within(*WILLIAMSBURG, max_walk_min=10)
        assert near, "central Williamsburg must have walkable stations"
        assert all(s.walk_min <= 10 for s in near)
        walks = [s.walk_min for s in near]
        assert walks == sorted(walks), "nearest first"
        assert any("L" in s.lines for s in near)
        assert len(near) <= 3

    def test_stations_within_empty_when_remote(self):
        assert stations_within(*FAR_ROCKAWAY_ISH) == []


class TestEstimates:
    def test_williamsburg_to_midtown_plausible(self):
        est = estimate_commute(*WILLIAMSBURG, *MIDTOWN)
        assert est.kind == "subway"
        assert 20 <= est.minutes <= 55, est
        assert "L" in est.route or "transfer" in est.route

    def test_short_hop_is_walk(self):
        est = estimate_commute(40.7140, -73.9585, 40.7172, -73.9567)
        assert est.kind == "walk" and est.minutes <= 15

    def test_shared_line_beats_transfer(self):
        """Same origin: a destination on the same line should not be slower
        than a comparable-distance destination requiring a transfer."""
        on_l = estimate_commute(*WILLIAMSBURG, 40.7308, -73.9817)   # 1 Av (L)
        off_l = estimate_commute(*WILLIAMSBURG, 40.7300, -73.9915)  # Astor Pl (6)
        assert "transfer" not in on_l.route
        assert "transfer" in off_l.route
        assert on_l.minutes < off_l.minutes

    def test_no_subway_fallback(self):
        est = estimate_commute(*FAR_ROCKAWAY_ISH, *MIDTOWN)
        assert est.kind == "no_subway" and est.minutes > 30

    def test_symmetricish(self):
        a = estimate_commute(*WILLIAMSBURG, *GREENPOINT)
        b = estimate_commute(*GREENPOINT, *WILLIAMSBURG)
        assert abs(a.minutes - b.minutes) <= 2

    def test_jersey_city_routes_via_path_not_fallback(self):
        """The bug: a Jersey City work address made EVERY card say 'no nearby
        subway'. With PATH stations in the table it must route properly."""
        est = estimate_commute(*WILLIAMSBURG, *JERSEY_CITY)
        assert est.kind == "subway", est
        assert "PATH" in est.route
        assert 25 <= est.minutes <= 75

    def test_steps_ordered_walk_ride_walk_and_sum_to_total(self):
        """The itinerary contract: ordered steps, each timed, summing to the
        total — walk in first, walk out last, ride(s) between."""
        est = estimate_commute(*WILLIAMSBURG, *MIDTOWN)
        kinds = [s.kind for s in est.steps]
        assert kinds[0] == "walk" and kinds[-1] == "walk"
        assert "ride" in kinds
        assert all(s.minutes >= 1 and s.label for s in est.steps)
        assert sum(s.minutes for s in est.steps) == est.minutes

    def test_transfer_appears_as_its_own_step(self):
        est = estimate_commute(*WILLIAMSBURG, *MIDTOWN)  # L → 4/5/6 area
        assert [s.kind for s in est.steps].count("transfer") == 1
        assert [s.kind for s in est.steps] == ["walk", "ride", "transfer", "ride", "walk"]
