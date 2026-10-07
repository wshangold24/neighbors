"""Provenance-aware fetch manifest (Milestone 1).

The HTTP cache knows *what* was fetched; this manifest records *why* —
which preferences and which provider produced each fetch. That lets a later
run answer: "did a previous fetch already cover what I'm about to ask for?"
If yes, the listings are already in the store and the network can be skipped.

Storage: one JSON object per line (JSONL) in ~/.neighbors/cache/manifest.jsonl.
JSONL is append-only, so recording a fetch never rewrites history.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from .models import Preferences

# Only fields that change WHAT a provider fetches belong in the fingerprint.
# k, weights, amenities etc. affect ranking, not the fetch query — including
# them would make identical fetches look different and defeat coverage reuse.
FETCH_RELEVANT_FIELDS = ("min_price", "max_price", "price_stretch", "neighborhoods")


def prefs_snapshot(p: Preferences) -> dict:
    """The fetch-relevant slice of preferences, as plain JSON data."""
    return {f: getattr(p, f) for f in FETCH_RELEVANT_FIELDS}


def prefs_fingerprint(p: Preferences) -> str:
    """Stable short hash of the fetch-relevant preferences."""
    canonical = json.dumps(prefs_snapshot(p), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _price_range(snap: dict) -> tuple[float, float]:
    """The effective [lo, hi] price window a fetch with these prefs asked for."""
    lo = snap["min_price"] or 0
    hi = snap["max_price"] * (1 + snap["price_stretch"])
    return lo, hi


def covers(old: dict, new: dict) -> bool:
    """Does a fetch made with `old` prefs include everything a fetch with
    `new` prefs would ask for? Expressed as a rule set — all must hold."""
    old_lo, old_hi = _price_range(old)
    new_lo, new_hi = _price_range(new)
    old_hoods = {h.lower() for h in old["neighborhoods"]}
    new_hoods = {h.lower() for h in new["neighborhoods"]}
    rules = (
        old_lo <= new_lo,                       # price window is a superset…
        old_hi >= new_hi,                       # …on both ends
        # geography: a city-wide fetch (no neighborhoods) covers anything;
        # otherwise the old area must contain every new neighborhood
        not old_hoods or (bool(new_hoods) and new_hoods <= old_hoods),
    )
    return all(rules)


class Manifest:
    """Append-only record of provider fetches with their preference context."""

    def __init__(self, path: Path):
        self.path = path

    def record(self, provider: str, prefs: Preferences, listings_found: int,
               requests_used: int = 0) -> dict:
        entry = {
            "provider": provider,
            "prefs_fingerprint": prefs_fingerprint(prefs),
            "prefs_snapshot": prefs_snapshot(prefs),
            "fetched_at": time.time(),
            "listings_found": listings_found,
            "requests_used": requests_used,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a torn write must not poison the whole manifest
        return out

    def latest_covering(self, provider: str, prefs: Preferences,
                        max_age_s: float) -> dict | None:
        """Most recent fetch by `provider` that is fresh enough AND covered
        the current preferences (so its listings are already in the store)."""
        now = time.time()
        new_snap = prefs_snapshot(prefs)
        candidates = [
            e for e in self.entries()
            if e["provider"] == provider
            and now - e["fetched_at"] <= max_age_s
            and e["listings_found"] > 0
            and covers(e["prefs_snapshot"], new_snap)
        ]
        return max(candidates, key=lambda e: e["fetched_at"], default=None)
