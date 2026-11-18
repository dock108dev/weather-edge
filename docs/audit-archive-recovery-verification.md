# Slice 7 · Archive recovery rehearsal

## Coverage and boundary

The staged starting tree was `dfc04af62ff9d0717782475196d1de163a89ca09`. Port 8765 served `app-AT27CFET.js` as PID 52663, holding the paper reconciliation lock. Collector PID 1709 was enabled and running. The backup command rejected that live root because of the held collector lock. Neither process was restarted or paused during the drill. The existing unstaged README link was preserved.

The representative source was `/private/tmp/weather-slice7.bI4pKA/seed`, separate from the live data. It contained all five authoritative SQLite databases in full; the recorded station day for the early replay, raw station and replay files, model artifacts and availability receipts, selected Research records, Backtests, saved Weather runs, the original fixture accounts and both fixture captures, and selected live captures. The synthetic materials were clearly separate from live trading: a disposable Stop marker and IEM backoff, a controlled November 14 pending order and frozen reconciliation proposal, and three controlled capture files including its later payout. Venue pacing files were copied separately into the seed. The seed omitted most of the raw weather and research directories and most live captures. No extraction trimmed the venue database or its embedded search tables.

The authoritative stores were acquired one at a time with pinned SQLite read snapshots while live collection continued: venues at 04:11:29.229529Z, weather at 04:11:39.193120Z, marine at 04:11:39.209904Z, TWC at 04:11:39.315961Z, and ledger at 04:11:39.329188Z on November 17 UTC (November 16 Eastern). The named case checks establish consistency of the selected evidence across these acquisition cuts. The completed seed was quiet from the backup's opening inventory at 04:24:20.043960Z through the closing inventory at 04:24:33.581471Z. The separate database cutoffs and schema digests are recorded in the manifest. The latest raw receipt ID and book batch cutoff, database checks, and manifest file hashes were validated against the published bundle.

The backup contained **4,848 files, 7,541,567,211 logical bytes (7.02 GiB), and 19 frozen paper run files**. Manifest SHA-256: `a96c57f35ffa62a1bb9194a0ec9cb5d8e16cf22f32dfb8212771ee2cbac2cfc7`. Candidate code and tests were staged as `fef4a7762bf78b7546a3189c76b6d20d8d2eed8a` before adding documentation. Asset `app-AT27CFET.js` had SHA-256 `e9573a8a1b3457d46b3a4a6b7d2f20d7f0927b2ef2f8fd54fb460d5de69d724b`; the manifest also identifies HTML and CSS. The build from copied locked dependencies matched all three.

## Exact commands

From the repository checkout, with new bundle and restore paths (or a fresh sibling pair when repeating):

    r=/private/tmp/weather-slice7.bI4pKA
    data="$r/seed"
    bundle="$r/final/archive"
    restored="$r/final/restored"
    .venv/bin/python -m weather_edge.archive_backup backup "$data" "$bundle" --quiescent --source-id fef4a7762bf78b7546a3189c76b6d20d8d2eed8a --scope representative-seeded-slice7 --venue-receipt 162551
    .venv/bin/python -m weather_edge.archive_backup verify "$bundle"
    .venv/bin/python -m weather_edge.archive_backup restore "$bundle" "$restored"
    .venv/bin/python -m weather_edge.archive_backup verify "$bundle" --restored "$restored"
    .venv/bin/python -m weather_edge.capture_index --directory "$restored/captures"
    .venv/bin/python -m weather_edge.capture_index --directory "$restored/fixtures" --fixture
    PYTHONPATH="$r/offline:$PWD" sandbox-exec -f "$r/offline/server.sb" .venv/bin/python -m weather_edge.server --no-collect --data "$restored" --port 18765

The macOS sandbox denied outbound connections. A Python socket audit counted app network attempts, allowing only the HTTP server's reverse lookup of its numeric loopback bind address. Chrome ran with non-loopback host resolution disabled, and the page request audit checked the requests made by the restored app. The initial audit guard also trapped the local reverse lookup before the listener bound; that harness failure is preserved. The corrected guard kept the external-denial checks.

## Restored witnesses

