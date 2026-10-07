# NYC Rental Data Sources — Exhaustive Research

Goal: sources for **verifiable, reputable, currently-available** apartments for rent in NYC.
Ranked by how authoritative/legal the feed is. Each entry notes access model, cost, verifiability,
and how it fits aptfinder's provider model (`aptfinder/providers/`).

Legend for **Verifiability**: how directly the data ties to a real, agent-/owner-attested, currently-listed unit.

---

## 0. What aptfinder already does

| Source | Type | Status in app | Notes |
|---|---|---|---|
| **RentCast** | Official REST API | ✅ registered (`rentcast.py`) | Only true API source. Free tier 50 req/mo; app spends ≤1–2 calls/run + 12h cache. Aggregates ~140M property records incl. active rental listings. |
| **StreetEasy** | Best-effort scrape | ✅ registered (`streeteasy.py`) | No public API + PerimeterX bot protection → frequently returns nothing (403 treated as final). |
| **RentHop** | Best-effort scrape | ✅ registered (`renthop.py`) | More scrape-tolerant than StreetEasy. |
| **Independent geo-verification** | Cross-check | ✅ (`verify.py`) | Geocodes each listing's address via OSM Nominatim; flags `ok`/`mismatch`/`not_found` vs. provider coords (>1.5km = suspect). This is your *verifiability* layer today. |
| **DemoProvider** | Synthetic | ⚠️ test-only, unregistered | Never mixed into real results. |

Your current architecture (best-effort providers + independent geo-verification + validation gate)
is the right skeleton. The list below is about *feeding* it better sources.

---

## Tier 1 — Authoritative / official feeds (most reputable & verifiable)

These are the actual systems of record brokers publish into. Getting access is the hard part
(licensing, sometimes a broker relationship), but the data is canonical and currently-available by design.

### 1. REBNY RLS (Residential Listing Service) via RESO Web API — *the* NYC source
NYC has **no traditional MLS**; REBNY's RLS is its equivalent. As of 2024 it migrated to the
**RESO Web API** (OData / RESO Data Dictionary), powering listings across 581 member firms / 12,000+ agents
and 1,000+ data connections. Feed products: **IDX, VOW, and "Product"**.
- **Access:** through RLS syndication/pre-licensed data partners; requires agreeing to data-use/licensing terms. Often needs a brokerage or vendor relationship.
- **Verifiability:** highest — this is the source brokers post exclusives to.
- **aptfinder fit:** a `rebny_rls` provider hitting a RESO OData endpoint would be the gold-standard source. RESO is standardized, so parsing is well-documented.

