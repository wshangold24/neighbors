# How neighbors matches apartments

This document explains the method behind the app — how a pile of scraped and API-fetched listings becomes a ranked list of apartments you'd actually want. It assumes no background in search or machine learning; concepts are introduced as they're needed.

## 1. The problem with filters

Every listing site works the same way: you set filters (max $3,500, 1 bed, Williamsburg) and see only listings that pass *all* of them. Filters are binary — a $3,510 apartment is invisible, a great place one neighborhood over is invisible. But your actual preferences aren't binary; they're a *center of gravity* with soft edges. The "similar listings" module on those sites often feels better than the search itself precisely because it ranks by closeness instead of filtering by cutoffs.

neighbors is built on that observation. Instead of asking "does this listing pass every test?", it asks "**how far is this listing from what you want?**" and sorts by the answer.

## 2. The pipeline at a glance

```
providers (RentCast API, NYC Housing Connect)
        │  rate-limited, cached, budgeted HTTP
        ▼
   validation          reject garbage (impossible price, non-NYC coords)
        ▼
   listing store       merge into persistent memory across runs
        ▼
   verification        confirm a real registered residential dwelling
        │               (GeoSearch → PLUTO → HPD, NYC Open Data)
        ▼
   hard gate           discard anything outside your acceptable region
        ▼
   KNN ranking         sort survivors by weighted distance to your ask
        ▼
   feedback loop       likes/hides reorder future runs & teach weights
```

Each stage is a separate module (`providers/`, `models.py`, `store.py`, `engine.py`, `knn.py`, `feedback.py`) that does one job and passes plain data to the next — the CLI is only a thin shell around them.

## 3. Turning an apartment into numbers: feature vectors

> **Concept: feature vector.** To compare things mathematically, you describe each one as a list of numbers — a point in space. A 2-bed, $3,200, 700 ft² apartment in Williamsburg becomes something like (3200, 2, 1, 40.708, −73.957, 700, {dishwasher, laundry}). Once everything is a point, "similar" has a precise meaning: *close together in that space*.

neighbors uses six features: price, bedrooms, bathrooms, location (latitude/longitude), square footage, and amenities. Your preferences become a point in the same space (your neighborhoods are converted to a geographic center; your budget becomes the price coordinate), so listings and preferences are directly comparable.

**Normalization — why raw numbers can't be compared.** Price differences are in the thousands; bedroom differences are single digits. If you naively summed differences, price would drown out everything. So each feature's difference is first rescaled to roughly 0-to-1, where 0 means "identical" and 1 means "about as different as reasonable candidates get." A $1,000 price gap and a cross-borough location gap end up numerically comparable, and the *weights* (next section) then express how much each should matter.

Two features need special handling:

- **Location** uses the *haversine formula* — the math for distance between two latitude/longitude points on a sphere. (You can't just subtract coordinates: a degree of longitude is ~85 km in NYC but ~111 km at the equator; haversine accounts for the Earth's curvature and gives true kilometers.) The result is scaled so ~8 km ≈ "different part of the city."
- **Amenities** aren't numbers, so distance is *overlap*: the fraction of your must-have amenities the listing has. All of them → 0 distance; none → 1.

## 4. Distance, weights, and KNN

*(For the exact per-feature formulas, how every preference maps into the gate and
the soft score, and the design invariants that keep scoring bugs from slipping in,
see **`SCORING.md`** — this section is the intuition, that doc is the spec.)*

> **Concept: weighted Euclidean distance.** Euclidean distance is the straight-line distance you learned in geometry, generalized to any number of dimensions: square each per-feature difference, sum, take the square root. *Weighted* means each squared difference is multiplied by an importance factor first. Squaring has a useful side effect: one very wrong feature hurts more than several slightly wrong ones — a listing that's perfect except in Inwood should rank below one that's mildly off everywhere.

```
distance(you, listing) = √( w_price·d_price² + w_beds·d_beds² + … )
```

Default weights (in `knn.py`): price 3.0, beds 2.5, location 2.0, amenities 1.5, baths 1.0, sqft 0.8 — price and bedrooms dominate, square footage is a tiebreaker. Milestone 5 (section 8) can adjust these from your feedback.

> **Concept: KNN (k-nearest neighbors).** Given a query point, find the k points closest to it. That's the whole algorithm. Here the query is your preferences (or a seed listing), the points are apartments, and k is how many results you asked for. With a few thousand listings, comparing against every one (a "linear scan") takes milliseconds — no clever indexing needed.

