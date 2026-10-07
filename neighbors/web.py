"""Local web app: a stdlib HTTP server bound to 127.0.0.1 (no dependencies).

Browsing is a visual task — cards, one-click like/hide and direct links to
source listings beat typing `like 3` in a terminal. This module is only the
HTTP plumbing: it validates requests, routes them to `Api` (webapi.py) and
encodes the JSON. Errors are {"error": message} with a 4xx/5xx status.

Routes (all JSON except the page):
  GET  /                    the single-page app (web.html)
  GET  /about               why Neighbors exists (about.html)
  GET  /api/state           prefs, store stats, quota, options, verification
  GET  /api/results         ranked results (?alpha=0..1, ?bbox=s,w,n,e,
                            ?poly=lat,lon,lat,lon,...)
  GET  /api/similar?id=X    seed mode: neighbors of one listing
  GET  /api/liked           the shortlist, for the compare view
  GET  /api/digest          what's new since last acknowledged
  GET  /api/geocode         ?street=&city=&zip= → coordinates
  POST /api/feedback        {"id", "verdict": "like" | "hide" | "clear"}
  POST /api/prefs           partial Preferences fields to update
  POST /api/area            {"bbox": [...]} | {"poly": [...]} | {} to clear
  POST /api/fetch           run a live fetch (plan- and quota-respecting)
  POST /api/verify          {"batch": N} | {"id": X}: check NYC registries
  POST /api/enrich          {"id": X}: RentCast amenity lookup
  POST /api/digest/ack      mark the digest as read
  POST /api/rentcast-key    {"key": "..."} (blank clears)
  POST /api/setup/dismiss   hide the first-run setup card
"""

from __future__ import annotations

import json
import logging
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .webapi import MAX_VERIFY_BATCH, Api, parse_bbox, parse_poly

__all__ = ["Api", "make_handler", "make_server", "serve"]

HOST = "127.0.0.1"   # local only: the API reads and writes the user's data
# static pages, by path
PAGES = {"/": "web.html", "/index.html": "web.html", "/about": "about.html"}
PAGE_DIR = Path(__file__).parent
MAX_BODY_BYTES = 1 << 20

# A client hanging up mid-response (navigating away, an aborted fetch) is
# expected, and nothing can be sent over a dead socket — so these are
# swallowed rather than logged as tracebacks.
DISCONNECTS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

log = logging.getLogger("neighbors.web")


