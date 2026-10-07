# Design decisions

This document records the technical and system-design decisions behind Neighbors:
what was chosen, why, and which alternatives were considered and rejected. For
*how the ranking math works*, see [METHOD.md](METHOD.md) (intuition) and
[SCORING.md](SCORING.md) (exact formulas and invariants). For *what the app must
do*, see [SPEC.md](SPEC.md).

**Context that drives most decisions:** Neighbors is a single-user tool that runs
on the user's own computer, searching a few thousand NYC listings. The data
sources are free tiers and public datasets with strict limits. The project has to
be easy to install, honest about its data, and safe with the user's API keys.

---

## 1. System overview

```
             ┌──────────── entry points (imperative shell) ────────────┐
             │  cli.py  (neighbors)        web.py + webapi.py (web UI)  │
             └──────────────┬───────────────────────────┬──────────────┘
                            │ plain data in / out       │
  ┌─────────────── pure core (no I/O, unit-tested) ─────┴─────────────────┐
  │ models · knn · engine (gate + rank + fetch plan) · learn · transit    │
  │ manifest (coverage) · verify (verdicts) · digest · views (UI JSON)    │
  └───────────────────────────────┬───────────────────────────────────────┘
                                  │
  ┌──────────────── edges: state & network ───────────────────────────────┐
  │ store / feedback / quota → one SQLite file (~/.neighbors/store.db)    │
  │ http.SafeSession → rate limit · disk cache · budget · backoff         │
  │ providers: RentCast API · NYC Housing Connect · verification datasets │
  │ keystore → OS credential store (Keychain / Credential Manager / …)    │
  └───────────────────────────────────────────────────────────────────────┘
```

Every search follows the same pipeline: **fetch → validate → merge into the store
→ verify → hard gate → KNN rank (blended with likes) → display**.

---

## 2. Architecture

### Functional core, imperative shell

**Decision:** keep ranking, gating, fetch planning, learning, commute estimates
and verification verdicts as pure functions over plain data. Only `cli.py`,
`web.py`/`webapi.py`, the store and the HTTP layer do I/O.

**Why:** ranking bugs are subtle (see the invariants in SCORING.md), so the logic
has to be easy to test exhaustively. Pure modules are tested with plain values,
with no mocks or servers. Dependencies point one way too: `verify.py` takes its
building lookup as an injected callable, so verification logic runs with zero
network.

**Alternatives:**
- *A layered service/repository architecture.* Too much ceremony for one process
  and one user.
- *Logic inside the CLI and request handlers.* This is where the project started.
  It made the CLI and web app drift apart and made ranking hard to test.

### Two shells over one core

**Decision:** the CLI and the web app are thin, independent shells over the same
modules and the same on-disk state. The web app re-reads state from disk on every
request.

**Why:** the user can switch between terminal and browser, even at the same time,
without restarting anything or syncing state. Re-reading on each request costs
milliseconds at this scale and removes a whole class of stale-cache bugs.

**Alternative:** *the web app as the only interface, with the CLI calling it over
HTTP.* This would force a running server just to search from the terminal.

---

## 3. Ranking

### Similarity ranking (weighted KNN) instead of filters

**Decision:** represent listings and preferences as points in a six-feature space
(price, beds, baths, location, size, amenities), then rank by weighted distance.

**Why:** filters are binary, but preferences have soft edges. A $3,550 apartment
against a $3,500 budget is a near-miss the user would want to see, ranked by how
near it is. KNN gives a continuous score, an explainable per-feature breakdown
(the "furthest from your ask" chip), and a natural "more like this one" mode.

**Alternatives:**
- *Hard filters with a "loosen filters" button.* This still hides near-misses and
  makes the user guess which filter to relax.
- *A learned ranking model (gradient-boosted trees or learning-to-rank).* There's
  no training data for a single user, it isn't explainable per feature, and it
  would add heavy dependencies.
- *Text or embedding similarity.* Listings are mostly structured numbers, and
  descriptions from these sources are sparse.

### Two-stage ranking: hard gate, then soft score

