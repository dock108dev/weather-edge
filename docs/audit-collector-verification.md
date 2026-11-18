# Slice 5 · Collector health and source-to-ticket verification

## Baseline and failure reproduction

The real index matched every entry of Slice 4 tree `778213fcc6ebcc4bf99db9848c43a44704b360ec`. The remediation-plan link in the README was the sole unstaged edit before this slice. Port 8765 listened as PID 17498 and the normal root loaded `app-PP2BMT67.js`. Collector PID 23194 held the worker lock, with its original start at `2026-11-16T05:04:45.452912Z` and collection enabled. Chrome showed one Polymarket US feed label for both daily-high and native daily-low/rain prices.

- An escaping Kalshi book exception produced `refresh_kalshi_books` alongside `Kalshi public books`. A later successful check cleared the canonical row and left the orphan active. Forcing its fallback status write to fail raised out of the scheduler error path.
- The candidate keeps one canonical Kalshi row through success, failure, and recovery. It preserves the error time and reason after recovery and folds the old orphan into canonical history. Failures with a known lane go to that lane. A scheduler-level failure with unknown attribution uses a labeled task issue. The injected SQLite write error appears with source, exception, and traceback in `collector/status-errors.jsonl` and a sibling task still records its result.
- On the same disposable inputs, both original native lanes reported one row and a clean check while archived progress said `1/2 books returned`. The candidate reports a partial response for each lane and retains one usable book and one missing or invalid target. A Kalshi request covered two tickers in one HTTP receipt. An open third ticker was outside that priority batch. The manual fetch returned the existing `{"received": 1}` shape and left the automatic batch unchanged.

| Disposable lane at 2026-11-15T18:00:02Z | Open sides | Posted | Displayed ladder | Ready | Exact sample offered by funnel | Eligible rechecks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Kalshi, three open instruments | 6 | 1 | 1 | 0 | 1 displayed ladder | 0 |
| Polymarket US native, two open instruments | 4 | 1 | 1 | 0 | 1 displayed ladder | 0 |

The board measures distinct venue/instrument/side once at the stated clock and $25 budget. Exact ticket GETs confirmed both sampled display cases were blocked for paper review. Instrument coverage uses one row per instrument in the lane and intersects that evaluation's open cases with the latest completed automatic batch. In Kalshi receipt 6, `KXHIGHNY-26NOV15-B49.5` was usable and `KXHIGHNY-26NOV15-B51.5` was omitted. Polymarket US receipt 7 provided the usable 21¢ low offer, and receipt 8 preserved the rain identity failure. The low HTTP receipt was `18:00:01Z`, first app availability `18:00:02Z`, and the stale exchange image `16:00:01Z`.

The next disposable automatic cycle recovered to two usable books, low receipt 10 and rain receipt 11. Both exact archive records had HTTP time `18:00:16Z`, first app availability `18:00:17Z`, and exchange image `16:00:16Z`. The observation cutoff was `18:00:17Z`. This exercises the publication path for both families with controlled replies.

## Retained controls

All clocks below are UTC. Time, $25 maximum order, origin-confirmed policy, and retained evidence were held fixed across code versions.

| Exact side and decision clock | Retained evidence | Result |
| --- | --- | --- |
| Polymarket US Miami daily high 81–82°F YES, Nov 16 `02:24:16.891316Z` | Listing 3. Origin capture `20261116T022356-6ae567ca.json`, HTTP `02:23:55.226384Z`, app `02:23:56.822391Z`. Delayed HTTP `02:24:55.603573Z`, app `02:24:57.223819Z`. | Earlier positive control. One eligible recheck and one simulated fill. |
| Same Miami YES, `02:28:48Z` | Origin capture `20261116T022828-de303907.json`, exchange `02:27:26.647556973Z`, HTTP `02:28:27.673275Z`, app `02:28:28.880002Z`. Matching cached capture `20261116T022845-da845b23.json`, HTTP `02:28:44.318946Z`, app `02:28:45.296339Z`. | 15.91 contracts for $1.3433. Later HTTP `02:29:27.512497Z` became available `02:29:29.408454Z` and was stale. One failed window, zero eligible rechecks. |
| Same Miami YES, `02:28:58Z` | Same cached response and listing. | Origin receipt had expired. The cached response still supported display and the paper preview was blocked. |
| Kalshi Central Park daily low `KXLOWTNYC-26NOV16-B37.5` YES, Nov 17 `00:52:32Z` | Listing 162390, book 162551, fee history 1724. Requested `00:52:31.049671Z`, received `00:52:31.183327Z`. No app-publication or exchange-image timestamp in this retained book. | 140 contracts, $1.35 indicative fee, $24.80 all-in. The missing exchange image clock blocks a paper recheck. |

