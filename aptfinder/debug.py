"""Debug reporting for aptfinder.

Run the CLI with --debug to write a report into ~/.aptfinder/debug/ — one
timestamped file per run (run_<timestamp>.json), plus latest.json always
holding the most recent report. Each is a single paste-able JSON document
containing everything needed to diagnose a bad run: environment, args,
preferences, every HTTP request (status/cache/timing), per-provider outcomes
with tracebacks, captured logs, ranked-result feature breakdowns, and any
crash traceback. The newest KEEP_REPORTS files are kept; older ones pruned.

Secrets (API keys) are redacted from every report before it is written.
"""

from __future__ import annotations

import itertools
import json
import logging
import os
import platform
import sys
import time
from pathlib import Path

from . import __version__

DEBUG_DIR = Path.home() / ".aptfinder" / "debug"
KEEP_REPORTS = 25            # newest reports kept; older ones pruned
REDACTED = "***REDACTED***"
_seq = itertools.count()     # disambiguates multiple saves in one process


class LogCapture(logging.Handler):
    """Collects all aptfinder log records for inclusion in the report."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(
                f"{record.levelname} {record.name}: {record.getMessage()}"
            )
        except Exception:
            pass


class DebugSession:
    """Accumulates diagnostics during a run; save() writes the report."""

    def __init__(self, args: dict | None = None):
        self.started = time.time()
        self.args = dict(args or {})
        self.secrets: set[str] = set()
        self.provider_stats: list[dict] = []
        self.session = None          # SafeSession, attached by fetch_listings
        self.prefs = None
        self.listing_count: int | None = None
        self.results: list | None = None   # [(Listing, dist, parts), ...]
        self.error: str | None = None
        self.log_capture = LogCapture()
        root = logging.getLogger("aptfinder")
        root.addHandler(self.log_capture)
        if root.level in (logging.NOTSET, logging.WARNING):
            root.setLevel(logging.DEBUG)

    def add_secret(self, value: str | None) -> None:
        if value and len(value.strip()) >= 6:
            self.secrets.add(value.strip())

    # ---------- report assembly ----------

    def _results_summary(self) -> list[dict] | None:
        if self.results is None:
            return None
        return [
            {
                "rank": i + 1,
                "id": l.id,
                "source": l.source,
                "price": l.price,
                "beds": l.beds,
                "baths": l.baths,
                "sqft": l.sqft,
                "neighborhood": l.neighborhood,
                "distance": round(d, 4),
                "feature_distances": {k: round(v, 4) for k, v in parts.items()},
            }
            for i, (l, d, parts) in enumerate(self.results[:20])
        ]

    def build(self) -> dict:
        http = None
        if self.session is not None:
            http = {
                "budget": self.session.budget,
                "budget_spent": self.session.spent,
                "requests": self.session.history,
            }
        return {
            "meta": {
                "aptfinder_version": __version__,
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "duration_s": round(time.time() - self.started, 2),
                "args": self.args,
            },
            "preferences": self.prefs.to_dict() if self.prefs else None,
            "http": http,
            "providers": self.provider_stats,
            "listing_count": self.listing_count,
            "results_top": self._results_summary(),
            "log": self.log_capture.records[-400:],
            "error": self.error,
        }

    def save(self, directory: Path | None = None) -> Path:
        """Write run_<timestamp>.json into the debug folder (plus latest.json),
        prune old reports, and return the new report's path."""
        directory = directory or DEBUG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.build(), indent=2, default=str)
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        # pid + per-process counter guarantee unique, chronologically sortable
        # names even for saves within the same second.
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = directory / f"run_{ts}-{os.getpid() % 100000:05d}-{next(_seq):03d}.json"
        path.write_text(text)
        (directory / "latest.json").write_text(text)
        self._prune(directory)
        return path

    @staticmethod
    def _prune(directory: Path) -> None:
        reports = sorted(directory.glob("run_*.json"))
        for old in reports[:-KEEP_REPORTS]:
            try:
                old.unlink()
            except OSError:
                pass
