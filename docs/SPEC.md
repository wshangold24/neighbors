# Neighbors — functional spec

This is the behavioral contract for Neighbors. Every requirement has an ID, and
every ID is verified by at least one automated test that cites it in its
docstring (`grep -rn "WEB-07" tests/` finds the test for a requirement).

Scope: what a user can observe from the two entry points — the terminal CLI and
the web app. Ranking math is specified separately in [SCORING.md](SCORING.md).

Tests never touch the real network: live sources, geocoding, and building
verification are stubbed, and the browser tests block every non-local request.

---

## CLI

| ID | Requirement | Test |
|---|---|---|
| CLI-01 | `neighbors --help` exits 0 and describes the program as `neighbors`, listing the `web` command. | `tests/test_spec_cli.py` |
| CLI-02 | `python -m neighbors` behaves identically to the `neighbors` command. | `tests/test_spec_cli.py` |
| CLI-03 | On first run with no saved preferences, the CLI prompts for them, saves them to `~/.neighbors/preferences.json`, then searches. | `tests/test_spec_cli.py` |
| CLI-04 | `neighbors --cached` ranks the stored listings with **zero** network requests and prints numbered results with a 0–100 match score. | `tests/test_spec_cli.py` |
| CLI-05 | `neighbors --cached` with an empty store explains how to get listings and exits with status 1 — no traceback. | `tests/test_spec_cli.py` |
| CLI-06 | `neighbors --prefs` updates saved preferences and exits without fetching or ranking. | `tests/test_spec_cli.py` |
| CLI-07 | `neighbors web --port N --no-browser` starts the web app on `127.0.0.1:N` and serves the page. | `tests/test_spec_cli.py` |
| CLI-08 | Removed tuning flags (`--budget`, `--interval`, `--max-pages`, `--host`) are rejected with a usage error. | `tests/test_spec_cli.py` |
| CLI-09 | `--rentcast-key KEY` saves the key to the OS credential store (macOS Keychain, Windows Credential Manager, Linux Secret Service) — never a plain file; `--rentcast-key ""` removes it. Without a credential store it exits 1 and points to `RENTCAST_API_KEY`. | `tests/test_spec_cli.py` |

## Web app

### Page & layout

| ID | Requirement | Test |
|---|---|---|
| WEB-01 | The page title is **Neighbors** and the header shows the **Neighbors** brand. | `tests/e2e/test_web_ui.py` |
| WEB-02 | On load the page renders ranked listing cards. Each card shows price, match %, beds/baths, neighborhood, and address. | `tests/e2e/test_web_ui.py` |
| WEB-03 | The header shows live counts: available, verified, and liked listings. | `tests/e2e/test_web_ui.py` |
| WEB-04 | Strong matches (≥ 60%) appear first, best match first. Weaker matches are grouped under a "stretch options" label. | `tests/e2e/test_web_ui.py` |
| WEB-05 | Loading and using the page produces no JavaScript errors. | `tests/e2e/test_web_ui.py` (every test) |
| WEB-06 | In list view, the page makes no requests to hosts other than the local server. | `tests/e2e/test_web_ui.py` |
| WEB-07 | At phone width (375px) the page has no horizontal scroll. | `tests/e2e/test_web_ui.py` |
| WEB-08 | Text from listing sources and user-entered place names is rendered as text, never as HTML, and outbound links are http(s) only. | `tests/e2e/test_web_ui.py` |
| WEB-09 | A failed or non-JSON request is reported in a toast instead of breaking the page. | `tests/e2e/test_web_ui.py` |

### Feedback

| ID | Requirement | Test |
|---|---|---|
| WEB-10 | Clicking ♥ on a card likes it: the liked count increments, a "Liked · N" button appears, and the like persists across reloads. | `tests/e2e/test_web_ui.py` |
| WEB-11 | A liked listing can be un-liked from the compare dialog, and the liked count goes back down. | `tests/e2e/test_web_ui.py` |
| WEB-12 | Clicking ✕ hides a card. It disappears immediately and never comes back, even after a reload. | `tests/e2e/test_web_ui.py` |
| WEB-13 | The "Liked · N" button opens a compare dialog that lists every liked apartment. | `tests/e2e/test_web_ui.py` |

### Ranking & exploration

