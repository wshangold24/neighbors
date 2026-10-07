"""Learned feature weights from feedback (Milestone 5).

Intuition: if every apartment you like is in the same two neighborhoods but
their prices are scattered across your whole budget, then location clearly
matters to you more than price. Formally: features where your liked listings
AGREE (low spread of per-feature distances) get more weight; features where
they scatter get less.

Guardrails, in order of importance:
  * learned weights only affect the soft score — never the hard gate,
    so learning cannot cause drift outside your preferences
  * below MIN_LIKES verdicts we return None (not enough signal to beat noise)
  * the result is blended 50/50 with the hand-tuned defaults for stability
  * weights are stored in a plain JSON file you can inspect or delete
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .knn import DEFAULT_WEIGHTS, Query, distance
from .models import Listing, Preferences

MIN_LIKES = 5
BLEND = 0.5          # learned share; (1 - BLEND) stays with the defaults
SPREAD_FLOOR = 0.05  # avoids divide-by-zero when likes agree perfectly


def learn_weights(prefs: Preferences, liked: list[Listing],
                  defaults: dict | None = None) -> dict | None:
    """Return blended weights, or None when there isn't enough feedback."""
    defaults = defaults or DEFAULT_WEIGHTS
    if len(liked) < MIN_LIKES:
        return None

    q = Query.from_preferences(prefs)
    per_listing_parts = [distance(q, l, defaults, explain=True)[1] for l in liked]

    # Low spread across likes = the feature is consistently important to you.
    spread = {f: statistics.pstdev([p[f] for p in per_listing_parts]) + SPREAD_FLOOR
              for f in defaults}
    raw = {f: 1.0 / spread[f] for f in defaults}

    # Rescale so total weight mass matches the defaults (comparable distances).
    scale = sum(defaults.values()) / sum(raw.values())
    learned = {f: raw[f] * scale for f in defaults}

    return {f: round((1 - BLEND) * defaults[f] + BLEND * learned[f], 3)
            for f in defaults}


# ---------- persistence ----------

def load_weights(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        # only accept a complete, well-formed weight set
        return data if set(data) == set(DEFAULT_WEIGHTS) else None
    except (json.JSONDecodeError, TypeError):
        return None


def save_weights(path: Path, weights: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(weights, indent=1))
