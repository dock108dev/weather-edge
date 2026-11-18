# Replay and saved evidence

## Choose a clock and keep the exact ticket

Open **Weather → Historical replay** and choose a date from the local archive. An exact link can carry the venue, instrument, YES/NO side, station, and decision time. The timeline resolves the clock against the selected archive and reports any clamp to a saved endpoint. Trade waits for that resolution before requesting the exact Replay ticket. A Live history ticket can instead use its explicit decision time. Pin a live capture to review that receipt; choose **Latest data** to follow collection again.

**Forecast ahead** holds the decision at the exact origin, including fractional seconds where supplied. Moving the valid-time slider or choosing a later hour changes what is being forecast, while all evidence still has to be known at the origin. The link keeps the origin and valid time separately so it can reopen the same view.

Observed time, forecast issue, publication/receipt, and valid time remain separate. A station reading or as-issued forecast can inform a decision when it is available at that clock. A model also needs the exact digest, evidence cutoff, and a local availability witness; a model trained later cannot score a historical decision just because it accepts the inputs. A climate report can establish the official weather result before a verified payout arrives. Payouts and later revisions enter at their own evidenced clocks.

## Open and reproduce a saved result

Use the [synthetic walkthrough](../README.md#open-the-app) for a run from an empty archive. For a retained result, open [Miami Backtest 40ee74c6230a6dd3846fcbb7](http://127.0.0.1:8765/?view=backtest&run=40ee74c6230a6dd3846fcbb7) or choose a run from the saved list. A Backtest case link keeps the run and case index; inspect the signal, delayed recheck, and payout separately. Run the [saved ID command](../README.md#save-and-replay-a-paper-trial) against the same data root. The Miami example uses the original frozen v2 engine and reproduces three signals and one fill.

An original run keeps its input evidence, rates, clocks and engine. Legacy v2 results use their frozen receipt rules. Current v3 runs can use the [strict paper clocks](capture.md#paper-orders-and-recheck-clocks); live runs default to app availability and origin-confirmed depth. **Evaluate frozen evidence under current rules** saves a child with the changed decisions and missing evidence. It does not add receipts that the original failed to preserve. **Check later payouts** is a separate operation that needs newer verified settlement evidence.

## Original November 5, 2026 replay

The following commands and observations describe the original archived sample. The build command makes public requests and is optional for the offline walkthrough.

Run from the repository root after installing its Python dependencies.

```sh
uv run --locked python -m weather_edge.history
```

The command builds `.data/replay.json` for the three series below. `--series nyc` builds only Central Park. The first run caches raw exchange responses, IEM station and climate files, NOAA MADIS Metar archives, and reduced NOAA NBM station cards. Subsequent runs reuse the cache. The historical command makes public, read-only requests for archived data.

| Settlement station | Climate day in UTC | Received readings | Forecast issues | Official high | Winning YES bucket |
| --- | --- | ---: | ---: | ---: | --- |
| KNYC Central Park | Nov 5 05:00–Nov 6 05:00 | 37 | 6 | 72°F | 71–72°F |
| KMIA Miami International Airport | Nov 5 05:00–Nov 6 05:00 | 24 | 6 | 85°F | 85–86°F |
| KLAX Los Angeles International Airport | Nov 5 08:00–Nov 6 08:00 | 25 | 7 | 78°F | 78–79°F |

## Contract outcomes

Each Polymarket US event has six separate binary instrument slugs. The market descriptions name the corresponding station and the NWS Climatological Report (Daily). The NWS climate day runs from midnight to midnight in local standard time. The exchange applies the official report's precision, which is whole °F for these CLI highs. Temperature range endpoints are inclusive here. For example, the 71–72°F bucket includes 72°F even though the slug ends in `gte71lt72f`. The exchange's individual settlement endpoint returns $1 for the winning YES and $0 for its NO. [Polymarket US product filing](https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/60333), [weather settlement FAQ](https://docs.polymarket.us/faqs/weather-faqs), [NWS observation definition](https://www.weather.gov/lot/weather_observations_faq).

The official temperatures become available on Nov 6 at 06:45 UTC for Central Park, 09:22 UTC for Miami, and 09:54 UTC for Los Angeles. At Central Park the official maximum was recorded at 6:15 p.m. local standard time. The highest replayed observation was 71°F. [Central Park CLI](https://mesonet.agron.iastate.edu/api/1/nwstext/202611060645-KOKX-CDUS41-CLINYC), [Miami CLI](https://mesonet.agron.iastate.edu/api/1/nwstext/202611060922-KMFL-CDUS42-CLIMIA), [Los Angeles CLI](https://mesonet.agron.iastate.edu/api/1/nwstext/202611060954-KLOX-CDUS46-CLILAX).

Polymarket US's current FAQ lists next-day settlement at 8 a.m. Eastern, with extensions for review. The replay holds back the verified payout until the public daily market report's posted time on Nov 7 at 02:10:45 UTC. That archive provides an upper bound on when the $1 payout was publicly recorded. The exact settlement transaction time is unavailable. Archived closed-book times, market maturity metadata, report release, expected settlement, and recorded payout each retain their own timestamps. [Daily market report](https://www.polymarketexchange.com/files/daily-market-report/20261106-daily-market-report.csv), [report manifest](https://www.polymarketexchange.com/files/daily-market-report/manifest.json).

For Central Park, the last archived price sample is labeled 05:00 UTC on Nov 6. The winning contract's archived book closed at 05:00:18.970 UTC. The market's maturity field was 10:00 UTC. The CLI released at 06:45 UTC, and the FAQ schedule corresponds to 13:00 UTC. [Central Park event](https://gateway.polymarket.us/v1/events/slug/temp-nychigh-2026-11-05), [archived winning-contract book](https://gateway.polymarket.us/v1/markets/tc-temp-nychigh-2026-11-05-gte71lt72f/book).

## What each timestamp means

- **Readings.** Observation time comes from the IEM ASOS/METAR archive. Each value shown has a matching original station report in the NOAA MADIS archive, joined by exact report text after whitespace and formatting normalization. Replay availability is MADIS's recorded receipt time for the matched report. The 14:51 UTC KNYC row lacks a matched receipt, leaving a gap in the replay. [IEM ASOS archive](https://mesonet.agron.iastate.edu/ASOS/), [NOAA MADIS METAR](https://madis.ncep.noaa.gov/madis_metar.shtml).
- **Forecasts.** Each NOAA National Blend of Models card retains its nominal cycle, archive posting time from S3 `Last-Modified`, and valid time. The daily maximum uses the same-issue NBS TXN value when provided. The fallback takes the highest available forecast hour in the climate day and can cover only the remaining part of the day. The forecast profile comes from NBH. Paired values become available after both cards have posted. [KNYC 12Z NBH bulletin](https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20261105/12/text/blend_nbhtx.t12z), [KNYC 12Z NBS bulletin](https://noaa-nbm-grib2-pds.s3.amazonaws.com/blend.20261105/12/text/blend_nbstx.t12z), [NOAA NBM v5.0](https://www.weather.gov/media/notification/pdf_2026/scn26-24NBM_V5.0.pdf).
- **Display prices.** Each series has 8,646 price records, with 1,441 timestamped points per bucket. The exchange supplies YES and NO sides independently. Its archive returned minute-aligned timestamps even for a request with second-precision bounds. The replay preserves those timestamps and waits one minute from each sample's label before revealing it because the within-minute timing is unknown. The 71–72°F bucket's first sample is $0.28 YES and $0.80 NO at 05:00 UTC, available to the replay at 05:01 UTC. A display price carries no executable quantity, so the historical bankroll has no fill evidence. [Polymarket US price-history API](https://docs.polymarket.us/api-reference/price-history/get-price-history).

## Retained examples

These examples use retained station and market evidence.

- [November 6 Central Park](http://127.0.0.1:8765/?day=2026-11-06&station=KNYC) → **70 to 71°** → timeline **End**. The official high is 70°F and YES paid $1. Press **Left Arrow** once from the end to see the official high while the payout is still pending.
- [November 6 Miami](http://127.0.0.1:8765/?day=2026-11-06&station=KMIA) shows one omitted reading with no matched receipt. **Station readings and receipt clocks** shows a separate delayed one-minute archive.
- [April 1 Central Park](http://127.0.0.1:8765/?day=2026-04-01&station=KNYC) has station weather, the official climate report, and global weather fields. The exchange has no corresponding daily-high listing.
- [Original November 5 replay](http://127.0.0.1:8765/?day=2026-11-05&station=KNYC) opens the 71–72°F bucket and its verified settlement.
- On November 6, switch **Markets** off, switch **Wind** on, focus the globe and press **Enter**. The Western Atlantic probe shows the temperature, cloud cover, and wind direction and speed with distinct NOAA issue and valid clocks. Drag or use the arrow keys to move the center. Click any point to probe it.
- Switch **Marine** on in Live for NOAA NDBC point measurements. Use **Waves** for the separate NOAA GFS Wave forecast. The probe shows the forecast issue, publication and valid times and the measurement’s actual time and local receipt. The wave map samples the native 0.25° field at whole degrees. Cells outside the native wet mask remain blank.
- Switch **Radar** on for hourly observed NOAA MRMS precipitation and its publication clock, independent of the **Precipitation** GFS forecast. The station-temperature chart uses a single known HRRR issue at the selected clock, with the 12Z and 18Z acquisitions shown as they arrive.
- Select a point and turn on **HRRR cell** to see the nearest native 3 km temperature when a retained field is known at the cursor. The [Denver wind case](http://127.0.0.1:8765/?view=research&kind=models&run=hrrr-upwind-thermal-gradient-v1-af1432c1c853665f&case=0) opens the 64.3°F upwind cell and 75.1°F downwind cell from the same issue. The GFS probe remains labeled separately.
- [Wave forecast lead comparison](http://127.0.0.1:8765/?view=research&kind=models&run=noaa-wave-lead-coast-v1-808b61e6a1ec87cb&case=0) opens 6-, 24- and 48-hour issues on the same Pacific buoy measurement. The globe independently labels the native cell, the derived value when all contributing cells are wet, and the archived buoy observation.

The replay catalog keeps weather-only station-days alongside exchange-backed station-days. The coverage line counts evidence available at the cursor. Archived display prices support price comparisons and produce zero simulated fills. Full station rules, receipt policies, and payout provenance are in the archive documentation.