| Case | Result |
| --- | --- |
| Kalshi Central Park low, `KXLOWTNYC-26NOV16-B37.5`, YES at `2026-11-17T00:52:32Z` | Receipt `162551` decompressed to SHA-256 `6e7d4eee55cd8d3d4d0445dc4c0fb215807d9a5c3c247f37213f5a87be66fd78`, identical to `receipts.digest`. HTTP and Chrome showed 140 contracts, a $1.35 indicative fee, and no exchange image clock. |
| Miami capture `20261116T022828-de303907.json` | Rebuilt index selected the exact book for market `2487915`, YES `4975216`, NO `4975217`. The complete indexed ladder matched the source JSON. SHA-256 `3c194e645c4e6d2efd17382307327373b4d0fd1e84bc29ed05d41565ffc599c8`. HTTP receipt `02:28:27.673275Z`, exchange image `02:27:26.647556973Z`, first completed capture `02:28:28.880002Z`. Rebuilding fixtures separately yielded both originals with matching digests. |
| November 6 Central Park 70–71°F YES at `21:00:00Z` | Reopened at 95¢ from the `20:59:00Z` sample. The 67°F NOAA forecast issued at `18:00:00Z` and available at `18:53:57Z` remained visible. The selected clock lacked a qualifying model. Chrome followed Trade to the station weather. |
| November 15 Central Park positive control at `19:00:25Z` | Retained 8.8295997% model odds with digest `1a937086151da8cc9b7e54bb8122e844ae72f3f382e090eb144045e6ca25c8ff` and receipt `34adf181d7ab8f7495c7178605e1a9231801df8c761d9d58cfadac300750eff5`. The forecast was issued at `12:00:00Z` and available at `13:25:17.018083Z`. Chrome opened the Model evidence panel. |
| Miami Backtest `40ee74c6230a6dd3846fcbb7` | HTTP and Chrome opened the original **3 signals, 1 fill**, $498.6567 cash and $1.3433 committed. SHA-256 `6ac624aae8685257aca1378b28a043ad72ca4d5ca5bc162db19e3b1a4a573af4`. The offline recomputation matched the original frozen result. |
| Raw ASOS minute trace | The first copy preserved the CSV digest but changed the reported local retrieval time from `2026-11-15T07:06:33.439251Z` to the copy time. With source modification times recorded and restored, the app's raw trace builder reproduced the original retrieval clock and all 1,440 observations from the final restore. CSV SHA-256 `8ce27b8f211ae29ded82f405d3e8fd38f1ef9821a8543775c7ffc3bca8bf8fe0`. |

Preboot verification found no standalone indexes, process PID, or reconciliation lock. The expected Stop marker hash was `c4f951f605f5ce128c064a8b0e199a84d4416b7755bbcf9453cf5c75b67046ef`; the collector stayed stopped after startup and the marker and IEM backoff were unchanged. The preserved book and weather timestamps are evidence clocks in UTC.

## Ledger and failures

Preboot the disposable live namespace held **$478.29 available, $21.71 reserved**, one pending order `706c8b24-0007-4000-8000-000000000007`, and one recorded proposal with SHA-256 `d8c3082753b8abf64f8333155550a5e3df756561007bb1477036f72596605c2a`. Its expected entry `65fa27a7c45c9304f786867d` had not been published. On boot the worker published that exact run, then consumed the archived synthetic payout to settle 200 contracts once. The live namespace ended at **$678.29 available, $0 committed, no exposure**, one order, and an empty proposal table. Fixture balances remained **$488.016 available and $11.984 committed**, with two original rows and the original station-day exposure. Restart left the counts, account summaries, and hashes unchanged.

- Entry `65fa27a7c45c9304f786867d`: SHA-256 `a724d08a3a1f5fbe8c97a988178aba41337c144b20728f8e238350684f2700b6`.
- Payout child `3fac9ce8dfdef823c2757ebb`: SHA-256 `7c7fd219956ad55739a1147cb304b77483b78cc6f4e5ce1039c3354b74cec701` in the final restore, unchanged after restarting. Its archived payout source was `20261115T050400-734c0105.json`, SHA-256 `9acd78622194424df054e60d83db888760e60292890b32c9e751b0471109e3ab`. Independently publishing the child in the fault branch yielded the same ID and accounting; the file has its own save clock.
- In a separate APFS clone of the first fresh restore, the entry was committed and its file was removed before boot. The worker identified the order and required run and left $21.71 committed. The missing Miami Backtest returned HTTP 400 naming its exact file, while healthy Weather run `ecd402e5bbd0faa43788dd1b` opened. Restoring just the ledger parent and restarting produced the same payout child and cash; the unrelated Backtest remained absent in that damaged root.
- Process-exit tests interrupted backup and restore after a file write. Only a named incomplete workspace remained; verification rejected it and a fresh retry succeeded. Separate tests rejected a held lock, a changing source, an occupied or nested destination, an altered same-size backup file, and an altered original-file timestamp. A committed-WAL control verified that directly opening the main SQLite file missed the new receipt while the backup recovered its exact body.

