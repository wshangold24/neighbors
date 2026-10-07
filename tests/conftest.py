"""Shared test fixtures: local HTTP server, SafeSession factory, CLI runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from aptfinder.http import SafeSession  # noqa: E402
from aptfinder.models import Preferences  # noqa: E402
from aptfinder.providers.demo import DemoProvider  # noqa: E402
from aptfinder.store import Store  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    """Scriptable endpoints: /ok, /forbidden, /flaky (429 once, then 200),
    /error (always 500). Records request timestamps per path."""

    hits: dict[str, list[float]] = {}

    def do_GET(self):
        path = self.path.split("?")[0]
        self.hits.setdefault(path, []).append(time.monotonic())
        n = len(self.hits[path])
        if path == "/ok":
            self._respond(200, "<html>hello</html>")
        elif path == "/forbidden":
            self._respond(403, "blocked")
        elif path == "/flaky":
            if n == 1:
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.end_headers()
                self.wfile.write(b"slow down")
            else:
                self._respond(200, "recovered")
        elif path == "/error":
            self._respond(500, "boom")
        else:
            self._respond(404, "nope")

    def _respond(self, code: int, body: str):
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *a):  # silence
        pass


@pytest.fixture()
def http_server():
    Handler.hits = {}
    server = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{server.server_port}"
    yield base, Handler
    server.shutdown()


def make_session(tmp_path, **kw) -> SafeSession:
    defaults = dict(budget=20, min_interval=0.3, timeout=5.0, max_retries=3)
    defaults.update(kw)
    s = SafeSession(tmp_path / "cache", **defaults)
    s.limiter.jitter = 0.05  # keep tests fast/deterministic
    return s


# ---------- CLI helpers ----------

# One line per preference prompt, in order: max price, min price, beds, baths,
# neighborhoods, amenities, sqft, k, stretch %, data sources.
PROMPT_INPUT = "3500\n0\n1\n1\nwilliamsburg, greenpoint\nlaundry_in_unit\n0\n10\n15\nall\n"


def run_cli(args: list[str], home: Path, stdin: str = "",
            extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home), **(extra_env or {})}
    env.pop("RENTCAST_API_KEY", None) if not (extra_env or {}).get("RENTCAST_API_KEY") else None
    return subprocess.run(
        [sys.executable, "run.py", *args],
        cwd=PROJECT_ROOT, env=env, input=stdin,
        capture_output=True, text=True, timeout=120,
    )


def seed_home(home: Path, with_listings: bool = True) -> None:
    """Pre-populate ~/.aptfinder as if a prior live fetch had succeeded.
    DemoProvider is used ONLY here, to generate test fixtures — it is not
    reachable from the CLI."""
    d = home / ".aptfinder"
    d.mkdir(parents=True, exist_ok=True)
    prefs = Preferences(max_price=3500, beds=1,
                        neighborhoods=["williamsburg", "greenpoint"],
                        amenities=["laundry_in_unit"], k=10)
    (d / "preferences.json").write_text(json.dumps(prefs.to_dict()))
    if with_listings:
        store = Store(d / "store.db")
        store.upsert(DemoProvider(None, n=150).fetch(prefs))
        store.save()