Both Miami previews used the same model digest `1a937086151da8cc9b7e54bb8122e844ae72f3f382e090eb144045e6ca25c8ff` and listing 3's `04:59:00Z` event cutoff. The earlier archives witness actual receipt membership. They predate batch scope telemetry, so priority selection is labeled unwitnessed. In particular, the Kalshi receipt identifies the exact ticker in a shared request without proving the archived selection policy.

The staged Slice 4 export and the candidate replayed the same Miami-containing Polymarket US high tape from `02:24:00Z` through `02:40:00Z`, counting two sides per exact publication, with the preview five seconds after the board evaluation. Both had 3,024 open side appearances and 1,701 posted ladder appearances. The latest-receipt policy gave 3 Ready, 3 eligible previews, and 0 eligible rechecks. Matching-origin retention gave 7 Ready, 6 eligible previews, and 1 eligible recheck. A recurring side counts again at a different clock. The counts describe this tape and its settings.

## Disposable control and browser checks

- The HTTP handler verified durable Stop despite a corrupt source health row and returned working sibling telemetry alongside the read issue. Two separate bounded worker processes, PIDs 75417 and 75418, restarted against the same paused root and made zero fake provider calls. Explicit Start removed the marker and worker 75419 made one call against the same fake provider. The rollover regression selected verified next-day highs on the next book cycle using the previous discovery.
- Chrome rendered the candidate bundle with recorded responses from that disposable HTTP handler. It showed separate daily-high and low/rain feed labels, the 21¢ usable low offer with separate receipt, app and image clocks, and 2 selected / 2 requested / 1 usable / 1 missing or invalid on each native lane. The omitted rain target's YES link reached the exact ticket with receipt 8 and the same identity failure. A delayed pre-Stop response was released after the control returned. The page still showed **Collection stopped**, a source telemetry read fault, and the cached 21¢ offer. Explicit Start restored the collecting label in the recording. The disposable responses were served on an isolated static path because the shell could not bind a second listener.
- The focused collector, Trade, native archive, paper eligibility, Backtest and historical HTTP suite passed 54 tests. The consolidated suite passed 190 tests on Python 3.13.16. The frontend build passed on Node 24.22.0 and whitespace checks passed. Chrome verified the final isolated asset `app-VW7AGNWW.js` with explicit stale-image and app-availability labels. Asset SHA-256 `434006c16bde1fb89cec2c296df35a2abf7c9c8ffe3dc991f7758d5250b12e47`.

## Before the normal listener cutover

The first shell denied loopback binding, process signaling and writes to `.git/index.lock` with “Operation not permitted.” At that checkpoint the normal listener served Slice 4 as PID 17498 and collector PID 23194 still held the worker lock. Chrome reopened the retained Kalshi ticket on that listener.

Read-only archive observation at `2026-11-17T03:19:55.538456Z` found both families under the earlier worker. The clock verified their arrivals and preserved the open publication gap.

| Polymarket US exact instrument | Receipt | Local HTTP receipt | Exchange image | First app availability |
| --- | ---: | --- | --- | --- |
| `tc-temp-mdwlow-2026-11-16-lt24f` | 173006 | Nov 17 `03:19:54.544472Z` | Nov 16 `17:29:44.380045444Z` | Unwitnessed |
| `raincli-rain-daily-2026-11-16-kmdw` | 173008 | Nov 17 `03:19:54.890628Z` | Nov 16 `08:02:55.676940140Z` | Unwitnessed |

