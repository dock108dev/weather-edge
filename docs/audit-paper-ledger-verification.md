# Slice 6 · Unattended paper ledger and crash recovery

## Baseline and reproduced failures

The starting index was `93b712f00dab0207c3e5b82c27f2607bf00560a6`. Port 8765 was running Slice 5 as PID 1548, with `app-X4FAQKKP.js`. Collector PID 1709 was enabled and running. The README remediation-plan link was the sole unstaged edit. The live ledger had two existing fixture records and no live orders.

The Slice 5 code reproduced all three failures through bound HTTP handlers on disposable roots:

- An empty `GET /api/trade/orders` created the ledger. A due order `8d825c00-d97d-413d-91c8-fb30a26e8296` remained pending until GET, then moved to Simulated and published `258da94b4eebf61bc4ed1f9a`. The old handler held the ledger writer transaction through archive reads, evaluation, publication, and payout work.
- A process exited after publishing entry run `fe093c9376b12454c46be0f0` and before updating order `8f3a3c17-9e75-4806-b2f5-4237207a2b82`. SQLite rolled back to pending. The retry at a later wall clock published `0ebccfa67929df4b9f36f09e`, leaving the original artifact on disk.
- An app offline across the recheck and payout recovered 200 contracts at $21.71 on restart, but entry run `57d918a97375365e509ad1eb` froze at `2026-11-16T05:09:00Z`. The payout receipt was already available at `05:04:00Z` and failed the later-than-parent check. Cash stayed $478.29 with $21.71 committed.

The stopping condition is the owner-specified closed-tab flow, restart recovery, and consistent accounting on the candidate, plus the requested local checks and cutover. Controlled evidence clocks and a durable transition witness answer those questions without a live soak. UTC below refers to archived evidence clocks or timestamps explicitly marked UTC.

## Ownership and operation

The loopback listener starts the paper worker even with `--no-collect`. Once a ledger exists, one listener holds `trade-paper/reconcile.lock` under the resolved data root. Other listeners can serve GET and POST and check periodically for the lock. A POST wakes its own listener; the fallback poll also sees work submitted through another listener. Normal interrupt joins the worker before releasing ownership. If a process exits abruptly, the OS releases the lock and the next owner resumes from SQLite. Keep the lock file in place. Two distinct data roots remain independent.

Collection Stop only controls collection. The worker reads retained evidence and can advance paper orders while collection is stopped. It cannot obtain an uncollected book or payout and does not start the collector, call a provider, or modify the Stop marker. When no listener is running, catch-up begins on the next owner startup. Errors such as an unreadable archive or a failed publication appear with order ID and reason in the listener log. They leave that transition retryable and allow other orders with readable evidence to advance.

The ledger GET opens SQLite in read-only mode, using one snapshot for rows, cash, committed funds, and station-day exposure. If no ledger exists it returns $500 and empty lists without creating the root. Trade refreshes pending orders and live positions awaiting payout while visible, and refreshes on tab return. Fixture orders use their own namespace and retained fixture clock; they have no payout feed.

## Recovery and accounting

Submission uses one short writer transaction to match the request, check cash and the station-day cap using the contract's local day, and reserve the reviewed cost. Exact retries are resolved before repricing and return the same order after expiry or settlement. Different venue, instrument, side, budget, or saved review key returns HTTP 409 for a reused ID. Older rows without a saved review key can only compare venue, instrument, side, and budget.

Entry evaluation uses the saved signal, due time, deadline, exact native members, reviewed price/size, model identity, and locked fee schedule. V3 freezes the first eligible decision at its app-availability clock, or freezes a no-fill window at its deadline. A book first available later cannot change either result. Legacy v2 still uses the receipt clock and its existing archive grace; v2 Ridge prefixes still resolve the reviewed digest. The worker uses the retained model vintage when the active artifact rotates. A missing referenced artifact keeps the order retryable. Both the archive reads and the replay happen outside the ledger write transaction.

The first transition write records the fully frozen proposal under the order key, with a fingerprint of the expected row. The second step publishes and verifies the run. The last write compares the pending intent or simulated parent to the expected row, updates accounting by replacing the order result, and removes the proposal in the same commit. On restart an existing proposal bypasses archive/model evaluation; already published bytes are checked and reused. Payout uses its own verified receipt clock and a child linked to the entry run. The child retains the exact receipt ID and digest, and publication verifies that entry terms still match. A verified $0 payout also closes the position and releases its commitment.

## Controlled checks

| Process exited after | PID | Order | Verified entry run | Verified child | Final cash / committed |
| --- | ---: | --- | --- | --- | --- |
| Reservation commit | 48517 | `38070964-35d8-4655-9147-74fcfb0e3c52` | `c1ac3aeb860037bb4193515f` | — | $478.29 / $21.71 |
| Entry proposal commit | 48559 | `b480861e-98d1-4747-9d39-7a2c014013a7` | `99c4a9b4150c35782ee5307c` | — | $478.29 / $21.71 |
| Entry artifact publication | 48613 | `d3e67310-9675-44a9-a03b-e5e45cc89c0d` | `6d149afa698355e96a2c542d` | — | $478.29 / $21.71 |
| Entry terminal commit | 48655 | `e93299fa-cd78-4616-8e1a-24fa00cda181` | `9050e5db0d917ad30f19704f` | — | $478.29 / $21.71 |
| Payout proposal commit | 48677 | `f0ba92ff-173a-4ffa-abfb-567962637dba` | `2d13a198001ad7268d8a9124` | `377be31b68fe7ea9c4b9cf72` | $678.29 / $0 |
| Payout artifact publication | 48697 | `5ffa47f6-6791-428d-a050-7b1b3d47cb63` | `d48742d6c532b6aae5dbdfc0` | `8a857afbdb23168fdf6c7756` | $678.29 / $0 |
| Payout terminal commit ($0 YES payout) | 48746 | `4d8cb300-8669-4430-97c4-042a755a1925` | `1c4dd1f1358ef182dca04011` | `8fa7eba56a00d1a718151115` | $478.29 / $0 |

