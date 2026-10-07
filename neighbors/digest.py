"""'What's new' digest — what changed in the store since you last looked.

Pure logic over a Store's records and Feedback (no I/O), so it's easy to test
and can back both an in-app banner and a scheduled/CLI summary. The three
signals a renter actually cares about between visits: newly-seen listings,
price drops (especially on places they liked — the strongest buy signal in this
domain), and listings that have just been verified as real.
"""

from __future__ import annotations


def build_digest(store, feedback, since: float) -> dict:
    """Summarize everything that changed at or after `since`."""
    liked = set(feedback.liked)
    new_ids: list[str] = []
    price_drops: list[dict] = []
    liked_drops: list[dict] = []
    newly_verified = 0

    for rec in store.records().values():
        listing = rec.get("listing", {})
        lid = listing.get("id")
        if rec.get("first_seen", 0) >= since:
            new_ids.append(lid)

        hist = rec.get("price_history", [])
        if len(hist) >= 2 and hist[-1][0] >= since and hist[-1][1] < hist[-2][1]:
            drop = {"id": lid, "address": listing.get("address", ""),
                    "old": hist[-2][1], "new": hist[-1][1],
                    "drop": hist[-2][1] - hist[-1][1]}
            price_drops.append(drop)
            if lid in liked:
                liked_drops.append(drop)

        v = rec.get("verify")
        if v and v.get("status") == "ok" and v.get("at", 0) >= since:
            newly_verified += 1

    return {
        "since": since,
        "new_count": len(new_ids),
        "new_ids": new_ids[:10],
        "price_drops": price_drops,
        "liked_drops": liked_drops,
        "newly_verified": newly_verified,
        "has_news": bool(new_ids or price_drops or newly_verified),
    }