**Decision:** first drop listings outside the user's acceptable region (price cap
plus stretch, bedroom floor, boroughs and neighborhoods, avoided areas). Then rank
the survivors. Likes and learned weights only affect the second stage.

**Why:** without a gate, feedback causes *drift*. Liking one expensive apartment
would pull other over-budget listings into the results. Separating "acceptable"
from "preferred" makes the guarantee easy to state and test: **likes reorder; they
never widen.**

**Alternative:** *a single blended score with large penalties.* Large penalties can
still be outweighed, so the guarantee becomes "usually" instead of "always".

### Directional distance and "unknown ≠ absent"

**Decision:** being better than asked is free (cheaper, more baths, more space),
and only shortfalls cost. The one exception is beds, where an extra bedroom costs
half as much as a missing one. Missing data is a mild fixed penalty in the score
and a pass in the gate.

**Why:** symmetric distance would punish a $3,200 listing for a $3,500 budget. And
most sources don't report amenities, so treating "not mentioned" as "missing"
cratered every score. This happened once, and it's now invariant U, with
regression tests.

### Blending with likes

**Decision:** `score = α · distance_to_preferences + (1 − α) · distance_to_nearest_like`,
with α = 0.6 by default and three UI presets.

**Why:** "near any liked apartment" (the nearest seed) captures multi-modal taste,
such as liking both a Williamsburg loft and a Park Slope brownstone. One knob, α,
is easy to explain and expose.

**Alternative:** *averaging all liked listings into one centroid.* Two different
likes average out to a point that matches neither.

### Weight learning that can't hurt

**Decision:** with five or more likes, give more weight to the features where the
likes agree (low spread) and less to those where they scatter. Blend the result
50/50 with the hand-tuned defaults, apply it to the soft score only, and store it
in an inspectable JSON file.

**Why:** a handful of likes is too little data for anything bigger. This method is
deterministic, explainable, and bounded, and it can't escape the gate.

**Alternatives:**
- *Logistic regression on like/hide.* It needs far more labels and can over-fit
  badly with five examples.
- *No learning.* This would leave useful signal on the table.

### Brute-force search instead of a spatial index

**Decision:** compute the distance to every candidate (a linear scan) using the
haversine formula. Plain Python, no numpy.

**Why:** a few thousand listings rank in milliseconds. A KD-tree, ball tree or
PostGIS would add dependencies and complexity for no visible gain. This is worth
revisiting at about 10⁵ listings.

### Neighborhoods as areas, not points

**Decision:** location distance is measured to a neighborhood's *edge*, so a
listing inside the area scores 0. Areas are precise NTA polygons when the optional
`neighborhoods.json` is built, and sized disks otherwise.

**Why:** distance to a centroid penalized apartments on the far side of the very
neighborhood the user asked for. The disk fallback keeps the app working out of the
box, without a build step.

---

## 4. Data

### Sources: a licensed API and open data, no scraping

**Decision:** RentCast (a licensed listings API) for market-rate inventory, and
NYC Housing Connect (Socrata open data) for affordable lotteries. Earlier
StreetEasy and RentHop scrapers were **removed**.

**Why:** scraping those sites violates their terms, breaks whenever their markup
changes, and isn't something to publish. Links to listing sites stay outbound
links; the app doesn't copy their content or photos.

**Trade-off accepted:** fewer listings, and no photos, compared with scraping.
Housing Connect has no rent field, so affordable rents are *estimated* from income
(AMI) bands and labeled "est." everywhere.

### Verification against NYC building records

**Decision:** resolve each listing's address with NYC GeoSearch, then check PLUTO
(is it residential, and how many units?) and HPD (is it registered?). Record a
verdict per listing, cache it for 30 days, verify at most 8 per fetch, and let the
UI verify more on demand.

**Why:** listings can be phantom, mis-located or non-residential. Official building
records are free, authoritative and terms-of-service-clean. A lookup that fails
because the request budget ran out leaves the listing *unverified*, never
*suspect*. This was a bug fixed during review.

### One SQLite file instead of JSON files or a database server

