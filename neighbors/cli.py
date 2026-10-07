"""The `neighbors` command.

  neighbors [search]        fetch → merge into store → rank → print
  neighbors web             the full web app (see docs/WEB_GUIDE.md)
  neighbors engine          iterative like/hide session
  neighbors cache-status    what's been fetched, from which prefs, how stale
  neighbors quota           monthly API usage per provider
  neighbors weights         current ranking weights (learned or default)

This module is the imperative shell (I/O, prompting, printing); ranking and
fetch planning live in pure modules (engine, knn, manifest, …).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import traceback
from pathlib import Path

from .debug import DebugSession
from .engine import DEFAULT_ALPHA, plan_fetch, rank
from .feedback import Feedback
from .http import BudgetExceeded, SafeSession
from .knn import DEFAULT_WEIGHTS, Query, knn, match_pct
from .learn import MIN_LIKES, learn_weights, load_weights, save_weights
from .manifest import Manifest
from .models import AMENITIES, NEIGHBORHOOD_COORDS, Listing, Preferences
from .providers import PROVIDERS
from .quota import QuotaLedger
from .store import Store

APP_DIR = Path.home() / ".neighbors"
PREFS_FILE = APP_DIR / "preferences.json"
CACHE_DIR = APP_DIR / "cache"
DB_FILE = APP_DIR / "store.db"   # listings, feedback and quota share one SQLite DB
WEIGHTS_FILE = APP_DIR / "weights.json"
MANIFEST_FILE = CACHE_DIR / "manifest.jsonl"

# A covering fetch older than this is considered stale — matches the HTTP
# cache TTL so "coverage skip" and "cache hit" expire together.
COVERAGE_TTL_S = 12 * 3600


# ---------- prompting ----------

def ask(prompt: str, default=None, cast=str):
    suffix = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {prompt}{suffix}: ").strip()
        if not raw:
            if default is not None:
                return default
            continue
        try:
            return cast(raw)
        except ValueError:
            print("    invalid value, try again")


def ask_multi(prompt: str, valid: list[str], current: list[str], none_word: str,
              normalize=lambda t: t) -> list[str]:
    """Validated multi-select. These values feed provider API calls (area
    slugs, amenity filters), so anything not in `valid` is rejected and the
    user is re-asked — a typo must never become a silent bad query."""
    valid_set = set(valid)
    while True:
        raw = ask(prompt, ", ".join(current) or none_word)
        if raw.strip().lower() == none_word:
            return []
        chosen = [normalize(t.strip().lower()) for t in raw.split(",") if t.strip()]
        unknown = [c for c in chosen if c not in valid_set]
        if not unknown:
            return chosen
        print(f"    not recognized: {', '.join(unknown)}")
        print(f"    valid choices: {', '.join(valid)}")


def prompt_preferences(existing: Preferences | None = None) -> Preferences:
    p = existing or Preferences()
    print("\nTell me what you're looking for (Enter keeps the default):\n")
    p.max_price = ask("Max monthly rent ($)", p.max_price, int)
    p.min_price = ask("Min monthly rent ($, 0 = none)", p.min_price, int)
    p.beds = ask("Bedrooms (0 = studio)", p.beds, float)
    p.baths = ask("Bathrooms", p.baths, float)

    hoods = sorted(NEIGHBORHOOD_COORDS)
    print(f"\n  Known neighborhoods:\n    {', '.join(hoods)}\n")
    p.neighborhoods = ask_multi(
        "Neighborhoods, comma-separated (or 'any')", hoods, p.neighborhoods, "any")

    print(f"\n  Amenities you can pick from:\n    {', '.join(AMENITIES)}\n")
    p.amenities = ask_multi(
        "Must-have amenities, comma-separated (or 'none')", AMENITIES, p.amenities,
        "none", normalize=lambda t: t.replace(" ", "_"))

    p.min_sqft = ask("Min square feet (0 = don't care)", p.min_sqft or 0, int) or None
    p.k = ask("How many results", p.k, int)
    stretch_pct = ask("Allow results over budget by up to (%)", int(p.price_stretch * 100), int)
    p.price_stretch = max(0, stretch_pct) / 100

    print(f"\n  Data sources:\n    {', '.join(PROVIDERS)}\n")
    p.sources = ask_multi(
        "Sources to fetch from, comma-separated (or 'all')",
        list(PROVIDERS), p.sources, "all")
    return p


def resolve_sources(args, prefs: Preferences) -> list[str]:
    """--sources (explicit override) > saved preference > all providers."""
    if args.sources:
        return [s.strip() for s in args.sources.split(",") if s.strip()]
    return list(prefs.sources) or list(PROVIDERS)


def load_prefs() -> Preferences | None:
    """Saved preferences, or None if absent or unreadable."""
    try:
        data = json.loads(PREFS_FILE.read_text())
        return Preferences.from_dict(data) if isinstance(data, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def save_prefs(p: Preferences) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(p.to_dict(), indent=2))


# ---------- fetching ----------

def _make_provider(name: str, session: SafeSession, rentcast_key: str | None):
    """Instantiate a provider, or return (None, reason) when it can't run."""
    cls = PROVIDERS.get(name)
    if not cls:
        return None, f"unknown provider '{name}' (choices: {', '.join(PROVIDERS)})"
    if name == "rentcast":
        provider = cls(session, api_key=rentcast_key)
        if not provider.api_key:
            return None, ("no API key — save one with `neighbors --rentcast-key KEY` "
                          "or set RENTCAST_API_KEY")
        return provider, None
    return cls(session), None


