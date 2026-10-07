"""Persistent like/hide feedback (Milestone 3).

Verdicts survive across runs and drive two things:
  * hidden listings never appear again
  * liked listings become KNN seeds that reorder results (see engine.py)
    and, with enough of them, teach the feature weights (see learn.py)

Stored in the shared SQLite database (`store.db`) alongside the listings —
tables `feedback(id, kind, ts)` and `seen(id, n)`. A legacy `feedback.json` is
imported once on first open. The in-memory dicts and method signatures are
unchanged, so callers are unaffected.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (id TEXT, kind TEXT, ts REAL, PRIMARY KEY (id, kind));
CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, n INTEGER);
"""


class Feedback:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.liked, self.hidden, self.seen = self._load()
        if not (self.liked or self.hidden or self.seen) and self._migrate_legacy_json():
            self.liked, self.hidden, self.seen = self._load()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    def _load(self) -> tuple[dict, dict, dict]:
        conn = self._connect()
        try:
            liked: dict[str, float] = {}
            hidden: dict[str, float] = {}
            for lid, kind, ts in conn.execute("SELECT id, kind, ts FROM feedback"):
                (liked if kind == "liked" else hidden)[lid] = ts
            seen = {lid: n for lid, n in conn.execute("SELECT id, n FROM seen")}
        finally:
            conn.close()
        return liked, hidden, seen

    def _migrate_legacy_json(self) -> bool:
        legacy = self.path.parent / "feedback.json"
        if legacy == self.path or not legacy.exists():
            return False
        try:
            data = json.loads(legacy.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        conn = self._connect()
        try:
            self._write(conn, data.get("liked", {}), data.get("hidden", {}),
                        data.get("seen", {}))
            conn.commit()
        finally:
            conn.close()
        return True

    @staticmethod
    def _write(conn, liked: dict, hidden: dict, seen: dict) -> None:
        conn.execute("DELETE FROM feedback")
        conn.execute("DELETE FROM seen")
        conn.executemany("INSERT INTO feedback (id, kind, ts) VALUES (?, ?, ?)",
                         [(k, "liked", v) for k, v in liked.items()] +
                         [(k, "hidden", v) for k, v in hidden.items()])
        conn.executemany("INSERT INTO seen (id, n) VALUES (?, ?)", list(seen.items()))

    def save(self) -> None:
        conn = self._connect()
        try:
            self._write(conn, self.liked, self.hidden, self.seen)
            conn.commit()
        finally:
            conn.close()

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
