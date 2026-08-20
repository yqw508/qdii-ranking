# QDII Ranking Repository Instructions

## Scope

- Treat this repository as the implementation and operating source of truth for the QDII US main
  ranking and global supplement ranking.
- Accept both active and passive/index QDII strategies when the contract benchmark is sufficiently
  explicit. Exclude standalone exchange-traded ETF shares; keep eligible OTC RMB feeder shares.
- Exclude bond and commodity strategies from both rankings. The global supplement may include equity,
  REIT, volatility, leveraged, and inverse strategies when all other rules pass.
- Treat requests to analyze or verify one fund as read-only. Do not change ranking rules, generated
  output, Git state, or CloudBase state unless the user also asks for an update or a fix.

## Established Ranking Rules

- Keep purchasable China-offered OTC RMB A shares, including an explicit RMB primary share without a
  C/D marker. Exclude RMB C/D, USD, HKD, back-end, and standalone ETF shares.
- Require scale strictly above CNY 300 million, inception strictly more than three years before the
  ranking date, trailing three-year adjusted return of at least 50%, and a known direct-sale quota
  strictly above CNY 1,000. Unlimited direct sale qualifies; exactly CNY 1,000 does not.
- Require the latest prospectus disclosed by the ranking date to contain one recognized principal
  market benchmark weighted at least 80%. Allow no more than 20% cash, deposit, or low-risk benchmark.
  Treat a second market benchmark as a composite style and exclude it. Cross-check the RMB product
  summary; an explicit conflict blocks publication, while a missing or unreadable summary is a warning.
- Exclude benchmarks targeting China, Hong Kong, or pan-Asia. Do not exclude a global or multi-market
  benchmark merely because it contains a minority allocation to those markets.
- Put US equity benchmarks, including leveraged or inverse variants, in the US main ranking. Require a
  conservatively confirmed US-equity exposure of at least 50%; a failed US-equity product cannot move
  to the global supplement. Unresolved positions only increase the possible upper bound.
- Put the remaining eligible non-US, global, multi-market, REIT, and volatility strategies in the
  global supplement. Keep bond and commodity strategies excluded.
- Rank the US main list by Nasdaq-100 correlation descending, beta distance from 1 ascending, confirmed
  US-equity exposure, institutional holding, trailing three-year return descending, and fund code.
- Rank the global supplement by three-year annualized-return/absolute-maximum-drawdown ratio, then
  three-year return, smaller drawdown, institutional holding, scale descending, and fund code. A true
  zero drawdown is displayed as infinity and sorts before finite scores.
- Evaluate performance, contract benchmark, US exposure, and direct quota for every candidate reaching
  that stage. Apply the ten-fund limit only after full scans. Either list may contain 0-10 funds; both
  lists empty blocks publication.
- Keep the 95% holder-period completeness rule. Never infer an unknown subscription quota; preserve
  manager-announcement channel and share aggregation rules and report exclusions as warnings.

## Update And Publish

When the user says `更新榜单` or otherwise requests a ranking refresh from this repository, treat it as
authorization to complete the full update and publication workflow:

1. Load the bundled workspace dependencies and use their Python executable.
2. Run `scripts/update_qdii_ranking.py` from the repository root with the established defaults. It must
   write `output/qdii-ranking/latest.json`, `latest.csv`, `latest.md`, `latest.html`, and the identical
   `public/index.html`.
3. Read `latest.json` and `latest.md`; inspect every warning. Stop without publishing if ranking-critical
   source data fails, a required candidate cannot be evaluated, or generated formats disagree.
4. Run `python -m unittest discover -s scripts -p "test_*.py"` with the bundled Python runtime.
5. Run `scripts/validate_qdii_ranking.py`. Verify full-scan counters, both final orderings, benchmark and
   quota sources, the ranking date, and byte-identical HTML files.
6. Stage only intended repository changes, commit them, and push `main` to `origin`.
7. Deploy the verified static page with:

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

8. Verify the independent application URL returns HTTP 200 and contains the ranking date, every fund
   code in both lists, and the corresponding direct and agency quotas. Share:
   `https://qdii-ranking-web-run-cool-d2gy0iw957219659c.webapps.tcloudbase.com/?v=<YYYY-MM-DD>`.

Do not deploy a partial or warning-blind result. Explain material unresolved look-through intervals,
contract exclusions, and quota warnings in the final report. Read `references/methodology.md` when a
source changes, a report period is skipped, or parsing becomes unresolved.