def _billable_requests(session: SafeSession, since: int) -> int:
    """Real network responses since index `since` (cache hits are free)."""
    return sum(1 for h in session.history[since:] if h["event"] == "response")


def fetch_listings(prefs: Preferences, sources: list[str],
                   budget: int = 25, min_interval: float = 4.0, max_pages: int = 2,
                   rentcast_key: str | None = None,
                   dbg: DebugSession | None = None,
                   ignore_coverage: bool = False) -> list[Listing]:
    """Fetch from every planned provider. Fetch planning (coverage + quota)
    decides which providers actually hit the network; every real fetch is
    recorded in the manifest (provenance) and the quota ledger.

    `ignore_coverage=True` forces past the "already covered recently" skip —
    used when the store has no active listings, so a stale-but-covered state
    can't lock the app into showing nothing."""
    session = SafeSession(CACHE_DIR, budget=budget, min_interval=min_interval)
    manifest = Manifest(MANIFEST_FILE)
    ledger = QuotaLedger(DB_FILE)
    decisions = {d.provider: d for d in
                 plan_fetch(prefs, sources, manifest, ledger, COVERAGE_TTL_S,
                            ignore_coverage=ignore_coverage)}
    if dbg:
        dbg.session = session

    listings: list[Listing] = []
    for name in sources:
        decision = decisions[name]
        if decision.action == "skip":
            print(f"  {name}: skipped — {decision.reason}")
            if dbg:
                dbg.provider_stats.append({"provider": name, "skipped": decision.reason})
            continue
        provider, problem = _make_provider(name, session, rentcast_key)
        if provider is None:
            print(f"  ! {name}: {problem}. Skipping.")
            if dbg:
                dbg.provider_stats.append({"provider": name, "error": problem})
            continue
        if dbg and name == "rentcast":
            dbg.add_secret(provider.api_key)

        print(f"  Fetching from {name} …", flush=True)
        stat = {"provider": name, "listings": 0, "error": None}
        t0, history_mark = time.monotonic(), len(session.history)
        try:
            found = provider.fetch(prefs, max_pages=max_pages)
        except BudgetExceeded as e:
            print(f"  ! {e}")
            stat["error"] = f"BudgetExceeded: {e}"
            if dbg:
                dbg.provider_stats.append(stat)
            break
        except Exception as e:  # a broken provider must not kill the run
            logging.getLogger("neighbors").warning("%s failed: %s", name, e)
            stat["error"] = traceback.format_exc()
            found = []

        billable = _billable_requests(session, history_mark)
        ledger.record(name, billable)
        if found:
            manifest.record(name, prefs, len(found), requests_used=billable)
        stat.update(listings=len(found), elapsed_s=round(time.monotonic() - t0, 2),
                    requests=billable)
        if dbg:
            dbg.provider_stats.append(stat)
        print(f"    → {len(found)} listings")
        listings.extend(found)

    ledger.save()
    if not listings and all(d.action == "fetch" for d in decisions.values()):
        print("\n  No live listings retrieved (sites likely blocked scraping).")
        print("  Try again later or add another provider.")
    print(f"  Network requests used: {session.spent}/{budget} "
          "(rest served from cache or skipped)")
    return listings


