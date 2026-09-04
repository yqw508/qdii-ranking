# QDII Ranking Repository Instructions

## Scope

- Treat this repository as the implementation and operating source of truth for the QDII US main
  ranking and global supplement ranking.
- Limit candidates to QDII and `指数型-海外股票`; do not accept ordinary domestic equity, mixed, or
  index funds. Accept both active and passive/index strategies within that scope. Contract benchmark metadata is display-only
  and never an eligibility or routing gate. Exclude standalone exchange-traded ETF shares; keep
  eligible OTC RMB feeder shares.
- Exclude bond and commodity strategies from both rankings. The global supplement may include equity,
  REIT, volatility, leveraged, and inverse strategies when all other rules pass.
- Treat requests to analyze or verify one fund as read-only. Do not change ranking rules, generated
  output, Git state, or CloudBase state unless the user also asks for an update or a fix.

## Established Ranking Rules

- Keep purchasable China-offered OTC RMB A shares, including an explicit RMB primary share without a
  C/D marker. Exclude RMB C/D, USD, HKD, back-end, and standalone ETF shares.
- Do not impose a fund-scale eligibility threshold. Require inception strictly more than three years
  before the ranking date and trailing three-year adjusted return of at least 30%. When a complete
  five-year return exists, require at least 50%; when a complete ten-year return exists, require at
  least 100%. Missing five- or ten-year history bypasses only that corresponding threshold. Require a known
  direct-sale quota of at least CNY 200; unlimited direct sale and exactly CNY 200 both qualify.
- Parse the latest prospectus benchmark for display. Keep recognized, composite, unrecognized, and
  unreadable states. Benchmark identity, market, country/region, asset class, structure, weight,
  conflicts, and parse status must never exclude or route a fund. Cross-check the latest RMB product
  summary and report conflicts as warnings.
- Apply the established China, Hong Kong, and pan-Asia keywords only to US-main routing. A matching
  fund remains eligible and routes to the global supplement regardless of confirmed US exposure. Never
  infer geography routing from a contract benchmark; the global supplement has no geography exclusion.
- Evaluate conservative US-equity exposure for every three-year-performance-qualified candidate in
  the fixed type scope. Put confirmed exposure at or above 50% in the US main ranking only when the
  name does not match a geography keyword; route keyword matches and all remaining candidates to the
  global supplement. A crossing interval follows its
  confirmed lower bound and must remain visible. Unresolved positions only increase the possible upper
  bound; a critical report or holding-table parse failure blocks publication.
- Rank the US main list by Nasdaq-100 correlation descending, beta distance from 1 ascending, confirmed
  US-equity exposure, institutional holding, trailing three-year return descending, and fund code.
- Rank the global supplement by three-year annualized-return/absolute-maximum-drawdown ratio, then
  three-year return, smaller drawdown, institutional holding, scale descending, and fund code. A true
  zero drawdown is displayed as infinity and sorts before finite scores.
- Evaluate performance, contract benchmark, US exposure, and direct quota for every candidate reaching
  that stage. Apply the ten-fund limit only after full scans. Either list may contain 0-10 funds; both
  lists empty blocks publication.
- Keep the 95% holder-period completeness rule. Never infer an unknown subscription quota; preserve
  manager-announcement channel and share aggregation rules and report exclusions as warnings. A quota
  notice selected by title but yielding no effective transition makes the quota unresolved; never keep
  an older limit in that case.
- Calculate five- and ten-year adjusted returns from the existing NAV history when complete and apply
  their conditional eligibility thresholds without using them for ranking. Parse the official
  annualized comprehensive fund operating expense from the latest RMB product summary. Missing long
  history bypasses its threshold; missing fee data is reported but does not affect eligibility, and
  NAV returns are not reduced by the fee a second time.

## Index Valuation Research Page

- Keep `/valuation/` and its schema independent from QDII ranking rules. The overview contains exactly
  three Snowball direct snapshots, three research proxies, and one external gold-model snapshot. Show
  direct/external ratings only as source ratings; never apply those labels to research proxies.