The payout rows stayed offline through the entry and the later native payout receipt, then restarted. Subsequent passes and exact HTTP retries made no accounting or artifact changes. In a copied, identical tape, online evaluation and late catch-up produced the same entry ID. Each published file's hash and the native payout receipt numbers are in `.data/browser-verification/slice6-closeout/restart-boundaries.txt`.

- Late app availability left v3 order `2c17dccb-463b-4754-bb58-5e6363910586` skipped in `5ae377dbac98ba7be482161b`, restoring $500 cash with $0 committed and no exposure. With the identical tape and a v2 intent, the worker waited through the grace period and used the in-window receipt in `61b519b67ecb872b07cc8e34`.
- A blocked model evaluation left GET and a second ledger writer free to proceed. A commit between GET's allocation and row queries still produced one consistent snapshot. Identical HTTP submissions returned a single reservation; conflicting requests returned 409. Distinct requests competing for final cash or station-day room admitted one winner.
- A broken capture file left a live order pending while a sibling fixture advanced. A separate publication failure left the proposal in SQLite; retry used its ID after the fault was removed. The listener's ordinary no-recheck wait and an evidence/archive failure are separate outcomes.
- The rehearsal used SQLite backup and copied the related run files. It preserved the real fixture Simulated and Skipped rows, plus a disposable settled row `886d28ea-b0f4-4622-a21b-b10169f3293f` from Slice 5 code with child `5f7d3ff1cbaa17ad35751924`. V2 pending order `810447ec-5cb1-4811-bcc1-0dc5a03d14b9` advanced to `254f10126f88a3ee4875a13c` without changing its key or intent. Cash remained $656.58 and the KNYC November 15 commitment/exposure stayed $21.71 as it moved from reserved to filled. The fixture snapshot and all nine original run hashes were unchanged. The outbox table was created lazily on the disposable copy; the existing orders schema and rows needed no rewrite. The broader archive restore remains Slice 7.

## Disposable Chrome flow

Chrome recorded order `1e45c914-4199-46ac-85f8-3484a8ce1329` on port 8876 and exited with its only tab closed. The harness then published the delayed receipt and advanced its controlled clock to `2026-11-15T19:00:41Z`. A local SQLite read observed the fill; the HTTP log still had the same two ledger GETs from before closure. Chrome reopened Trade and showed 200 contracts, $21.71 cost, $478.29 available and $21.71 committed. Entry run `a6d72742d2e260d0d1b6ec45` has SHA-256 `c3e8705c311152719919f688861bc2f14c1306a643d98b95747c3ffa82f48e67`.

PID 48518 held the worker lock. PID 48635 on port 8877 was standby on the same root. It acquired ownership after clean shutdown of PID 48518 and settled the open position from a retained native receipt. Chrome's existing page picked up the $200 payout through polling, leaving $678.29 cash and $0 committed. Child run `0dde7924ea3d39b2e91afbae` has SHA-256 `370036ad9b74ff31068139adee2dbe42df220c7f8f5dade725649c7763da3233`. Both listeners used `--no-collect`, and the disposable Stop marker kept the exact same bytes. The screenshots, logs and machine-readable flow record are in `.data/browser-verification/slice6-closeout/`.

Separate bound restarts on port 8878 checked both branches before any ledger GET. The late case, order `c453e03a-61ad-4bc6-bf17-3f8ee27ef12c`, skipped after PID 54769 exited and PID 54831 restarted, saving `56dbf4ada8b003d9fabfbdab` and returning to $500 cash and $0 committed. The offline case retained both the eligible recheck and later payout with the listener down. PID 55017 restarted after PID 54911 and recovered order `8d78d3ed-613e-49b3-9593-87c6f6ba7f69` to entry `3d90ed30f2013aa6f986bf7f` and payout child `bfbf58f5ea11858ba2d0318c`. Final cash was $678.29, with $0 committed and no station-day exposure. The read snapshots, hashes and PIDs are recorded in `listener-branches.json`.

## Normal listener closeout · November 16, 2026

- The final focused regressions passed 55 tests; the consolidated suite passed 206 tests in 27.36 seconds. The frontend build and whitespace checks passed. The normal listener serves `app-AT27CFET.js`, SHA-256 `e9573a8a1b3457d46b3a4a6b7d2f20d7f0927b2ef2f8fd54fb460d5de69d724b`. Served HTML and JavaScript bytes match `dist`.
- PID 52663 replaced PID 1548 on port 8765 with `--no-collect` and held the reconciler lock. Collector PID 1709 stayed enabled and running with the same worker record and no Stop marker. The live ledger had $500 available and $0 committed. The isolated fixture ledger still showed $488.016 cash, $11.984 committed, and the two original rows. The live ledger database and all seven ledger run files matched their pre-cutover hashes; no live orders were submitted.
- Chrome opened the normal Trade view and reopened the original Miami v2 Backtest `40ee74c6230a6dd3846fcbb7` with 3 signals and 1 fill. The original file still hashes to `6ac624aae8685257aca1378b28a043ad72ca4d5ca5bc162db19e3b1a4a573af4`.
- The implementation and tests were staged as tree `9de709046d629974440075ddf0ad579190f40c3b` before adding this verification record and checklist. The final tree is recorded in `.data/browser-verification/slice6-closeout/staged-tree.txt` because embedding it in the staged record would change the tree. The previous index contents and the unstaged README link were preserved.
