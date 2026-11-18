# Model odds remediation · local verification

This records slices 0–2 of the audit. Listing and fee parity belongs to slices 3–4.

## Starting state

- Unborn `main`; existing index tree `228d4f6efd6960e7b3498d768109e3ccefe0ee81`, verified using a copy of the index. The working tree started with an unstaged README link to the untracked audit plan. Both were left as found.
- Normal app on `127.0.0.1:8765` (listener PID 77801 at baseline). Collector enabled and running, PID 23194 since `2026-11-16T05:04:45.452912Z`, with 22 configured stations and 32 reported source lanes. Retained listings included 18 daily highs, six lows and three rain markets. The data root was about 30 GiB; the rebuildable capture index was 611,770,368 bytes at baseline.
- Original assets: HTML SHA-256 `be1bc93202ec36d01c6fbf90e13a5140bb771f1ee1f363baa232af6e092fa9d3`, `app-QNYW4K2C.js` SHA-256 `0d2d2f92a39132287b772d75c00f8c0d58f011f2ec87791a47d479ae76cecba1` and `app-LC5NZTVK.css` SHA-256 `56d821a46dd82c0ceeeec55e8277df22b774dbad8bd73e330d4934da5f430e6f`.
- Representative saved files under `.data`:

| Type | ID | Frozen result | File SHA-256 |
| --- | --- | --- | --- |
| Miami Backtest | `40ee74c6230a6dd3846fcbb7` | 3 signals, 1 fill, $498.6567 cash, $1.3433 committed | `6ac624aae8685257aca1378b28a043ad72ca4d5ca5bc162db19e3b1a4a573af4` |
| Availability witness trial | `ecd402e5bbd0faa43788dd1b` | 21 signals, zero fills, $500 cash | `191453592e9b8d9cf82dae03075bde004eb299e6200e0ce99f43421f840ee8bd` |
| Existing Weather trial | `4988baf9c3305752d65f881c` | 18 signals, 2 fills, $469.3802 cash, $30.6198 committed | `17415fbca9ee8b16b7e1491922e3a46d71d27045facc25c9a30420a9ee4fb051` |

All three use `book-recheck-v2`. The Miami ID reopened in Chrome and reproduced from its original frozen input in the candidate.

## Matched retained cases

