# Slice 11 · Integrated verification and live qualification

November 17, 2026. Exact live Kalshi tickets now open directly from retained Kalshi evidence, even while another request holds the Polymarket capture lock. The same live Miami Kalshi side went from 21.90 seconds to 0.78 seconds click-to-visible across two installed Chrome observations. The paired fixed-clock check isolated the application change. Installed live samples produced zero eligible exact previews on either venue, so there is no live recheck-rate denominator for this observation. On the fixed Miami-containing Polymarket US tape, **one of six complete eligible previews** rechecked and simulated a fill. The later named 02:28 Miami preview expired, and its delayed receipt was stale. Kalshi’s sampled public books lacked an exchange-image timestamp.

## Installed candidate

Port [8765](http://127.0.0.1:8765/) started with listener and reconciler PID 68173 and collector PID 1709. The actual starting index was `c4eb97e05043ca1e48520d808bc3f6e29b0570e6`. The verified source export with the Kalshi fix and regression is `5f3faa7f16d7e25e5c7189b9acb667c64cc09bbf`. The final index adds this report and its documentary cross-references. Record its exact fingerprint in [staged-tree.txt](../.data/browser-verification/slice11-closeout/staged-tree.txt) outside the tree it fingerprints. The README audit link is still the owner’s single unstaged hunk.

The normal listener and reconciler now run as PID **23611**. Its startup imports `weather_edge.local_http` from the same repository as the changed backend. The installed `server.py` is byte-identical to the candidate export, SHA-256 `fb1dbed5e0531dbaa58cd68a0af361ed88f9dfdbd1d29c191e6b211d7ab68952`. The install receipt has the backend, HTML, CSS, and font hashes. Chrome loaded `app-FNIXP4VU.js`, SHA-256 `d4e76dd313613cce9c176238b1085f84615b34c6d832fe10663a968c78b96399`. All nine served files matched the locked clean build byte-for-byte. Collector PID **1709** continuously held its worker lock, enabled and running since `2026-11-17T03:33:30.428867Z`. The live ledger, seven ledger artifacts, frozen Miami Backtest, and collector worker record matched their before hashes. Installed operations were loopback GETs. [Installation](../.data/browser-verification/slice11-closeout/normal-install.json) · [Final runtime and hashes](../.data/browser-verification/slice11-closeout/installed-final.log)

## What changed in the ticket path

The live browser started on the regular Trade board and clicked Miami `KXHIGHMIA-26NOV17-T85` YES. The first opening took 21.90 seconds, during which the book changed. The exact Kalshi route first ran the same recent-capture and model preparation used for Polymarket. Its live Kalshi rows come from the retained native catalog and exact book instead. The timing by itself could reflect load, so the bound HTTP comparison used the same restored receipt and fixed UTC clock while a competing request held `CAPTURE_LOCK` for up to two seconds.

```python
if selected[0] == venue_archive.KALSHI and not fixture and not archived:
    # Live Kalshi previews use the exact archived listing and book. There
    # is no matching event in the Polymarket tape or model to evaluate.
    capture, at, lookup = None, datetime.now(UTC), None
```

The previous route waited for release and returned in **2.7775 s**. The candidate finished while the lock was held in **0.5741 s**. The two bodies were identical at `2026-11-17T00:52:32Z`, including the book ID, prices, indicative fee, debit, clock fields and eligibility blocker. The same case exercises live exact GET with context/model access forced to fail in the HTTP regression. Archived, forecast, fixture, board and Polymarket paths still use context where they need it. [Ticket route](../weather_edge/server.py#L924) · [HTTP regression](../tests/test_listing_evidence.py#L177) · [Fixed-clock harness](../.data/browser-verification/slice11-closeout/kalshi-contention.py)

The repeat on 8765 took **0.783 s** for the same live Kalshi side with a later receipt. The two Chrome timings are single observations under different load. The fixed-clock check isolates the lock wait. One pre-install board probe timed out at the sampler’s 30-second limit during overlapping Chrome activity. That request had no board or ticket denominator. The broad board and Weather reads still use shared capture work. The first post-install browser pass also reached its 90-second timeout on the older November 15 modeled ticket. The bound listener logged HTTP 200 after the browser deadline, and a focused installed retry reopened the ticket and verified the same model, quote and pinned origin. The precise source of the slow broad reads remains unisolated. Two Polymarket browser clicks also varied with context and load. None of these live cases had an eligible preview to carry through the wait.

## Miami tape and Kalshi book witness

The Miami instrument was `tc-temp-miahigh-2026-11-15-gte81lt82f` YES, native market `2487915`, side `4975216`, local contract date **November 15**. Both rechecks use the same November 16 UTC tape through **02:40:00Z**, listing 3 and its **04:59:00Z** cutoff, and model digest `1a937086151da8cc9b7e54bb8122e844ae72f3f382e090eb144045e6ca25c8ff`.

| UTC decision | Reviewed and displayed evidence | Outcome |
| --- | --- | --- |
| 02:24:16.891316 | Origin HTTP 02:23:55.226384, first app availability 02:23:56.822391. | 15.91 contracts, $0.11 fee, $1.3433 maximum loss. Eligible recheck on 02:24:55.603573 HTTP / 02:24:57.223819 app, within 02:24:46.891316–02:25:01.891316. One simulated fill and no verified payout by cutoff. |
| 02:28:48 | Origin HTTP 02:28:27.673275, app 02:28:28.880002, image 02:27:26.647556973. Cache HIT HTTP 02:28:44.318946, app 02:28:45.296339, `Age: 16`, same image and ladder. | 15.91 contracts, $0.11 fee, $1.3433 maximum loss, origin 20.326725 seconds old with 9.673275 seconds left. Recheck 02:29:18–02:29:33 saw HTTP 02:29:27.512497 / app 02:29:29.408454 with the same stale image. One failed window and zero eligible rechecks. |
| 02:28:58 | The same cached 3¢ offer for 0.51 contracts remained visible. The reviewed origin was now 30.326725 seconds old. | Displayed depth only. No eligible preview or recheck window. |

On the same browser path, both policies presented a Ready board at 02:28:43. At 02:28:48 the latest-receipt policy demoted the click to a cached-book blocker. Matching-origin retention kept **Record paper order** enabled. At 02:28:58 the same button was absent. The 02:24 control reached Ready in the browser and its delayed receipt qualified in replay. The browser drove the actual Trade card and HTTP handler on disposable data with fixed clocks. Independent exact CLI and replay used the retained tape. The newer matching ladder supports display. The unexpired origin supplies the preview’s evidence clock. A later local receipt must independently qualify for a recheck. [Same-path Chrome controls](../.data/browser-verification/slice11-closeout/fixed-miami-chrome.json) · [Exact preview](../.data/browser-verification/slice11-closeout/miami-preview.json) · [Positive control](../.data/browser-verification/slice11-closeout/miami-positive.json)

For Kalshi Central Park `KXLOWTNYC-26NOV16-B37.5` YES, local contract date **November 16**, at **November 17 00:52:32Z**, Chrome and the exact API showed **140 contracts, $1.35 indicative fee and $24.80 maximum loss**. Book 162551 was requested at `00:52:31.049671Z` and received at `00:52:31.183327Z`. Its retained body hashes to `6e7d4eee55cd8d3d4d0445dc4c0fb215807d9a5c3c247f37213f5a87be66fd78`. It has no first-app-publication clock or exchange-image clock. The terms join listing 162390, series 156336, event 3360, series fee history 1720 and event fee history 1724. The event and fee revisions support indicative sizing. The missing **exchange image time** blocks paper recheck, even on newer books with recorded app publication. The raw sampled batch reply has `ticker` and `orderbook_fp`, whose only fields are `yes_dollars` and `no_dollars`. HTTP request time, HTTP response headers, local receipt and app publication time each describe transport or availability. [Raw and normalized joins](../.data/browser-verification/slice11-closeout/live-evidence.json) · [Kalshi multi-book schema](https://docs.kalshi.com/api-reference/market/get-multiple-market-orderbooks)

## Live two-venue funnel

Four baseline board GETs completed between **November 17 15:54:13.520353Z and 16:03:05Z**. One additional board timed out. Three board GETs completed on the installed candidate between **16:05:14.106658Z and the 16:13:44Z cutoff**, including the existing worker’s next completed book batches. This was a bounded sample at a $25 budget. Neither venue produced a Ready appearance or an eligible preview to follow for a full recheck window.

| Loopback sampler | Open | Posted | Ladder | Ready | Exact GETs | Eligible previews | Eligible recheck rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| Baseline Polymarket US | 864 | 612 | 208 | 0 | 4 | 0 | — |
| Baseline Kalshi | 3,732 | 2,982 | 400 | 0 | 4 | 0 | — |
| Installed Polymarket US | 648 | 470 | 158 | 0 | 3 | 0 | — |
| Installed Kalshi | 3,096 | 2,511 | 300 | 0 | 3 | 0 | — |

The CLI sampled the same Midway daily rain NO and Philadelphia Kalshi rain YES each time, both local contract date **November 17**. Each venue contributes one distinct sampled side and station-day across the two phases. For the installed CLI, 155 and 297 displayed ladder appearances were unsampled. For the baseline CLI, 204 and 396 were unsampled. There were zero failed ticket GETs, incomplete eligible windows, missing/reconstruction-failed eligible receipts, simulated fills and unresolved payouts in the live CLI population. Counts of zero in the recheck event log come from an empty eligible-preview denominator. [Baseline summary](../.data/browser-verification/slice11-closeout/observed-live-baseline.json) · [Installed summary](../.data/browser-verification/slice11-closeout/observed-live-final.json)

Candidates are open side appearances after the retained listing window check. The denominator for posted is open appearances, for ladders it is posted, and Ready is evaluated on open sides. The CLI attempts at most one exact ticket **per venue per successful board**, first by highest EV Ready with deterministic identity ties, otherwise the most recent displayed ladder with deterministic identity ties. Browser lane coverage uses one GET-backed Trade board per lane and clicks one exact displayed offer per lane. It prefers the same instrument as the earlier run when available, then a Miami YES ladder, then the first offered ladder. Each browser row is its own board evaluation and can include overlapping station-days across venues. The denominator for eligible previews is attempted exact GETs. The denominator for rechecks is complete, reproducible eligible windows. A display-only order remains in coverage as indicative and cannot enter that last denominator.

| Browser lane and board clock UTC | Open | Posted | Ladder | Ready | Exact GETs | Eligible previews |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Baseline Polymarket US highs 15:58:08 | 144 | 125 | 38 | 0 | 1 | 0 |
| Baseline Polymarket US lows/rain 15:58:16 | 72 | 27 | 14 | 0 | 1 | 0 |
| Baseline Kalshi public 15:58:40 | 984 | 791 | 100 | 0 | 1 indicative | 0 |
| Installed Polymarket US highs 16:05:22 | 144 | 129 | 38 | 0 | 1 | 0 |
| Installed Polymarket US lows/rain 16:05:51 | 72 | 27 | 14 | 0 | 1 | 0 |
| Installed Kalshi public 16:05:55 | 1,032 | 837 | 100 | 0 | 1 indicative | 0 |

The full initial browser rows contained 144, 72 and 984 distinct sides on the Polymarket high, Polymarket low/rain and Kalshi lane snapshots. The final snapshots contained 144, 72 and 1,032, across **12, 12 and 76** dated station-days respectively. These are distinct within each board; the repeated CLI totals above are appearances. In both browser passes, each of the three lanes had one successful exact GET, zero eligible, zero complete eligible recheck windows and zero simulated fills or unresolved payouts from those clicks. On the final Miami high ticket, **Refresh prices** moved from a cached-book blocker to uncertain model support with a new receipt, preserving the exact side and withholding order entry. The Midway rain ladder had an exchange image from 11:37:08Z, hours before its new HTTP and app receipts. Kalshi’s final live ticket at 16:05:58.176518Z used receipt 222972, HTTP 16:05:42.115498Z / app 16:05:42.147065Z, for 2,337 indicative contracts with $1.62 fees and $24.99 debit. [Chrome trace, clocks and timings](../.data/browser-verification/slice11-closeout/final-browser/chrome-installed.json)

Five distinct retained Kalshi automatic batches joined to the CLI board evaluations. Their full requests had 352 usable targets in total, all with a local receipt and zero with an exchange-image clock. Including exact-ticket receipts, the live sample inspected nine distinct batched bodies with **633** repeated ticker members and no image-time field. The CLI instrument coverage counts only open weather markets intersected with each distinct batch. For example, the first board had 72 Polymarket high instruments with 24 selected/usable, 36 Polymarket low/rain with 14 selected, 12 usable and two valid empty books, and 450 Kalshi instruments with 61 selected/usable. A limited priority batch leaves open instruments outside its scope even when every requested book parses. [Coverage, receipt bodies and selected markets](../.data/browser-verification/slice11-closeout/live-evidence.json)

### Retrospective denominators

| Tape and policy | Open | Posted | Ladder | Ready | Exact previews observed or simulated | Eligible preview | Eligible recheck | Simulated fill |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Nov 16 02:24–02:40 UTC, latest receipt | 3,024 | 1,701 | 1,701 | 3 | 3 | 3 | 0/3 | 0 |
| Same tape, matching-origin retention | 3,024 | 1,701 | 1,701 | 7 | 7 | 6 | 1/6 | 1 |
| Nov 16 03:34–03:46:30 UTC, actual Polymarket US GETs | 4,536 | 2,016 | 560 | 12 | 11 | 11 | Unobserved | Unobserved |
| Same actual GET observations, Kalshi | 18,088 | 13,554 | 1,030 | 0 | 14 | 0 | — | 0 |

The paired tape counts two appearances per exact instrument per completed publication, with a five-second board-to-preview retest. All eligible windows completed. The latest-receipt side had three cached recheck failures. Origin retention had three cached and two stale failures plus the one fill. Both policy rows share the same evidence, and the named positive is part of that row. One origin-policy Ready appearance expired before its preview. There was one unresolved simulated payout. The second comparison uses 14 actual earlier boards, with a fixed cutoff of 03:46:30Z. Of 12 Ready appearances, 11 were sampled and one was unsampled. The 11 eligible tickets are ten appearances of the same Miami side and one Central Park appearance, spanning two station-days on **November 15**. Their legacy JSON records omit the top-level probability and model vintage. At the first clock the current exact replay reproduced the quote and order and returned the retained model digest and 29.56439% probability. The current audit correctly classifies all 11 as unreconstructable previews. There were zero missing preview receipts, zero incomplete windows, and no supported recheck-rate denominator for that historical HTTP cohort. [Retrospective sample analysis](../.data/browser-verification/slice11-closeout/observed-retained.json) · [First exact comparison](../.data/browser-verification/slice11-closeout/historical-first-preview-differences.json)

### Same-tape size and delay

The current sensitivity tool used first app availability on the same 02:24–02:40 UTC tape. Every variant held the models, forecast evidence, station-day cap, fee policy and payout cutoff fixed. The lane population was 1,512 book receipts, 48 distinct sides and four station-days. Variants used the same six signal appearances of one distinct Miami side and station-day. The successful 15-second variants at both sizes were the **same** Miami YES position, signaled when the 02:28:01.831147Z app publication arrived and filled at the 02:28:28.880002Z publication. The 30-second settings missed that book because their reaction started after publication. The earlier sensitivity report used a 02:27:57.070561Z HTTP receipt as the signal clock. On that clock, its 30-second variant could use the 02:28:27.673275Z HTTP recheck. This explains the shifted delay result for that position. The live-board positive control used a separate 02:24:16.891316Z decision.

| Strategy | Budget | Reaction | Signals | Rechecked | Fills | Allocated / committed | Resolved / unresolved |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Two-sided | $10 and $25, each | 15 s | 6 each | 1 each | 1 each | $1.3433 / $1.3433 each | 0 / 1 each |
| Two-sided | $10 and $25, each | 30 s and 60 s, each | 6 each | 0 | 0 | $0 / $0 | 0 / 0 |
| Two-sided buffered | $25 | 30 s | 6 | 0 | 0 | $0 / $0 | 0 / 0 |

Two size variants repeat one station-day and side with no settled outcomes. Order defaults stay at the current setting. [Sensitivity report](../.data/browser-verification/slice11-closeout/sensitivity.json) · [Exact signals and positions](../.data/browser-verification/slice11-closeout/sensitivity-population.json)

## Integrated build and browser

- Clean export with `uv sync --locked`, `npm ci` and `npm run build`. Python 3.13.16, uv 0.13.1, Node 24.22.0, npm 11.19.1, Chrome 157.0.8103.79 and Playwright Core 1.64.0-beta-1791568591000. The integrated source passed **213 tests** in 35.75 seconds. The changed exact route then passed **19 focused checks** from its own clean source export. The scope included the live and archived Kalshi chain and the Polymarket and historical model controls. [Build and fixture commands](audit-local-verification.md) · [Python logs](../.data/browser-verification/slice11-closeout/)
- Empty Trade, Weather, Backtest, Market Archive and Research rendered. Bound HTTP verified Host and Origin, JSON limits, read/export paths and no protected writes after rejected requests. The offline fixture created one event, four rounds and 24 depth images. Chrome saved `55f689d562ca07642aec5996`, reopened that POST’s ID and showed 5 signals, 1 fill, 3 skips, 1 awaiting recheck and 1 unresolved payout. CLI `paper_runs rerun` matched the frozen v3 result. The gzip SHA-256 remained `5cebb8422738eae95cb22b290c609fc9afdcc9b460194168cff6310da29d0f2a`. Python’s audit logged zero outbound DNS/socket attempts and Chrome logged zero external page requests. Registry installs and advisory checks ran separately. [Fixture response](../.data/browser-verification/slice11-closeout/fixture-backtest.json) · [CLI rerun](../.data/browser-verification/slice11-closeout/fixture-rerun.json)
- Chrome’s fixture checks reproduced ES2022 compatibility, the pinned forecast origin, and an open, focused Book evidence disclosure after refresh from receipt #1 to #2. Restored and installed Chrome opened the exact November 6 Central Park 95¢ YES quote from 20:59Z at the 21:00Z origin with no model probability. NOAA 67°F guidance remained available from its 18:53:57Z receipt. Forecast ahead and reload preserved exact side, station-day and **21:00Z** origin while the valid clock moved to **November 7 04:00Z** on the restore and **18:00Z** on the installed retry. The November 15 19:00:25Z control retained **8.8295997%**, model receipt `34adf181d7ab8f7495c7178605e1a9231801df8c761d9d58cfadac300750eff5`, 12:00Z issue and 13:25:17.018083Z availability. The normal and disposable listeners reopened original Miami `40ee74c6230a6dd3846fcbb7` with three signals, one fill and $1.3433 committed. Its v2 rerun and original gzip hash `6ac624aae8685257aca1378b28a043ad72ca4d5ca5bc162db19e3b1a4a573af4` matched. [Restored HTTP witnesses](../.data/browser-verification/slice11-closeout/restore-http.json) · [Restored Chrome](../.data/browser-verification/slice11-closeout/chrome-restored.json) · [Installed Chrome retained retry](../.data/browser-verification/slice11-closeout/installed-retained/chrome-installed-retained.json)

### Public advisories

Checked November 17 at 15:54Z against the public registries. `pip-audit 2.10.1` scanned 19 resolved macOS Python pins through the PyPI advisory database. A second pin-only pass included the Windows marker for `colorama==0.4.6` to cover all 20 locked distributions. npm 11.19.1 `npm audit --json` reported 35 dependencies including development and optional package metadata. Both reported zero findings, with no skipped Python pins or audit registry errors. Locks were byte-identical in both source exports. This is registry-level coverage for the resolved package versions. Native libraries inside wheels, the base interpreter, Chrome and the OS are outside this scan. npm’s esbuild install-script policy notice was present and the locked build succeeded. No dependency change was warranted by the results. [pip-audit](https://pypi.org/project/pip-audit/) · [npm audit](https://docs.npmjs.com/cli/v11/commands/npm-audit/) · [Lock checksums](../.data/browser-verification/slice11-closeout/locks.sha256)

| Lock | SHA-256 |
| --- | --- |
| `uv.lock` | `41e86769079e06ffa8e4bb0e86cfed7e7f9203842360d4214e62b1489624b6db` |
| `package-lock.json` | `617e08cd680eee6aa78c2423bbd8a70c217cbbe3a3a8395ca5fb6aa608bd1a9b` |

## Representative restore and ledger

The verified Slice 7 manifest `a96c57f35ffa62a1bb9194a0ec9cb5d8e16cf22f32dfb8212771ee2cbac2cfc7` contains **4,848 files, 7,541,567,211 logical bytes** and all five authoritative SQLite databases at separate recorded acquisition cuts. The subset includes seven original/controlled live capture files, two fixture captures, selected original station/weather and model evidence, frozen paper artifacts and the saved outbox proposal. A fresh restore verified against the bundle before index rebuild. The source subset and restore ran separately from the live collector. [Manifest verification](../.data/browser-verification/slice11-closeout/restore-verified.json) · [Slice 7 scope](audit-archive-recovery-verification.md#coverage-and-boundary)

Before boot, order `706c8b24-0007-4000-8000-000000000007` held **$478.29 cash / $21.71 reserved** and pending proposal `d8c3082753b8abf64f8333155550a5e3df756561007bb1477036f72596605c2a`. The reconciler published entry `65fa27a7c45c9304f786867d` from the proposal, then payout child `3fac9ce8dfdef823c2757ebb` from the restored native payout capture. The first terminal check read SQLite before any ledger GET. Result was one $200 payout, **$678.29 cash / $0 committed**, one order, zero exposure and no proposal. Fixture remained $488.016 cash / $11.984 committed. Both artifacts reran correctly. Read-only GETs for both namespaces and the subsequent listener restart preserved database and artifact hashes. The fresh payout child has the same ID and inputs as Slice 7, with its own save timestamp. The disposable listener retained its Stop marker and made zero outbound attempts. [Before and after ledger](../.data/browser-verification/slice11-closeout/ledger-before-restart.json) · [Restart hash checks](../.data/browser-verification/slice11-closeout/ledger-restart-hashes.log)

## Archive growth

The live measurements started November 17 at **15:54:12.490760Z** and **16:03:15.099793Z**. Free bytes on the shared APFS data volume went from **62,352,355,328** to **53,540,564,992** while the disposable restore and verification workspace were present. Over those nine minutes the live root added **23,347,412 logical bytes** and **33,120,256 allocated bytes**, principally in the venue store and the rebuildable live capture index. APFS clone accounting can count shared extents per file. Use volume free bytes for headroom. [Before](../.data/browser-verification/slice11-closeout/archive-before.json) · [After](../.data/browser-verification/slice11-closeout/archive-after.json)

| Path | Logical before → after bytes | Allocated before → after bytes |
| --- | ---: | ---: |
| Live root | 34,231,408,857 → 34,254,756,269 | 34,492,710,912 → 34,525,831,168 |
| Venue SQLite | 8,627,777,536 → 8,644,943,872 | 8,641,409,024 → 8,658,186,240 |
| Capture index | 855,953,408 → 858,882,048 | 856,158,208 → 869,576,704 |
| Capture JSON | 464,247,183 → 465,571,477 | 483,901,440 → 485,310,464 |
| Expansion | 9,864,069,185 stable | 9,937,903,616 stable |
| HRRR | 5,244,552,611 stable | 5,261,598,720 stable |
| GEFS | 1,799,530,537 stable | 1,816,797,184 stable |
| OMO | 695,618,769 → 696,997,025 | 700,608,512 → 702,058,496 |
| Research | 473,315,805 stable | 477,282,304 stable |
| Marine SQLite | 85,884,928 → 86,286,336 | 86,872,064 stable |
| Weather SQLite | 10,702,848 → 10,801,152 | 11,030,528 stable |
| TWC SQLite | 4,489,216 stable | 5,308,416 stable |
| Paper ledger and frozen order runs | 94,417 stable | 159,744 stable |

The fresh restored working root measured 7,541,957,104 logical and 7,569,846,272 allocated bytes after rebuild and recovery. It proves the named records, the five databases at their manifest cuts and the ledger receipt chain. A full bundle plus a full restored working copy on this shared volume is outside the measured headroom and operation. Whole-archive coherence, other raw weather and research populations, full rebuild time and disaster recovery remain open at that scope. No archive retention change was made. [Restore measurement](../.data/browser-verification/slice11-closeout/archive-restored-size.json) · [Archive procedure](archive-recovery.md)

## Reproduce the evidence

From the repository, use the requested starting tree or the verified candidate tree above for the clean export. The [bound fixture procedure](audit-local-verification.md#clean-export-and-locked-build) has the exact build and fixture commands. For a new restore, use the [published Slice 7 bundle and restore commands](audit-archive-recovery-verification.md#exact-commands), a fresh disposable root, then the `recover.sh` and Chrome harnesses in the local evidence directory. The installed checks used normal Trade cards and read-only loopback requests. The fixed-clock browser harness pins only the selected market’s tape and decision clocks. `SHA256SUMS` covers the machine-readable reports, raw HTTP observations, joined source bodies, screenshots, command logs and harnesses.

```sh
audit=.data/browser-verification/slice11-closeout
python=.venv/bin/python
old_samples=.data/browser-verification/funnel-20261115/observations.jsonl
# Use fresh outputs when repeating the live sample. Its clocks will differ.
"$python" -m weather_edge.trade_audit sample --port 8765 --output /tmp/slice11-sample.jsonl
"$python" -m weather_edge.trade_audit observed --observations "$audit/live-samples.jsonl" \
  --through "$(cat "$audit/live-baseline-cutoff.txt")" --output /tmp/slice11-before.json
"$python" -m weather_edge.trade_audit observed --observations "$audit/live-final.jsonl" \
  --through "$(cat "$audit/live-final-cutoff.txt")" --output /tmp/slice11-installed.json
"$python" -m weather_edge.trade_audit observed --observations "$old_samples" \
  --start 2026-11-16T03:34:00Z --through 2026-11-16T03:46:30Z --output /tmp/slice11-retained.json
miami=tc-temp-miahigh-2026-11-15-gte81lt82f
for clock in 02:24:16.891316 02:28:48 02:28:58; do
  "$python" -m weather_edge.trade_audit exact --venue polymarket-us --instrument "$miami" --side YES \
    --start "2026-11-16T${clock}Z" --through 2026-11-16T02:40:00Z --output "/tmp/slice11-${clock}.json"
done
"$python" -m weather_edge.trade_audit exact --venue kalshi --instrument KXLOWTNYC-26NOV16-B37.5 \
  --side YES --start 2026-11-17T00:52:32Z --through 2026-11-17T00:53:30Z --output /tmp/slice11-kalshi.json
for kind in replay sensitivity; do
  "$python" -m weather_edge.trade_audit "$kind" --start 2026-11-16T02:24:00Z \
    --through 2026-11-16T02:40:00Z --output "/tmp/slice11-${kind}.json"
done
"$python" -m weather_edge.archive_backup measure .data --volume /tmp > /tmp/slice11-archive.json
```

```sh
uv export --locked --no-emit-project --no-hashes --output-file /tmp/slice11-python-lock.txt
uvx --from pip-audit==2.10.1 pip-audit -r /tmp/slice11-python-lock.txt --no-deps --disable-pip --format json
sed -E 's/ ; (sys_platform|implementation_name).*//' /tmp/slice11-python-lock.txt > /tmp/slice11-all-pins.txt
uvx --from pip-audit==2.10.1 pip-audit -r /tmp/slice11-all-pins.txt --no-deps --disable-pip --format json
npm audit --json
```
