# Engine Plan: from one-shot search to an apartment-finding engine

The current app is a single-shot pipeline: fetch → validate → rank → print. This plan describes how to evolve it into an iterative engine that accumulates knowledge across runs, learns what you actually like, and never drifts outside your acceptable range — while staying frugal with API quota.

## The core problem

Running the app several times today has three weaknesses. Each run overwrites `listings.json`, so a great apartment seen Tuesday is forgotten by Thursday. The cache knows *what* it fetched but not *why* — there's no record of which preferences or provider produced an entry, so a later run can't tell whether cached data is reusable for the current question. And seed mode (`--like`) can chase similarity into places that violate your actual constraints, because similarity and acceptability are different things.

The design principle for everything below: **preferences define a hard acceptable region; similarity only reorders candidates inside it.** Iteration should widen *coverage* (more listings seen), never widen *criteria* (unless you explicitly change prefs).

## Milestone 1 — Provenance-aware cache

Replace the anonymous hash-named cache files with a cache **manifest** (`~/.aptfinder/cache/manifest.jsonl`, one JSON line per entry):

```json
{"key": "ab12…", "url": "https://api.rentcast.io/…", "provider": "rentcast",
 "prefs_fingerprint": "d41d8c…", "prefs_snapshot": {"max_price": 3500, "neighborhoods": ["williamsburg"]},
 "fetched_at": "2026-07-03T10:15:00", "listings_extracted": 214, "status": "ok"}
```

The `prefs_fingerprint` is a stable hash of the *fetch-relevant* preference fields (price range, neighborhoods — not k or weights, which don't affect what's fetched). This gives future iterations exactly what you asked for: any run can look at the manifest and know which preferences and which API produced each cache entry.

Then make reuse smarter than exact-URL matching: before fetching, a run checks the manifest for entries whose prefs snapshot **covers** the current prefs (e.g. a cached `price 0–4000, all Brooklyn` fetch covers a `price 0–3500, williamsburg` query — filter locally instead of refetching). This "coverage check" alone will cut most repeat API calls.

Implementation: extend `DiskCache` with a `Manifest` class; `SafeSession.get()` takes optional `provenance=` metadata; add `aptfinder cache-status` command to print what's cached, from which prefs, and how stale it is.

## Milestone 2 — Persistent listing store

Replace overwrite-on-fetch `listings.json` with an append-and-merge store (`~/.aptfinder/store.json`, or SQLite once it grows):

- Each listing keyed by a stable identity (normalized address + unit); providers' IDs mapped onto it, so the same apartment seen on RentCast and Housing Connect merges into one record with both URLs.
- Track `first_seen`, `last_seen`, `price_history` (detect drops — a $150 drop on a liked listing is exactly the signal you want surfaced), and `active` (not seen in N days → probably rented, demoted but kept for history).
- Every fetch **adds** to the store; ranking always runs over all active stored listings. Iterating twice a week builds a picture of the market no single fetch can give — this is the single biggest win for "run a couple times."
- Output gains flags: `NEW` (first seen this run), `↓$150` (price drop), `seen 12d` (staleness).

## Milestone 3 — Feedback and drift-free iteration (the wrapper)

A new `engine` subcommand wrapping the existing pipeline in a session loop:

```
python run.py engine        # fetch-if-stale → rank → interactive review
```

After showing results, it accepts lightweight verdicts: `like 3`, `hide 7`, `open 2` (browser). Verdicts persist in `~/.aptfinder/feedback.json`.

Ranking then becomes two-stage, which is the drift control you asked about:

1. **Hard gate:** candidates must be inside the acceptable region derived from prefs — `price ≤ max*(1+stretch)`, `beds ≥ wanted` (or exact, configurable), within X km of chosen neighborhoods, must-have amenities present. Nothing outside the gate is ever shown, no matter how similar it is to something you liked.
2. **Soft score inside the gate:** `score = α · dist_to_prefs + (1−α) · min(dist_to_liked_seeds)` — liked listings act as KNN seeds, but only to *reorder* the acceptable pool. `α` is the drift knob (default ~0.6); `α=1` ignores likes entirely, and hidden listings (and near-duplicates of them) are excluded.

This directly fixes today's `--like` behavior: seeds reorder within your constraints instead of wandering away from them. Also stop re-showing: results already shown in previous sessions are marked or deprioritized so each iteration surfaces genuinely new candidates.

## Milestone 4 — Quota planning across runs

Iterating multiple times makes per-run budgets insufficient — add a monthly ledger (`~/.aptfinder/quota.json`) counting billable calls per provider per calendar month. RentCast free tier = 50/month, so warn at 40, refuse at 48 (configurable). Before fetching, the engine decides per provider: cache fresh (< TTL) → skip; manifest coverage hit → filter locally; otherwise fetch only if ledger allows. `engine --plan` prints the decision without executing, so you can see cost before spending quota. This makes "run a couple times" safe by construction: the engine, not you, tracks whether a run needs the network at all.

## Milestone 5 — Learned weights (optional, later)

With ~20+ like/hide verdicts, fit per-feature weights instead of hand-tuned `DEFAULT_WEIGHTS`: start with per-feature agreement statistics between liked listings (e.g. likes cluster in 2 neighborhoods → location weight up), later a small logistic regression on feature differences (pure stdlib, no sklearn). Always show the learned weights and allow reset — explainability over cleverness. Guardrail: learned weights only affect the soft score, never the hard gate, so learning can't cause drift either.

## Suggested order & effort

| Step | Depends on | Size | Payoff |
|---|---|---|---|
| M1 cache manifest | — | ~150 loc | provenance + fewer API calls |
| M2 listing store | — | ~200 loc | memory across runs, price history |
| M3 engine + feedback | M2 | ~250 loc | drift-free iteration, the actual "engine" |
| M4 quota ledger | M1 | ~100 loc | safe multi-run cadence |
| M5 learned weights | M3 | ~150 loc | personalization |

M1 and M2 are independent and each immediately useful; M3 is where it starts feeling like an engine. Every milestone keeps the current CLI working unchanged and lands with tests (manifest coverage logic, store merging/price history, hard-gate never violated by seeds, ledger refusal).

## A realistic cadence once built

Morning: `engine` run (fetches only if cache is stale and quota allows, typically 1–2 RentCast calls). Review 10–15 results, `like`/`hide` a few, done in two minutes. Evening or next day: `engine --cached` — re-ranks the store with your updated feedback, zero network. Weekly: check `cache-status` and price-drop flags on liked listings. That's roughly 15–30 RentCast calls a month — inside the free tier — while the store and feedback keep improving every result set.