The [November 6 ticket](http://127.0.0.1:8765/?view=trade&day=2026-11-06&station=KNYC&venue=polymarket-us&instrument=tc-temp-nychigh-2026-11-06-gte70lt71f&side=YES&archived=1&at=2026-11-06T21%3A00%3A00Z) is Polymarket US Central Park YES, native instrument `tc-temp-nychigh-2026-11-06-gte70lt71f`. The retained input is `.data/archive/2026-11-06/KNYC.json`. At 4 p.m. Eastern the baseline ticket/Weather join showed 1.4% (1.3621206468% from inference). Backtest omitted this model. The candidate ticket and exact station API both withhold the probability, the station timeline has no causal model series for this day, and the Backtest lookup agrees. All retain the 95¢ YES sample at `20:59Z`; Weather retains the 67°F NOAA forecast and 61.0°F most recent observation at origin. The official 70°F report appears at `2026-11-07T06:26:00Z`. Verified YES $1 appears separately at `2026-11-08T02:24:19Z`.

The full study digest is `1a937086151da8cc9b7e54bb8122e844ae72f3f382e090eb144045e6ca25c8ff`. Training evidence cutoff: `2026-10-28T08:40:00Z`. Earliest available receipt: `2026-11-15T07:28:17.382690Z`, frozen trial `ecd402e5bbd0faa43788dd1b`, receipt SHA-256 `34adf181d7ab8f7495c7178605e1a9231801df8c761d9d58cfadac300750eff5`. It is under `.data/model-availability/study/<digest>/<receipt>.json`.

The [qualifying control](http://127.0.0.1:8765/?view=trade&mode=live&history_mode=live&archived=1&day=2026-11-15&station=KNYC&venue=polymarket-us&instrument=tc-temp-nychigh-2026-11-15-gte49lt50f&side=YES&at=2026-11-15T19%3A00%3A25Z) uses November 15 Central Park 49–50°F YES at `19:00:25Z`. The retained book offer was 86¢, size 0.01, received at `19:00:22.784976Z`. Ticket and station decision returned 8.8295997% with the same exact study digest and receipt, using a forecast available at `13:25:17.018083Z`. Fixtures independently exercise the inclusive boundary when either the training cutoff or artifact witness is last.

## Recovery and verification boundary

- Empty legacy artifact on baseline: unhandled `EOFError` from the availability scan; execution never reached listener construction. On candidate: logs `legacy model unavailable (EOFError): model.joblib` and reaches listener construction. The disposable HTTP handler served home, station weather, retained prices and a healthy study estimate. Replacing the legacy artifact reopened the family through the active load path; retained digest `fd2b26762f8c61e26ab8fd45c70b5637ea471fd0858f7103a243d64e1cfe6b30` produced an eligible legacy estimate on November 16 at `23:52:47.088269Z`. New fixture saves freeze the full digest and receipt; an earlier save has no modeled probability.
- Python: 169 passed with the locked virtual environment. An isolated `scan() -> []` negative control failed each of the three repaired quote tests. The HTTP fixture exercises the real handler and uses a real loopback server where binding is permitted; in this session it dispatched raw HTTP requests in process.
- `npm run build` passed in a disposable checkout of the build inputs, using the existing locked dependencies. Candidate HTML SHA-256 `81d34c74edbb804b3ac818339db403451cd40246ee99ccb56c255ce3b1f7b57d`; candidate `app-ZVHIMTBP.js` SHA-256 `6ed531ae7049019c8361a80741fe7761e8c3eadbbb033f37b00cbbc3e5ebb661`.
- Chrome exercised the emitted candidate with recorded responses from the new handler and disposable retained data. Trade → Weather → Forecast ahead → 8 p.m. → reload preserved the 4 p.m. origin and exact Central Park YES. No new price, reading, report, payout or paper action appeared. Replay showed report before payout and withheld the early archived payout disclosure. Returning to Live removed the historical pricing context. The eligible control exposed the full digest and receipt; the Miami Backtest reopened by ID and displayed its original fill.
- The managed shell denied even an ephemeral loopback bind and denied writes to the repository Git index. Opening normal port 8765 in Chrome verified the original app and collecting state. The candidate browser run used a disposable static route on that listener and recorded data, so it does not prove a running candidate backend. The milestone still needs a normal app restart with the candidate and the real index update.

On a shell permitted to bind loopback, rerun the affected tests and build. A disposable startup can use `uv run --locked python -m weather_edge.server --data "$disposable_data" --no-collect --port 18765`. For the normal check, rebuild and restart only the listener on 8765, verify the two links above and [saved Backtest](http://127.0.0.1:8765/?view=backtest&run=40ee74c6230a6dd3846fcbb7), and confirm the independent collector kept its prior state.

## Normal listener closeout · November 16, 2026

The follow-up shell permitted a local bind and writes to the real index. The app now serves the candidate on port 8765 as PID 32237. The collector stayed enabled and running as PID 23194. Built HTML SHA-256 `43ba1da0b9531a46a8ef6e29f7535fae40d4e4de41b8c2fdf35300e0b8c4de3c` names `app-F7SBZCS5.js` with SHA-256 `af01af3cc5494c0e491212b347bf9db9229cc75dbbab765ac864f09b39b90fd9`.

- The disposable instance bound port 18765 with the empty legacy file and served home, the early quote and weather, and the later healthy study estimate through real HTTP. Replacing the file made the legacy digest `fd2b26762f8c61e26ab8fd45c70b5637ea471fd0858f7103a243d64e1cfe6b30` eligible through the real decision route. The final HTTP regression also covers a serialized legacy artifact with a non-callable ridge estimator.
- The normal listener returned the 95¢ November 6 quote with model availability unverified and the November 15 estimate of 8.8295997% with its original digest and receipt. Chrome followed the early ticket into Forecast ahead, advanced valid time to 8 p.m., and reopened with the 4 p.m. origin, same YES contract, and zero report or payout evidence. At the official report clock it showed 70°F and zero payouts. Live capture restored live context. The later ticket showed its full model evidence, and the Miami Backtest opened from its original saved ID. Its file SHA-256 still matches the baseline above.
- Compatibility review found that past pending legacy intents stored the digest prefix in their model version and no full digest. Rechecks now recognize the same legacy model version and method when a valid full digest matches that prefix. Full-digest intents retain exact matching. Replay retains the normal ledger refresh behavior and updates timeline odds when the exact historical decision arrives.
- Browser logs showed two identical pinned ticket reads before the client recorded the already requested historical context. On the final build the same browser route logged one exact ticket request and opened its model evidence. That request completed about 15 seconds after navigation in one run. Loading the retained live session remains the larger cost for the performance slice.
- The final Python suite passed 171 tests with loopback binding available. `npm run build` and staged whitespace checks passed. The real index contains the 17 milestone paths, including the intent compatibility fix and this record. The audit plan and its README link remain as separate working-tree changes.
