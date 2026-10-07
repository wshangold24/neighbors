# Web app guide

## Launch

Requires **Python 3.9+** (like the rest of the app — `run.py` checks and tells you if not).

```bash
python run.py web            # starts at http://127.0.0.1:8787 and opens your browser
python run.py web --port 9000
```

Stop with Ctrl-C. Everything the web app shows and saves lives in the same `~/.aptfinder/` files the CLI uses — state is re-read from disk on every request, so you can use the web app and the CLI interchangeably, even at the same time, without restarting either.

## The screen

**Header** — live stats: active vs. total known listings, how many you've liked, whether learned weights are on, and your RentCast quota for the month (`4/50`). The **ranking mode buttons** set how results are ordered: *My criteria* (pure preferences), *Balanced* (the default), or *Like my likes* (leans on apartments you've hearted). Whatever the mode, likes can never pull results outside your budget/beds/area bounds. Under **advanced** there's a slider for fine-tuning the same blend (`--alpha` in the CLI). Switching modes re-ranks instantly — no network cost.

Once you have likes, each card grows a small **prefs ↔ likes bar** showing what that card's ranking is based on: mostly blue means it's there because it fits your stated criteria; mostly green means it's there because it resembles something you liked.

**Cards** — one per apartment, best match first: match %, price with **↓$ price-drop** and **NEW** badges, facts, neighborhood, the feature it's *furthest* from your ask on (the orange chip — this is the tradeoff you'd be making), price history when it has changed, and a "shown ×N" chip when you've seen it in previous sessions.

**Buttons on each card:**

| Button | What it does |
|---|---|
| ♥ like | Marks it liked. Similar apartments rise in future rankings (never beyond your preference bounds), and with 5+ likes the feature weights start learning what you care about. Click again to clear. |
| ✕ hide | Never shows it again, anywhere. |
| ≈ similar | Seed mode — the "related listings" view, ranked by symmetric similarity to *this* apartment. This is the click-a-real-apartment flow: find one you like anywhere, locate its card, hit ≈. Chaining ≈ repeatedly builds a **breadcrumb trail** at the top (`search ▸ apt A ▸ apt B`) — click any crumb to jump back to that apartment's similar view, or `search` to reset. |
| source links ↗ | Opens the source: for Housing Connect listings, a search for that lottery; for RentCast (no per-listing page), an address search. Photos aren't shown in-app — RentCast's API has none, and copying images off other sites violates their terms — so the links are the honest bridge. |
| realtor ↗ | Looks the address up scoped to realtor.com — one click to the page you'd find by searching manually. Realtor.com's terms prohibit scraping and embedding, so linking out is the compliant integration. |
|  maps / gmaps ↗ | Opens the apartment's address in Apple Maps or Google Maps. |

**Important places & commutes** — in Preferences, add named places (Work, roommate's office, a friend's apartment) with segmented fields: **street / city / zip** — any city works (Jersey City, Hoboken, NYC…; the station table includes the PATH system, so NJ destinations route properly). Click **verify** to resolve the address against a real geocoder (OpenStreetMap); a green ✓ shows the canonical resolved address so you know it's valid *before* saving. Save is strict: unverified rows are auto-verified on save, and if any address can't be located the dialog stays open with the failing row marked — an invalid address can never be saved. Editing a field clears its verification.

Each card's commute shows the **full ordered itinerary with per-step times**: 🚶 walk to the station (which lines), 🚇 the ride (which train, including typical wait), 🔁 any transfer, 🚶 walk out — summing exactly to the total. The **~N min ↗** total opens Apple Maps transit directions for that exact trip; **Google Maps ↗** is alongside.

