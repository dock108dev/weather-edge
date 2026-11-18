# Slice 8 browser and performance verification · November 17, 2026

Baseline: staged tree `5affac5566777c70f42d726cd11ff4789f04fcc8`. The normal listener served `app-AT27CFET.js` as PID 52663 and owned the paper reconciliation lock. Collector PID 1709 owned the collector lock. The README's existing audit link was the only unstaged edit. The browser and simulated clocks ran on a separate loopback listener and a filesystem clone plus SQLite backups of retained inputs. The disposable ledger was separate. The HTTP listener was audited for outbound connections and Chrome withheld network access to the offline venue destination.

## Radar and focus

Chrome 157 reported `'packed': Illegal use of reserved word` before Radar was enabled. The fix changes the GLSL variable only; packing, scale and the missing-cell sentinel are the same. The candidate linked a WebGL program through Chrome's SwiftShader backend and issued a radar draw with the relevant uniform enabled. With WebGL explicitly disabled, it rendered the same map through Canvas 2D. In both runs the map changed when Radar was enabled, used the tested frame, and showed the same publication clock as the probe.

| Replay UTC on Nov 12 | Frame | Valid UTC | Published UTC | Central Park |
| --- | --- | --- | --- | --- |
| 18:01:00 | `2026111217-f000` | 17:00 | 17:02:42 | 1.10 mm |
| 18:08:00 | `2026111218-f000` | 18:00 | 18:02:44 | 4.62 mm |

At 18:08, `[-67, 27]` displayed **Unavailable** and `[-121.49, 38.58]` displayed **0 mm** in both renderers. The two frames were ingested on November 15, after their November 12 publication times. No change was made to the distinction between the live receipt gate and the historical publication gate.

The controlled non-fixture Chrome page ran the normal eight-second poll with stepped reply receipts. In the baseline, the venue link, Forecast & station readings, Market Archive, and Book evidence summary all lost focus. In the candidate all four kept the same semantic focus, scroll position, and open Book evidence disclosure after the nodes were replaced. Enter and click opened the same exact Weather and Market Archive views, and activated the same venue event on an offline intercepted response. Enter and click also toggled the book disclosure. Focus and the unsubmitted value stayed in the budget field, and polling left the search input alone. Expiry removed the record action, announced **Book evidence expired**, and moved focus to the order preview heading. A Ready-zero board, keyboard opening of a qualified row, $17.50 budget repricing, and $8.50 remaining-room repricing passed.

## Exact clocks and saved records

- The November 16 Central Park Kalshi `KXLOWTNYC-26NOV16-B37.5` YES live-history link kept the decision at `2026-11-17T00:52:32Z` and displayed book receipt 162551, 140 contracts and a $1.35 indicative fee. Book evidence was usable while the station request was held. Active ledger polls and elapsed time did not replace the archived preview. Trade → Weather → Trade and reload kept the same side, venue, instrument, contract day, station and clock.
- The Replay URL initially asks for the same post-midnight clock. This day has a retained global weather window through midnight UTC and no saved matching station archive. The browser explains the limitation, resolves the requested clock to `2026-11-16T23:59:59.999Z`, and preserves the exact selection across Weather and reload. The baseline sent the prior URL clock before the held session resolved. The candidate showed a waiting state, made no speculative ticket request, and used the resolved UTC clock for the earlier book, receipt 158806 at 23:59:52.334258Z (7¢ retained YES offer).
- Research displayed the three `live-nbm-late-issue-comparison-v1-` records separately by creation time and record suffix. IDs ending in `567976561afe0d42`, `278c8044bc27a340`, and `af9f07a61248eb7b` were created at 00:04:45.834714Z, 00:04:22.848116Z, and 00:03:33.934985Z on November 16. The exact API record matched each card, and the evidence download stayed on that record's URL even though all three gzip bodies had the same SHA-256. Search, kind filtering, keyboard focus, and reload passed at desktop and 390 px. The newer HRRR record `hrrr-intraday-price-benchmark-v1-530ed9da7c445099` and its predecessor `hrrr-intraday-price-benchmark-v1-d8ba5ff7f1fcb637` opened independently with reciprocal links. The narrow Research and Trade pages had a 390 px viewport and 390 px page width.
- Forecast ahead opened with a fixed origin and returned to Replay. The original Miami Backtest `40ee74c6230a6dd3846fcbb7` reopened with 3 signals and 1 fill.