class BadRequest(Exception):
    """Malformed request input; answered with {"error": message}."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------- request parsing ----------

Query = dict  # parse_qs output: name -> list of values


def _param(query: Query, name: str) -> str:
    return query.get(name, [""])[0]


def _alpha(query: Query) -> float | None:
    raw = _param(query, "alpha")
    if not raw:
        return None
    try:
        alpha = float(raw)
    except ValueError:
        alpha = float("nan")
    if not 0.0 <= alpha <= 1.0:   # also rejects nan
        raise BadRequest("alpha must be a number between 0 and 1")
    return alpha


def _query_bbox(query: Query) -> tuple | None:
    raw = _param(query, "bbox")
    if not raw:
        return None
    bbox = parse_bbox(raw.split(","))
    if bbox is None:
        raise BadRequest("bbox must be four numbers: south,west,north,east")
    return bbox


def _query_poly(query: Query) -> list | None:
    raw = _param(query, "poly")
    if not raw:
        return None
    nums = raw.split(",")
    poly = (parse_poly([nums[i:i + 2] for i in range(0, len(nums), 2)])
            if len(nums) % 2 == 0 else None)
    if poly is None:
        raise BadRequest("poly must be 3+ lat,lon pairs")
    return poly


def _body_str(body: dict, name: str, required: bool = False) -> str:
    value = body.get(name)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value):
        raise BadRequest(f"'{name}' must be a{' non-empty' if required else ''} string")
    return value


def _body_area(body: dict) -> dict:
    """{} clears the saved area; otherwise a valid bbox or poly is required."""
    if body.get("bbox") is None and body.get("poly") is None:
        return {}
    poly = parse_poly(body.get("poly")) if body.get("poly") is not None else None
    bbox = parse_bbox(body.get("bbox")) if body.get("bbox") is not None else None
    if poly is None and bbox is None:
        raise BadRequest("area needs a bbox [s,w,n,e] or a poly of 3+ [lat,lon] points")
    return {"poly": poly} if poly else {"bbox": bbox}


def _body_batch(body: dict) -> int:
    batch = body.get("batch", 20)
    if isinstance(batch, bool) or not isinstance(batch, int) or batch < 1:
        raise BadRequest(f"'batch' must be an integer from 1 to {MAX_VERIFY_BATCH}")
    return batch


# ---------- routes ----------

Route = Callable[[Api, Any], dict]

GET_ROUTES: dict[str, Route] = {
    "/api/state": lambda api, q: api.state(),
    "/api/results": lambda api, q: api.results(
        alpha=_alpha(q), bbox=_query_bbox(q), poly=_query_poly(q)),
    "/api/similar": lambda api, q: api.similar(_param(q, "id")),
    "/api/liked": lambda api, q: api.liked(),
    "/api/digest": lambda api, q: api.digest(),
    "/api/geocode": lambda api, q: api.geocode(
        street=_param(q, "street"), city=_param(q, "city"), zip_code=_param(q, "zip")),
}

POST_ROUTES: dict[str, Route] = {
    "/api/feedback": lambda api, b: api.set_feedback(
        _body_str(b, "id", required=True), _body_str(b, "verdict")),
    "/api/prefs": lambda api, b: api.update_prefs(b),
    "/api/area": lambda api, b: api.save_area(**_body_area(b)),
    "/api/fetch": lambda api, b: api.fetch(),
    "/api/verify": lambda api, b: api.verify(
        batch=_body_batch(b), listing_id=_body_str(b, "id") or None),
    "/api/enrich": lambda api, b: api.enrich(_body_str(b, "id", required=True)),
    "/api/digest/ack": lambda api, b: api.ack_digest(),
    "/api/rentcast-key": lambda api, b: api.save_rentcast_key(_body_str(b, "key")),
    "/api/setup/dismiss": lambda api, b: api.dismiss_setup(),
}


# ---------- HTTP ----------

def make_handler(api: Api) -> type[BaseHTTPRequestHandler]:
    """A request handler class bound to `api`."""

    class Handler(BaseHTTPRequestHandler):
        timeout = 60   # a stalled client can't pin a server thread forever

        def log_message(self, *args):   # keep the terminal quiet
            pass

        def handle(self):
            try:
                super().handle()
            except DISCONNECTS:
                pass

        def _send(self, status: int, body: bytes, ctype: str,
                  headers: dict | None = None) -> None:
            try:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                # page and API are always dynamic: never serve a stale copy
                self.send_header("Cache-Control", "no-store, must-revalidate")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)
            except DISCONNECTS:
                pass

        def _send_json(self, data: dict, status: int = 200, headers: dict | None = None) -> None:
            self._send(status, json.dumps(data).encode(), "application/json", headers)

        def _read_body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                raise BadRequest("invalid Content-Length") from None
            if length < 0:
                raise BadRequest("invalid Content-Length")
            if length > MAX_BODY_BYTES:
                raise BadRequest("request body too large", 413)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:   # JSONDecodeError and UnicodeDecodeError
                raise BadRequest("invalid JSON") from None
            if not isinstance(body, dict):
                raise BadRequest("request body must be a JSON object")
            return body

        def _dispatch(self, routes: dict[str, Route], other: dict[str, Route],
                      params: Callable[[], Any]) -> None:
            path = urlparse(self.path).path
            route = routes.get(path)
            if route is None:
                if path in other:
                    allow = "POST" if routes is GET_ROUTES else "GET"
                    self._send_json({"error": "method not allowed"}, 405, {"Allow": allow})
                else:
                    self._send_json({"error": "not found"}, 404)
                return
            try:
                result = route(api, params())
            except BadRequest as e:
                self._send_json({"error": str(e)}, e.status)
            except DISCONNECTS:
                pass
            except Exception as e:   # report to the UI instead of dropping the socket
                log.exception("%s %s failed", self.command, path)
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
            else:
                self._send_json(result, getattr(result, "status", 200))

        def do_GET(self):
            url = urlparse(self.path)
            page = PAGES.get(url.path.rstrip("/") or "/")
            if page:
                self._send(200, (PAGE_DIR / page).read_bytes(), "text/html; charset=utf-8")
                return
            self._dispatch(GET_ROUTES, POST_ROUTES, lambda: parse_qs(url.query))

        def do_POST(self):
            self._dispatch(POST_ROUTES, GET_ROUTES, self._read_body)

    return Handler


def make_server(app_dir: Path, port: int = 0) -> ThreadingHTTPServer:
    """Bind 127.0.0.1:`port` (0 = any free port). Caller runs serve_forever()."""
    app_dir = Path(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)
    return ThreadingHTTPServer((HOST, port), make_handler(Api(app_dir)))


def serve(app_dir: Path, port: int, open_browser: bool = True) -> None:
    """Run the web app until Ctrl-C. Raises OSError if the port is taken."""
    server = make_server(app_dir, port)
    url = f"http://{HOST}:{server.server_port}/"
    print(f"  neighbors web UI: {url}  (Ctrl-C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        server.server_close()