**Decision:** listings, feedback and the quota ledger share one SQLite file. The
queryable fields are columns, and the full record is a JSON column. Writes are
deltas: only changed rows, with counters incremented in place.

**Why:** it needs no install or server, and it gives atomic writes plus safe
concurrent access from the CLI and the web app. The original JSON files were
rewritten whole on every save, so two processes could silently lose each other's
likes. The JSON-column layout ports directly to Postgres JSONB if the app ever
becomes multi-user.

**Alternatives:**
- *JSON files.* These were the original design; rewrites weren't atomic and
  concurrent saves lost data.
- *Postgres or another server database.* This would add an install step for a
  single-user tool.
- *An ORM.* Unnecessary at a few tables.

### Identity by normalized address

**Decision:** a listing is identified by its normalized address, not by the
provider's id.

**Why:** the same unit can appear under different ids from different sources or
fetches. Keying by address merges them, keeps a single price history, and lets
"↓ price drop" and "NEW" mean something.

### The store remembers the market

**Decision:** fetches merge into the store instead of replacing it. Listings that
haven't been seen for 7 days become inactive but are kept as market reference.

**Why:** no single fetch shows the whole market. Accumulating fetches gives price
history, "what's new since your last visit", and better results the more the app
is used.

---

## 5. Networking and limits

### One guarded HTTP session for everything

**Decision:** all outbound requests go through `SafeSession`, which provides:
- a per-domain rate limit (about one request every 4 seconds, with jitter)
- a hard per-run request budget (25)
- a 12-hour disk cache (with atomic writes)
- exponential backoff that honors `Retry-After`
- timeouts on every request

**Why:** the free tiers and public APIs are shared resources. Centralizing these
guards means a new provider gets them automatically and can't forget them.

### Quota ledger and fetch coverage

**Decision:** a monthly ledger counts billable RentCast calls and stops at 45 of
the 50 free calls, leaving headroom for miscounts. A fetch *manifest* (append-only
JSONL) records which preferences each fetch covered, so a repeat search that's
already covered skips the network.

**Why:** the per-run budget protects a single run; the ledger protects the month.
Coverage tracking makes "search again" free, and the cache alone can't do that
because a new query has a new cache key.

**Alternative:** *rely on the provider returning HTTP 429.* By the time that
happens, the free tier is already spent.

### Offline commute estimates

**Decision:** estimate commutes from a curated table of about 105 subway and PATH
stations: walk in, wait, ride at an effective speed, a transfer penalty, then walk
out. Every estimate links to live Apple or Google Maps directions.

**Why:** a routing API would cost money or quota for every card and every saved
place, and would make the app depend on the network just to render. Estimates are
good enough to *compare* apartments, and the app labels them as estimates.

**Alternative:** *the Google Directions API or a GTFS router (OpenTripPlanner).*
More accurate, but paid or heavyweight for this purpose.

---

## 6. Web app

### Standard-library HTTP server bound to 127.0.0.1

**Decision:** use `http.server.ThreadingHTTPServer` with a declarative route table,
strict request validation (body size limit, JSON object bodies, typed parameters)
and real 4xx/5xx status codes. It listens only on localhost, and each kind of write
is serialized with a lock.

**Why:** there are no dependencies, nothing to configure, and nothing exposed to the
network. The app serves one user on one machine.

**Alternatives:**
- *Flask or FastAPI.* Pleasant, but extra dependencies for about a dozen routes.
- *Electron or a native app.* Far heavier to build and ship.

### A single HTML file with vanilla JavaScript

**Decision:** the UI is one self-contained `web.html` (inline CSS and JavaScript,
no build step), plus a static `about.html`. Leaflet (for the map) loads lazily from
a CDN the first time the map opens.

**Why:**
- It installs with `pip`, with no Node toolchain, bundler or `node_modules`.
- The UI follows a simple model: one `state` object goes in, a `render()` call
  produces the page, and every action is an API call followed by a refresh. The
  server owns all real state.
- The list view makes no requests to other hosts.

**Robustness rules that came out of the review:**
- All untrusted text is escaped before it reaches `innerHTML`, which closes an XSS
  hole.
