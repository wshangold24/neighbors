# aptfinder

NYC rental search that ranks by **similarity (weighted KNN)** instead of hard filters — so near-misses ($3,550 when your cap is $3,500, Greenpoint when you searched Williamsburg) show up ranked by closeness, like a site's "similar listings" module.

## Quickstart (easiest — no terminal needed)

You only need **Python 3.9+** installed ([python.org/downloads](https://www.python.org/downloads/); on Windows check *"Add Python to PATH"*). If you have several Pythons installed, the launcher finds and uses a compatible one automatically — you don't have to activate anything first.

- **macOS:** double-click **`Start aptfinder.command`** in Finder.
- **Windows:** double-click **`run.bat`**.
- **Linux / any terminal:** run `./run.sh`.

The first launch sets up a private environment and installs everything (a few minutes); after that it starts instantly and opens the app in your browser. No API key is required to start — the free NYC Housing Connect source works out of the box (a RentCast key adds market-rate listings; set it in the app or with `--rentcast-key`).

### Or with Docker (nothing else to install)

```bash
docker build -t aptfinder .
docker run -p 8787:8787 -v aptfinder-data:/data aptfinder
# then open http://localhost:8787
```

### Or as a normal Python package

```bash
pip install .          # gives you an `aptfinder` command
aptfinder web          # local browser UI
```

For development, `pip install -e ".[dev]"` then `pytest`.

First run prompts (in the app) for preferences, saved to `~/.aptfinder/`. To change them from the terminal without searching, use `aptfinder --prefs`.

## Commands

Once installed, use the `aptfinder` command; from a source checkout, `python run.py` is equivalent (e.g. `aptfinder web` == `python run.py web`).

```bash
python run.py                      # search (default): fetch → store → rank
python run.py web                  # local browser UI — see WEB_GUIDE.md
python run.py engine               # iterative terminal session: like/hide loop
python run.py engine --plan        # dry run: show fetch decisions, spend nothing
python run.py --cached --like 3    # seed mode: listings similar to result #3
python run.py cache-status         # what's cached, from which prefs, how stale
python run.py quota                # monthly API usage (--set-used to sync)
python run.py weights              # current ranking weights (learned or default)
```

Every fetch merges into a persistent store (`NEW` and `↓$price-drop` flags in results); ranking always runs over all recently-seen listings, so results improve the more you run it. Results show a 0–100 match score and which feature each result is furthest from your ask on. In `engine`, type `like N`, `hide N`, `open N` (browser), `r` (re-rank), `q` — feedback persists and reorders future runs without ever escaping your preference bounds (see `METHOD.md`).

## CLI reference

| Flag | Default | Purpose |
|---|---|---|
| `--sources` | saved preference (or all) | one-run override of the sources preference |
| `--cached` | off | rank the stored listings; zero network |
| `--like REF` | — | seed mode: neighbors of result #N or a listing id |
| `--alpha X` | 0.6 | engine drift knob: 1.0 = ignore likes, lower = lean on them |
| `--plan` | off | with `engine`: print fetch/skip decisions and exit |
| `--rentcast-key KEY` | — | save RentCast API key for future runs |
| `--budget N` | 25 | hard cap on network requests this run |
| `--interval SEC` | 4 | min seconds between requests to the same site |
| `--max-pages N` | 2 | max result pages per provider |
| `--prefs` | off | update saved preferences and exit — no fetch/search |
| `--reset-prefs` | off | re-prompt all preferences, then continue with the search |
| `--reset-weights` | off | with `weights`: delete learned weights |
| `--set-used P=N` | — | with `quota`: sync a provider's monthly count with its dashboard |
| `--port N` | 8787 | with `web`: port for the local web UI |
| `--debug` | off | write paste-able diagnostic report (see Debug mode) |
| `-v` / `--verbose` | off | debug-level logging to console |

## Data sources

Which providers to fetch from is a **saved preference** (toggle in `--prefs` or the web app's Preferences dialog; none selected = all). The `--sources` flag overrides it for a single run:

```bash
python run.py                                   # uses your saved sources preference
python run.py --sources rentcast                # this run only: official API only
python run.py --cached                          # reuse the store, zero network
```

Both live sources are ToS-clean: RentCast's terms permit commercial use, storage, and display of its data, and NYC Open Data is public by law. (The former StreetEasy and RentHop scrapers were **removed** — scraping their pages violates their terms of use.)

### RentCast — market-rate listings (official licensed API)

The primary listings source ([docs](https://developers.rentcast.io/reference/rental-listings-long-term)). Get a key at [rentcast.io/api](https://www.rentcast.io/api).

| | |
|---|---|
| **Data provided** | Active market-rate rental listings: price, beds/baths, sqft, coordinates, property type, listed date. Sourced from MLS/broker feeds. |
| **Value** | The bread-and-butter inventory — real apartments currently for rent citywide. |
| **Freshness** | Listings are refreshed continuously; the provider requests `status=Active` and `daysOld ≤ 60`. |
| **Usage limit** | Free tier = **50 requests/month**. The provider spends **1–2 calls per run** (one 500-listing page, radius-filtered) and a 12h cache makes repeats free. A monthly quota ledger hard-stops at **45/50** so you can't overrun the tier. |
| **Connectivity** | HTTPS, `X-Api-Key` header. Needs a key (below). |
| **Notes** | RentCast has no per-listing web page, so result URLs are address searches; neighborhood is reverse-mapped from coordinates. |

```bash
export RENTCAST_API_KEY=your_key          # env var, or:
python run.py --rentcast-key your_key     # saved to ~/.aptfinder/rentcast_key (chmod 600)
```

### Housing Connect — affordable-lottery inventory (NYC Open Data)

NYC's official affordable-housing lottery inventory, via two Socrata datasets joined on Lottery ID: [`vy5i-a666`](https://data.cityofnewyork.us/d/vy5i-a666) (by lottery) + [`nibs-na6y`](https://data.cityofnewyork.us/d/nibs-na6y) (by building).

| | |
|---|---|
| **Data provided** | Currently-**Active** rental lotteries: building address, coordinates, bedroom distribution, AMI (income) bands, BBL/BIN. |
| **Value** | Unique, genuinely below-market inventory that appears on no market-rate site — real units you can apply for today. |
| **Freshness** | Updated **monthly** (automated), published by HPD. Only lotteries with status `Active` are surfaced. |
| **Usage limit** | Free/open. Shares NYC Open Data's rate pool (see app token below). |
| **Connectivity** | HTTPS Socrata SODA API. **No key required.** |
| **Important caveat** | These datasets have **no rent field** — affordable rents are AMI-based. Each unit's price here is an **estimate** from its dominant AMI band and bedroom size, tagged `income_restricted` and labeled "affordable · est." in the UI. Treat these as lottery opportunities, not fixed-price leases. |

### Listing verification — PLUTO + HPD + GeoSearch (NYC Open Data)

Not a listings source — the **verification layer** that confirms every fetched listing is a *real, registered, residential dwelling* (see `verify.py`). For each listing:

`address → NYC GeoSearch (BBL + BIN) → PLUTO (residential units/class) → HPD registration (is it registered?)`

| Dataset | Provides | Agency · Freshness |
|---|---|---|
| [GeoSearch](https://geosearch.planninglabs.nyc) | Canonical NYC address → BBL, BIN, coordinates | NYC Planning Labs (PAD-based) · ~quarterly |
| PLUTO [`64uk-42ks`](https://data.cityofnewyork.us/d/64uk-42ks) | Residential unit count, building class, land use, year built | Dept. of City Planning · **quarterly** (v26v1) |
| HPD Registrations [`tesw-yqqr`](https://data.cityofnewyork.us/d/tesw-yqqr) | Whether the building is HPD-registered (required for 3+ unit rentals) | HPD · **monthly** |

**Value:** turns "is this address real?" into "is this a registered residential building where it's listed?" — catching phantom, mis-located, or non-residential listings. Bounded to 8 checks per fetch and cached for 30 days to respect rate limits. "Verify all" (in the Update menu) checks the whole pool in batches; each card also has a "verify now" for a single building.

### Amenities: reported, derived, inferred

Rental sources publish little structured amenity data, so the app uses a **three-tier model** that keeps facts and guesses separate (details in `METHOD.md`):

| Tier | Source | Used for | Shown as |
|---|---|---|---|
| **Reported** | the listing source | the hard gate (unknown ≠ absent — no amenity data never excludes a listing) | plain chip |
| **Derived** (facts) | PLUTO class D → `elevator`; RentCast property lookup → `central_air` / `parking` | credits ranking; gate-safe | ✓ chip |
| **Inferred** (guesses) | building floors/age/units → likely `doorman`, `dishwasher`, laundry, etc. | nothing — advisory only | "likely (unconfirmed)" |

The **RentCast property lookup** is on-demand (a "Look up amenities" button on a listing) and spends **one** RentCast API call, quota-checked — so it's opt-in per listing, never run across the pool. The Preferences amenity picker marks with ✓ the amenities the app can actually confirm from data.

`aptfinder/providers/demo.py` generates synthetic listings but is **test-only**: it is not registered as a source and is used exclusively by the test suite.

### NYC app token (recommended, free)

NYC Open Data works **anonymously**, but anonymous requests share a small throttled pool across all users, so under load they can be rate-limited (HTTP 429). Registering a free **app token** moves you to a per-token budget (~1,000 requests/hour) — plenty for this app, and it makes Housing Connect fetches and listing verification reliable rather than occasionally throttled. It is not secret auth (it only identifies your app for rate-limiting), but it noticeably improves reliability.

1. Create a free account at [data.cityofnewyork.us](https://data.cityofnewyork.us) → **Developer Settings** → **App Tokens** → create one.
2. Provide it to aptfinder any of these ways (resolved in this order):

```bash
export NYC_APP_TOKEN=your_token                       # env var, or:
echo "your_token" > ~/.aptfinder/nyc_app_token        # saved token file
```

Without a token the app still runs — it just relies on the shared anonymous pool and may occasionally be throttled on a heavy fetch.

## Rate limiting & request safety

All network traffic goes through one `SafeSession`:

| Guard | Default | Flag |
|---|---|---|
| Min interval per domain (+ jitter) | 4s | `--interval` |
| Hard request budget per run | 25 | `--budget` |
| Disk cache TTL (hits are free) | 12h | — |
| Pages per provider | 2 | `--max-pages` |
| Backoff on 429/5xx, honors Retry-After | yes | — |

## How the matching works

Full explanation with teaching notes in **`METHOD.md`**. Short version: listings and your preferences become points in a six-feature space (price, beds, baths, geo, sqft, amenities); ranking is weighted KNN with directional limits (within-budget is free, only shortfalls count); the `engine` adds two-stage ranking — a hard gate from your preferences that similarity can never escape, then a soft score blending distance-to-prefs with distance-to-liked-seeds (`--alpha`). Fetches are provenance-tracked so covered queries skip the network, and a monthly quota ledger protects the RentCast free tier. With ≥5 likes, feature weights are learned from your feedback (soft score only — learning can't cause drift).

**`COMPATIBILITY.md`** is the precise spec of the scoring: every preference's role in the gate and the soft score, the exact per-feature formulas, and the **design invariants** whose violation causes ranking bugs — read it before changing ranking.

**Precise neighborhood boundaries (optional).** Location compatibility treats each neighborhood as an *area*: a listing inside it scores a perfect location match, off-center or not. Out of the box that area is a disk sized from the neighborhood spacing. For pixel-accurate boundaries, run once (needs network, free — NYC Open Data NTA polygons):

```bash
python scripts/build_neighborhoods.py    # writes aptfinder/neighborhoods.json
```

The app then uses the real polygons and falls back to disks for anything unbuilt — so the file is entirely optional.

`ENGINE_PLAN.md` is the original design doc for these milestones (all now implemented).

## Adding a provider

Subclass `Provider` in `aptfinder/providers/`, implement `fetch()` using `self.session.get()` (rate limiting comes free), register it in `providers/__init__.py`.

## Debug mode

```bash
python run.py --debug            # any command works with --debug
```

Each debug run writes its own report into `~/.aptfinder/debug/` as `run_<timestamp>.json`, with `latest.json` always holding the most recent one (the newest 25 are kept, older reports pruned automatically). Each report is a single paste-able JSON document for reporting problems. It contains the environment and args, your preferences, every HTTP request (URL, status, cache hit/miss, retries, timing, bytes), per-provider outcomes with full tracebacks, validation-drop logs, the top-20 ranked results with per-feature distance breakdowns, and any crash traceback (a crash under `--debug` is captured into the report instead of killing the run, exit code 2). API keys are redacted from the report before it's written.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/
```

144 tests across six suites (`test_web.py` runs the web API against a live local server thread): `test_functionality.py` (KNN semantics, geo math, validation rules, preference persistence), `test_integration.py` (SafeSession against a live local HTTP server — rate limiting, caching, budget, backoff, block handling; provider parsers on realistic fixtures; CLI end-to-end as a subprocess), `test_engine.py` (manifest coverage, store merging/price history, hard gate + drift guarantees, quota ledger, weight learning, engine session), and `test_debug.py` (HTTP history, report contents, secret redaction, crash capture). Tests never hit real websites; synthetic data (`DemoProvider`) is used only here.

## Project layout

Pure logic lives in modules; `cli.py` is the only place that prints or prompts.

```
run.py                          entry point
aptfinder/
  cli.py                        imperative shell: commands, prompts, display
  models.py                     Listing/Preferences, validation, neighborhood coords + areas
  neighborhoods.json            optional NTA boundary polygons (generated; see scripts/)
  knn.py                        feature space, weighted distance, KNN, match scoring
  engine.py                     hard gate + two-stage ranking, fetch planning (pure)
  store.py                      persistent listing store (SQLite): merge, price history, flags
  manifest.py                   fetch provenance + coverage reasoning (pure)
  feedback.py                   persistent like/hide verdicts
  quota.py                      monthly API quota ledger
  learn.py                      weight learning from feedback (pure)
  transit.py                    offline subway commute estimates (pure)
  web.py + web.html             local web UI (stdlib server; see WEB_GUIDE.md)
  http.py                       SafeSession: rate limiting, cache, budget, history
  debug.py                      --debug report assembly (secret redaction)
  verify.py                     listing verification (GeoSearch → PLUTO → HPD)
  providers/
    rentcast.py                 market-rate listings (licensed API, needs key)
    housingconnect.py           affordable-lottery inventory (NYC Open Data)
    socrata.py                  NYC Open Data (SODA) client + app token
    demo.py                     synthetic data — test-only
scripts/build_neighborhoods.py  one-time: fetch NTA boundary polygons (optional)
tests/                          see Testing
COMPATIBILITY.md                the exact scoring spec + design invariants
METHOD.md                       how the matching works, with teaching notes
ENGINE_PLAN.md                  design doc for the engine milestones
```

## Runtime files

```
~/.aptfinder/preferences.json   saved preferences
~/.aptfinder/store.db           SQLite database — listings, like/hide feedback, and the monthly quota ledger, all in one file (legacy store.json/feedback.json/quota.json are imported automatically)
~/.aptfinder/weights.json       learned ranking weights (if enough likes)
~/.aptfinder/cache/             HTTP response cache (12h TTL) + manifest.jsonl
~/.aptfinder/rentcast_key       saved RentCast API key (chmod 600)
~/.aptfinder/nyc_app_token      saved NYC Open Data app token (optional)
~/.aptfinder/debug/             per-run --debug reports (run_<timestamp>.json + latest.json)
```
