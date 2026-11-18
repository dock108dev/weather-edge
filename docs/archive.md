# Weather and exchange archive

Use the date selector in **Historical replay**. November 5 is the original demonstration. November 6 adds three complete market replays, global temperature and cloud frames, and a separate minute-by-minute station trace. April 1 opens the same weather workflow for Central Park, Miami and Los Angeles. The three exchange slug lookups returned 404 for April 1, so the weather still opens with its climate reports and empty market lanes.

## Rebuild and extend

Run these commands from the project root. The first command retrieves time-stamped weather, contract rules, prices and publications. The globe commands fetch indexed byte ranges from NOAA GFS. A date range is inclusive. Omit `--end` for a single day.

```sh
uv run --locked python -m weather_edge.archive --start 2026-11-03 --end 2026-11-13
uv run --locked python -m weather_edge.archive --start 2026-11-06 --minute-trace
uv run --locked python -m weather_edge.archive --start 2026-04-01
uv run --locked python -m weather_edge.grids replay --start 2026-11-03 --end 2026-11-13
uv run --locked python -m weather_edge.clouds replay --start 2026-11-03 --end 2026-11-13
uv run --locked python -m weather_edge.grids replay --start 2026-04-01
uv run --locked python -m weather_edge.clouds replay --start 2026-04-01
```

Use `--series nyc miami la` to select stations and `--weather-only` to skip exchange lookups. Complete station-days and compact source receipts are cached under `.data/`. The backfill resumes station-days with unresolved source errors and skips completed ones. `--refresh` rechecks IEM and exchange responses and retains prior weather evidence if those sources become unavailable. The default replay continues to use the original November 5 cache when present. A corrupt new station-day appears as a catalog issue and leaves its peers usable.

## Coverage at verification

The local library held **36 station-days across 12 dates**, with 33 market-backed station-days and three weather-only station-days. It included **891 receipt-matched routine or special station readings**, **228 as-issued station forecast/card entries**, **42 complete official CLI reports**, and **285,318 minute-stamped exchange display samples**. The minute trace adds **4,320 retrospective readings** across the three November 6 stations. The standard replay omitted 110 station readings with unavailable original receipts, concentrated on November 13. An explicit `--receipt-proxy-minutes` opt-in exposes unmatched records with a labeled observation-time-plus-lag proxy. The November and April handoff replays use verified receipts only.

Publicly timed payout evidence was available for 30 market station-days, covering 180 binary buckets. Miami on November 4 and both Miami and Los Angeles on November 12 have reconciled final payout values via Polymarket US's settlement endpoint. The evaluated daily market reports did not verify an availability clock for those three cases, leaving the replay payout lane empty. Historical market data has no executable order-book depth. The archived closed-book lookup only supplies eligible closing timestamps. [Market payout endpoint](https://docs.polymarket.us/api-reference/markets/get-market-settlement), [Daily Market Report manifest](https://www.polymarketexchange.com/files/daily-market-report/manifest.json), [Price history](https://docs.polymarket.us/api-reference/price-history/get-price-history)

| November 6 station | Verified-receipt readings | Official high | Winning bucket | Official report publication |
| --- | ---: | --- | --- | --- |
| Central Park | 51 | 70°F | 70–71°F | Nov. 7 at 01:26 EST |
| Miami | 23 | 86°F | 85–86°F | Nov. 7 at 04:22 EST |
| Los Angeles | 24 | 83°F | 82–83°F | Nov. 7 at 01:35 PST |

Miami omitted one METAR because its archive receipt did not match exactly. All three November 6 exchange payouts can be revealed at 21:24:19 EST November 7, when the Daily Market Report was posted. The exact payout transaction time is unavailable. The report is a conservative public availability bound. [Central Park climate report](https://mesonet.agron.iastate.edu/api/1/nwstext/202611070626-KOKX-CDUS41-CLINYC), [Miami climate report](https://mesonet.agron.iastate.edu/api/1/nwstext/202611070922-KMFL-CDUS42-CLIMIA), [Los Angeles climate report](https://mesonet.agron.iastate.edu/api/1/nwstext/202611070935-KLOX-CDUS46-CLILAX), [November 7 market report](https://www.polymarketexchange.com/files/daily-market-report/20261107-daily-market-report.csv), [Publication manifest](https://www.polymarketexchange.com/files/daily-market-report/manifest.json)

## What clock each source supplies

- **Exact-station METAR/SPECI.** The IEM extract includes temperature and dew point in °F, wind speed in knots, wind direction in degrees, and one-hour precipitation in inches when reported. The NOAA MADIS receipt clock is joined to the original message and observation timestamp. Distinct original messages and receipt times are retained. A source with only an unverified receipt is a visible gap unless a timing proxy was explicitly selected. [IEM ASOS archive](https://mesonet.agron.iastate.edu/ASOS/), [NOAA MADIS METAR](https://madis.ncep.noaa.gov/madis_metar.shtml)
- **NOAA NBM station cards.** Forecast cards retain run issue, archive publication and valid times. The later archive publication is used when cards are paired. Forecasts are gridded guidance postprocessed to named stations. They are separate from observations and climate reports. [NOAA NBM documentation](https://vlab.noaa.gov/web/mdl/nbm-documentation)
- **NWS CLI reports.** The archive discovers the WMO/AFOS products by issuance and uses their published UTC time. It verifies the climate date printed in the raw product and retains complete climate reports as separate records, including later revisions. The fixed local standard time climate window follows the contract. The daily high, daily low and where available the precipitation and snowfall are values directly reported in the CLI product. [IEM NWS text archive](https://mesonet.agron.iastate.edu/nws/text.php), [Exchange weather questions](https://docs.polymarket.us/faqs/weather-faqs), [QCX temperature rule filing](https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts/60333)
- **NCEI one-minute ASOS via IEM.** Tested and integrated for November 6 as retrospective station context. At Miami the minute trace peaked at 87°F and the official climate high was 86°F. IEM documents a typical 18–36-hour or longer archive delay and a different averaging basis. The trace has observation time and application retrieval time. Its historical public receipt is unknown. The app keeps this free public archive separate from causal replay inputs, estimates and settlement. [IEM one-minute download](https://mesonet.agron.iastate.edu/request/asos/1min.phtml), [IEM note on one-minute measurements and ASOS reporting](https://mesonet.agron.iastate.edu/onsite/news.phtml?id=1469)
- **Global NOAA grid.** The collector uses anonymous S3 range requests over indexed GRIB2 and ecCodes to extract 1° temperature at 2 m and total cloud cover. The standard replay requests three-hour valid steps over several 00Z and 12Z run cycles. Grid records preserve the nominal issue, archive publication and valid time. New caches also record their local ingestion timestamp. Previously cached records may carry only upstream clocks. [NOAA GFS open data](https://registry.opendata.aws/noaa-gfs-bdp-pds/), [ecCodes](https://confluence.ecmwf.int/display/ECC/ecCodes+Home)
- **Exchange display history.** Each archive sample uses the original minute timestamp plus a conservative one-minute availability lag. Exact executable prices and sizes require actual captured books. Reported ladder, quote age and verification behavior for those snapshots are described in the capture notes. [Polymarket US price history](https://docs.polymarket.us/api-reference/price-history/get-price-history)

At Central Park on November 6, the 66.2°F report received at 10:50 a.m. EST moved the preselected model's 70–71°F YES probability from 49.2% to 21.4%. The 7 a.m. EST forecast and observed maximum of 69°F were unchanged across the receipt. The hourly forecast comparison and recent readings supplied new evidence for the correction. The official high later settled the bucket YES at 70°F.