- Links are http(s) only.
- When requests overlap, only the newest response is applied.
- API failures become toasts instead of uncaught errors.

**Alternatives:**
- *React, Vue or Svelte with a bundler.* Better ergonomics at scale, but a build
  step and toolchain for a single-page tool.
- *Server-rendered templates (Jinja).* Requires a dependency, and the UI is
  interaction-heavy anyway.

### Warm, light "reading surface" design

**Decision:** a paper-like light palette, serif headings, one terracotta accent,
and cards that put the decision-relevant facts (price, match %, commute, the main
tradeoff) first. Details open in a dialog so the grid never reflows.

**Why:** apartment hunting is reading and comparing, not monitoring a dashboard.

---

## 7. Security and privacy

### API keys in the OS credential store

**Decision:** keys are stored with the `keyring` library, which uses the macOS
Keychain, Windows Credential Manager, or Linux Secret Service. The lookup order is
an explicit flag, then the environment variable, then the credential store.
Machines without a store (headless servers, CI) use the environment variable, and
the app says so clearly.

**Why:** a plaintext key file, even with 0600 permissions, can be read by any
process running as the user and gets swept into backups. The OS store encrypts the
key at rest and controls which apps may read it. The key is never echoed back to
the UI, and it is redacted from `--debug` reports.

**Alternatives:**
- *A 0600 file.* This was the original design; see above.
- *Environment variables only.* Hostile for non-technical users and easy to leak
  through shell history.
- *An encrypted file with a master password.* Reinvents the OS keychain with worse
  UX.

### Local-only by construction

There are no accounts, no telemetry and no server component. The only outbound
traffic is the documented data sources, all through `SafeSession`.

---

## 8. Quality

### Spec-driven tests

**Decision:** [SPEC.md](SPEC.md) lists every user-visible behavior with an ID
(CLI-xx, WEB-xx), and each ID is verified by a test that cites it. The suite has
four layers:
- **Pure-logic unit tests:** KNN, gate, learning, transit, verification.
- **Integration tests:** `SafeSession` against a live local HTTP server, provider
  parsers on realistic fixtures, and the CLI as a subprocess.
- **API tests:** the JSON API against a live server thread.
- **Browser end-to-end tests:** the real UI in headless Chromium via Playwright.

**Isolation rules:** tests never touch the real network (non-local requests are
blocked, and live seams are stubbed), the real `~/.neighbors`, or the real keychain
(an in-memory credential store stands in).

**Why:** the browser tests found real bugs that API tests couldn't have, such as a
CSS rule that kept a hidden button visible. The isolation rules exist because a
test once read a developer's saved key and made a live API call.

**Alternatives:**
- *API-only tests.* Fast, but blind to the JavaScript layer, which is where the
  XSS and UI-state bugs were.
- *Selenium.* Slower and flakier than Playwright's auto-waiting assertions.
- *Snapshot (screenshot) testing.* Brittle against intentional design changes.

### Packaging and running

**Decision:** a standard `pyproject.toml` with a `neighbors` console script. The
runtime dependencies are `requests` and `keyring`. There are exactly two ways to
run it: `neighbors` (terminal) and `neighbors web` (browser).

**Why:** earlier per-OS launchers and a Dockerfile multiplied the paths to maintain
and document without adding much. One install command and two run commands are
easy to explain and to test (CLI-01 to CLI-09).

---

## 9. Known limitations and next steps

- **Coverage:** two listing sources means a thinner market than the big sites.
  Another licensed feed would plug in as a `Provider` subclass.
- **Scale:** linear-scan ranking and per-request disk reads are right for one
  user. A hosted multi-user version would move to Postgres (JSONB) with a spatial
  index and per-user state.
- **Commutes:** they're estimates by design. A GTFS-based router would be the
  upgrade if accuracy ever mattered more than independence from the network.
- **Learning:** it's deliberately conservative. With much more feedback, a small
  pairwise ranking model could replace the spread heuristic. It would still apply
  to the soft score only, behind the gate.