**Compatibility breakdown** — click any card's match % to expand a clean per-feature breakdown: price, location, beds, baths, sqft, and amenities each shown as a 0–100 fit with a thin bar. The mapping is linear from the ranking math itself (100 = that feature is exactly what you asked for), so the numbers mean something — this is the same data the overall score is computed from, just unrolled. Every card then shows a commute row per place answering two questions: **roughly how long by transit** (`~34 min`) and **which line** (`L from Bedford Av (4 min walk) to Union Sq`, or `→ transfer →` when lines differ). These are *offline estimates* from a built-in subway-station table — good for comparing apartments, not for catching trains — so each row also has **** (Apple Maps) and **G** (Google Maps) links that open exact, live transit directions for that specific trip.

**Amenities (in the details modal)** — chips come in three flavors so you can trust them appropriately: a plain chip is what the listing source reported; a **dashed ✓ chip** is a *fact* confirmed from NYC building/property records (e.g. `elevator` from the building's class); and a dotted **"likely (unconfirmed)"** chip is a *guess* inferred from the building's size and age (e.g. a 20-floor tower likely has a doorman). Guesses never affect filtering or ranking — they're just hints. For RentCast listings, a **"Look up amenities"** button fetches that property's record (central air, parking) — it spends **one** RentCast API call and is quota-checked, so it's opt-in per listing.

**Verification badge** — every card, compare column, and the details modal shows an "is this a real rental?" line. On **Fetch new listings**, each listing is checked against NYC's building registries (address → GeoSearch → PLUTO → HPD): green ✓ *verified rental* means a real, registered residential building near where it's listed and on an official source; amber ⚠ flags a red flag (not a residential building, resolves elsewhere, or gone stale); grey means not yet checked. Affordable Housing Connect units also carry an **"affordable · est."** tag — a reminder their rent is an AMI-based estimate, not an exact figure.

**Preferences** — edits the same saved preferences as the CLI prompt, including "days before an unseen listing counts as gone" (default 7 — NYC listings move in days-to-weeks, so anything our fetches haven't seen in a week is treated as rented and excluded from ranking, but kept in the store as market reference) and **data sources**: checkboxes for which providers "Fetch new listings" uses (none checked = all). The two sources are **rentcast** (market-rate, spends your monthly API quota) and **housingconnect** (NYC affordable lotteries, free/no key) — e.g. untick RentCast to preserve quota and pull only affordable inventory. The **amenities** picker marks with a green ✓ the amenities the app can actually confirm from data (elevator, central air, parking, income-restricted, no-fee); the rest are best-effort — a source rarely reports them, so selecting them **never excludes** a listing, it only nudges ranking. There's also a **"Prioritize verified listings"** control (off / gentle / strong) that sets how hard verification status reorders results.

**Fetch new listings** — runs a live fetch through the exact same safety stack as the CLI: coverage check (a recent fetch that already covers your prefs is skipped for free), monthly quota auto-stop at 45/50, rate limiting, caching, then building verification. The toast tells you what came back: fetched / new / price drops / verified.

## Web vs CLI — when to use which

| Task | Use |
|---|---|
| Daily triage: browse, like/hide, open source pages | **Web** — cards and one-click verdicts are what it's for |
| "Find more like this one" | **Web** (≈ similar) |
| Quick check without leaving the terminal | **CLI** `python run.py --cached` |
| See fetch decisions before spending quota | **CLI** `python run.py engine --plan` |
| Sync quota with RentCast's dashboard | **CLI** `python run.py quota --set-used rentcast=4` |
| Inspect/reset learned weights | **CLI** `python run.py weights` / `--reset-weights` |
| Debug a weird run for reporting | **CLI** any command with `--debug` |
| Scripting/automation (cron, scheduled digests) | **CLI** |

Rule of thumb: **the web app is for deciding about apartments; the CLI is for operating the machine.**

## A note on quota accounting

The in-app counter only sees calls made *through this app* — calls made before the ledger existed, or directly (curl, the RentCast dashboard's own test button), aren't counted. If the header number disagrees with [RentCast's dashboard](https://app.rentcast.io/app/api), the dashboard is the truth: sync with `python run.py quota --set-used rentcast=N`. The app stops fetching at 45/50 on purpose, so a miscount can never overrun the free tier.