# ---------- display ----------

def show_results(results: list[tuple[Listing, float, dict]],
                 annotations: dict[str, str] | None = None) -> None:
    annotations = annotations or {}
    if not results:
        print("\nNo results.")
        return
    print()
    for i, (l, d, parts) in enumerate(results, 1):
        beds = "Studio" if l.beds == 0 else f"{l.beds:g}bd"
        sqft = f", {l.sqft} ft²" if l.sqft else ""
        amen = f" | {', '.join(l.amenities[:4])}" if l.amenities else ""
        flags = f"  [{annotations[l.id]}]" if l.id in annotations else ""
        drivers = sorted(parts.items(), key=lambda kv: -kv[1])[:2]
        why = ", ".join(f"{k}" for k, v in drivers if v > 0.25) or "close on everything"
        print(f"{i:>3}. [{match_pct(d):>3}%] ${l.price:,}  {beds}/{l.baths:g}ba{sqft}  "
              f"{l.neighborhood or '?'} — {l.address}{amen}{flags}")
        print(f"       {l.url}   (furthest on: {why})")
    print()


# ---------- modes ----------

def mode_search(prefs: Preferences, listings: list[Listing],
                annotations: dict[str, str] | None = None) -> list:
    q = Query.from_preferences(prefs)
    results = knn(q, listings, k=prefs.k)
    show_results(results, annotations)
    return results


def mode_seed(prefs: Preferences, listings: list[Listing], seed_ref: str,
              annotations: dict[str, str] | None = None) -> list:
    """Find neighbors of a listing you liked (by result index or id)."""
    seed = None
    if seed_ref.isdigit():
        idx = int(seed_ref) - 1
        ranked = knn(Query.from_preferences(prefs), listings, k=len(listings))
        if 0 <= idx < len(ranked):
            seed = ranked[idx][0]
    if seed is None:
        seed = next((l for l in listings if l.id == seed_ref), None)
    if seed is None:
        print(f"  Couldn't find listing '{seed_ref}'. Use a result number or listing id.")
        return []
    print(f"\n  Similar to: ${seed.price:,} {seed.beds:g}bd — {seed.address} ({seed.neighborhood})")
    q = Query.from_listing(seed, max_price=prefs.max_price * (1 + prefs.price_stretch))
    results = knn(q, listings, k=prefs.k, exclude_ids={seed.id})
    show_results(results, annotations)
    return results


# ---------- main ----------

COMMANDS = ["search", "engine", "web", "cache-status", "quota", "weights"]

DESCRIPTION = """\
Find NYC rentals that fit — including the near-misses strict filters hide.

  neighbors              fetch new listings, rank them, print the best matches
  neighbors web          open the full web app in your browser

other commands:
  search                 same as plain `neighbors`
  engine                 interactive like/hide session that learns your taste
  cache-status           what has been fetched, and how fresh it is
  quota                  this month's API usage per provider
  weights                current ranking weights (learned or default)
"""


