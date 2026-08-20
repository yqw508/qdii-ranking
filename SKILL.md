---
name: update-qdii-ranking
description: Update and report a China-offered OTC QDII fund ranking led by conservatively confirmed US-equity exposure, with institutional holding ratios as a reference, plus current fund scale, fund age, trailing one-year and three-year returns and drawdowns, purchase availability, and direct-versus-agency subscription limits. Use when the user asks to update a QDII ranking, find institutionally held QDII funds, screen purchasable RMB shares, compare US exposure, performance, or subscription quotas, or refresh the previously defined QDII leaderboard.
---

# Update QDII Ranking

Generate the ranking with the bundled script. Treat its structured output as the source of truth and
surface any warnings instead of filling missing quota data by inference.

## Run The Update

1. Call `codex_app__load_workspace_dependencies` to locate the bundled Python runtime.
2. Run `scripts/update_qdii_ranking.py` with that Python executable from the user's intended output
   working directory.
3. Use no flags for the established defaults: keep funds established for more than three years, exclude
   pure-debt funds and names containing `债 亚洲 中国 港`, require scale above CNY 300 million, keep
   RMB A shares (including an explicit RMB primary share without a C/D marker) and current purchasable
   funds only, require trailing three-year adjusted return of at least 50%, require a conservatively
   confirmed US-equity exposure of at least 50%. Evaluate performance for the complete base-qualified
   pool, evaluate US exposure for every performance-qualified fund, then rank by confirmed US-equity
   exposure and limit the output to 10. Use institutional holding only as the first tie-break reference.
4. Pass user changes through the supported flags rather than editing the script.
5. Read `output/qdii-ranking/latest.json` and `latest.md` after the run. Use the self-contained
   `latest.html` for local browser viewing. Publish the identical `public/index.html` through HTTPS
   when the user needs a link that opens inside WeChat.
6. Check every warning. For an `unknown` quota, open the linked notice and report it as unresolved unless
   the notice unambiguously supplies the missing value.

Use this command shape:

```powershell
& <bundled-python> <skill-dir>\scripts\update_qdii_ranking.py `
  --top 10 `
  --min-scale 3 `
  --min-age-years 3 `
  --min-three-year-return-pct 50 `
  --min-us-equity-pct 50 `
  --exclude-keywords 债 亚洲 中国 港 `
  --output-dir .\output\qdii-ranking `
  --publish-dir .\public
```

Optional flags:

- `--as-of YYYY-MM-DD`: evaluate announcement transitions on a historical date.
- `--min-age-years N`: require inception strictly earlier than N years before the as-of date.
- `--min-three-year-return-pct N`: require trailing three-year adjusted return of at least N percent.
- `--min-us-equity-pct N`: require the confirmed lower bound of US-equity exposure to be at least N
  percent; the default is 50.
- `--cache-dir <path>`: choose the persistent performance, fund-exposure, periodic-report, and
  look-through cache directory.
- `--allow-partial-holder-period`: deliberately use an incompletely disclosed newest holder period.
- `--output-dir <path>`: choose another output location.
- `--publish-dir <path>`: choose the static-site output directory; `index.html` is written here and
  defaults to `./public`.

## Present The Result

- Lead with the resulting US-equity exposure ranking, including fund code, inception date,
  institutional ratio, scale,
  trailing one-year and three-year returns and maximum drawdowns, confirmed US-equity exposure, direct
  quota, and agency quota.
- State the holder report date, scale report dates, performance observation dates, and quota as-of date
  separately.
- Say when fewer than 10 funds qualify; never relax filters automatically.
- Explain that `正常开放` means no manager-level cap was found and that an agency may impose a lower
  operational limit.
- Explain all-channel or A/C aggregation rules when present so limits are not incorrectly added.
- Link the fund announcement used for each limited quota.
- Link the periodic report used for each US-equity exposure value. Explain ambiguous intervals and
  unresolved underlying positions instead of presenting them as point estimates.
- For mobile delivery, use the HTTPS deployment of `public/index.html`. It uses a compact summary with
  expandable fund details and contains no external runtime assets; only source-document links require
  network access. Keep `latest.html` as the identical local copy. CloudBase default domains show a
  mandatory first-visit risk notice; use an ICP-filed custom domain when the WeChat link must open the
  ranking without that intermediate confirmation.

## Troubleshoot

Read [references/methodology.md](references/methodology.md) when an endpoint changes, a report period is
skipped, or a quota is marked `unknown`. Keep the default 95% completeness threshold and source
precedence unless the user explicitly changes the methodology.

Do not substitute web-search snippets for a failed ranking-critical endpoint. Stop and report the source
failure. Quota parsing may degrade to `unknown` because it is explicitly designed not to guess.
