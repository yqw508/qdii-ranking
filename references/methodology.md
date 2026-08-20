# QDII Ranking Methodology

## Data Sources

- Fund universe, current scale, inception, purchase state, holder data, NAV history, and announcement
  index come from the public Eastmoney fund endpoints recorded in the generated JSON.
- Current prospectuses, RMB product summaries, quota notices, and periodic reports use the official
  manager disclosures mirrored by the announcement PDF endpoint. Selection never uses a document
  published after the requested ranking date.
- `contract-benchmarks.json` is the versioned benchmark catalog. It records aliases, market scope,
  region, asset class, style, excluded targets, and ordinary/leveraged/inverse/volatility structure.
- `us-equity-instruments.json` contains official-source classifications used for conservative
  fund-of-funds look-through.
- Nasdaq `XNDX` gross total-return history and SAFE USD/CNY central parity produce the CNY Nasdaq-100
  benchmark used by the US main ranking.

The public endpoints are not guaranteed APIs. A ranking-critical fetch or parse failure stops the run;
the updater never silently drops a required evaluation.

## Shared Eligibility

1. Keep purchasable OTC RMB A shares and an explicit RMB primary share without C/D markers. Exclude
   RMB C/D, USD, HKD, back-end shares, and standalone ETFs; eligible feeder funds and LOFs remain.
2. Use the newest holder half-year/year-end period whose fund count reaches 95% of the preceding
   complete period. Skip a partially disclosed newer period with a visible warning.
3. Require scale strictly above CNY 300 million, inception strictly over three years, and trailing
   three-year adjusted return of at least 50%.
4. Keep active and passive/index strategies. Exclude bond and commodity strategies.
5. Parse the latest prospectus disclosed on or before the ranking date. Require one recognized market
   benchmark with at least 80% weight. A missing weight is 100% only when the document names no other
   benchmark. Permit at most 20% cash, deposit, reference-rate, or other low-risk benchmark; a second
   market benchmark is a composite style and is excluded.
6. Cross-check the newest eligible RMB product summary. An explicit index or weight conflict blocks
   publication. A missing or unreadable summary does not override the prospectus and is reported.
7. Exclude a principal benchmark targeting China, Hong Kong, or pan-Asia. A global or multi-market
   benchmark is not excluded merely for containing a minority exposure to these markets.
8. Resolve the manager-level direct-sale quota for every otherwise eligible candidate. Unlimited sale
   qualifies; a known amount must be strictly greater than CNY 1,000. Unknown direct quota is excluded
   with a warning. Agency quota is displayed but does not affect eligibility.

## US Main Ranking

- Accept US equity benchmarks and their leveraged or inverse variants. A US equity product failing the
  US-exposure rule cannot move to the global supplement.
- Read direct US equities from the latest eligible report's country table. For fund-of-funds, classify
  disclosed underlying funds using official sources. Unknown positions contribute zero to the confirmed
  lower bound and their full weight to the possible upper bound; an interval midpoint is never used.
- Require confirmed US-equity exposure of at least 50%. An interval crossing 50% is conservatively
  excluded and reported. A critical report or table parse failure stops the run.
- Rank by three-year CNY Nasdaq-100 weekly-return correlation descending, then `abs(beta - 1)`, confirmed
  US exposure, institutional holding, three-year return descending, and fund code ascending.

## Global Supplement Ranking

- Accept remaining recognized non-US, global, multi-market, REIT, and volatility strategies. Do not
  include candidates already in the US main ranking, bond/commodity strategies, or excluded target
  markets.
- Compute the three-year annualized return from the actual adjusted-NAV start/end dates. Divide it by
  the absolute three-year maximum drawdown. A true zero drawdown stores `null`, displays `∞`, and sorts
  before finite ratios.
- Rank by return/drawdown ratio, three-year return, smaller absolute drawdown, institutional holding,
  scale descending, and fund code ascending.

Each stage scans its complete candidate set before either list is truncated to ten. One list may be
empty or shorter than ten without relaxing thresholds; both lists empty stops publication.

## Performance And Nasdaq Fit

- Use the latest NAV on or before the ranking date and the latest NAV on or before the corresponding
  date one or three years earlier. Build adjusted wealth from consecutive NAVs, cash distributions, and
  explicit source adjustment events. Maximum drawdown is peak-to-trough and stored as non-positive.
- Convert XNDX to CNY by multiplying each USD index level by the latest USD/CNY central parity on or
  before that date. Neither source may be carried forward more than seven days; future matching is
  forbidden.
- Take the final adjusted fund wealth observation in each ISO week over three years and form returns
  only across adjacent ISO weeks. Require at least 140 paired returns spanning at least 1,000 days.
- Correlation is Pearson correlation, beta is sample covariance divided by benchmark sample variance,
  and tracking error is the sample standard deviation of weekly active returns times `sqrt(52)`.

## Quota Precedence

Process relevant manager notices chronologically and apply transitions effective on or before the
ranking date. A later transition overrides an earlier one for the same channel.

- A direct-sale amount updates direct sale only; an agency amount updates agency sale only.
- A notice without channel distinction updates both. `全部销售机构累计` is one shared ceiling and must
  not be added across channels.
- A restoration notice sets `unlimited` only when it contains no replacement ceiling. Future automatic
  restoration is a separate transition.
- Unreadable or ambiguous active notices make the affected quota unknown. Never infer a value.

## Cache And Output

NAV calculations, benchmark history, fund exposure, legal documents, periodic reports, and underlying
classifications are cached with source dates and catalog fingerprints. PDF cache hits must still be valid
PDFs with extractable text. Historical runs never consume a future NAV, benchmark point, allocation, or
announcement.

JSON schema 8 is the structured source of truth. `records` contains the US main list,
`global_supplement.records` contains the supplement, and `exclusion_summary` records reason counts and
codes. CSV and Markdown combine both lists with an explicit list field. `latest.html` and
`public/index.html` are generated from the same payload and must be byte-identical.
