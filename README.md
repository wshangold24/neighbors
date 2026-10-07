# Neighbors

**NYC rental search that ranks by similarity instead of filtering.**

Listing sites hide a $3,550 apartment when your cap is $3,500, and a great place in
Greenpoint when you searched Williamsburg. Neighbors turns your preferences and every
listing into points in a feature space (price, beds, baths, location, size, amenities).
It then ranks by **weighted k-nearest-neighbors**, so near-misses show up in order of
how close they are.

- **Similarity ranking.** Each listing gets a 0–100 match score and shows the one
  tradeoff you'd be making.
- **Learns from you.** Liking and hiding listings reshapes future rankings. After five
  likes, feature weights are learned from your choices. Hard bounds (budget, beds,
  area) are never crossed.
- **Verified listings.** Every address is checked against NYC's building registries
  (GeoSearch → PLUTO → HPD) to confirm it's a real, registered residential building.
- **Commute estimates.** Offline subway and PATH routing to the places you care about
  (work, friends), with a step-by-step itinerary.
- **Polite networking.** All traffic goes through one rate-limited, cached,
  budget-capped HTTP session, and a monthly quota ledger protects the free API tier.
- **Few dependencies.** Python standard library plus `requests`. The web UI is a single
  HTML file served by `http.server`.

## Setup

**1. Install** (requires Python 3.9+):

