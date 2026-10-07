"""Persistent like/hide feedback (Milestone 3).

Verdicts survive across runs and drive two things:
  * hidden listings never appear again
  * liked listings become KNN seeds that reorder results (see engine.py)
    and, with enough of them, teach the feature weights (see learn.py)

Stored in the shared SQLite database (`store.db`) alongside the listings —
tables `feedback(id, kind, ts)` and `seen(id, n)`.
"""

from __future__ import annotations

import time
from pathlib import Path

from .store import open_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (id TEXT, kind TEXT, ts REAL, PRIMARY KEY (id, kind));
CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, n INTEGER);
"""


class Feedback:
    """In-memory `liked`/`hidden` ({id: timestamp}) and `seen` ({id: count})
    dicts, persisted by save().

    Callers may also edit the dicts directly. save() writes only the difference
    from what was loaded — verdicts set or removed, and seen-count increments —
    so a long-lived instance (an interactive CLI session) can't wipe out
    verdicts another process (the web UI) saved in the meantime."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.liked: dict[str, float] = {}
        self.hidden: dict[str, float] = {}
        self.seen: dict[str, int] = {}
        with open_db(self.path, _SCHEMA) as conn:
            for lid, kind, ts in conn.execute("SELECT id, kind, ts FROM feedback"):
                (self.liked if kind == "liked" else self.hidden)[lid] = ts
            self.seen = dict(conn.execute("SELECT id, n FROM seen"))
        self._snapshot()

    def _snapshot(self) -> None:
        self._saved = {"liked": dict(self.liked), "hidden": dict(self.hidden)}
        self._saved_seen = dict(self.seen)

    def save(self) -> None:
        with open_db(self.path, _SCHEMA) as conn:
            for kind, current in (("liked", self.liked), ("hidden", self.hidden)):
                before = self._saved[kind]
                conn.executemany("DELETE FROM feedback WHERE id = ? AND kind = ?",
                                 [(lid, kind) for lid in before if lid not in current])
                conn.executemany(
                    "INSERT OR REPLACE INTO feedback (id, kind, ts) VALUES (?, ?, ?)",
                    [(lid, kind, ts) for lid, ts in current.items()
                     if before.get(lid) != ts])
            conn.executemany(
                "INSERT INTO seen (id, n) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET n = n + excluded.n",
                [(lid, n - self._saved_seen.get(lid, 0)) for lid, n in self.seen.items()
                 if n != self._saved_seen.get(lid, 0)])
        self._snapshot()

    def mark_seen(self, listing_ids: list[str]) -> None:
        """Remember what was shown, so repeat results can be flagged and each
        session surfaces genuinely new candidates."""
        for lid in listing_ids:
            self.seen[lid] = self.seen.get(lid, 0) + 1

    def like(self, listing_id: str) -> None:
        self.hidden.pop(listing_id, None)   # a like overrides an old hide
        self.liked[listing_id] = time.time()

    def hide(self, listing_id: str) -> None:
        self.liked.pop(listing_id, None)
        self.hidden[listing_id] = time.time()
