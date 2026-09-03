# QDII Ranking Methodology

## Data Sources

- Fund universe, current scale, inception, purchase state, holder data, NAV history, and announcement
  index come from the public Eastmoney fund endpoints recorded in the generated JSON.
- Current prospectuses, RMB product summaries, quota notices, and periodic reports use the official
  manager disclosures mirrored by the announcement PDF endpoint. Selection never uses a document
  published after the requested ranking date.
- `contract-benchmarks.json` is the versioned benchmark catalog. It records aliases, market scope,
  region, asset class, style, descriptive target flags, and ordinary/leveraged/inverse/volatility
  structure. Every catalog field is display-only.
- `us-equity-instruments.json` contains official-source classifications used for conservative
  fund-of-funds look-through.
- Nasdaq `XNDX` gross total-return history and SAFE USD/CNY central parity produce the CNY Nasdaq-100
  benchmark used by the US main ranking.

The public endpoints are not guaranteed APIs. A ranking-critical fetch or parse failure stops the run;
the updater never silently drops a required evaluation.

## Shared Eligibility

1. Keep purchasable OTC RMB A shares and an explicit RMB primary share without C/D markers. Exclude
   RMB C/D, USD, HKD, back-end shares, and standalone ETFs; eligible feeder funds and LOFs remain.
   Limit the universe to QDII fund types and `指数型-海外股票`; ordinary domestic equity, mixed, and
   index funds are outside the candidate pool.
2. Use the newest holder half-year/year-end period whose fund count reaches 95% of the preceding
   complete period. Skip a partially disclosed newer period with a visible warning.
3. Do not impose a fund-scale threshold. Require inception strictly over three years and trailing
   three-year adjusted return of at least 30%. If a complete five-year adjusted return exists, require
   at least 50%. If a complete ten-year adjusted return exists, require at least 100%. A missing long
   window bypasses only its own conditional threshold; an available value must pass.
4. Keep active and passive/index strategies. Exclude bond and commodity strategies.
5. Parse the latest prospectus disclosed on or before the ranking date and keep recognized, composite,
   unrecognized, or unreadable status plus all recognized benchmark components. Benchmark purity,
   identity, market, country/region, asset class, structure, component count, weight, and parse status
   do not control eligibility or list routing.
6. Cross-check the newest eligible RMB product summary. An explicit index or weight conflict, a missing
   document, or unreadable text is reported but does not exclude the fund. Parse the official
   annualized comprehensive fund operating expense from the same RMB summary when available.
7. Apply the configured China, Hong Kong, and pan-Asia geography keywords to US-main routing only.
   A matching fund remains eligible but routes to the global supplement regardless of confirmed US
   exposure. Contract benchmarks never trigger geography routing.
8. Resolve the manager-level direct-sale quota for every otherwise eligible candidate. Unlimited sale
   qualifies; a known amount must be at least CNY 200. Unknown direct quota is excluded with a warning.
   Agency quota is displayed but does not affect eligibility.

## US Main Ranking

- Read direct US equities from the latest eligible report's country table. For fund-of-funds, classify
  disclosed underlying funds using official sources. Unknown positions contribute zero to the confirmed
  lower bound and their full weight to the possible upper bound; an interval midpoint is never used.
- Require confirmed US-equity exposure of at least 50% and no configured geography keyword in the fund
  name. A critical report or table parse failure stops the run. Contract benchmark classification does
  not decide list placement.
- Rank by three-year CNY Nasdaq-100 weekly-return correlation descending, then `abs(beta - 1)`, confirmed
  US exposure, institutional holding, three-year return descending, and fund code ascending.

## Global Supplement Ranking

- Accept every remaining eligible strategy whose confirmed US-equity exposure is below 50%, including
  intervals whose possible upper bound crosses 50%. Also accept every eligible fund whose name matches
  a configured US-main geography keyword, even when confirmed exposure is 50% or higher. Display the
  full conservative interval and the routing reason. Do not include candidates already in the US main
  ranking or bond/commodity strategies; the global list has no geography-name exclusion.