## Normal listener closeout · November 16, 2026

- In Chrome on the bound disposable listener, both partial lanes showed 2 selected, 2 requested, 1 usable and 1 missing or invalid. The harness held an actual running GET response past the HTTP Stop response. Chrome stayed at **Collection stopped** after the held response arrived and still displayed the 21¢ offer. Explicit Start restored **Collecting live data**. The 23 focused collector and eligibility tests passed with loopback binding available.
- A fresh normal build serves port 8765 as PID 1548. The HTML SHA-256 is `794111f10d1df6a0a32c2deef5355a065b5cbeb5712195248f311b48f2d79f62`. It loads `app-X4FAQKKP.js` with SHA-256 `a52c1879921a8d053c3b27c68bb2f5af90415aac2c7b7be42118a8a3573722f6`. Its JavaScript matches the disposable candidate after normalizing generated dependency path comments. Collector PID 23194 released its worker lock before PID 1709 started at `2026-11-17T03:33:30.428867Z`. Collection was enabled before and after the handoff.
- The replacement worker's first automatic **priority lows and rain** batch was ID 3. It started `03:33:30.460855Z` and completed `03:33:36.717758Z`. It selected and requested 14 instruments. Three returned usable depth and 11 returned valid empty books, with zero missing, invalid or failed targets. At observation `03:34:06.491947Z`, the exact book and raw receipt joins verified each clock below. Requested time preceded the HTTP receipt, app availability followed the receipt, and both availability witnesses preceded batch completion. This observation used the archive and local API.

| Exact instrument | Receipt | HTTP receipt UTC | First app availability UTC | Exchange image UTC |
| --- | ---: | --- | --- | --- |
| `tc-temp-mdwlow-2026-11-16-gte32f` | 174006 | Nov 17 `03:33:33.442601Z` | Nov 17 `03:33:36.538571Z` | Nov 16 `19:34:41.792455846Z` |
| `raincli-rain-daily-2026-11-16-knyc` | 174008 | Nov 17 `03:33:33.675675Z` | Nov 17 `03:33:36.538571Z` | Nov 17 `00:38:26.022587235Z` |

- Both first-batch witnesses were cache hits with stale exchange images. The normal browser showed the batch counts and opened the exact Midway low ticket from its health table. The current Midway low and Central Park rain tickets exposed receipt, publication, and exchange clocks independently and identified stale images. The rain YES ticket displayed 2¢ and stayed display-only. The retained Kalshi ticket returned receipt 162551 and the original $1.35 indicative fee through the exact replay API. A cold live-history request exceeded the client's 32-second deadline before the listener logged 200. The normal Backtest reopened the frozen Miami run with 3 signals and 1 fill. The live ledger and original Miami file matched their Slice 4 hashes `ad4605dfdad65ba7baa77c72736481c2ac3e933ed9c4e959def55be0bef15cc0` and `6ac624aae8685257aca1378b28a043ad72ca4d5ca5bc162db19e3b1a4a573af4`.
- The real index matched the reviewed candidate tree `cc80e45213b591635bd814d415bb2f274a063346` before the closeout documentation was added. The final staging includes the same 15 intended paths, including the verification and checklist updates. The earlier README link remains a working-tree edit.

## Read-only diagnostics

```sh
.venv/bin/python -m weather_edge.trade_audit sample --output /tmp/weather-edge-board.jsonl
.venv/bin/python -m weather_edge.trade_audit observed --observations /tmp/weather-edge-board.jsonl \
  --through 2026-11-17T03:30:00Z --output /tmp/weather-edge-observed.json
.venv/bin/python -m weather_edge.trade_audit exact --venue polymarket-us \
  --instrument tc-temp-miahigh-2026-11-15-gte81lt82f --side YES --start 2026-11-16T02:28:48Z \
  --through 2026-11-16T02:40:00Z --output /tmp/weather-edge-miami-yes.json
```

Sample and exact measurements use GET requests to the local app or retained files. The observation report counts sampled appearances, ticket fetch failures, eligible previews, and incomplete, unobserved, missing and failed recheck windows. Unattended reconciliation remains Slice 6 work.
