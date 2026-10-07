"""Browser test fixtures: a live Neighbors server over a seeded, throwaway
~/.neighbors, with every network-touching seam stubbed, driven by a headless
Chromium that refuses any request not addressed to that server."""

from __future__ import annotations

import threading
from urllib.parse import urlparse

import pytest

from neighbors import cli, web
from neighbors.providers.demo import DemoProvider

from ..conftest import seed_home


class LiveApp:
    """Handle on the running app: its URL, data dir, and recorded side effects."""

    def __init__(self, url: str, app_dir):
        self.url = url
        self.app_dir = app_dir
        self.fetch_calls = 0
        self.external_requests: list[str] = []
        self.js_errors: list[str] = []


def _stub_network(monkeypatch, app: LiveApp) -> None:
    def fake_fetch_listings(prefs, sources, **_kw):
        app.fetch_calls += 1
        # a different seed than seed_home's → genuinely new listings
        listings = DemoProvider(None, n=40, seed=99).fetch(prefs)
        for listing in listings:
            listing.id = f"fresh-{listing.id}"
        return listings

    monkeypatch.setattr(cli, "fetch_listings", fake_fetch_listings)
    monkeypatch.setattr(web.Api, "_building_verifier",
                        lambda self, budget=40: (lambda address, lat, lon: {"found": False}))
    monkeypatch.setattr(web.Api, "geocode",
                        lambda self, *a, **kw: {"found": False, "error": "offline in tests"})


def _start(tmp_path, monkeypatch, with_listings: bool) -> tuple[LiveApp, object]:
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("RENTCAST_API_KEY", "NYC_APP_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    seed_home(tmp_path, with_listings=with_listings)
    app_dir = tmp_path / ".neighbors"
    server = web.make_server(app_dir, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = LiveApp(f"http://127.0.0.1:{server.server_port}/", app_dir)
    _stub_network(monkeypatch, app)
    return app, server


def _guard_page(page, app: LiveApp) -> None:
    """Block (and record) any request to a non-local host; record JS errors."""
    local = urlparse(app.url).netloc

    def route(r):
        if urlparse(r.request.url).netloc == local:
            return r.continue_()
        app.external_requests.append(r.request.url)
        return r.abort()

    page.route("**/*", route)
    page.on("pageerror", lambda exc: app.js_errors.append(str(exc)))


@pytest.fixture()
def live_app(tmp_path, monkeypatch, page):
    """A seeded app plus a guarded page. Asserts WEB-05 (no JS errors) on teardown."""
    app, server = _start(tmp_path, monkeypatch, with_listings=True)
    _guard_page(page, app)
    yield app
    server.shutdown()
    assert app.js_errors == [], f"WEB-05: JavaScript errors: {app.js_errors}"


@pytest.fixture()
def empty_app(tmp_path, monkeypatch, page):
    app, server = _start(tmp_path, monkeypatch, with_listings=False)
    _guard_page(page, app)
    yield app
    server.shutdown()
    assert app.js_errors == [], f"WEB-05: JavaScript errors: {app.js_errors}"
