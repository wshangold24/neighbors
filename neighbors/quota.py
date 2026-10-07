"""Monthly API quota ledger (Milestone 4).

Per-run budgets (SafeSession) protect a single run; this ledger protects the
MONTH. It counts billable requests per provider per calendar month so that
iterating several times a week can never blow through a free tier.

Only providers listed in MONTHLY_LIMITS are constrained — scrapers have no
monthly quota (they're already throttled per-run by SafeSession).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

# MONTHLY_LIMITS is the provider's REAL tier ceiling (what you pay for /
# what the dashboard shows). SOFT_STOP is where WE stop fetching — below the
# ceiling so a miscount on our side can never overshoot the real limit.
# RentCast free tier = 50 requests/month.
MONTHLY_LIMITS = {"rentcast": 50}
SOFT_STOP = {"rentcast": 45}
WARN_AT = {"rentcast": 35}

_SCHEMA = "CREATE TABLE IF NOT EXISTS quota (provider TEXT, month TEXT, n INTEGER, " \
          "PRIMARY KEY (provider, month));"


def current_month() -> str:
    return time.strftime("%Y-%m")


class QuotaLedger:
    """Persistent {provider: {month: count}} counter, stored in the shared
    SQLite database (`store.db`, table `quota`). A legacy quota.json is imported
    once on first open."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._counts = self._load()
        if not self._counts and self._migrate_legacy_json():
            self._counts = self._load()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        return conn

    def _load(self) -> dict:
        conn = self._connect()
        try:
            counts: dict[str, dict[str, int]] = {}
            for provider, month, n in conn.execute("SELECT provider, month, n FROM quota"):
                counts.setdefault(provider, {})[month] = n
        finally:
            conn.close()
        return counts

    def _migrate_legacy_json(self) -> bool:
        legacy = self.path.parent / "quota.json"
        if legacy == self.path or not legacy.exists():
            return False
        try:
            self._counts = json.loads(legacy.read_text())
        except (json.JSONDecodeError, OSError):
            return False
        self.save()
        return True

    def save(self) -> None:
        conn = self._connect()
        try:
            conn.execute("DELETE FROM quota")
            conn.executemany(
                "INSERT INTO quota (provider, month, n) VALUES (?, ?, ?)",
                [(p, m, n) for p, months in self._counts.items() for m, n in months.items()])
            conn.commit()
        finally:
            conn.close()

    def spent(self, provider: str, month: str | None = None) -> int:
        return self._counts.get(provider, {}).get(month or current_month(), 0)

    def record(self, provider: str, n: int) -> None:
        if n <= 0:
            return
        month = current_month()
        self._counts.setdefault(provider, {})
        self._counts[provider][month] = self._counts[provider].get(month, 0) + n

    def set_used(self, provider: str, n: int) -> None:
        """Overwrite this month's count — for syncing with the provider's own
        dashboard (this ledger only sees calls made through this app)."""
        self._counts.setdefault(provider, {})[current_month()] = max(0, n)

    def allow(self, provider: str) -> tuple[bool, str]:
        """(allowed, reason). Providers without a monthly limit always pass."""
        limit = MONTHLY_LIMITS.get(provider)
        if limit is None:
            return True, ""
        used = self.spent(provider)
        stop = SOFT_STOP.get(provider, limit)
        if used >= stop:
            return False, (f"monthly quota auto-stop reached ({used}/{limit} used, "
                           f"stop at {stop}) — resets next month, or fix the count "
                           "with: quota --set-used")
        if used >= WARN_AT.get(provider, stop):
            return True, f"quota warning: {used}/{limit} used this month"
        return True, f"{used}/{limit} used this month"

    def summary(self) -> dict[str, dict]:
        """Current-month view for the `quota` command."""
        month = current_month()
        return {
            p: {"used": self.spent(p, month), "limit": MONTHLY_LIMITS.get(p),
                "stop_at": SOFT_STOP.get(p)}
            for p in sorted(set(self._counts) | set(MONTHLY_LIMITS))
        }