## Matched performance

Chrome marked a usable Ready list, first exact ticket, matching KNYC station content, and the next animation frame after a real temperature draw (WebGL draw or Canvas 2D paint). Requests and timings are recorded separately in the Chrome resource lists and per-request HTTP trace. A new Chrome profile and first load for each URL were followed by a same-profile reload against the same listener. The board was first in each listener process; the Kalshi first load used a fresh browser profile but inherited the server caches warmed by the board. Indexed capture files were retained; reload started a new JS frame cache; API JSON was no-store; OS file cache was not flushed. The viewport was 1450×1150 and the disposable server wall clock was fixed at `2026-11-17T04:40:00Z` against the same data snapshot.

Times are seconds from browser navigation. Local API counts are completed resources at the observation point; slower runs can include another scheduled poll.

| Route and cache state | Ready board | Exact ticket | KNYC readings | Actual grid paint | Local API requests |
| --- | ---: | ---: | ---: | ---: | ---: |
| Live board, first | 3.96 → 4.06 | — | 16.41 → 18.98 | 16.45 → 18.99 | 7 → 9 |
| Live board, reload | 2.76 → 2.69 | — | 5.55 → 5.25 | 5.60 → 5.26 | 5 → 5 |
| Kalshi live history, first | — | 20.18 → 3.04 | 19.72 → 12.13 | 19.77 → 12.15 | 7 → 6 |
| Kalshi live history, reload | — | 21.04 → 0.77 | 21.46 → 7.05 | 21.51 → 7.08 | 7 → 5 |

The exact-ticket HTTP handler itself went from 18.98/20.81 s to 0.35/0.36 s. The exact station session went from 18.33/21.09 s to 10.78/6.61 s. Grid downloads took 4–5 ms in Chrome. The board itself was essentially unchanged; first-load station and grid on the general board were slower in this pair, with the full capture and low-book work still on that path. No parts of this comparison use a latency threshold for acceptance.

The original live-history ticket and related station request both assembled the live context, including Polymarket low tape and several thousand sibling Kalshi book rows. Cold full capture loading could occupy the shared capture lock for several seconds. Server-side compact assembly and the actual grid fetch/paint were much smaller parts of the delay. The ticket now uses the exact Kalshi ladder and station day without rebuilding the board or scanning sibling books. A gap or unpublished latest image retains the existing broader lookup and can select the earlier app-available receipt. Station charts still load retained history, reading only chart inputs without joining receipt proof for every image. Polymarket event discovery now checks books on scoped catalog members instead of scanning the venue-wide receipt window. The exact ticket is independently usable while the station and grid continue loading; those paths still have material archive work.

The consolidated local suite passed **213 tests**, including the exact Kalshi cutoff, app publication, and whole-second receipt cases. The frontend build passed. Chrome recorded a simulated YES order in the disposable ledger, and the server network audit observed zero provider attempts. One concurrent fallback run timed out before the second probe result; the timeout is retained. An isolated repeat with probe diagnostics passed on the same build, as did the earlier fallback pass.

Port 8765 now serves `app-JQJD3MN3.js` (SHA-256 `1f32d80b64556ee14f0e01c587ce5530919e2e933c1cbcf1294621f4bba19827`). The HTML, CSS and JavaScript matched the built candidate. PID 63535 owns the listener and paper reconciliation lock; collector PID 1709 kept its worker lock and remained enabled and running. The live ledger, seven frozen paper files, Miami Backtest, and collector worker record matched their pre-integration hashes. Read-only Chrome on port 8765 reopened the exact live-history Kalshi ticket and the frozen Research case. The existing unstaged README link was preserved.

The final staged tree is in `.data/browser-verification/slice8-closeout/staged-tree.txt`. That directory also contains the timing samples, profile, browser witnesses, screenshots, and preserved baseline failures. Slice 9 owns broader clean setup and browser compatibility.