### 2. Perchwell — RLS-native RESO Web API vendor
Perchwell built the first standards-based, vendor-agnostic RLS data model with a RESO Web API for NYC brokerages. A practical on-ramp to RLS data if you partner or license through them.
- **Verifiability:** same as RLS (it's the RLS feed).

### 3. OneKey MLS (suburban NY: Westchester, Long Island, Hudson Valley, parts of Queens/Bronx reach)
NY's largest MLS. Delivers RESO Web API feeds (IDX / VOW / back-office) via **MLS Grid** (standardized business rules across MLSs) or approved IDX vendors (IDX Broker, Showcase IDX, Buying Buddy).
- **Access:** agree to MLS data-use policy → get credentials from MLS software provider. Usually requires an MLS-member broker.
- **aptfinder fit:** covers the metro fringe StreetEasy is weak on. Great complement to RLS for outer-borough/suburban rentals.

### 4. RESO Web API (standard, not a source itself)
The OData-based standard both RLS and OneKey now speak. Worth building one generic RESO client that any RESO feed (RLS, OneKey, MLS Grid) can plug into — one parser, many sources.

---

## Tier 2 — Portal syndication feeds (reputable, semi-open)

Free-to-join publisher programs. You normally *push* listings in, but partner/ILS status can also let you *pull* — and the feed **specs are public**, so they document exactly what a clean listing record looks like.

### 5. Zillow Rentals Network feed (covers Zillow + Trulia + HotPads)
Free program; requires approval from Zillow's Rentals Integrations team. XML feed (their Rental Listings Feed Guide, or MITS format), static feed URL, 4–6 week testing.
- **Note:** Zillow's *consumer* API is partner-only (no independent dev access) — but the **feed program** is open and free to join.
- **Verifiability:** high — landlord/PM-attested, fee-transparency fields.

### 6. HotPads feed
Zillow-owned; has its own published Rental Listings Feed spec + Real-Time Listing API syndication guide. Same ecosystem as Zillow.

### 7. Apartments.com network (ApartmentFinder, ForRent.com, Apartamentos.com, ApartmentHomeLiving)
50M+ monthly visitors; free syndication network. Primarily large/multifamily buildings.
- **Access:** syndication partners (e.g., Rentec Direct, property-management platforms) rather than a raw public API.

### 8. Realtor.com listings (via API.market / third-party wrappers)
Realtor.com data is a common practical stand-in for Zillow-style data through open developer wrappers.

---

## Tier 3 — Aggregator / data-vendor APIs (developer-accessible, paid)

Independent-developer-friendly REST APIs. Data is aggregated (so slightly less "system-of-record" than RLS) but they're built for exactly this use case.

### 9. RentCast — *(already integrated)* best independent-dev option
140M+ properties, 38k+ ZIPs. Endpoints: property records, AVM value, rent estimate, comps, **active listings**, market stats. Free 50/mo → $74 (1k) → $199 (5k) → $449 (25k).

### 10. Rent Estimate / Realtor Listings APIs (API.market, HouseCanary, etc.)
Marketplace-style APIs offering rental listings + rent estimates with open signup. Useful as a second aggregator to cross-check RentCast (agreement between two = stronger verifiability signal).

### 11. HouseCanary / ATTOM / CoreLogic / Estated
Property-data heavyweights. Stronger on valuation/ownership/parcel data than live rentals, but valuable for **enrichment & verification** (does this address exist? who owns it? is the building real?).

---

## Tier 4 — Government / open data (free, highly reputable, niche coverage)

Not general market-rate listings, but *authoritative* and great for verification + specific inventory.

### 12. NYC Housing Connect (affordable-housing lotteries)
Official city portal for below-market units. Advertised lotteries are also published as **NYC Open Data / data.gov datasets** ("Advertised Lotteries on Housing Connect by Lottery" and "…by Building") — queryable via the Socrata Open Data API (SODA).
- **Verifiability:** maximal (city-run). A distinct, high-value *category* of available units.

### 13. NYC HDC (Housing Development Corp) "Find" + NYCHA
Additional affordable/public housing inventory and waitlists.

### 14. NYC Open Data (Socrata / data.cityofnewyork.us) — verification layer
- **HPD** datasets (registrations, violations, litigation, rent-stabilization / Speculation Watch List).
- **PLUTO / MapPLUTO** (every tax lot: units, year built, owner) — verify a building exists and its unit count is plausible.
- **DOB** permits/complaints; **DHCR** rent-stabilization building lists.
- **Use:** cross-check every scraped/aggregated listing against real parcel + registration data (extends your `verify.py` beyond geocoding — you could flag "address exists but not a residential building" or "building has 0 units").

### 15. Federal: HUD USER / Fair Market Rents, Census ACS
Not listings, but ground-truth for sane price bounds and market context (your KNN price feature could use FMR as a reference point).

---

## Tier 5 — Open marketplaces & scrapes (broad coverage, weaker verifiability)

Highest volume, lowest trust — exactly where your independent verification earns its keep.

### 16. StreetEasy *(already integrated, scrape)* — dominant NYC portal, hostile to bots (PerimeterX).
### 17. RentHop *(already integrated, scrape)* — more tolerant; strong NYC rental coverage.
### 18. Craigslist NYC — RSS feeds per search exist; high fraud rate → verification mandatory. FSBO/by-owner inventory you won't find on broker feeds.
### 19. Facebook Marketplace / Groups — large by-owner inventory, no clean API, heavy scam risk.
### 20. Naked Apartments, Localize.city, Nooklyn, LeaseBreak (sublets), Listings Project, Gypsy Housing — NYC-specific niche portals.
### 21. Direct property-management / large-landlord sites — Related, TF Cornerstone, Equity Residential, Stonehenge, etc. Publish their own current availability; scraping one landlord's own site is high-trust (they own the unit) and often API-ish (JSON endpoints behind the page).
### 22. Brokerage IDX sites — Corcoran, Douglas Elliman, Compass, Brown Harris Stevens all render RLS data; their public pages are effectively RLS mirrors.

---

## Cross-cutting: verification & reputability techniques (regardless of source)

Your `verify.py` geocode check is step one. To make listings *verifiable* and filter scams/stale/duplicates:

1. **Two-source agreement** — trust a unit more when ≥2 independent feeds (e.g., RentCast + a portal) show the same address/price. Cheap, powerful.
2. **Parcel cross-check (NYC PLUTO)** — confirm the address is a real residential lot with plausible unit count.
3. **Building registration (HPD)** — confirm the building is a registered multiple dwelling; flag ones with open litigation/violations.
4. **Rent-stabilization check (DHCR / HPD)** — surface whether a unit is likely stabilized (huge signal for NYC renters).
5. **Freshness / recency gate** — you already drop RentCast listings >60 days old; apply a `listed_date` staleness filter everywhere.
6. **Duplicate collapse** — same unit appears across RLS-mirroring brokerage sites; dedupe by normalized address + beds + price band.
7. **Price-sanity vs. HUD FMR / ACS** — flag listings implausibly below market (classic scam signal).
8. **Image/contact heuristics** — duplicate stock photos or off-platform contact pushes = scam markers (Craigslist/FB especially).
9. **Broker license lookup (NYS DOS)** — verify the listing agent's license is real and active.

---

## Recommended roadmap for aptfinder

1. **Now (cheap wins):** add a **second aggregator API** (a Realtor/rent-estimate API) for two-source agreement, and layer **NYC PLUTO + HPD** cross-checks into `verify.py`. All free/low-cost, big verifiability jump.
2. **Add Housing Connect** as a distinct provider via the Socrata SODA API — free, authoritative, unique inventory.
3. **Build one generic RESO Web API client** so you're ready to plug in **REBNY RLS (via Perchwell/RLS partner)** and **OneKey MLS (via MLS Grid)** the moment you secure licensing — that's the reputable ceiling for NYC rentals.
4. **Harden scrapes** (StreetEasy/RentHop/Craigslist) as *volume* sources, always gated by the verification stack above — never trusted on their own.

---

## Sources
- [RentCast API](https://www.rentcast.io/api) · [RentCast rental-listings endpoint](https://developers.rentcast.io/reference/rental-listings-long-term)
- [Zillow Rentals Feed Integrations](https://www.zillowgroup.com/developers/api/rentals/rentals-feed-integrations/) · [Publish to Zillow Rental Network](https://www.zillow.com/z/rental-property-advertising/listing-feeds/)
- [REBNY RLS](https://www.rebny.com/rls/) · [RLS technical solutions](https://www.rebny.com/rls-technical-solutions/) · [RLS RESO migration press release](https://www.rebny.com/press-release/rebnys-residential-listing-service-completes-data-migration-to-help/) · [Perchwell + RLS RESO Web API](https://www.prnewswire.com/news-releases/brokerages-in-new-york-start-2024-with-a-competitive-advantage--the-first-standards-based-vendor-agnostic-listing-management-data-model-with-an-rls-reso-web-api-from-perchwell-302040161.html)
- [OneKey MLS Data Delivery](https://corporate.onekeymls.com/products/data-delivery) · [OneKey Data Delivery Resources](https://support.onekeymls.com/hc/en-us/articles/27251536794644-Data-Delivery-Resources) · [RESO Web API](https://www.reso.org/reso-web-api/)
- [NYC Housing Connect](https://housingconnect.nyc.gov/PublicWeb/) · [HPD Open Data](https://www.nyc.gov/site/hpd/about/open-data.page) · [Advertised Lotteries dataset (data.gov)](https://catalog.data.gov/dataset/advertised-lotteries-on-housing-connect-by-lottery) · [NYC HDC Find](https://www.nychdc.com/find)
- [HotPads Rental Listings Feed Guide](https://filenet.hotpads.com/+guides/RentalListingsFeedGuide.pdf) · [Apartments.com syndication (Rentec)](https://www.rentecdirect.com/blog/rental-listing-network/)
- [Best Real Estate APIs 2026 (API.market)](https://api.market/blog/magicapi/real-estate/best-real-estate-api) · [Best Real Estate APIs (ScrapingBee)](https://www.scrapingbee.com/blog/best-real-estate-apis-for-developers/) · [Real Estate APIs list (GitHub)](https://gist.github.com/patpohler/36c731113fd113418c0806f62cbb9e30)