- Compute the three-year annualized return from the actual adjusted-NAV start/end dates. Divide it by
  the absolute three-year maximum drawdown. A true zero drawdown stores `null`, displays `∞`, and sorts
  before finite ratios.
- Rank by return/drawdown ratio, three-year return, smaller absolute drawdown, institutional holding,
  scale descending, and fund code ascending.

Each stage scans its complete candidate set before either list is truncated to ten. One list may be
empty or shorter than ten without relaxing thresholds; both lists empty stops publication.

## Performance And Nasdaq Fit

- Use the latest NAV on or before the ranking date and the latest NAV on or before the corresponding
  date one, three, five, or ten years earlier. Build adjusted wealth from consecutive NAVs, cash
  distributions, and explicit source adjustment events. Maximum drawdown is peak-to-trough and stored
  as non-positive. Five- and ten-year returns require a complete window; otherwise they remain `null`
  with the available NAV history start date displayed.
- Convert XNDX to CNY by multiplying each USD index level by the latest USD/CNY central parity on or
  before that date. Neither source may be carried forward more than seven days; future matching is
  forbidden.
- Take the final adjusted fund wealth observation in each ISO week over three years and form returns
  only across adjacent ISO weeks. Require at least 140 paired returns spanning at least 1,000 days.
- Correlation is Pearson correlation, beta is sample covariance divided by benchmark sample variance,
  and tracking error is the sample standard deviation of weekly active returns times `sqrt(52)`.

Five- and ten-year returns apply only their conditional eligibility thresholds and do not participate
in ranking. The annualized comprehensive operating expense is display-only and is not an application
or redemption fee. Adjusted NAV returns already reflect fund operating expenses, so the displayed
expense is never subtracted again.

## Quota Precedence

Process relevant manager notices chronologically and apply transitions effective on or before the
ranking date. A later transition overrides an earlier one for the same channel.

- A direct-sale amount updates direct sale only; an agency amount updates agency sale only.
- A notice without channel distinction updates both. `全部销售机构累计` is one shared ceiling and must
  not be added across channels.
- A restoration notice sets `unlimited` only when it contains no replacement ceiling. Future automatic
  restoration is a separate transition.
- Unreadable or ambiguous active notices make the affected quota unknown. Never infer a value.
- A notice selected as quota-relevant by its title must produce at least one effective transition. If
  it does not, mark the quota unresolved instead of retaining an older announcement state.

## Cache And Output

Each daily run revalidates ranking-critical sources rather than trusting the prior result. NAV history
uses `Last-Modified`; a `304` reuses normalized points only after the latest fund-page NAV date and value
agree. A changed source, missing validator, corrupt cache, or mismatch forces a complete download.

Each performance-qualified fund has one daily announcement-index snapshot shared by contract, holding,
and quota processing. A cold cache paginates until the legal-document history is seeded; later runs
check the first page and merge new IDs. Parsed contract profiles, report exposure, and quota transitions
are keyed by source announcement IDs, catalog fingerprints, and parser versions. A parser-version change
invalidates only its derived cache. Cached failures remain failures until the source or parser identity
changes.

PDF bytes remain cached by announcement ID. On a derived-result cache miss they must still be valid PDFs
with extractable text. Historical runs never consume a future NAV, benchmark point, allocation, or
announcement. Ranking-critical revalidation failure stops publication instead of falling back to the
previous day's ranking.

Every run writes an unpublished `run-metrics.json` with phase durations, categorized HTTP calls and
bytes, retries, conditional responses, PDF extractions, and cache statistics. GitHub Actions compares it
with the versioned pre-optimization baseline; performance misses warn but never bypass data validation.

