# QDII Ranking Methodology

## Data Sources

- Fund universe: `https://fund.eastmoney.com/js/fundcode_search.js`
- Holder-period summary and detail: `FundDataPortfolio_Interface.aspx`, `dt=11` and `dt=10`
- Current scale and purchase state: `https://fund.eastmoney.com/{code}.html`
- Inception date: `https://fund.eastmoney.com/{code}.html`
- Net-worth trend: `https://fund.eastmoney.com/pingzhongdata/{code}.js`
- Announcement index: `https://api.fund.eastmoney.com/f10/JJGG`
- Announcement PDF: `https://pdf.dfcfw.com/pdf/H2_{announcement_id}_1.pdf`
- Official issuer product pages recorded in `us-equity-instruments.json` for underlying-fund
  classification and dated global-fund allocations

Treat these endpoints as public but undocumented. Stop the run when a ranking-critical response
cannot be parsed. Never silently drop a failed page or fund.

## Ranking Rules

1. Keep QDII RMB A shares. Treat an explicitly RMB-denominated primary share without an A/C suffix as
   A-equivalent. Exclude USD, HKD, back-end fee shares, C/D shares, and standalone ETFs.
2. Use the newest holder period whose fund count reaches 95% of the immediately preceding complete
   half-year or year-end period. This prevents early disclosure data from being treated as complete.
3. Exclude the `QDII-纯债` category and names containing `债` before ranking.
4. Require current fund scale to be strictly greater than the configured threshold.
5. Require the inception date to be strictly earlier than three years before the requested as-of date.
6. Keep open and limited-purchase funds; remove suspended and unknown purchase states.
7. Apply the established geographic exclusions `亚洲`, `中国`, and `港` before limiting the result.
8. Calculate trailing performance for every base-qualified candidate using six bounded workers. Require
   the three-year adjusted return to meet or exceed the configured threshold. Any failed candidate
   calculation stops the update.
9. For every performance-qualified candidate, parse the latest quarterly, semiannual, or annual report
   disclosed on or before the requested as-of date. Require the conservatively confirmed US-equity
   exposure to meet or exceed the configured threshold.
10. Rank all fully qualified candidates by confirmed US-equity exposure descending, institutional
    holding ratio descending, three-year adjusted return descending, and fund code ascending. Apply the
    configured top count only after this full scan; resolve subscription quotas only for the final list.

## US-Equity Exposure

- Count equities and depositary receipts listed in the United States. Do not count US bonds merely
  because they trade in the United States.
- Read direct US holdings from the report's country distribution as a percentage of fund net assets.
- For fund-of-funds, read total fund investments and the top underlying funds. US-specific equity funds
  contribute their full disclosed weight; fixed income, commodities, and non-US regional funds
  contribute zero.
- A global equity fund may use a numeric US allocation only when it comes from the official issuer, is
  dated on or before the parent fund report date, and is no more than 120 days old. Structural US-only,
  non-US, fixed-income, and commodity classifications do not expire but must retain an official source.
- Unknown underlying funds and the positive residual between total fund investments and disclosed
  top-fund weights contribute zero to the confirmed lower bound and their full weight to the possible
  upper bound.
- `confirmed_pct >= threshold` qualifies. `possible_pct < threshold` is excluded. An interval crossing
  the threshold is also excluded and emits a warning; no midpoint is calculated.
- Failure to read the selected periodic report or a critical holdings table stops the update. Failure to
  classify an underlying fund does not stop the update because the conservative interval preserves the
  uncertainty explicitly.

Performance results are cached by fund code and as-of date. Fund-level exposure calculations are cached
by fund code, announcement ID, report period, calculation-method version, and underlying-catalog
fingerprint; threshold status is reapplied at runtime. Periodic report PDFs are cached by announcement
ID. Every PDF cache hit is checked for a valid PDF and extractable text; a damaged entry is downloaded
again. Underlying classifications are cached by normalized instrument name, parent report date, and
catalog fingerprint. Damaged or incompatible calculated caches are rebuilt. Historical runs never use
announcements, NAV observations, or numeric allocations dated after the requested as-of/report date.

## Performance Metrics

- End the trailing period on the latest published NAV date on or before the requested as-of date.
- Build separate one-year and three-year windows, starting from the latest published NAV date on or
  before the same calendar date one or three years earlier.
- Build an adjusted wealth index from consecutive unit NAVs. Add per-share cash distributions to the
  ex-dividend NAV; for other explicitly marked net-worth events, use the source's daily adjusted return.
- Trailing return is the adjusted wealth change from each window's start observation to the end.
- Maximum drawdown is the largest peak-to-trough decline in that adjusted wealth index and is stored as
  a non-positive percentage.
- Preserve the actual start and end NAV observation dates for both windows. If a fund lacks a complete
  window, keep that window's metrics null and emit a warning rather than annualizing partial history.

## Quota Precedence

Process relevant announcements chronologically and apply only transitions effective on or before the
requested as-of date. A later effective transition overrides an earlier value for the same channel.

- An explicit direct-sale amount updates only direct sale.
- An explicit agency amount updates only agency sale.
- A notice without a channel distinction updates both channels.
- `全部销售机构累计` means the displayed channel values share one cross-channel ceiling and must not
  be added together.
- A notice that restores unrestricted large subscriptions sets both channels to `unlimited` only when
  it contains no replacement ceiling.
- An automatic future restoration inside a notice becomes a separate transition.

Use `unknown` when the PDF cannot be read, the wording is ambiguous, or a limited fund has no confirmed
manager-level direct quota. Preserve the source link and tell the user to inspect it.

## Output Semantics

- `unlimited` means no active manager-level large-subscription ceiling was found. A distributor may
  still enforce a lower transaction or payment limit.
- `limited` is a per-day, per-fund-account ceiling and normally includes regular subscription and SIP.
- `share_class_rule` records whether A and C are combined or separate.
- `channel_rule` records whether direct and agency channels differ or share an all-channel ceiling.
- Dates for holder data, inception, scale, performance observations, quota effectiveness, and the run
  must always remain visible.
- `us_equity_exposure.confirmed_pct` is the ranking value. `possible_pct`, `unresolved_pct`, components,
  report date, announcement date, and source URL preserve the calculation audit trail.
- `latest.html` is a self-contained mobile presentation generated from the same payload as JSON, CSV,
  and Markdown. It does not replace JSON as the structured source of truth.