def _bounded(cast, lo, hi):
    """argparse type: `cast(raw)` within [lo, hi], with a readable error."""
    def parse(raw: str):
        try:
            value = cast(raw)
        except ValueError:
            value = None
        if value is None or not lo <= value <= hi:
            raise argparse.ArgumentTypeError(f"expected a value from {lo} to {hi}, got '{raw}'")
        return value
    return parse


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="neighbors", usage="neighbors [command] [options]",
        description=DESCRIPTION, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", nargs="?", default="search", choices=COMMANDS,
                    help=argparse.SUPPRESS)

    search = ap.add_argument_group("search options")
    search.add_argument("--cached", action="store_true",
                        help="re-rank the listings already stored; no network")
    search.add_argument("--prefs", action="store_true",
                        help="edit saved preferences and exit")
    search.add_argument("--reset-prefs", action="store_true",
                        help="re-enter all preferences, then search")
    search.add_argument("--like", metavar="REF",
                        help="show listings similar to result #N or a listing id")
    search.add_argument("--sources", metavar="LIST",
                        help=f"comma-separated providers for this run ({', '.join(PROVIDERS)})")
    search.add_argument("--rentcast-key", metavar="KEY",
                        help="save a RentCast API key to your system keychain "
                             '(use "" to remove it)')

    web = ap.add_argument_group("web options")
    web.add_argument("--port", type=_bounded(int, 0, 65535), default=8787,
                     help="port for the web app (default 8787)")
    web.add_argument("--no-browser", action="store_true",
                     help="don't open a browser automatically")

    other = ap.add_argument_group("other options")
    other.add_argument("--alpha", type=_bounded(float, 0.0, 1.0), default=DEFAULT_ALPHA,
                       help=f"engine: weight on preferences vs. liked listings, 0..1 "
                            f"(default {DEFAULT_ALPHA})")
    other.add_argument("--plan", action="store_true",
                       help="engine: show what would be fetched, then exit")
    other.add_argument("--set-used", metavar="PROVIDER=N",
                       help="quota: sync this month's count with the provider's dashboard")
    other.add_argument("--reset-weights", action="store_true",
                       help="weights: discard learned weights")
    other.add_argument("--debug", action="store_true",
                       help="write a shareable diagnostic report to ~/.neighbors/debug/")
    other.add_argument("-v", "--verbose", action="store_true", help="verbose logging")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if (args.verbose or args.debug) else logging.WARNING,
        format="%(name)s: %(message)s")

    dbg = None
    if args.debug:
        dbg = DebugSession(args=vars(args))
        dbg.add_secret(args.rentcast_key)
        dbg.add_secret(os.environ.get("RENTCAST_API_KEY"))

    exit_code = 0
    try:
        exit_code = _run(args, dbg)
    except Exception:
        if dbg is None:
            raise
        dbg.error = traceback.format_exc()
        print("\n  ! Crashed — traceback captured in the debug report.")
        exit_code = 2
    finally:
        if dbg is not None:
            path = dbg.save()
            print(f"\n  Debug report written to: {path}")
            print("  Paste its contents when reporting problems — API keys are redacted.")
    return exit_code


def save_rentcast_key(key: str) -> bool:
    """Store (or, if blank, remove) the RentCast key in the OS credential store."""
    from .keystore import KeystoreUnavailable, store_name
    from .providers.rentcast import save_api_key
    try:
        save_api_key(key)
    except KeystoreUnavailable as e:
        print(f"  ! Couldn't save the RentCast key: {e}")
        return False
    where = store_name()
    print(f"  RentCast key saved to your {where}." if key.strip()
          else f"  RentCast key removed from your {where}.")
    return True


def _run(args, dbg: DebugSession | None) -> int:
    """Dispatch to the requested command. Informational commands need no
    preferences; everything else resolves prefs first."""
    if args.rentcast_key is not None:
        if not save_rentcast_key(args.rentcast_key):
            return 1
        if not args.rentcast_key.strip():
            return 0          # `--rentcast-key ""` only removes the key
    informational = {"cache-status": cmd_cache_status, "quota": cmd_quota,
                     "weights": cmd_weights}
    if args.command in informational:
        return informational[args.command](args)
    if args.command == "web":
        from .web import serve  # lazy: the web app imports cli for fetching
        try:
            serve(APP_DIR, args.port, open_browser=not args.no_browser)
        except OSError as e:
            print(f"  ! Can't start the web app on port {args.port}: {e.strerror or e}.")
            print("    Is it already running? Try another port with --port.")
            return 1
        return 0

    if args.prefs:  # prefs-only mode: edit + save, touch nothing else
        prefs = prompt_preferences(load_prefs())
        save_prefs(prefs)
        if dbg:
            dbg.prefs = prefs
        print(f"\n  Preferences saved to {PREFS_FILE}. Nothing fetched — "
              "run without --prefs to search (or with --cached to re-rank the last fetch).")
        return 0

    prefs = None if args.reset_prefs else load_prefs()
    if prefs is None or args.reset_prefs:
        prefs = prompt_preferences(prefs)
        save_prefs(prefs)
        print(f"\n  Saved to {PREFS_FILE} (edit anytime, or rerun with --reset-prefs)")
    else:
        print(f"  Using saved preferences from {PREFS_FILE} (--reset-prefs to change)")
    if dbg:
        dbg.prefs = prefs

    if args.command == "engine":
        return cmd_engine(args, prefs, dbg)
    return cmd_search(args, prefs, dbg)