**Directionality — the subtle part.** Distance is normally symmetric: $300 under target and $300 over are equally "far." But preferences aren't targets, they're *limits*, and limits are one-sided. So preference queries use **directional distance**: any price within budget costs nothing (only over-max is penalized, ~2.5× hard; under a floor, mildly), extra bathrooms are free, extra square feet above your minimum are free. Only *shortfalls* count. Bedrooms are asymmetric differently: fewer than wanted hurts a lot, extra hurts a little (a 3-bed when you wanted 2 is livable; the reverse isn't).

Seed queries (`--like 3`, "find things like this one") deliberately stay **symmetric**: an apartment at half the seed's price is *not* similar to it — it's a different kind of apartment. Getting this right required distinguishing "close to what I want" from "similar to this thing"; they sound alike but have different math.

**The match score.** Raw distances (0.3? 1.7?) mean nothing to a human, so they're mapped to 0–100% via exponential decay: `100·e^(−d/1.4)`. Exponential rather than linear because near the top differences matter a lot (distance 0 → 100%, 0.5 → 70%) while far away they don't (3.0 vs 3.5 are both "bad"; ~12% vs ~8%). Each result also shows which feature it's *furthest* on — the score's biggest contributor — so a "72% — furthest on: location" tells you exactly what the tradeoff is.

## 5. Memory across runs: the store

A single fetch is a snapshot; apartment hunting is a movie. The **listing store** (`store.py`) makes runs cumulative:

- Every fetch *merges* rather than overwrites. Listings are identified by normalized address — not provider ID — so the same unit found on RentCast and Housing Connect becomes one record with both source links. A re-fetched apartment is **updated in place, never duplicated**.
- Each record keeps `first_seen`, `last_seen`, and a **price history**. Rankings display `NEW` for first-seen-today, `↓$150` for price drops (a drop on a listing you liked is the strongest signal in this domain — the landlord is getting nervous), and staleness for listings not seen in days.
- Listings unseen for 14 days are treated as probably-rented: excluded from ranking, kept for history.

**Storage.** The store lives in a single **SQLite** database (`store.db`), using Python's stdlib `sqlite3` — no server, no install. The schema is lightly *denormalized*: one row per apartment, with the queryable fields (id, price, beds, coordinates, last_seen) as columns and the full record kept in a JSON `data` column. This keeps writes simple (one upsert per apartment, keyed by the normalized-address `PRIMARY KEY`), matches the record shape the rest of the app already passes around, and ports cleanly to Postgres later (the JSON column becomes `JSONB`). Because all persistence goes through the one `Store` class, swapping backends never touches the engine or the web layer. A legacy `store.json` from an earlier version is imported automatically the first time the DB is opened.

## 6. Knowing what you already know: the manifest

Every fetch is recorded in a **provenance manifest** — which provider, when, how many listings, and crucially *which preferences* drove it (as a snapshot plus a fingerprint hash of the fetch-relevant fields).

This enables **coverage reasoning**: before spending a network request, the app checks whether a recent fetch already *covers* the current question. Coverage is a superset check — a fetch for "$0–4,000 anywhere in Brooklyn" contains everything a fetch for "$0–3,500 in Williamsburg" would return, so the narrower query can be answered from the store for free. The rules: the old price window must contain the new one on both ends, and the old area must contain every new neighborhood (a city-wide fetch covers anything). Only the fields that change *what gets fetched* are fingerprinted — changing k or weights doesn't invalidate coverage, because it doesn't change the data.

Layered under this, a **monthly quota ledger** counts billable API calls per provider per calendar month (cache hits are free) and refuses RentCast fetches at 45/50 — so iterating several times a week can never blow the free tier. `neighbors engine --plan` shows the fetch/skip decision per provider, with reasons, before anything is spent.

## 7. The engine: iterating without drifting

The `engine` command turns one-shot search into a session: rank, then `like N` / `hide N` / `open N`, re-rank, repeat. Feedback persists forever. The danger with feedback-driven similarity is *drift* — like a slightly-expensive place, get shown expensive places, like one of those… and three iterations later everything is $800 over budget. The engine prevents this structurally with **two-stage ranking**:

> **Concept: constraints vs. objectives.** A *constraint* must hold (budget ≤ $3,500); an *objective* is optimized (as close to ideal as possible). Mixing them into one score is how drift happens — enough similarity can then "buy off" a constraint violation. Keeping them separate makes violation impossible rather than merely unlikely.

**Stage 1 — the hard gate (constraints).** A declarative rule list derived from your preferences: price under cap+stretch, at least your bedroom count, within ~8 km of your neighborhoods, must-have amenities present (with a twist: listings that report *no* amenity data aren't excluded, because providers often omit amenity data — unknown ≠ absent). Anything failing any rule is discarded before scoring. No amount of similarity to a liked listing can resurrect it.

**Stage 2 — the soft score (objectives), only inside the gate:**

```
score = α · distance_to_preferences + (1−α) · min(distance_to_each_liked_listing)
```

Your likes act as extra magnets pulling similar apartments up the ranking — but only apartments that already passed the gate. `α` (default 0.6, `--alpha`) is the drift knob: 1.0 ignores likes entirely; lower values lean harder on them. Hidden listings and already-liked listings are excluded from results (you know about them), and `min` over seeds means matching *any* liked apartment closely is enough — your likes may legitimately describe two different "types" of place.

So iteration widens **coverage** (the store accumulates more of the market) while the gate keeps **criteria** fixed. That's the whole drift story.

## 8. Verification and the three-tier amenity model

Two related honesty problems: is a listing *real*, and what does it actually *have*? Both are answered by cross-referencing NYC's authoritative open data, and both are careful to distinguish **fact** from **guess**.

**Verification — is this a real registered residential dwelling?** After a fetch, each listing is resolved against NYC's building registries: its address → NYC GeoSearch (returns the official BBL + BIN) → PLUTO (residential units, building class) → HPD registrations (is the building registered, as required for 3+ unit rentals?). The verdict — *verified rental*, *not a residential building*, *resolves elsewhere*, or *no building record* — is cached per listing and drives a trust badge. It also gently reorders results (verified listings float up; flagged ones sink) via a sort-only penalty that never changes a listing's match %, with an off/gentle/strong strength you control.

**The amenity model — three tiers, never conflated.** Rental sources publish surprisingly little structured amenity data (RentCast's listings API returns essentially none), so a listing arriving with an empty amenity list is the norm. The subtlety is that *unknown is not the same as absent*, and the code encodes three distinct confidence levels:

- **Reported** (`amenities`) — what the source stated. This is the *complete* set, so it's what the hard gate reasons about: if a source lists amenities and a must-have isn't among them, the listing is genuinely missing it. If the source lists *nothing*, the gate treats it as unknown and keeps the listing (unknown ≠ absent), and the soft score applies only a mild penalty rather than scoring it as "missing everything" — otherwise every data-less listing would be cratered identically.
- **Derived** (`derived_amenities`) — high-confidence **facts** inferred from authoritative records: `elevator` from PLUTO building class D; `central_air` / `parking` from a RentCast property-record lookup (on demand, one API call). These credit the soft score and are shown with a ✓, but are kept *separate* from reported amenities so they can never flip the gate into strict mode and exclude a listing for lacking some *other* amenity we have no data on.
- **Inferred** (`inferred_amenities`) — low-confidence **guesses** from a building's size and age (a 20-floor tower *probably* has a doorman; a 2015 building *probably* has in-unit laundry). These are advisory only: surfaced in the UI as "likely (unconfirmed)", and deliberately excluded from both the gate and the score.

The practical upshot: what the app *asks* you to prefer is aligned with what it can actually *know*. The preferences UI marks the amenities it can confirm from data, and everything richer than that would require a licensed listing feed.

## 9. Learning your weights

With enough likes (≥ 5), the app can learn what actually matters to you. The idea needs no ML machinery:

> **Intuition:** look at your liked apartments *through the lens of each feature*. If every like is within a few blocks of each other but their prices are scattered across your whole budget, your behavior is telling you location matters and price (within budget) doesn't. Formally: compute each like's per-feature distance from your preferences; features where likes **agree** (low spread, measured by standard deviation) get more weight, features where they scatter get less.

The learned weights are rescaled so total weight mass matches the defaults (keeping distances comparable), then blended 50/50 with the hand-tuned defaults for stability. Guardrails: learning never touches the hard gate (it cannot cause drift), it's inspectable (`neighbors weights` shows each learned weight against its default), and it's disposable (`--reset-weights`).

## 10. Why each piece is where it is

The architecture follows one rule: **pure logic in modules, side effects at the edge**. `knn.py`, `engine.py`, `manifest.py`, `learn.py` are pure functions over plain data — same inputs, same outputs, no printing, no file I/O — which is what makes the extensive test suite possible. `store.py`, `feedback.py`, `quota.py` own exactly one file each. All network traffic goes through one `SafeSession` (rate limiting, caching, budgets, request history). `cli.py` is the only place that prints, prompts, or decides what to do — the imperative shell around a functional core. Adding a provider, a feature to the distance function, or a gate rule each means touching exactly one place.
