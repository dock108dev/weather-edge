# Final engineering closeout · Cold reads after capture publication

November 17, 2026. The full live capture snapshot now extends a verified receipt prefix on publication. The matched Trade → Weather run went from **22.30 s to 2.31 s** to matching Central Park readings. The same November 15 49–50°F YES Forecast ahead reload kept its original quote, side, model witness, and full selected market history.

## Reproduction

The baseline was staged tree `56f691776adb4e06b45c9163bddc746160565978`. The candidate export overlaid the three Python changes and the focused regression, staged as source tree `c04f86c352feffbb7bf0a440ad5744fdfe8ae533`. Both ran `uv sync --locked`, `npm ci`, and `npm run build`. Their nine emitted assets were identical. [Baseline build](../.data/browser-verification/cold-publication/baseline-build.log) · [Candidate build](../.data/browser-verification/cold-publication/candidate-build.log)

The experiment used a copy-on-write clone of the [Slice 12 retained snapshot](audit-broad-read-verification.md#matched-replay-setup) with **13,023** high captures. The fixed browser and server clock was **2026-11-17 16:40:10Z**. A compact session read before each case warmed the indexed pages through the normal SQLite connections and loaded the process's full capture snapshot. The SQLite connections still opened per read, so the operating system could evict pages between requests. After warming, the harness renamed the next real collector receipt, `20261117T163955-c5d6ff90.json`, into the disposable capture directory. That publication completed at **16:39:55.359403Z**. The normal collector and data root stayed outside the experiment. [Input hashes](../.data/browser-verification/cold-publication/publications.sha256)

- **Trade → Weather.** Started on Miami so the Central Park selection had to expand a different station timeline. Published immediately before selecting the Central Park YES side. Timed the quote from selection, then matching station/date with the temperature chart and first real draw from the Weather link.
- **Forecast ahead.** Opened the pinned November 15 ticket and advanced the valid time with Forecast ahead, then published immediately before reload. The active run used the normal paper request that began at the historical origin before preview. The idle run intercepted only `/api/paper` before it reached the server. A separate normal panel opening verified the completed trial response.
- **Chrome.** Used the Slice 12 instrumentation for a WebGL `drawArrays` call with the temperature uniform enabled. Both builds used Chrome 157 and SwiftShader. An initial reload pass inherited the fixed-time shim's page clock across navigation. The paired grid follow-up used Chrome's native navigation clock for the first draw. The trace records server request start and end with a monotonic clock. The stopping condition was a paired measurement with the same evidence, followed by the installed browser check. [Chrome harness](../.data/browser-verification/cold-publication/journeys.cjs) · [Server harness](../.data/browser-verification/cold-publication/trace-server.py)

Run from the repository with the recorded disposable directory and the corresponding locked export. Use separate shells for the server and Chrome. Change `build` to `candidate` and `port` to `18766` for the second run.

```sh
out="$PWD/.data/browser-verification/cold-publication"
scratch=$(cat "$out/workdir.txt")
build=baseline
port=18765
(cd "$scratch/$build" && PYTHONPATH="$PWD/scripts/offline:$PWD" \
  .venv/bin/python "$out/trace-server.py" "$scratch/snapshot" "$port")

CAPTURE_TARGET="$scratch/snapshot" \
PUBLICATION="$scratch/publications/20261117T163955-c5d6ff90.json" \
PLAYWRIGHT_CORE="$(cat "$out/playwright.txt")" \
node "$out/journeys.cjs" "$port" "$build-reproduction" "$(cat "$scratch/snapshot/clock.txt")" \
  live-1 replay-active replay-idle paper-open early-1
```

## Matched browser and server measurements

| Chrome milestone | Baseline (s) | Candidate (s) |
| --- | ---: | ---: |
| Live exact ticket after selecting the Central Park YES side | 1.27 | 1.30 |
| Trade → Weather, matching readings | 22.30 | 2.31 |
| Trade → Weather, first temperature grid draw | 22.26 | 2.27 |
| Forecast ahead reload, usable ticket, paper active | 27.97 | 13.49 |
| Forecast ahead reload, matching readings, paper active | 23.65 | 9.17 |
| Forecast ahead reload, first grid draw, paper active | 23.56 | 9.11 |
| Forecast ahead reload, ticket and model evidence, paper active | 27.98 | 13.49 |
| Forecast ahead reload, usable ticket, paper idle | 16.69 | 5.44 |
| Forecast ahead reload, matching readings, paper idle | 16.72 | 5.71 |
| Forecast ahead reload, first grid draw, paper idle | 16.65 | 5.64 |
| Forecast ahead reload, ticket and model evidence, paper idle | 20.49 | 9.31 |

Chrome's rendering and the harness's DOM observation run on separate clocks. The WebGL call can precede the harness's observation of the completed readings. An earlier baseline linked visit produced matching readings in **14.61 s**. The final table uses one paired linked visit and the paired native-clock reloads. [Baseline linked Chrome](../.data/browser-verification/cold-publication/baseline/journeys.json) · [Candidate linked Chrome](../.data/browser-verification/cold-publication/candidate/journeys.json) · [Baseline reload Chrome](../.data/browser-verification/cold-publication/baseline-grid/journeys.json) · [Candidate reload Chrome](../.data/browser-verification/cold-publication/candidate-grid/journeys.json)

| Server phase | Linked session baseline → candidate (s) | Active reload baseline → candidate (s) | Idle reload baseline → candidate (s) |
| --- | ---: | ---: | ---: |
| Raw high-capture bodies read | 13,024 → 1 | 13,024 → 1 | 13,024 → 1 |
| Indexed read, including sync | 4.54 → 0.16 | 5.99 → 3.49 | 3.29 → 1.09 |
| Assembly after indexed read | 12.70 → 0.21 | 10.45 → 0.19 | 7.22 → 0.24 |
| Full capture lock held by loader | 17.99 → 0.37 | 16.44 → 3.67 | 11.31 → 1.33 |
| Weather session wait on capture lock | <0.01 → <0.01 | 16.41 → 3.65 | 11.29 → 1.33 |
| Compact native low assembly on session | 4.00 → 1.73 | 6.18 → 4.52 | 4.53 → 3.60 |
| Concurrent paper request | — | 23.60 → 22.87 | — |

The paper scan started about four seconds before each active reload. Chrome canceled its fetch at preview, but the server continued and decoded **126,145** native book rows in each run. The paper request already had the warm snapshot and spent under 0.01 s waiting for the capture lock. In the baseline idle reload, removing the paper request still left a **10.50 s** indexed read and full merge. In the candidate active reload the remaining delay includes a **3.49 s** indexed refresh under overlap and compact native listing and clock work. The paper scan is still expensive when it overlaps, and the first read of a fresh process still assembles the full tape. [Baseline linked trace](../.data/browser-verification/cold-publication/baseline-server.log) · [Candidate linked trace and second publication](../.data/browser-verification/cold-publication/candidate-server.log) · [Baseline reload trace](../.data/browser-verification/cold-publication/baseline-grid-server.log) · [Candidate reload trace](../.data/browser-verification/cold-publication/candidate-grid-server.log)

## What changed

The live snapshot checks that the current ordered manifest still starts with the exact filenames, receipt clocks, digests, and sizes it already assembled. It reads the tail's raw bodies in the same SQLite read transaction. A duplicate that takes over the canonical filename, a backfilled receipt, a replacement, or a removal that changes the retained prefix triggers a full rebuild. [Index read](../weather_edge/capture_index.py#L254)

```python
extends = manifest[:len(previous)] == previous
if extends:
    name, received, *_ = previous[-1]
    rows = db.execute(query.format(body=",r.raw", tail="AND (r.received_us,r.filename)>(?,?)"),
                      (version, received, name)).fetchall()
```

The shared merge still appends the original session summaries, resolves duplicate books and payouts in receipt order, and sorts the full books of affected events. It reuses completed event histories for the rest of the archive. When an inserted book changes a progress mark, readers holding a previous snapshot keep their previous book dictionaries. The live route revalidates the directory revision under the full capture lock. The bounded Trade window remains independent. [Snapshot refresh](../weather_edge/capture.py#L567) · [Progress and history](../weather_edge/capture.py#L523) · [HTTP entry point](../weather_edge/server.py#L565)

The next publication on the same listener, **16:40:10.396080Z**, read one body in **0.20 s** and completed indexed read plus merge in **0.41 s**. The complete selected Central Park history went from **15,168 to 15,174** price points, with every prior point retained. The second receipt was absent one microsecond before its cutoff and present at the inclusive cutoff. The pinned ticket response bytes stayed identical. [Next publication](../.data/browser-verification/cold-publication/next-publication.json) · [Server trace](../.data/browser-verification/cold-publication/candidate-server.log)

## Evidence controls

- The selected live and November 15 session payloads and bucket history hashes matched across builds, including all **31,254** November 15 price points. At the fixed **19:00:25Z** origin, the November 15 ticket kept **YES, 86¢, and 8.8295997%**. The quoted book was received at **19:00:07.722343Z**, with capture availability **19:00:10.155309Z** in `20261115T190010-6154b464.json` (SHA-256 `934972e5f17379efa6aea56512c7df3208a968613c1f752d6370dc528b45648a`). The ticket body and the model decision were identical before and after reload as the valid time advanced to **20:00Z**. Model witness SHA-256 was `34adf181d7ab8f7495c7178605e1a9231801df8c761d9d58cfadac300750eff5`. [Candidate exact ticket](../.data/browser-verification/cold-publication/candidate/replay-active-exact-ticket.json)
- November 6 kept the **95¢** exact YES quote and matching Central Park readings through reload. The quote probability and decision stayed null at **21:00Z** as the forecast valid time advanced to **22:00Z**. [November 6 ticket](../.data/browser-verification/cold-publication/candidate/early-1-exact-ticket.json)
- Opening the panel rendered the same result in both builds, **228** scanned rows and a trial with **zero fills** at the November 15 origin. The serialized panel payload matched at SHA-256 `ad23651df87c27302934bb7821e3e926bb10e9187ebca23686a4fe7f7e72fab7`. [Candidate panel payload](../.data/browser-verification/cold-publication/candidate/paper-open-result.json)
- The focused snapshot regression covers a second append containing a payout only, late book-time insertion and progress marks, a new canonical duplicate filename and removal. It verifies retained capture attribution and compares each transition to a fresh full merge. [Regression](../tests/test_capture_index.py#L76)

- Miami's earlier **02:24:16.891316Z** ticket qualified with an eligible recheck at **02:24:57.223819Z**. The later **02:28:48Z** preview still had **9.673275 s** left, then failed recheck on a stale book and was blocked by **02:28:58Z**. The three exact audit responses matched the Slice 12 baseline bytes. The frozen Miami Backtest reran with **three signals and one fill**, and the compressed artifact retained SHA-256 `6ac624aae8685257aca1378b28a043ad72ca4d5ca5bc162db19e3b1a4a573af4`. [Miami controls](../.data/browser-verification/cold-publication/paper-controls.log) · [Frozen rerun](../.data/browser-verification/cold-publication/miami-rerun.json)
- The byte and hash comparisons cover session payloads, all selected bucket prices, exact ticket bodies, the pinned decision, and panel output. Chrome recorded zero page errors or external requests in those comparisons. The disposable server recorded zero outbound attempts. [Evidence parity](../.data/browser-verification/cold-publication/evidence-parity.json)

## Locked checks and installation

- The focused run passed **35 tests**, followed by the **three** capture-index tests after the final snapshot memory adjustment. The consolidated locked export passed **215 tests in 28.18 s** with the offline socket audit. [Focused checks](../.data/browser-verification/cold-publication/focused.log) · [Snapshot retest](../.data/browser-verification/cold-publication/final-index-check.log) · [Consolidated run](../.data/browser-verification/cold-publication/pytest.log)
- Port **8765** serves the verified build with listener and reconciler PID **86998**. Collector PID **1709** continued running with collection enabled. Installed `server.py`, `capture.py`, and `capture_index.py` matched the locked candidate. The nine served assets matched the export, including `app-FNIXP4VU.js` at SHA-256 `d4e76dd313613cce9c176238b1085f84615b34c6d832fe10663a968c78b96399`. [Install receipt with all hashes](../.data/browser-verification/cold-publication/normal-install.json)
- Normal Chrome showed the live ticket in **1.53 s**, matching Central Park readings in **2.79 s**, and the first grid draw in **2.72 s**. The November 15 reload showed the exact ticket in **6.70 s**, matching readings in **6.99 s**, the first grid draw in **6.91 s**, and the ticket with model evidence in **12.96 s**. Both exact retained tickets and their model decisions matched the disposable candidate. Chrome opened the frozen Miami Backtest with **three signals and one fill**. The ledger, its seven frozen runs, the original Miami artifact, and the collector worker record passed their final hash checks. [Installed Chrome](../.data/browser-verification/cold-publication/installed/journeys.json) · [Installed Backtest](../.data/browser-verification/cold-publication/installed-frozen.log) · [Process and lock ownership](../.data/browser-verification/cold-publication/installed-ownership.log) · [Protected hashes](../.data/browser-verification/cold-publication/protected-final.log)
- The final staged tree includes the tested Python changes, the regression and this audit update. The owner's README link remains unstaged. [Final staged tree](../.data/browser-verification/cold-publication/staged-tree.txt)