def _load_store_listings(args, prefs, dbg) -> tuple[Store, list[Listing]] | None:
    """Shared fetch-then-read-store step for search and engine. Returns None
    (after printing why) when there is nothing to rank."""
    store = Store(DB_FILE)
    if args.cached:
        if len(store) == 0:
            print("  No stored listings yet — run once without --cached first.")
            return None
        print(f"  Store: {len(store)} known listings (no network).")
    else:
        # if nothing in the store is still "active", a covering-fetch record
        # would otherwise keep us from refreshing — force past it.
        no_active = not store.active(window_days=prefs.active_window_days)
        fetched = fetch_listings(prefs, resolve_sources(args, prefs),
                                 rentcast_key=args.rentcast_key,
                                 dbg=dbg, ignore_coverage=no_active)
        stats = store.upsert(fetched)
        store.save()
        print(f"  Store: {len(store)} known listings "
              f"({len(stats.new)} new, {len(stats.price_drops)} price drops this run)")

    listings = store.active(window_days=prefs.active_window_days)
    inactive = len(store) - len(listings)
    if inactive:
        print(f"  ({inactive} older listings kept as reference, excluded from ranking — "
              f"unseen for over {prefs.active_window_days} days)")
    if dbg:
        dbg.listing_count = len(listings)
    if not listings:
        print("No active listings available.")
        return None
    return store, listings


# ---------- command: search (the original one-shot flow) ----------

def cmd_search(args, prefs: Preferences, dbg: DebugSession | None) -> int:
    loaded = _load_store_listings(args, prefs, dbg)
    if loaded is None:
        return 1
    store, listings = loaded
    annotations = store.annotations()

    if args.like:
        results = mode_seed(prefs, listings, args.like, annotations)
    else:
        results = mode_search(prefs, listings, annotations)
        print("Tip: 'neighbors engine' gives you like/hide feedback that improves results.")
    if dbg:
        dbg.results = results
    return 0


# ---------- command: engine (iterative session) ----------

ENGINE_HELP = ("  commands: like N | hide N | open N | r (re-rank) | q (quit)")


def cmd_engine(args, prefs: Preferences, dbg: DebugSession | None) -> int:
    if args.plan:  # dry run: show fetch decisions, spend nothing
        decisions = plan_fetch(prefs, resolve_sources(args, prefs), Manifest(MANIFEST_FILE),
                               QuotaLedger(DB_FILE), COVERAGE_TTL_S)
        print("\n  Fetch plan (nothing executed):")
        for d in decisions:
            print(f"    {d.provider:<12} {d.action:<6} {d.reason}")
        print()
        return 0

    loaded = _load_store_listings(args, prefs, dbg)
    if loaded is None:
        return 1
    store, listings = loaded
    feedback = Feedback(DB_FILE)
    weights = load_weights(WEIGHTS_FILE)
    if weights:
        print("  Using learned weights (neighbors weights to inspect).")

    def rerank():
        ranked = rank(prefs, listings, feedback, alpha=args.alpha, weights=weights)
        results = [(r.listing, r.score, r.parts) for r in ranked]
        # flag repeats from earlier sessions, then remember this showing
        notes = store.annotations()
        for l, _, _ in results:
            if feedback.seen.get(l.id):
                notes[l.id] = (notes.get(l.id, "") + " shown before").strip()
        feedback.mark_seen([l.id for l, _, _ in results])
        show_results(results, notes)
        return results

    results = rerank()
    if dbg:
        dbg.results = results
    print(ENGINE_HELP)

    while True:
        try:
            line = input("engine> ").strip().lower()
        except EOFError:
            break
        if not line:
            continue
        cmd, _, arg = line.partition(" ")
        if cmd == "q":
            break
        if cmd == "r":
            results = rerank()
            continue
        if cmd in ("like", "hide", "open") and arg.isdigit() and 0 < int(arg) <= len(results):
            listing = results[int(arg) - 1][0]
            if cmd == "open":
                import webbrowser
                webbrowser.open(listing.url)
                print(f"  opened {listing.url}")
            else:
                getattr(feedback, cmd)(listing.id)
                verb = "liked" if cmd == "like" else "hidden"
                print(f"  {verb}: {listing.address} (r to re-rank with this feedback)")
            continue
        print(ENGINE_HELP)

    feedback.save()
    _maybe_learn_weights(prefs, store, feedback)
    print(f"  Session saved: {len(feedback.liked)} liked, {len(feedback.hidden)} hidden.")
    return 0


