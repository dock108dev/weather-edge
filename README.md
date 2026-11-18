# Weather Edge

A local NOAA weather globe, as-issued station archive, Polymarket US and Kalshi market archive, and $500 paper bankroll. The collector makes public read-only requests. The browser reads the local evidence. Positions and balances are simulated.

## Open the app

From a clean checkout or an unpacked [staged source export](docs/audit-local-verification.md#clean-export-and-locked-build), install the locked dependencies and build. The [verification record](docs/audit-local-verification.md#verified-november-17-2026) lists the tested tool versions.

```sh
uv sync --locked
npm ci
npm run build
```

For a first offline result, use a disposable checkout with an empty `.data` directory. Run this from its root and leave the listener open. The socket audit blocks upstream requests after installation.

```sh
export UV_OFFLINE=1
export PYTHONPATH="$PWD/scripts/offline:$PWD"
uv run --locked python -m weather_edge.server --no-collect --data "$PWD/.data" --port 18765
```

In a second terminal at the same checkout root, generate the synthetic capture. The capture CLI writes to the checkout's own `.data/fixtures/`, so use the same checkout as the listener.

```sh
uv run --locked python -m weather_edge.capture --fixture --duration-seconds 30
```

Open the [synthetic Backtest](http://127.0.0.1:18765/?view=backtest&fixture=1), leave the fixture settings in place, and click **Run and save backtest**. The result shows the saved ID and adds `run=<id>` to the address. Copy that address and reopen it in another tab. To reproduce the frozen result, copy its ID into the command in [Save and replay a paper trial](#save-and-replay-a-paper-trial). The [synthetic Trade ticket](http://127.0.0.1:18765/?view=trade&fixture=1&venue=polymarket-us&instrument=tc-temp-nychigh-2026-11-15-gte49lt50f&side=YES&station=KNYC) uses the generated capture and a separate simulated balance. Neither fixture needs a trained model.

For the normal workspace, run the following from the checkout whose data you want to use, in a terminal without the offline environment above:

```sh
uv run --locked python -m weather_edge.server
```

Open [Weather Edge](http://127.0.0.1:8765/). Startup starts the detached public collector unless collection was explicitly stopped. Use `--no-collect` to open cached evidence without launching a worker; it doesn't stop an existing worker or the paper reconciler. If you have the retained archive, open the [Kalshi Central Park low YES ticket](http://127.0.0.1:8765/?view=trade&mode=live&history_mode=live&archived=1&day=2026-11-16&station=KNYC&venue=kalshi&instrument=KXLOWTNYC-26NOV16-B37.5&side=YES&at=2026-11-17T00%3A52%3A32Z) to see a displayed ladder and indicative fees with a blocked paper recheck. A clean archive has no historical ticket, and live model odds depend on retained, available model evidence. See [collection controls and missing-data diagnosis](docs/collection.md).

The home page puts Trade first. **Weather → Find a city or station** shows the relevant forecast, observed reading, settlement station, and exact contract. Choose **See matching market prices** to open the city’s board. Pins and tickets also carry the station into Weather. **Map layers** contains the existing grid and market controls, and **Point probe** opens when you inspect a point.

Use **Captures and source health → Capture receipt** to pin the timeline to local evidence. **Forecast ahead** freezes the origin and lets you choose a later NOAA valid hour while keeping the selected contract and side in view. The address preserves both clocks for reopening. **Return to live** refreshes the current evidence and restores the live Trade controls.

## Review a trade

Home shows the highest estimated net EV at the displayed order budget. The highlights show one side per station, date, and weather measure. **Show all qualified sides** reveals the remaining results. **Review order** opens the exact venue, contract, and side with maximum loss above the order action. Ready ranks open Polymarket US sides by net EV after fees, using a supported model, an origin-confirmed book receipt at most 30 seconds old, and an exchange image at most two minutes old. **Watch** keeps cases with stale, uncertain, closed, or display-only evidence. Kalshi Watch tickets show the native side ladder and a whole-contract fee estimate when the retained book and fee terms support it. The current Kalshi capture supplies a local receipt clock and has no equivalent paper recheck.

**Prices** shows retained book and listing offers for both venues, including when Ready is empty. Each price shows its receipt time. Listing offers supply no size. **Fetch public book** requests one exact open contract through the existing read-only collector and archives its response. The ticket still evaluates any freshness, depth, and paper recheck gaps separately.

Edit **Order budget** and select **Reprice** to update both the ranking and the ticket. **Try a $25 practice order** runs one synthetic example with a fresh $500 demo balance and shows its simulated result inline. **Forecast & station readings** carries the venue, bucket, and side into Weather. **Open on Polymarket US** and **Open on Kalshi** open the public event page. The ticket identifies the exact bucket, side, and station for manual selection.

**Record paper order** reserves the reviewed cost for one side in the separate $500 order ledger with a $25 station-day cap. The paper engine looks for the first eligible receipt after the live 30-second reaction delay, within a separate window of up to 15 seconds, and uses the reviewed quantity and highest price as limits. See [paper clocks and eligibility](docs/capture.md#paper-orders-and-recheck-clocks). The browser shows pending, simulated, and skipped orders with their supporting receipt times. **Save trial** in Weather still runs a strategy over a capture. The [synthetic trade fixture](http://127.0.0.1:8765/?view=trade&fixture=1) has its own balance and retained rechecks.

## Backtest retained evidence

Open [Backtest](http://127.0.0.1:8765/?view=backtest), choose a UTC date range or exact retained capture, and set the strategy, maximum order, and reaction delay. **Run and save backtest** freezes the inputs and returns a reopenable ID. Open an individual signal to inspect the decision-time reading and model vintage, receipt clocks, price and size at recheck, fees, and resolution. Each real decision can use only evidence received by that clock. Model availability uses the exact artifact digest with a timestamped local publication, earlier frozen trial, or first local observation, together with the model’s evidence cutoff.

Archived display samples count toward coverage and do not supply executable depth. The November 6 example links to the Central Park 70–71°F weather and payout replay. **Try synthetic fill and skip** opens a separate fixture with a filled example and a later price move that kills an edge. Existing **Saved trials** remain accessible in Trade and Weather.

## Browse markets and research

- [Market archive](http://127.0.0.1:8765/?view=markets) searches contracts by venue, family, station or region, date and status. CSV and JSON downloads use the filters in the form. The coverage drawer shows historical prices, native book clocks and open backfills.
- [Research](http://127.0.0.1:8765/?view=research) opens saved forecasts and paper results, including evaluation windows, independent sample counts, calibration, exact books, fee calculations and delayed rechecks. Download a study’s frozen evidence when offered. Earlier versions stay accessible when a corrected study is added.

See the [retained market and research examples](docs/venue-research.md#retained-examples) for exact links and the archive each one uses.

The local market SQLite archive is `.data/venues.sqlite3`. Immutable Polymarket US high receipt files are indexed in `.data/capture-index.sqlite3`, including native IDs, timestamps, and a digest of the original receipt. Existing weather, model, capture and paper-run files remain in their original directories.

```sh
uv run --locked python -m weather_edge.capture_index --station KNYC --day 2026-11-15
uv run --locked python -m weather_edge.venue_sources coverage
uv run --locked python -m weather_edge.venue_sources export .data/exports/weather-markets.csv
```

## Replay and weather

Choose **Historical replay** in Weather to inspect a retained station day and its exact market. The [replay guide](docs/replay.md) explains the clocks and links to [retained station, map, and contract examples](docs/replay.md#retained-examples).

## Rebuild the example datasets and model

These commands make public requests and build local evidence. They are optional for the synthetic walkthrough.

```sh
uv run --locked python -m weather_edge.archive --start 2026-11-06 --minute-trace
uv run --locked python -m weather_edge.archive --start 2026-11-03 --end 2026-11-13
uv run --locked python -m weather_edge.grids replay --start 2026-11-06
uv run --locked python -m weather_edge.clouds replay --start 2026-11-06
uv run --locked python -m weather_edge.wind replay --start 2026-11-06
uv run --locked python -m weather_edge.study
```

Backfill is incremental by station and date. The original pipeline stays available through `weather_edge.history` and `weather_edge.model`. The study preserves fit, calibration, selection, and later test windows. NOAA TXN was best on the later test and supplies production odds for evidence after that test was knowable. Each trained model and evaluation snapshot has local provenance.

## Save and replay a paper trial

**Save trial** freezes the book, model, fee, order size, and reaction assumptions. **Saved trials** opens the result even after retraining. **Check later payouts** creates a child run for newly received settlements and preserves the original entries. The ledger tracks open cost, realized results, and rechecks by station-day.

From the checkout used for the walkthrough, enter the ID returned by Backtest or listed under **Saved trials**. The CLI searches that data root's trial, Backtest, and order folders and checks the frozen inputs and result.

```sh
printf 'Saved run ID: '
read -r run_id
uv run --locked python -m weather_edge.paper_runs --data "$PWD/.data" rerun "$run_id"
```

A rerun uses the saved engine, clocks, probabilities, and fees. **Check later payouts** requires a later retained settlement. **Evaluate frozen evidence under current rules** creates a different result and leaves the parent available. See [replay and evidence clocks](docs/replay.md).

## Local checks

Run `uv run --locked pytest -q` for the Python suite. The [offline verification procedure](docs/audit-local-verification.md)
covers a clean build, a bound disposable HTTP server, and a synthetic save and reopen in Chrome.
Use its [separate retained-data branch](docs/audit-local-verification.md#separate-retained-data-controls)
when you have the named local artifacts.

## Methods and evidence

- [Audit remediation plan](docs/audit-remediation-plan.md)
- [Collection, source candidates, and contract families](docs/collection.md)
- [Replay, saved results, and evidence clocks](docs/replay.md) · [Archive and climate reports](docs/archive.md)
- [Research records and frozen evidence downloads](docs/venue-research.md#open-a-record-and-its-evidence)
- [Back up, verify, restore, and diagnose a local archive](docs/archive-recovery.md)
- [Model holdout, market comparison, and vintages](docs/model-study.md)
- [Live paper trials and timing sensitivity](docs/paper-study.md)
- [Live Trade qualification, previews, and rechecks](docs/live-trade-funnel.md)
- [Native ladders, taker fees, and payout mechanics](docs/capture.md)
- [Global cloud coverage](docs/clouds.md) and [10 m wind](docs/wind.md)
- [Venue coverage and contract research](docs/venue-research.md)
- [Expansion results, exact cases, and reproduction](docs/expansion-results.md)
- [Expansion progress and checkpoints](docs/expansion-progress.md)