```bash
git clone https://github.com/wshangold24/neighbors.git
cd neighbors
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

**2. Run it.** There are two ways to use Neighbors:

```bash
neighbors        # terminal: asks your preferences once, then prints ranked rentals
neighbors web    # full web app at http://127.0.0.1:8787 (opens your browser)
```

That's all you need. Affordable-housing listings from NYC Housing Connect work with
no key.

**3. Optional: add a RentCast key** to include market-rate listings. Get a free key at
[rentcast.io/api](https://www.rentcast.io/api), then either paste it into the web app
(the first-run banner, or **Preferences → Advanced**) or save it from the terminal:

```bash
neighbors --rentcast-key YOUR_KEY
```

The key is stored in your operating system's credential store, never in a plain file
and never in this repo:

| OS | Where the key lives | How to see or delete it |
|---|---|---|
| macOS | Keychain | Keychain Access app → search "neighbors" |
| Windows | Credential Manager | Control Panel → Credential Manager → Windows Credentials |
| Linux | Secret Service (GNOME Keyring / KWallet) | Seahorse ("Passwords and Keys") or KWalletManager |

To remove it, run `neighbors --rentcast-key ""` or clear the field in Preferences. On
a machine without a credential store (a headless server or CI), set the environment
variable instead: `export RENTCAST_API_KEY=YOUR_KEY`. When both are present, the
environment variable wins.

**4. Optional: add an NYC Open Data app token.** A free token from
[data.cityofnewyork.us](https://data.cityofnewyork.us) (Developer Settings → App Tokens)
raises the open-data rate limit, which makes building verification more reliable. Store
it the same way:

```bash
python -m keyring set neighbors nyc-app-token    # prompts for the token
```

Or set `NYC_APP_TOKEN` in your environment.

## Using the terminal CLI

```bash
neighbors                 # fetch → merge into your local store → rank → print
neighbors --cached        # re-rank what's already stored, with zero network
neighbors --like 3        # "more like result #3"
neighbors --prefs         # change saved preferences without searching
neighbors engine          # interactive loop: like N / hide N / open N / r / q
```

Run `neighbors --help` for the rest: quota and weight inspection, `--debug` reports,
and `--sources`.

## Using the web app

`neighbors web` (or `neighbors web --port 9000 --no-browser`) serves a local
single-page app:

- browse ranked cards, then ♥ like, ✕ hide, or find **≈ similar** apartments, with breadcrumbs
- compare your liked apartments side by side
- see a map view with box or freehand area search
- edit preferences, including neighborhoods to want or avoid and important places with commutes
- fetch, refresh, and verify listings from the **Update** menu
- read **About** (`/about`) for why the app exists and what it does
- switch between light and dark themes with the sun/moon button in the header

The CLI and the web app share the same state in `~/.neighbors/`, so you can use both
interchangeably. For a full tour, see [docs/WEB_GUIDE.md](docs/WEB_GUIDE.md).

## Data sources

| Source | What it provides | Access |
|---|---|---|
| [RentCast API](https://developers.rentcast.io/) | Active market-rate listings | Free key (50 requests/month, and the app stops at 45) |
| [NYC Housing Connect](https://data.cityofnewyork.us/d/vy5i-a666) | Affordable-housing lotteries (rents are AMI-based estimates) | Open data, no key |
| [GeoSearch](https://geosearch.planninglabs.nyc), [PLUTO](https://data.cityofnewyork.us/d/64uk-42ks), [HPD](https://data.cityofnewyork.us/d/tesw-yqqr) | Building verification (not listings) | Open data, no key |

Both listing sources are terms-of-service-clean: a licensed API and public open data,
with no scraping.

## How the ranking works

1. **Hard gate.** Listings outside your bounds (price cap plus stretch, minimum beds,
   avoided neighborhoods) are dropped. Similarity can never pull them back.
2. **Soft score.** The survivors are ranked by weighted distance to your preferences.
   Distance is directional: being under budget is free, and only shortfalls count.
   That distance is blended with the distance to the apartments you've liked.
3. **Learning.** With five or more likes, the feature weights adapt to what your
   likes have in common.

- [docs/METHOD.md](docs/METHOD.md) explains the approach from first principles.
- [docs/SCORING.md](docs/SCORING.md) is the exact scoring spec and its design invariants.
- [docs/DESIGN.md](docs/DESIGN.md) records the technical and system-design decisions, with
  the alternatives considered.

## Testing

The functional behavior is specified in [docs/SPEC.md](docs/SPEC.md). Every
requirement ID there is verified by a test that cites it.

```bash
pip install -e ".[dev]"
playwright install chromium   # one-time, for the browser tests
pytest                        # full suite
pytest --ignore=tests/e2e     # skip the browser tests
```

The suite covers the KNN math, the gate, and learning; the HTTP safety layer against a
live local server; provider parsers; the CLI end to end; the JSON API; and the web UI in
headless Chromium. Tests never touch the real network, your `~/.neighbors` data, or your
real keychain: an in-memory credential store stands in for it.

To watch the browser tests click through the UI:

```bash
pytest tests/e2e --headed --slowmo 400                 # all UI tests, visibly
pytest tests/e2e -k similar --headed --slowmo 800      # just the ones matching "similar"
PWDEBUG=1 pytest tests/e2e -k test_details_dialog      # step through in the Playwright Inspector
```

## Project layout

```
neighbors/
  cli.py               terminal entry point (the only module that prompts/prints)
  web.py               local web app: stdlib HTTP server, routing, validation
  webapi.py            web app actions (results, feedback, prefs, fetch, verify)
  views.py             pure listing → JSON view builders (trust, deltas, commutes)
  web.html             the single-page UI
  about.html           the About page: why Neighbors exists
  models.py            Listing / Preferences, validation, neighborhood geometry
  knn.py               feature space, weighted distance, KNN, match %
  engine.py            hard gate, two-stage ranking, fetch planning
  learn.py             weight learning from likes
  transit.py           offline subway/PATH commute estimates
  store.py             SQLite listing store: merge, price history, flags
  feedback.py          like/hide verdicts
  quota.py             monthly API quota ledger
  manifest.py          fetch provenance and coverage
  http.py              SafeSession: rate limiting, caching, request budget
  verify.py            building verification (GeoSearch → PLUTO → HPD)
  digest.py            "since your last visit" summary
  debug.py             --debug diagnostic reports (secrets redacted)
  keystore.py          API keys in the OS credential store (Keychain, etc.)
  providers/           rentcast, housingconnect, socrata client, demo (test-only)
scripts/
  build_neighborhoods.py   optional: fetch precise NTA boundary polygons
tests/                     unit, integration, CLI, API, and browser (e2e/) tests
docs/                      SPEC, DESIGN, METHOD, SCORING, WEB_GUIDE
```

Your local data (preferences, the SQLite store, and the HTTP cache) lives in
`~/.neighbors/`, never in the repo. API keys live in your OS credential store (see
[Setup](#setup)).

## License

[MIT](LICENSE)