def _maybe_learn_weights(prefs: Preferences, store: Store, feedback: Feedback) -> None:
    """Refresh learned weights when there's enough feedback to be meaningful."""
    liked = [l for l in (store.find(i) for i in feedback.liked) if l]
    learned = learn_weights(prefs, liked)
    if learned:
        save_weights(WEIGHTS_FILE, learned)
        print(f"  Learned weights updated from {len(liked)} likes "
              "(neighbors weights to inspect, --reset-weights to discard).")


# ---------- informational commands ----------

def cmd_cache_status(args) -> int:
    manifest, store = Manifest(MANIFEST_FILE), Store(DB_FILE)
    entries = manifest.entries()
    print(f"\n  Store: {len(store)} listings ({len(store.active())} active)")
    if not entries:
        print("  Manifest: no fetches recorded yet.\n")
        return 0
    print(f"  Manifest: {len(entries)} fetches recorded. Most recent per provider:")
    latest = {e["provider"]: e for e in sorted(entries, key=lambda e: e["fetched_at"])}
    for name, e in sorted(latest.items()):
        age_h = (time.time() - e["fetched_at"]) / 3600
        snap = e["prefs_snapshot"]
        hoods = ", ".join(snap["neighborhoods"]) or "city-wide"
        print(f"    {name:<12} {age_h:5.1f}h ago  {e['listings_found']:>4} listings  "
              f"${snap['min_price']}-{snap['max_price']} in {hoods}  "
              f"[{e['prefs_fingerprint']}]")
    print()
    return 0


def cmd_quota(args) -> int:
    ledger = QuotaLedger(DB_FILE)
    if args.set_used:
        provider, _, n = args.set_used.partition("=")
        provider, n = provider.strip(), n.strip()
        if provider not in PROVIDERS or not n.isdigit():
            print("  usage: quota --set-used PROVIDER=N  (e.g. rentcast=4; "
                  f"providers: {', '.join(PROVIDERS)})")
            return 1
        ledger.set_used(provider, int(n))
        ledger.save()
        print(f"  {provider}: count set to {n} for this month.")
    summary = ledger.summary()
    print("\n  API quota (this month):")
    if not summary:
        print("    nothing tracked yet")
    for provider, s in summary.items():
        limit = s["limit"] if s["limit"] is not None else "unlimited"
        stop = f"  (auto-stops fetching at {s['stop_at']})" if s.get("stop_at") else ""
        print(f"    {provider:<12} {s['used']} / {limit}{stop}")
    print("\n  Note: only calls made through this app are counted. If the provider's")
    print("  dashboard disagrees, sync with: quota --set-used rentcast=N\n")
    return 0


def cmd_weights(args) -> int:
    if args.reset_weights:
        WEIGHTS_FILE.unlink(missing_ok=True)
        print("  Learned weights deleted — back to defaults.")
        return 0
    learned = load_weights(WEIGHTS_FILE)
    current = learned or DEFAULT_WEIGHTS
    print(f"\n  Ranking weights ({'learned from your likes' if learned else 'defaults'}):")
    for feature in DEFAULT_WEIGHTS:
        marker = ""
        if learned:
            delta = learned[feature] - DEFAULT_WEIGHTS[feature]
            marker = f"  (default {DEFAULT_WEIGHTS[feature]}, {delta:+.2f})"
        print(f"    {feature:<10} {current[feature]:>6.2f}{marker}")
    print(f"\n  Learning needs at least {MIN_LIKES} liked listings "
          "(neighbors engine → like N).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
