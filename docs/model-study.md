# Three-station forecast study

Run `uv run python -m weather_edge.study` from the repository root. The command reuses the frozen weather inputs under `.data/study/v1/`, fits the candidates, applies the pre-test selection rule, and refreshes the report with the settled market archives present locally. The original Central Park benchmark is still available through `uv run python -m weather_edge.model` and `docs/model.md`.

The study directory retains source CSV and JSON responses, decoded NOAA station cards, `inputs.json.gz`, `selected.joblib`, and `report.json`. Each fit also preserves the exact joblib and frozen weather input under `.data/study/v1/vintages/<model SHA-256>/`. Within that vintage, `evaluations/<report SHA-256>/` retains the report and the matched input events in `market-inputs.json.gz`. The report records the weather-input digest, market-input digest, individual market source paths and digests, scoring code digests, and scikit-learn version. A rerun with identical bytes reuses its snapshot. The first snapshot-aware run archives the preceding model and report as they existed. Use `study.load_selected(vintage="<model SHA-256>")` to retrieve a saved local artifact. Treat these generated joblib files as trusted local artifacts.

Inference returns `vintage` as the full model SHA-256. The separate `version` field identifies the pre-test or operational role. Older saved paper runs retain their original frozen probabilities and method/version metadata.

## Evidence and timing

