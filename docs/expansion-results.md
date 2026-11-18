# Weather Edge results

The local app is at [127.0.0.1:8765](http://127.0.0.1:8765/). [Markets](http://127.0.0.1:8765/?view=markets) and [Research](http://127.0.0.1:8765/?view=research) use the retained SQLite catalogs, original weather and market receipts, and immutable local experiment records. [The progress log](expansion-progress.md) follows the November 15 window from 12:07:28 to 19:07:28 EST.

## Cases to open

| Case | What is visible |
| --- | --- |
| [Miami August 29 report and price clocks](http://127.0.0.1:8765/?view=research&kind=models&run=miami-climate-revision-price-clocks-v2-3f428e5efeb9ed7c&case=0) | NWS 90°F at 08:24Z and corrected 85°F at 09:10Z, alongside original trades and native trading close times. The related 90–91°F thresholds paid YES $1 on Kalshi and $0 on Polymarket US. The same weather interval had different resolution-source terms. |
| [Boston HRRR weather and exact payout](http://127.0.0.1:8765/?view=research&kind=models&run=hrrr-intraday-high-v1-c45b48e906377332&case=0) | METAR 52°F lower bound, HRRR remaining peak 48.2°F, NBM remaining peak 54°F, official high 52°F, native below-54 YES $1. Fixed blended probability of the winning interval rose from 14.3% to 89.1% versus the NBM control. |
| [Central Park guarded paper low entry](http://127.0.0.1:8765/?view=research&kind=strategies&run=paper-4ec152245f417c99baae&case=9) | A compatible 41°F exact report reduced the modeled YES chance of at least 42°F from 73.3% to 4.6% at the same weather clock. The original model admitted the delayed $6.56 simulated entry. The guarded arm skipped it on identical books. |
| [November 13 O’Hare measurable rain](http://127.0.0.1:8765/?day=2026-11-13&station=KORD&venue=kalshi&instrument=KXRAIN-26NOV13-CHI&bucket=KXRAIN-26NOV13-CHI&at=2026-11-14T13%3A41%3A00Z) | All 31 GEFS member paths were wet and 3 km HRRR showed 3.58 mm. Both MRMS summaries were zero, the later official gauge was 0.00 in, and the native contract paid YES $0. |
| [November Central Park monthly rain](http://127.0.0.1:8765/?view=markets&venue=kalshi&instrument=KXRAINNYCM-26NOV-1) | The 1-inch threshold paid early after the source crossed. The plot uses original public report editions, including 2.57 inches through November 12. |
| [November Albuquerque weekend rain](http://127.0.0.1:8765/?view=markets&venue=kalshi&instrument=KXRAINWKND-26NOV14-ABQ) | The contract uses two named source reporting dates. The retained November 14 official value was 0.11 in. November 15's formal value and the exchange payout were pending at the early browser check. |
| [New York Weather Index after the target minute](http://127.0.0.1:8765/?view=markets&venue=kalshi&instrument=KXTEMPNYCHS-26NOV1514-T47.99&index_at=2026-11-15T18%3A45%3A00Z) | A 49.64°F index was locally available at the 18:45Z decision. The 19:00 index value of 50.09°F appeared on receipt at 19:06:39.315851Z. Native YES on above 47.99°F paid $1. |
| [Pacific wave forecast and measured buoy](http://127.0.0.1:8765/?view=research&kind=models&run=noaa-wave-lead-coast-v1-808b61e6a1ec87cb&case=0) | The exact 6-, 24- and 48-hour issues open on the globe at the common valid time. Buoy 46029 measured 7.50 m. The 48-hour issue predicted 7.46 m and the 6-hour issue 6.13 m. |
| [Denver 40 km HRRR grid cells](http://127.0.0.1:8765/?view=research&kind=models&run=hrrr-upwind-thermal-gradient-v1-af1432c1c853665f&case=0) | The same hour's two native cells show 64.3°F upwind and 75.1°F downwind. The native Kalshi bracket is linked. A wind term trained in late summer failed to improve on the local forecast over the independent early autumn dates. |
| [Native wave components off the Columbia River](http://127.0.0.1:8765/?view=research&kind=models&run=wave-wind-sea-swell-v1-424f50c7c1a9e12e&case=0) | The six-hour native total was 5.27 m, with wind sea 4.81 m and primary swell 2.15 m. Buoy 46029 measured 5.20 m. Missing components stay blank. |
| [Los Angeles native trading close](http://127.0.0.1:8765/?view=markets&venue=polymarket-us&instrument=tc-temp-laxhigh-2026-11-14-gte69lt70f) | The generic event end was 04:59 UTC and the closed book reported a last trade at 06:26:41 and native close at 08:00:38. The listed market end was 13:00 and the verified payout has its own receipt. |
| [Cincinnati seasonal heating-degree-days](http://127.0.0.1:8765/?view=markets&venue=kalshi&instrument=KXHDDCVG-KCVG-01OCT26-31DEC26-T1500) | The exact active period has 298 confirmed HDD through November 14. The current seven-day NOAA card projects 94 HDD versus 152.3 in the same historical dates. The complete seasonal payout is pending. |
| [Isaias boundary and trades](http://127.0.0.1:8765/?view=research&kind=models&run=isaias-alabama-native-track-v3-1a53db9a2f5fc8ef&case=0) | The original NHC centers show the Florida-to-Alabama crossing between a 90 mph outside reading and a 65 mph inside reading. The exact Kalshi rule requires 74 mph at both ends. Its verified YES payout was $0. |
| [Boston warm-day archive](http://127.0.0.1:8765/?day=2026-08-27&station=KBOS&venue=kalshi&instrument=KXHIGHTBOS-26AUG27-B84.5&bucket=KXHIGHTBOS-26AUG27-B84.5&at=2026-08-28T11%3A12%3A36Z) | Twenty-six original MADIS-timed reports and seven earlier forecast runs. The official high was 85°F and exact native YES paid $1. |
| [Seattle original receipt recovery](http://127.0.0.1:8765/?day=2026-11-01&station=KSEA&venue=kalshi&instrument=KXHIGHTSEA-26NOV01-T52&bucket=KXHIGHTSEA-26NOV01-T52&at=2026-11-02T15%3A09%3A58.499780Z) | The normal replay now shows 25 exact-timed observations, seven earlier forecasts, a complete official report, 144 native display marks and the selected YES $1. The first 46°F observation was measured at 08:53Z and available at the retained MADIS receipt at 08:58Z. The prior replay file is preserved. |
| [Active Dallas Thanksgiving rain contract](http://127.0.0.1:8765/?view=markets&venue=kalshi&instrument=KXRAINTHANKSGIVING-26NOV26-DFW) | The retained 12Z GEFS outlook gives 22 of 31 members with measurable precipitation over the full local reporting date. The reporting threshold, member count and original source clocks are visible. |
| [Albuquerque Thanksgiving boundary range](http://127.0.0.1:8765/?view=markets&venue=kalshi&instrument=KXRAINTHANKSGIVING-26NOV26-ABQ) | Six to nine members reach 0.01 inches because six-hour forecast periods overlap the reporting boundary. The range retains the interval ambiguity. |
| [Washington National low-bound exception](http://127.0.0.1:8765/?view=research&kind=models&run=daily-low-full-receipt-bound-audit-v1-5ff7bb72d64f8c47&case=0) | An exact METAR received just before the February 16 climate close implied at most 35°F. The complete official daily low was 36°F. The case explains why a physical implication remains conditional. |
| [Midway afternoon high at the scheduled decision](http://127.0.0.1:8765/?view=research&kind=models&run=same-book-live-late-high-v2-970884f9c625d40c&case=7) | At 5 p.m. local standard time a 52°F measured floor and the complete remaining-hour traces lifted the 52–53°F model from 30.23% raw NOAA to 92.34% for the guarded HRRR/NBM blend. The first retained market display had a 98¢ YES bid, no offer, and a cached source image. The official high and payout were pending at the comparison. |
| [Midway low forecast before the target day](http://127.0.0.1:8765/?view=research&kind=models&run=daily-low-before-climate-day-v1-0e885e6a41d86f05&case=1) | The forecast decision was November 2 for a November 3 low. The weather link opens the earlier replay and its exact extended NOAA outlook. The native and comparison links open the distinct November 3 target. |
| [Buffalo next extended issue](http://127.0.0.1:8765/?view=research&kind=models&run=nbm-seven-day-station-temperature-v1-8056d770c1b26b13&case=1) | The November 16 forecast opens inside the November 15 live session at its original local receipt. The next day changes to 36–48°F after the bulletin arrives. |
| [Original Midway station receipt clocks](http://127.0.0.1:8765/?view=research&kind=models&run=live-station-receipts-v1-1cf47f81b9d9e358&case=0) | The exact 51.98°F METAR was measured at 20:53Z and first received from AWC at 20:57:03.848752Z. The identical NWS report arrived at 21:50:10.053286Z. In the final sample AWC arrived first on all 82 exact pairs across eight station-days. |
| [North Carolina D4 drought evidence](http://127.0.0.1:8765/?view=research&kind=models&run=us-drought-monitor-native-d4-v2-2aafaab31ea9dbcd) | Weekly U.S. Drought Monitor publication dates and contract reporting weeks are retained. The model score uses disjoint years. Native prices can be inspected separately around the applicable publication. |
| [New York hourly AirNow AQI](http://127.0.0.1:8765/?view=research&kind=models&run=airnow-nyc-hourly-and-daily-aqi-v2-9fb26b5566e5c947) | Hour-specific PM2.5 AQI 66 and the distinct daily AQI 158 demonstrate why using the daily number for the hour reversed eight of thirteen native NO outcomes in the matched example. |
| [Seattle forecast-office rain and late correction](http://127.0.0.1:8765/?view=research&kind=models&run=seattle-wfo-native-rain-v2-a5f83bcab8c21604&case=0) | This older native rule names the forecast office. On October 22, 2021 the first complete office report gave 0.00 in at 08:27Z. Native YES paid $0. The late 0.08-inch correction followed the 14:00Z expiry. Across the cohort the office and airport reports would give different threshold predictions on 59 co-eligible native contracts. |

## Matched model comparisons

Brier scores below are lower-is-better. Venue rows are price proxies restricted to the same markets and timestamps as the corresponding weather run. Counts are shown at the independent day or season level when available.

| Target | Held-out evidence | Result |
| --- | --- | --- |
| Daily high outside the fit stations | 153 station-days, 17 dates, 918 forecasts | NOAA .10384, nonlinear .08332. On 5,305 priced Kalshi strikes across these 153 days, venue midpoint .06239, nonlinear .09921, and NOAA .12466. |
| Warm daily-high transfer | 234 station-days, 26 dates, 1,404 forecasts | NOAA .09930, nonlinear .08718. An independent receipt-timing comparison retained nonlinear lift and showed that assumed receipt timing could overstate it. |
| HRRR afternoon high | 136 station-days, 17 dates, 272 decisions | Guarded NBM .07444, HRRR .06731, equal blend .06915. Pricing subset favored the venue midpoint at both decision times on both exchanges. |
| Independent warm HRRR afternoon high | 80 station-days, 10 dates, 160 decisions | Guarded NBM .08046, HRRR .06296, equal blend .06863. At 17 LST, the two complete HRRR cycles scored .04678 and .04643 under the same observed floor. |
| Scheduled Central Park and Midway lows | 32 station-days, 16 dates, 960 paid bucket scores | NOAA .14452, Ridge .13728, histogram .13200. For 603 matched priced buckets across 31 days, Polymarket US .14212, histogram .15393. |
| Portable physical low bound | 234 warm and 104 autumn station-days, 26 and 13 dates, 1,169 and 516 original-receipt decisions | Shared weather reference .08151 to physical guard .06315 in late summer and .08639 to .07030 in autumn. At two original stations, 87 tighter receipt-only clocks improved .08038 to .07166 with forecast fixed. |
| Daily measurable rain forecast windows | 255 station-days, 277 verified payouts | Same-window P06 .05404, P12 .06158. A learned combination gave no material additional gain. |
| Kalshi monthly measured rain | 423 station-months, 106 months, 12,690 thresholds | Climatology .15407, observed accumulation .10375, added wetness .10146. The later backfill gave 664 coherent native price and payout comparisons across 57 station-months. On those, venue midpoint scored .13814 and the best weather model .16323. |
| Kalshi weekend measurable rain | 145 later paired forecasts, five weekends | Six-hour probability union .06719, 12-hour union .07116, strongest single interval .10688. Friday market display led the six-hour model .07173 to .07638 on 126 same-case prices. |
| Kalshi Chicago monthly snowfall | 139 station-months, nine years, 3,042 thresholds | Climatology .18389, archived weather logistic .12844. Exact O’Hare and Midway native contracts use different station histories and need separate forecast targets. |
| Kalshi hourly NYC Weather Index | 484 later hourly targets, 21 dates, 4,858 payout thresholds | Same-cycle Central Park .10906, eight-airport forecast .09029. With a recent native index value available in the separate short sample, adding the airport projection gave no further lift. |
| Atlantic season storm counts | Nine later seasons and 324 thresholds | Climate .13228 and reference NOAA seasonal outlook .11740 on the archive counts. Season, basin, storm identity and crossing rules are retained. |
| NOAA monthly global temperature | 24 held monthly origins and 132 payout thresholds | Earlier weather fit .13181, ERA5 ten-day partial .11272. On 54 matched priced strikes across 18 months, venue .14422 and weather .18719. Initial NOAA releases remain distinct from the subsequently revised time series. |
| Lake Mead elevation | 70 held months, 1,400 thresholds | Climatology .1156, ridge .0910, corrected agency forecast .0937. Four historical instruments have conflicting native wording and strike direction and are flagged in the evidence. |
| Native wave forecast | 251 matched measurements, 14 buoys, 19 dates | Six-hour .249 m MAE, 24-hour .260 m, 48-hour .283 m. In the common 215 wet-neighbor sample, a strict interpolated six-hour estimate reduced .192 m to .167 m. |
| U.S. Drought Monitor D4 | 331 later forecasts, 120 state-seasons, eight years | Climate .05508, earlier-week logistic .01046. Matching the named report week reproduced 15 of 15 native payouts. |
| New York hourly air quality | 373 original images on 62 later days | Climate .21944, earlier-day Ridge .11394, adding wind and temperature .14939. Exact native price/payout overlap spans one date. |
| Great Lakes surface temperature | 26,040 later threshold and lead decisions from five lakes | Seasonal estimate .13654, same-lake anomaly .11506. Retrieving the original published edition matters since the retrospective smoothing can use future observations. |
| Northeast named storm rainfall | 74 later reporting periods on 37 dates, 444 threshold forecasts | Climate .06311, NBM .04279, empirical correction .03269. Twenty-four native payouts matched the exact source-day totals. Available price comparisons cover one storm. |
| Seattle forecast-office rain | 346 later verified payouts, 338 dates | Correct-source climatology .24248 and two-day wet analog .24597. A post-export backfill found 185 coherent prior-day price and payout pairs. On those, venue midpoint scored .15846 and office climatology .24033. A fitted blend improved the point estimate on 106 later 2022 dates and its paired week interval included zero. |

Several plausible inputs failed their same-case checks. The sampled GOES-19 near-peak observed clouds and surface shortwave retrievals added no stable improvement. Pressure change added no Brier gain across 84 station-days. Autumn HRRR and GEFS accumulation combinations did not beat the simplest rain forecast. Direct rounding of the greater-density MADIS airport temperature exceeded the complete daily high in 35 of 248 later decisions. Whole-Celsius readings appear in a separate chart series with native report, provider receipt, file publication, and local read times.

The recovered original November 1 clocks added 244 observations across nine airports with immutable supplemental records. As a one-date sensitivity on 270 exact paid threshold decisions, the native Brier fell from .12996 raw NOAA to .08451 under the physical guard. Replacing the actual market strikes with synthetic weather bins reversed the comparison. The common weather date and sensitivity to strike placement limit what this evidence can establish about a broader model.

## Reproduce a frozen run

Open the research record and use **Download study JSON** for the exact ID, settings, metrics, and source hashes. **Download frozen evidence** exposes the full test rows or the detailed replay. State the record ID and use the path under `provenance.frozen_inputs` or `provenance.frozen_input` with the module command. These examples read the retained local data.

```sh
uv run --locked python -m weather_edge.research_hrrr_intraday \
  --source .data/research/models/six-hour-high-station-transfer-v1-54f0efcec79d8fe6.json
uv run --locked python -m weather_edge.research_hrrr_market \
  --source .data/research/models/hrrr-intraday-high-v1-c45b48e906377332.json
uv run --locked python -m weather_edge.research_hrrr_stability --source \
  .data/research/models/hrrr-intraday-high-v1-9f23861df5ec8c8f.json \
  .data/research/models/hrrr-intraday-high-v1-c45b48e906377332.json
uv run --locked python -m weather_edge.research_polymarket_close_prices \
  --source .data/research/models/polymarket-temperature-close-clocks-v1-6d53b337f0ede0b8.json
uv run --locked python -m weather_edge.research_wave_leads
uv run --locked python -m weather_edge.expansion_replay --retained-only \
  --stations KPHL KBOS KPVD KORD KDEN KMDW KEWR KTTN --start 2026-08-24 --end 2026-11-13
uv run --locked python -m weather_edge.paper_runs rerun 4988baf9c3305752d65f881c
```

The wave and HRRR modules also accept `--frozen .data/<provenance path>`. Browser model records keep the evaluation split, independent sample counts, exact observations and forecasts, as well as same-case uncertainty where calculated. Earlier papers remain reproducible from their own immutable settings and do not follow a later research shadow's active pointer.

## Read the archive

The two-venue CSV uses one row per native instrument. Each row retains its exact venue identity, series/event references, text rules, settlement source, clocks and payout evidence. Search in [Markets](http://127.0.0.1:8765/?view=markets) by those identities or narrow by venue, family, reporting station or region, date and status. For a manageable paired example, the [Central Park daily low filter](http://127.0.0.1:8765/api/markets/export.csv?place=KNYC&day=2026-11-14&family=daily-low) exported 12 verified rows, six per venue. Long-running catalog discovery and historical imports write incrementally while the live collector watches a bounded set of books. The full export carries a SQLite snapshot cutoff and checksum in its manifest.

The archive keeps original payout records, display prices and trades, and full local book ladders with a separate upstream image clock when present. Public Kalshi REST books in the retained sample supplied local receipt times. Public Polymarket US gateway headers and native image clocks allow a separate origin qualification. A cataloged instrument can have no retained trade or timed book. The manifest gives query cursors, specific transport failures and bounded history gaps.

The final export's SQLite snapshot is November 15 at 6:47:38.010253 p.m. EST, high-water receipt `62608`. `.data/exports/weather-venue-full.csv.gz` has 331,700 rows, 52 columns and seven snapshot files are listed in `.data/exports/weather-venue-manifest.json`. The full file SHA-256 is `35ab947afd40236a61719e9e1e07056aa7208b022760779b296d269e9964e751`.

| Venue | Native instruments | Events | Verified payouts | Indexed market dates |
| --- | ---: | ---: | ---: | --- |
| Kalshi | 325,083 | 41,804 | 323,202 | 2021-08-06 through 2030-12-31 |
| Polymarket US | 6,617 | 1,124 | 6,444 | 2026-01-01 through 2027-01-01 |

The snapshot has 1,819 open, 22 upcoming, 212 closed awaiting payout, one suspended and 329,646 settled instruments. It retains 960,906 Kalshi candles, 77,319 Kalshi trades, 2,763,264 Polymarket US one-minute displays and 218 exact Polymarket US native closure clocks. The local SQLite ladders include 91,385 Kalshi books with zero exchange-image timestamps and 20,935 supplemental Polymarket US books with image times. The separately indexed original Polymarket US high tape has 94,680 books across 3,938 original files at this cut.

All 532 Kalshi weather series were paged to a terminal response across each of the full live, historical, event and multivariate enumerations. The separate recent refresh window is deliberately bounded. All four Polymarket US weather tag flag partitions reached a terminal page. Exact price-history gaps include 26 incomplete Kalshi hourly scopes across current `KXLOWTPERIOD` and `KXSJCLOWT`, and the `GTEMP-24-1.02/23` daily candle HTTP 404 despite retained trades. The Polymarket US New York October 15 low window for 58–59°F returned HTTP 500 and then an empty HTTP 200 on retry. Both original receipts, `55878` and `57962`, remain in the manifest with their requested time window.

## What the $500 replay can establish

Each rule starts with $500, a shared $25 station-day exposure cap across matched highs and lows, and the same chronological tape. Displayed ladder sizes and actual taker fees determine each simulated debit. A delayed book must arrive within 15 seconds of the chosen reaction target. Collateral stays committed until the first verified payout receipt and open positions are held at acquisition cost in the drawdown calculation. No eligible development payout at the split could support selecting between the sweep combinations, so the declared $25 size cap, 30-second delay and $0.25 net expected dollar threshold stayed in place.

The development tape had 57,381 local receipts across five station-days. The legacy policy accepted 18,646, including 11,001 explicit cache hits with zero or missing Age. Origin confirmation accepted 7,480. For the same 1,152 two-sided signal snapshots, the 30-second slot had 1,146 legacy rechecks and 626 origin-confirmed rechecks. At 5, 15, and 45 seconds the strict count was zero, and at 60 seconds it was 544. The typical same-market poll was 15.116 seconds against a 30-second cache lifetime. The repeating availability across delay slots tracks the cache cycle.

The [guarded low case](http://127.0.0.1:8765/?view=research&kind=strategies&run=paper-4ec152245f417c99baae&case=9) shows the most consequential shared-cash difference. An earlier 41°F exact-station reading lowered the modeled 42°F-or-warmer YES probability from 73.3% to 4.6%. The 600-contract naive entry debited $6.56 at the original delayed book and the guard skipped it. The browser shows the exchange image was 5h 1m older than that origin response. A tighter source-age sensitivity removes this class of apparent edge. The most concentrated NYC station-day accounted for 99.77% of the ordinary two-sided strategy's total forecast EV.

The [held-position weather revaluation](http://127.0.0.1:8765/?view=research&kind=models&run=paper-held-weather-scenarios-v1-dcf72d9e8aa6a11d&case=0) holds the three actual simulated quantities and $26.015 cash debit fixed. The original NOAA-based projection is +$363.90. The later clock-eligible guarded weather scenario gives −$16.36, with all three updated probabilities flagged uncertain. NYC's 53–54°F YES dominated the change. There was no verified payout or size-supported exit for these positions at the cutoff.

The [Miami delayed-depth case](http://127.0.0.1:8765/?view=research&kind=strategies&run=latency-be422c0b2d1f07aede3a&case=0) shows a recheck that actually arrived. At 65¢ for NO on 81–82°F and 70.44% forecast NO, the $10 cap could initially use 14.89 units for $0.48936 forecast net EV. Thirty seconds later only 0.01 units priced under the same conditions, leaving $0.00054. Repricing swapped forecasts and full ladders under identical cash controls isolated displayed quantity as the cause. The final origin-confirmed high policy had 84 missing rechecks and one lost edge in 193 signals. Those events need different explanations.

The bounded Midway supplemental watcher saved all 540 expected ladder receipts through 6:15:45 p.m. Of the 360 post-decision responses, all said `HIT`. There were 338 positive Ages, two explicit zeroes and 20 missing values. Four exchange images advanced, but the batch supplied zero origin-confirmed fills. This freezes the 6 p.m. weather-plus-venue comparison even though its ultimate official high and exchange payouts were still pending.

The second Midway watch from 6:40 to 6:47:30 recorded 186 ladders in 31 rounds. Of those, 184 were explicit `HIT` and two `EXPIRED`. The two origin responses carried exchange images 17 minutes 23 seconds and 22 minutes 40 seconds old. Six exchange image clocks advanced. Zero responses passed both the origin and 120-second exchange-image checks.

The [final cross-venue replay](http://127.0.0.1:8765/?view=research&kind=strategies&run=crossvenue-b81d52a9064f2286361a) found four actual differences among 5,439 paired verified payouts. It also found 1,852 neighboring real book receipts across 17 high thresholds and four station-days. Of 2,032 fee-adjusted comparisons at common same-side quantity, 11 were at least $0.25 cheaper on Polymarket US and 14 on Kalshi. The two venues' live Central Park and Midway low partitions used different exact integer intervals. Of 524 opposite-side candidate pairs, none gave even a nominal 25-cent advantage and none had proof of payout equivalence. The native quote comparisons remain visible for shopping.

The [paper strategy report](paper-strategy-research.md) includes the sensitivity grid, frozen reproduction commands, explicit signal and recheck failure reasons, and later recorded evidence. Native hourly Kalshi ladders with verified settlement support payout and quote analysis. By 6:48, 6,530 pre-close book receipts matched 130 paid hourly instruments. Zero had a verified image clock for a timestamp-eligible simulated fill.

### Final forward comparison

[High-only benchmarks](http://127.0.0.1:8765/?view=research&kind=strategies&run=paper-8fbef7b92db36c38a705) and [shared high and low bankroll](http://127.0.0.1:8765/?view=research&kind=strategies&run=paper-01608dbacf2aa00724fc) both end at the same real persisted receipt, 6:49:52.793448 p.m. EST. The first has 38,955 forward receipts across 30 instruments and five station-days, with 3,065 origin-qualified receipts. The shared tape has 56,303 across 42 instruments and five station-days, with 4,781 origin-qualified receipts. Both are one shared calendar date. Eligibility and signal counts refer to receipts, which include repeat observations of an instrument.

| Rule | Signals | Delayed rechecks | Paper fills | Average stake | Peak cash committed | Cash left | Forecast net EV |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| No trade, conservative high-probability NO, or observation trigger | 0 | 0 | 0 | $0 | $0 | $500 | $0 |
| NOAA threshold or any positive after-fee side, origin-confirmed highs | 193 | 109 | 3 | $8.6717 | $26.015 | $473.985 | $363.9048 |
| Any positive after-fee side, legacy freshness highs | 505 | 372 | 3 | $8.6717 | $26.015 | $473.985 | $363.9048 |
| Buffered any side, origin-confirmed highs | 173 | 96 | 2 | $9.6720 | $19.344 | $480.656 | $363.5745 |
| Fixed 10–90¢ range, origin-confirmed highs | 74 | 42 | 2 | $3.8874 | $7.7748 | $492.2252 | $0.8259 |
| Buffered 10–90¢ range, origin-confirmed highs | 54 | 29 | 1 | $1.1038 | $1.1038 | $498.8962 | $0.4956 |
| Shared highs and unguarded lows | 376 | 232 | 4 | $8.1438 | $32.575 | $467.425 | $797.1578 |
| Shared highs and precision-guarded lows | 193 | 109 | 3 | $8.6717 | $26.015 | $473.985 | $363.9048 |
| Native raw NOAA high reference with guarded lows | 889 | 654 | 5 | $10.2030 | $51.015 | $448.985 | $484.7543 |

All rows have zero verified daily payout receipts and zero distinct settled station-days as of the comparison. Realized P&L is $0 in the ledger and maximum drawdown measured at acquisition cost is $0. Three ordinary strict high fills used two station-days. One NYC station-day consumed 70.11% of their cash and supplied 99.77% of their forecast EV. The corrected historical midpoint validation selected zero weather weight. The partial live market check found zero complete actionable origin-confirmed vectors in 4,341 decision-clock checks, so the market-plus-weather variant stayed outside the final executable table.

The [7:03:12 p.m. joint follow-up](http://127.0.0.1:8765/?view=research&kind=strategies&run=paper-b0c030c0533eccdb85b7) retained 1,909 more receipts, reaching 58,212 on the forward tape. It held three high positions and four when unguarded lows shared the balance. Zero daily positions had a verified payout.

```sh
uv run --locked python -m weather_edge.strategy_runs --rerun \
  .data/research/evidence/strategies/8fbef7b92db36c38a70587dbc16a0697da1a51fc91fb00cb88a506e5efd0ffd3.json.gz
uv run --locked python -m weather_edge.strategy_runs --rerun \
  .data/research/evidence/strategies/01608dbacf2aa00724fca7c34029e28363d60d7c9340f1267a1376528d502e0c.json.gz
uv run --locked python -m weather_edge.paper_runs rerun 80bbe75102ddd1206d55a941
```