The exchange-premium tab uses the versioned `us-equity-etfs.json` allowlist. One delayed Eastmoney batch
quote supplies market price, IOPV, discount rate, change, turnover, and timestamps for all entries.
Displayed premium is the negated source discount rate and must agree with `price / IOPV - 1` within the
price-tick tolerance. The daily run caches valid records individually; quote failure falls back per ETF
with a visible timestamp and reportable warning but never weakens or blocks ranking validation. The
browser refresh uses the same validation rules and keeps the rendered value when a row fails. Static
and refreshed records share one compact table sorted by premium descending, with unavailable quotes
last. The daily run revalidates the current announcement index for all 25 ETFs, selects the latest
product summary available by the ranking date, and parses the disclosed annualized comprehensive fund
operating expense. An unchanged summary may reuse its parsed cache. A new unreadable summary replaces
the old value with unavailable; only an announcement-index outage may reuse the last parsed value, which
is visibly marked stale. This expense is display-only, is already reflected in fund assets, and excludes
investor-specific brokerage commissions.

JSON schema 12 is the structured source of truth. `records` contains the US main list,
`global_supplement.records` contains the supplement, and `exclusion_summary` records reason counts and
codes. `exchange_premium.records` contains the auxiliary 25-ETF snapshot. Each ranking record has a
`routing_reason`; filters expose `us_main_exclude_keywords` and an empty
`global_exclude_keywords`. CSV and Markdown combine both lists with explicit list and routing fields. `latest.html` and
`public/index.html` are generated from the same payload and must be byte-identical.

## Multi-Asset Valuation Research Page

The independent `/valuation/` route never changes QDII eligibility, routing, ordering, warnings, or
JSON schema. Its schema v2 overview contains three source modes. Snowball supplies current PE, PB,
ten-year source percentiles, ROE, dividend yield, and source rating for NDX, SP500, and GDAXI. The page
does not manufacture a history from these snapshots. The external gold page supplies its USD spot,
1/3/5/10-year and full-history model percentiles, residual, three factor values, freshness dates, and
source ratings. No source charts, backtests, position advice, or trading rules are republished.

The route without an `asset` query parameter is an overview-only comparison table. A valid
`?asset=<id>` opens a detail-only view with a return link and a source-grouped native asset selector.
Navigation preserves unrelated cache-busting and deployment query parameters. An unknown asset ID is
removed and returns to the overview instead of silently opening the configured default asset.

The three research proxies use Nasdaq daily closes and the DQYDJ monthly S&P 500 TTM PE series. For a
target ETF `T`, each model forms `R_m = mean(T)_m / mean(SPY)_m`, calibrates
`K = C_anchor * R_anchor / target_PE_anchor`, and publishes `PE_proxy_m = C_m * R_m / K`. The catalog
anchors are RSP 20.82 for 2025-06, EQWL 24.16 for 2026-03, and EWU 17.57 for 2026-07. The EWU path is
explicitly experimental because EWU tracks MSCI UK rather than FTSE 100, while its anchor comes from an
ISF FTSE 100 snapshot. It has no claimed error interval. RSP retains the observed 5%-20% external
snapshot error disclosure.

Each proxy keeps the latest 120 consecutive ended common months. Its percentile is
`100 * (count below + 0.5 * count equal) / 120`; P30/P50/P70 use inclusive linear interpolation. Proxy
details never contain source or house low/high labels and never present the values as official index PE.

Normalized source caches are separate files under `output/qdii-ranking/cache/index-valuation/` and are
fingerprinted by schema, complete asset-catalog hash, source identity, and parser version. Cold runs
fetch Snowball, gold, DQYDJ, RSP, EQWL, EWU, and SPY concurrently. Hot runs conditionally revalidate
Snowball and gold, fully reparse DQYDJ, and merge roughly three months of each Nasdaq series. The first
fully fresh run in a new calendar month performs all four ten-year Nasdaq scans; a failed scan does not
advance the marker.

A source failure can use only a cache that passes its current fingerprint and structural checks. With
no cache, dependent assets remain visible as `unavailable`; unaffected assets still publish. A cached
dependency marks its assets `cached_stale`. Validation blocks only an all-unavailable page or a broken
artifact contract. Run metrics retain per-source duration, status, bytes, request mode, cache fallback,
and the hot `<10s`/`<300KB` and cold `<15s` warning targets. `latest.html` and
`public/valuation/index.html` must remain byte-identical.