The settlement stations are Central Park (`KNYC`), Miami International (`KMIA`) and Los Angeles International (`KLAX`). Targets are published integer daily highs from the complete NWS daily climate reports. Climate days use midnight-to-midnight fixed local standard time, UTC−5 for New York and Miami and UTC−8 for Los Angeles. Each decision uses the latest available NOAA short-range `TXN` guidance for the climate day. The NBM `TXN` is an 18-hour forecast stamped at 00 UTC. The official daily high covers the full climate day. Flash updates to observations or products enter only after their recorded availability. [NOAA NBM station card](https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0), [IEM NWS climate report archive](https://mesonet.agron.iastate.edu/json/cli.py?station=KNYC&year=2026)

IEM routine and special ASOS weather-only tests use a ten-minute report-time lag. Those bulk rows lack original receipt time. The sensitivity table holds the fitted models fixed and changes test-time availability to zero and 30 minutes. The matched market replay uses archived MADIS receipt times where verified and leaves unmatched readings out. The delayed one-minute ASOS trace serves retrospective research. [IEM ASOS](https://mesonet.agron.iastate.edu/ASOS/), [NOAA MADIS METAR](https://madis.ncep.noaa.gov/madis_metar.shtml)

| Split | Inclusive 2026 dates | Calendar dates | Station-days | Snapshots |
| --- | --- | ---: | ---: | ---: |
| Fit | June 1–Sept. 26 | 118 | 354 | 2,124 |
| Probability calibration | Sept. 28–Oct. 13 | 16 | 48 | 288 |
| Pre-test selection | Oct. 15–27 | 13 | 39 | 234 |
| Later test | Oct. 29–Nov. 13 | 16 | 48 | 288 |

The dates and scoring rule were fixed before the expanded study. The original KNYC benchmark overlaps the later test. Its previously reported result was already known. Each partition holds all stations and snapshots for a date. The six snapshots are 21:00 on the prior climate day, then 03:00, 09:00, 13:00, 17:00, and 21:00 local standard time on the target date. The gap days allow completed CLI labels to arrive before the next partition's earliest decision. The June 17 00 UTC NOAA NBH/NBS run was absent for all three stations. Earlier eligible forecasts supplied the affected decisions, and all planned snapshots were available. The test spans NOAA's Nov. 3 change to NBM 5.1. [NOAA NBM version history](https://vlab.noaa.gov/web/mdl/nbm-documentation)

## Candidates and results

- **NOAA TXN** uses the published `TXN` point and `XND` spread in a Gaussian. A missing `XND` uses the station and time band spread measured during earlier calibration.
- **Station/time bias** adds the average fit-period NOAA error for the settlement station and decision-time band. Each fit date contributes an average residual within its station and band.
- **Ridge** fits the official high minus NOAA `TXN` with station, NOAA lead, time of day, recent observed high, and the available observation divergence and trend. The scikit-learn pipeline standardizes the inputs and fits fixed α = 10.
- **Ridge + forecast sky** adds one physical feature, NBM hourly sky cover near the warmest forecast hour. It averages available `SKY` fields within one hour of that peak, with a missingness indicator. Cloud cover influences daytime heating and can flag coastal cloud regimes. Its fitted standardized coefficient was −0.033°F per standard deviation, alongside +1.817°F for observed high relative to the forecast. These are conditional regression associations. [NOAA NBM sky-cover guidance](https://vlab.noaa.gov/web/mdl/nbm-textcard-v5.0)

The corrected candidates use the shape of station and time band calibration residuals, one averaged centered value per prior validation date and a 0.5°F smoothing scale. Bucket boundaries extend half a degree beyond inclusive integer endpoints. No-side probabilities are the exact complements of YES. A gap in a quoted event's buckets retains unrepresented mass. In 1,152 exhaustive test predictions, the largest absolute difference between total mass and one was 1.11 × 10⁻¹⁶. Limited-support flags use the number of archived residuals on either side of each outcome.

The pre-test selection minimized Brier equally weighted by station-day. Brier uses seven benchmark bins fixed for each station-day around its earliest scheduled NOAA forecast. Those splits include two tails and five two-degree interior ranges. The same partition applies to all candidates and all six decisions. The test contains 2,016 benchmark bucket cases per method and 16 calendar dates. Weather from neighboring dates and snapshots within a date are related.

| Forecast | Selection Brier | Test Brier | Test MAE | Test RMSE |
| --- | ---: | ---: | ---: | ---: |
| NOAA TXN | 0.10542 | **0.08590** | **0.965°F** | **1.394°F** |
| Station/time bias | 0.11250 | 0.11467 | 1.198°F | 1.633°F |
| Ridge | 0.10073 | 0.09552 | 1.063°F | 1.425°F |
| Ridge + forecast sky | **0.10038** | 0.09561 | 1.065°F | 1.424°F |

Sky data were present for all 288 paired test snapshots. The matched ablation slightly worsened Brier by 0.00009 and MAE by 0.002°F. At KLAX on Nov. 8, the sky-augmented model predicted 82.94°F at 03:00 local standard time and the official high was 88°F. The raw NOAA's largest misses were 4°F cold in the prior-evening forecasts at KNYC Nov. 6 and KMIA Nov. 2. The report includes methods, per-station and time-band metrics, source gaps, and all decision errors.

| Test observation lag | Raw NOAA Brier | Ridge Brier | Ridge + sky Brier |
| --- | ---: | ---: | ---: |
| 0 minutes | 0.08590 | 0.09396 | 0.09418 |
| 10 minutes | 0.08590 | 0.09552 | 0.09561 |
| 30 minutes | 0.08590 | 0.09586 | 0.09594 |

Ridge + forecast sky remains the time-valid pre-test choice. It is stamped `daily-high-v1-pretest` for historical decisions after the Oct. 28 08:40 UTC selection evidence cutoff. When older caches omitted the sky field, the app looks up the identical source and issue with an archive publication no later than the forecast's availability. Plain Ridge is the labeled fallback if peak sky data cannot be attached. NOAA led all three weather test metrics and all three observation lag checks. NOAA TXN is the operational default starting Nov. 14, 2026 at 09:43 UTC, the latest complete CLI label in the test, and is stamped `daily-high-v1-operational`. It keeps the pre-test archived uncertainty fallback. Historical decisions before the study use the earlier KNYC model where available.

## Contemporaneous Polymarket US comparison

The Nov. 3–13 archive provides 33 settled station-days, 165 fixed decision snapshots, and 990 matched cases across six published buckets per market. Every YES display sample had an archive label no older than 60 seconds and was available by the decision under the conservative one-minute availability rule. The matching uses official outcomes and verified MADIS receipts as available, with substantial missing observation receipts on Nov. 13. New York had two verified readings that day, Miami one, and Los Angeles zero. This comparison retained the affected predictions with their actual available feature subsets.

| Matched Brier | All stations | KNYC | KMIA | KLAX |
| --- | ---: | ---: | ---: | ---: |
| Venue YES display | **0.05817** | 0.07732 | **0.07548** | **0.02171** |
| NOAA TXN | 0.07638 | **0.07643** | 0.09321 | 0.05949 |
| Station/time bias | 0.11816 | 0.13066 | 0.09369 | 0.13012 |
| Ridge | 0.10283 | 0.10782 | 0.09692 | 0.10374 |
| Ridge + forecast sky | 0.10137 | 0.10400 | 0.09656 | 0.10354 |

The venue display series scored best overall. NOAA had the lowest score in New York. At 03:00 PST on Nov. 8 at KLAX the 87–88°F YES display was 48¢, NOAA assigned 29.56%, and Ridge + sky assigned 1.36%. The official maximum was 88°F. The market archive provides display prices only. Historical fill quality and paper P&L require actual book depth and recheck evidence.
