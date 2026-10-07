"""Tests for the what's-new digest (pure over store records + feedback)."""

from __future__ import annotations

from neighbors.digest import build_digest


class FakeStore:
    def __init__(self, records):
        self._records = records
    def records(self):
        return self._records


class FakeFeedback:
    def __init__(self, liked):
        self.liked = liked


def rec(lid, first_seen, price_history, verify=None):
    return {"listing": {"id": lid, "address": f"{lid} St"},
            "first_seen": first_seen, "price_history": price_history,
            **({"verify": verify} if verify else {})}


def test_new_price_drop_and_verified_since_cutoff():
    records = {
        "a": rec("a", first_seen=100, price_history=[[100, 3000]]),            # new
        "b": rec("b", first_seen=10, price_history=[[10, 3200], [150, 3000]]),  # drop after cutoff
        "c": rec("c", first_seen=10, price_history=[[10, 3000]],
                 verify={"status": "ok", "at": 150}),                          # newly verified
        "d": rec("d", first_seen=10, price_history=[[10, 3000], [20, 2900]]),   # drop BEFORE cutoff
    }
    d = build_digest(FakeStore(records), FakeFeedback({}), since=50)
    assert d["new_count"] == 1 and d["new_ids"] == ["a"]
    assert [x["id"] for x in d["price_drops"]] == ["b"]
    assert d["newly_verified"] == 1
    assert d["has_news"] is True


def test_liked_price_drops_are_flagged():
    records = {"b": rec("b", 10, [[10, 3200], [150, 3000]])}
    d = build_digest(FakeStore(records), FakeFeedback({"b": 1.0}), since=50)
    assert [x["id"] for x in d["liked_drops"]] == ["b"]
    assert d["price_drops"][0]["drop"] == 200


def test_nothing_new_reports_no_news():
    records = {"a": rec("a", first_seen=10, price_history=[[10, 3000]])}
    d = build_digest(FakeStore(records), FakeFeedback({}), since=50)
    assert d["has_news"] is False and d["new_count"] == 0