- Keep `/valuation/` overview-only when no asset is requested. A valid `?asset=<id>` opens a
  detail-only view with a return link and source-grouped selector; preserve unrelated query parameters
  and route unknown asset IDs back to the overview.
- Build every proxy from the latest 120 consecutive ended common months using DQYDJ S&P 500 PE and a
  target ETF/SPY monthly mean-price ratio. Preserve the versioned RSP, EQWL, and experimental EWU
  anchors in `references/index-valuation-catalog.json`; disclose that EWU does not track FTSE 100.
- Persist only per-source normalized caches under `output/qdii-ranking/cache/index-valuation/`.
  Revalidate Snowball and gold conditionally, DQYDJ in full, and all four Nasdaq tails every run. The
  first fully fresh run each month performs four full ten-year scans. Catalog or parser changes
  invalidate affected caches.
- A failed source may use only its validated current-fingerprint cache. Without one, keep dependent
  assets visible as unavailable and publish the remaining assets. Block valuation publication only
  when all assets are unavailable or artifact validation fails. Performance targets warn but never
  bypass structural validation.

## Update And Publish

When the user says `更新榜单` or otherwise requests a ranking refresh from this repository, treat it as
authorization to complete the full update and publication workflow:

1. Load the bundled workspace dependencies and use their Python executable.
2. Run `scripts/update_qdii_ranking.py` from the repository root with the established defaults. It must
   write `output/qdii-ranking/latest.json`, `latest.csv`, `latest.md`, `latest.html`, and the identical
   `public/index.html`.
3. Run `scripts/update_index_valuation.py` with its established defaults. It must write
   `output/index-valuation/latest.json`, `latest.html`, `run-metrics.json`, and the identical
   `public/valuation/index.html`.
4. Read the ranking `latest.json` and `latest.md` and the valuation `latest.json`; inspect every warning.
   Stop without publishing if ranking-critical
   source data fails, a required candidate cannot be evaluated, or generated formats disagree.
5. Run `python -m unittest discover -s scripts -p "test_*.py"` with the bundled Python runtime.
6. Run `node --test scripts/test_premium_refresh.mjs` and
   `node --test scripts/test_valuation_page.mjs`, then both `scripts/validate_qdii_ranking.py` and
   `scripts/validate_index_valuation.py`. Verify
   full-scan counters, both final orderings, the complete discovered listed-QDII premium snapshot and holding costs, benchmark and quota sources,
   the ranking date, all seven valuation asset IDs, proxy models/sample counts, and both pairs of
   byte-identical HTML files.
7. Stage only intended repository changes, including both generated public pages, commit them, and push
   `main` to `origin`.
8. Deploy the verified static page with:

   ```powershell
   tcb app deploy qdii-ranking-web `
     --env-id run-cool-d2gy0iw957219659c `
     --framework static `
     --output-dir public `
     --deploy-path /qdii `
     --cwd . `
     --force `
     --json
   ```

9. Verify the independent application root URL and `/valuation/` both return HTTP 200. Check the root
   contains the ranking date, every fund
   code in both lists, all discovered premium-tab QDII codes and holding costs, and the corresponding direct and agency quotas. Share:
   `https://qdii-ranking-web-run-cool-d2gy0iw957219659c.webapps.tcloudbase.com/?v=<YYYY-MM-DD>` and
   verify the valuation page contains all seven asset IDs, the default asset, source modes, proxy
   models, values, and fresh/stale/unavailable states.

Do not deploy a partial or warning-blind result. Explain material unresolved look-through intervals,
contract/fee warnings, and quota warnings in the final report. Read `references/methodology.md` when a
source changes, a report period is skipped, or parsing becomes unresolved.

The scheduled workflow runs daily at 07:07 Asia/Shanghai. Cache reuse must include a same-run source
revalidation: conditional NAV checks and one current announcement-index check per performance-qualified
fund. Never trade ranking freshness for runtime. Inspect `run-metrics.json` and the Actions performance
comparison when update latency changes materially.
