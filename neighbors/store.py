"""Persistent listing store (Milestone 2).

Fetches APPEND here instead of overwriting each other, so the app builds a
picture of the market across runs that no single fetch can give:

  * the same apartment seen on two sites merges into one record
  * price changes accumulate into a history (drops are gold)
  * listings not seen recently are treated as probably-rented (inactive)
    but kept for history

Identity: listings are keyed by normalized address, not provider id, because
the same unit appears under different ids on different sites — a re-fetched
apartment is UPDATED in place, never duplicated.

Storage: a single SQLite database (stdlib `sqlite3`, no install, no server).
The schema is lightly denormalized — one row per apartment, with the queryable
fields (id, price, beds, coords, last_seen) as columns and the full record kept
as a JSON `data` column. This mirrors the old JSON-file shape, so the rest of
the app is unchanged (it still goes through this class), and it ports cleanly to
Postgres later (the JSON column becomes JSONB).

The same database file also holds the feedback and quota tables (see
feedback.py, quota.py); `open_db` is the one place connections are made.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .models import Listing

# NYC rentals move fast (well-priced units go in days; median time on market
# is a few weeks). A listing our fetches haven't seen for a week is likely
# rented, so it drops out of ranking — but its record is KEPT in the store
# as reference (price history, "what does $X buy in this area").
ACTIVE_WINDOW_DAYS = 7
DAY_S = 24 * 3600


@contextmanager
def open_db(path: Path, schema: str) -> Iterator[sqlite3.Connection]:
    """Open the shared SQLite database, ensure `schema` exists, and yield the
    connection. Commits on success, rolls back on error, and always closes
    (sqlite3's own context manager commits but never closes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")   # tolerate concurrent readers
        conn.executescript(schema)
        with conn:
            yield conn
    finally:
        conn.close()


def identity(l: Listing) -> str:
    """Provider-independent key: lowercase address with punctuation and
    whitespace collapsed. '123 Kent Ave, Apt 4B' == '123 kent ave apt 4b'."""
    return re.sub(r"[^a-z0-9]+", " ", l.address.lower()).strip()


_LIST_FIELDS = ("amenities", "derived_amenities", "inferred_amenities")


def _heal(rec: dict) -> dict:
    """Repair a loaded record in place: list fields that were persisted as null
    (older schema, or a merge that fell back to a missing key) become []. A None
    here isn't iterable and crashes display/verification downstream."""
    listing = rec.get("listing")
    if isinstance(listing, dict):
        for f in _LIST_FIELDS:
            if listing.get(f) is None:
                listing[f] = []
    return rec


@dataclass
class UpsertStats:
    """What one merge changed — used for NEW / price-drop display flags."""
    new: list[str] = field(default_factory=list)          # listing ids
    price_drops: dict[str, tuple[int, int]] = field(default_factory=dict)  # id -> (old, new)
    updated: int = 0


_SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  key          TEXT PRIMARY KEY,   -- identity(): normalized address (dedup key)
  id           TEXT,               -- listing id (for find())
  price        INTEGER,
  beds         REAL,
  baths        REAL,
  lat          REAL,
  lon          REAL,
  neighborhood TEXT,
  source       TEXT,
  first_seen   REAL,
  last_seen    REAL,
  data         TEXT                 -- the full record dict, as JSON
);
CREATE INDEX IF NOT EXISTS idx_listings_last_seen ON listings(last_seen);
CREATE INDEX IF NOT EXISTS idx_listings_id ON listings(id);
"""


class Store:
    """Append-and-merge listing store persisted in SQLite.

    Record shape (one per unique apartment):
      {"listing": {...Listing fields...},
       "first_seen": epoch, "last_seen": epoch,
       "price_history": [[epoch, price], ...],
       "sources": {"rentcast": url, "housingconnect": url}}

    Records are loaded into memory on open and written back on save(). Callers
    may mutate the dicts returned by records() directly; save() writes only the
    records whose content changed since they were loaded or last saved, so two
    processes sharing the database (CLI and web UI) don't overwrite each other's
    untouched records with stale copies.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._records: dict[str, dict] = {}
        self._saved: dict[str, str] = {}    # key -> JSON as last read/written
        self._load()

    def _load(self) -> None:
        with open_db(self.path, _SCHEMA) as conn:
            rows = conn.execute("SELECT key, data FROM listings").fetchall()
        for key, data in rows:
            try:
                self._records[key] = _heal(json.loads(data))
            except (json.JSONDecodeError, TypeError):
                continue
            self._saved[key] = data

    @staticmethod
    def _row(key: str, rec: dict, data: str) -> tuple:
        l = rec.get("listing", {})
        return (key, l.get("id"), l.get("price"), l.get("beds"), l.get("baths"),
                l.get("lat"), l.get("lon"), l.get("neighborhood"), l.get("source"),
                rec.get("first_seen"), rec.get("last_seen"), data)

    def save(self) -> None:
        """Upsert every changed record in one transaction. The store only ever
        adds/updates, never deletes, so no rows need pruning."""
        changed = {}
        for key, rec in self._records.items():
            data = json.dumps(rec)
            if self._saved.get(key) != data:
                changed[key] = data
        if not changed:
            return
        with open_db(self.path, _SCHEMA) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO listings "
                "(key,id,price,beds,baths,lat,lon,neighborhood,source,first_seen,last_seen,data) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [self._row(k, self._records[k], d) for k, d in changed.items()])
        self._saved.update(changed)

    # ---------- merging ----------

    def upsert(self, listings: list[Listing], now: float | None = None) -> UpsertStats:
        """Merge a fetch into the store. Never deletes; only adds/refreshes."""
        now = time.time() if now is None else now
        stats = UpsertStats()
        for l in listings:
            key = identity(l)
            if not key:
                continue
            rec = self._records.get(key)
            if rec is None:
                self._records[key] = {
                    "listing": l.to_dict(),
                    "first_seen": now,
                    "last_seen": now,
                    "price_history": [[now, l.price]],
                    "sources": {l.source: l.url},
                }
                stats.new.append(l.id)
            else:
                stats.updated += 1
                rec["last_seen"] = now
                rec["sources"][l.source] = l.url
                old_price = rec["price_history"][-1][1]
                if l.price != old_price:
                    rec["price_history"].append([now, l.price])
                    if l.price < old_price:
                        stats.price_drops[rec["listing"]["id"]] = (old_price, l.price)
                # keep the freshest listing facts, but never lose known fields
                # (a scraper without sqft must not blank out sqft we had). Use
                # .get() for the old side: records written before a field existed
                # (e.g. derived_amenities) simply won't have that key, and the
                # merge must tolerate that rather than KeyError.
                fresh = l.to_dict()
                old = rec["listing"]
                kept_id = old["id"]  # feedback is keyed on this
                # prefer the fresh value; if it's empty, keep the old one — but
                # fall back to the fresh value (not None) when the old record
                # predates the field, so we never write a null into the store
                merged = {k: (fresh[k] if fresh[k] not in (None, "", [])
                              else old.get(k, fresh[k]))
                          for k in fresh}
                merged["id"] = kept_id
                rec["listing"] = merged
        return stats

    # ---------- reading ----------

    def records(self) -> dict[str, dict]:
        return self._records

    def active(self, now: float | None = None,
               window_days: int = ACTIVE_WINDOW_DAYS) -> list[Listing]:
        """Listings seen recently enough to plausibly still be on the market."""
        now = time.time() if now is None else now
        cutoff = now - window_days * DAY_S
        return [Listing.from_dict(r["listing"])
                for r in self._records.values()
                if r["last_seen"] >= cutoff]

    def annotations(self, now: float | None = None) -> dict[str, str]:
        """Display flags keyed by listing id: NEW, price drops, staleness."""
        now = time.time() if now is None else now
        out = {}
        for r in self._records.values():
            lid = r["listing"]["id"]
            tags = []
            if now - r["first_seen"] < DAY_S:
                tags.append("NEW")
            hist = r["price_history"]
            if len(hist) >= 2 and hist[-1][1] < hist[-2][1]:
                tags.append(f"↓${hist[-2][1] - hist[-1][1]:,}")
            days = int((now - r["last_seen"]) / DAY_S)
            if days >= 3:
                tags.append(f"seen {days}d ago")
            if tags:
                out[lid] = " ".join(tags)
        return out

    def find(self, listing_id: str) -> Listing | None:
        for r in self._records.values():
            if r["listing"]["id"] == listing_id:
                return Listing.from_dict(r["listing"])
        return None

    def __len__(self) -> int:
        return len(self._records)