| ID | Requirement | Test |
|---|---|---|
| WEB-20 | With zero likes, the "Balanced" and "Like my likes" modes are disabled. After a like they become enabled. | `tests/e2e/test_web_ui.py` |
| WEB-21 | Clicking a ranking mode makes it the active mode and re-ranks without a network fetch. | `tests/e2e/test_web_ui.py` |
| WEB-22 | "similar" on a card switches to a similar-listings view with a breadcrumb trail (`search ▸ <apartment>`). The seed apartment is not among its own results. | `tests/e2e/test_web_ui.py` |
| WEB-23 | Chaining "similar" extends the breadcrumb trail. Clicking `search` returns to normal results. | `tests/e2e/test_web_ui.py` |
| WEB-24 | "details" (or clicking a card body) opens a details dialog titled with that apartment's address. "Close" dismisses it. | `tests/e2e/test_web_ui.py` |
| WEB-25 | The List / Map toggle switches between the card grid and the map container. Map price pins stay legible, including affordable listings. | `tests/e2e/test_web_ui.py` |
| WEB-26 | When requests overlap, only the newest one's response is shown. A slow earlier response never replaces a newer view. | `tests/e2e/test_web_ui.py` |
| WEB-27 | Liking or hiding inside the similar view keeps you in that view, without adding a duplicate breadcrumb. | `tests/e2e/test_web_ui.py` |
| WEB-28 | For RentCast listings, the details dialog offers "Look up amenities" (one quota-checked API call). | `tests/e2e/test_web_ui.py` |

### Preferences

| ID | Requirement | Test |
|---|---|---|
| WEB-30 | "Preferences" opens a dialog pre-filled with the saved preferences. | `tests/e2e/test_web_ui.py` |
| WEB-31 | Saving changed preferences persists them (visible after reload and to the CLI), confirms with a toast, and offers to fetch listings for the new criteria. | `tests/e2e/test_web_ui.py` |
| WEB-32 | Cancel discards edits. | `tests/e2e/test_web_ui.py` |
| WEB-33 | Saved boroughs show as checked, so re-saving never silently drops them. | `tests/e2e/test_web_ui.py` |
| WEB-34 | Pressing Enter in a preferences field saves the dialog. | `tests/e2e/test_web_ui.py` |
| WEB-35 | Dialogs close on a backdrop click or Close, but not on clicks inside the dialog. | `tests/e2e/test_web_ui.py` |
| WEB-36 | A RentCast key entered in the first-run banner (or Preferences → Advanced) is saved to the OS credential store, never echoed back; the banner then disappears and Preferences shows it as saved. | `tests/e2e/test_web_ui.py` |

### Updating data

| ID | Requirement | Test |
|---|---|---|
| WEB-40 | The "Update" button opens a menu with Refresh, Verify all, and Fetch new listings. Clicking elsewhere or pressing Escape closes it. | `tests/e2e/test_web_ui.py` |
| WEB-41 | "Fetch new listings" pulls from the sources, merges new listings into the store, reports `fetched · new · drops · verified` in a toast, and shows the new listings. | `tests/e2e/test_web_ui.py` |
| WEB-42 | With an empty store, the page shows an empty state pointing to "Fetch new listings" instead of a blank grid. | `tests/e2e/test_web_ui.py` |
| WEB-43 | If the store has listings but the current filters rule them all out, the empty state says which filters excluded them and links to Preferences. | `tests/e2e/test_web_ui.py` |

### About page

| ID | Requirement | Test |
|---|---|---|
| WEB-50 | `/about` explains why Neighbors exists, what it does, and how it ranks. The app header links to it, and its call to action returns to the listings. | `tests/e2e/test_web_ui.py` |
| WEB-51 | The about page makes no external requests and has no horizontal scroll at 375px. | `tests/e2e/test_web_ui.py` |
| WEB-52 | When the app is running, the about page shows live counts (tracked, available, verified, liked) that match the app. | `tests/e2e/test_web_ui.py` |

### Theme

| ID | Requirement | Test |
|---|---|---|
| WEB-60 | A header toggle (on the app and the about page) switches between light and dark themes. The choice is remembered across reloads and shared between pages. | `tests/e2e/test_web_ui.py` |
| WEB-61 | Until the user picks a theme, the app follows the operating system's light/dark setting. | `tests/e2e/test_web_ui.py` |
| WEB-62 | In both themes, every text color meets WCAG AA contrast (≥ 4.5:1) on the surfaces it's used on. | `tests/e2e/test_web_ui.py` |

### API contract

The JSON API behind the page (`/api/state`, `/api/results`, `/api/similar`,
`/api/liked`, `/api/feedback`, `/api/prefs`, `/api/fetch`, `/api/verify`, …)
is covered endpoint by endpoint in `tests/test_web.py`.
