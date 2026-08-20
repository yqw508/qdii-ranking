# QDII Ranking Repository Instructions

## Scope

- Treat this repository as the implementation and operating source of truth for the QDII US-equity ranking.
- Keep the ranking focused on actively managed QDII funds. Do not broaden the accepted fund types to
  `指数型-海外股票` or other index/passive categories unless the user explicitly changes the scope.
- Treat requests to analyze or verify one fund as read-only. Do not change ranking rules, generated
  output, Git state, or CloudBase state unless the user also asks for an update or a fix.

## Established Ranking Rules

- Keep purchasable China-offered OTC RMB A shares, including an explicit RMB primary share without a
  C/D marker. Exclude RMB C/D, USD, HKD, back-end, and pure-debt shares.
- Require scale strictly above CNY 300 million and inception strictly more than three years before the
  ranking date.
- Exclude names containing `债`, `亚洲`, `中国`, or `港` under the established defaults.
- Require trailing three-year adjusted return of at least 50%.
- Require conservatively confirmed US-equity exposure of at least 50%. Unresolved positions may only
  increase the possible upper bound; never use an interval midpoint.
- Evaluate performance for every base-qualified candidate and US exposure for every
  performance-qualified candidate. Do not stop after finding ten funds.
- Rank by confirmed US-equity exposure descending, then institutional holding descending, trailing
  three-year return descending, and fund code ascending. Output the first ten without relaxing filters.
- Keep the 95% holder-period completeness rule. Do not use a partially disclosed newer period unless
  the user explicitly requests it.
- Never infer an unknown subscription quota. Use parsed manager announcements, preserve channel and
  share aggregation rules, and surface unresolved values as warnings.

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
5. Verify `output/qdii-ranking/latest.html` and `public/index.html` are byte-identical. Check the full-scan
   counters, final ordering, quota sources, and ranking date.
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

8. Verify the independent application URL returns HTTP 200, contains the ranking date and ten fund
   records, and matches key corrected quota values. Share the root URL with a date query parameter:
   `https://qdii-ranking-web-run-cool-d2gy0iw957219659c.webapps.tcloudbase.com/?v=<YYYY-MM-DD>`.

Do not deploy a partial or warning-blind result. Explain material unresolved look-through intervals and
quota warnings in the final report. Read `references/methodology.md` when a source changes, a report
period is skipped, or quota parsing becomes unresolved.