The stopping condition was the owner-specified disposable restore, exact evidence, index rebuild, successful and damaged ledger recovery, and local checks. The representative pass does not qualify the entire archive for disaster recovery. It established those results without a monitoring soak or interrupting the live collector.

## Disk measurements

The comparable live-root samples began at `2026-11-17T04:16:17Z` and `04:30:37Z` (14m20s apart, both November 16 Eastern). They measure regular files, excluding directory metadata and symlinks. Logical size is file length; allocated size sums per-file blocks. APFS shares extents for clones, so the allocated sum can count the same physical blocks more than once. Free bytes come from the filesystem. The live, backup, and restored roots all shared the same APFS data volume.

| Live path | Start logical MiB | End logical MiB | Change MiB | Dependency |
| --- | ---: | ---: | ---: | --- |
| Whole root | 31,082.73 | 31,113.80 | +31.06 | Allocated 30.61 → 30.63 GiB (+17.85 MiB) |
| `venues.sqlite3` | 7,059.57 | 7,084.88 | +25.31 | Original venue bodies, revisions, and embedded index |
| `capture-index.sqlite3` | 647.36 | 650.96 | +3.60 | Rebuilds from `captures/` |
| `captures/` | 351.07 | 352.81 | +1.74 | Original sessions and clocks; no demonstrated replacement |
| `marine.sqlite3` | 64.16 | 64.52 | +0.35 | Original marine receipts and data |
| `omo/` | 635.73 | 635.82 | +0.09 | Raw model vintages; keep |
| `weather.sqlite3` | 8.2188 | 8.2227 | +0.0039 | Original weather revisions and source state |
| `fixture-index.sqlite3` | 0.0977 | 0.0977 | 0 | Rebuilds from `fixtures/` |
| `twc-climate.sqlite3` | 4.2813 | 4.2813 | 0 | Climate receipts and reports |
| `trade-paper/orders.sqlite3` | 0.0742 | 0.0742 | 0 | Ledger intent, results, and outbox |

The two samples found no nonempty WAL files; the per-store measurements distinguish missing and zero-byte sidecars. The WAL regression separately proves that a nonempty committed WAL is included. The large `expansion/` (9.18 GiB), `hrrr/` (4.87 GiB), and `research/` (451.39 MiB) paths were stable in this interval. The full per-path logical and allocated samples are in the evidence directory.

Free space went from 98.00 GiB after seed acquisition to 69.72 GiB during the final drill and 69.64 GiB at the final bundle/restore measurement. That change includes the disposable copies from both passes and other volume activity; it is not live archive growth. The final bundle data and booted restore each reported 7.02 GiB logical (7.05 and 7.04 GiB allocated, respectively). The rebuilt sample indexes were 274,432 and 102,400 logical bytes. Two complete independent copies at today's sizes would require roughly 60 GiB of file data before allowing for rebuilt indexes, other volume activity, and ongoing collection. That is a headroom estimate, not a demonstrated full backup footprint or a retention trigger. The drill used no deletion or collection policy on the real archive.

## Local closeout

**213 Python tests passed**, the frontend build matched the assets served on port 8765, and the offline Chrome rehearsal recorded 63 loopback page requests and no external requests. The healthy listener acquired the disposable lock as PID 1278 and reopened as PID 4954. Its two ledger summaries and run hashes matched. The server network audit recorded zero provider attempts in the healthy and damaged rehearsals. Normal listener PID 52663 and collector PID 1709 retained their original locks. The live ledger, seven frozen ledger artifacts, original Miami run, and collector worker record matched the protected hashes; the live Stop marker stayed absent.

Manifest, index and HTTP witnesses, Chrome screenshots, disk samples, fault logs, and final staged tree are under `.data/browser-verification/slice7-closeout/`. The final staged tree is recorded there rather than in this tracked document because altering the document changes that tree. No commit, deployment, schema migration, or change to the normal listener was needed.
