# Local archive backup and restore

Take a backup from a **quiet data root**. A copy of a running data directory can capture a ledger commit after copying its run directory, or a SQLite main file that omits work committed in the WAL. Keep the matching source revision, locked dependencies, and build separately. The manifest identifies the source and served entry assets by SHA-256.

| Source under the data root | What to retain and why |
| --- | --- |
| `venues.sqlite3` | Keep the complete database, including raw HTTP bodies, receipt digests, book batches and their publication clocks, listings, fee revisions, payouts, and the embedded search index. Trade, Market Archive, and ledger recovery read this store. |
| `weather.sqlite3`, `marine.sqlite3`, `twc-climate.sqlite3` | Keep complete stores, including weather revisions, their receipt clocks, health state, and marine and climate receipts. |
| `captures/`, `fixtures/` | Keep original JSON files and their exact member identities, exchange image, HTTP receipt, and completed capture clocks. They are the inputs to the corresponding standalone indexes. |
| `archive/`, `archive-raw/`, `replay-raw/`, `replay.json`, `archive-observation-enrichment/` | Keep published station days, enrichment, and their source archives, including `madis-metar/`, `nbm-cards/`, and `expansion/replay/` when present. The ASOS minute trace uses the raw file's modification time for its local retrieval clock. |
| `model/`, `model-availability/`, `study/`, `research/` | Keep artifacts, vintages, availability proofs, and Research records with their evidence and frozen inputs. A proof can reference a frozen paper trial; Research records can reference other files under `research/`. |
| `paper-runs/`, `backtests/{live,fixture}/paper-runs/`, `trade-paper/paper-runs/`, `trade-paper/orders.sqlite3` | Keep frozen run bytes, entry and payout parents, both namespaces of the ledger, and its reconciliation proposal table. |
| `collector/paused`, `collector/listings.json`, `collector/iem-backoff.json`, `venues/*.pace` | Preserve bytes when present and absence of the Stop marker when enabled. Retain telemetry in the weather store. Fresh processes acquire their own locks and publish their own PID state. |
| Other paths, including `expansion/`, `hrrr/`, `gefs/`, `omo/`, layers, and research source directories | Retain them by default. A downloaded or transformed file can be the last local copy of a vintage, intermediate result, or publication clock. This drill does not certify reconstruction of those trees. |

The backup keeps regular files other than the two standalone indexes and their sidecars, the collector and reconciliation locks, `collector/worker.json`, and the external installed-dependency link at `strategy-snow/alta/reader/node_modules`. It writes SQLite backups in place of database main/WAL/journal sets. Unexpected links and special files fail the backup. Ordinary file modification times are verified as well as bytes.

## Take, check, and restore a backup

Use the matching project checkout. Choose existing, private parent directories with space for the bundle and restore. The source, bundle, and restore must be distinct and non-nested. Both output names must be unused.

For a full backup, close app tabs and stop all writers: the listener with its paper reconciler, the collector, and any standalone backfill, indexer, Research or export job. Use Ctrl-C for a foreground listener. For a detached worker, verify its actual PID and data root in the process list, then use `kill -TERM PID` and wait for it to exit. Check `lsof .data/collector/worker.lock .data/trade-paper/reconcile.lock` and confirm standalone writers are gone. Collection Stop alone does not stop the paper reconciler. Preserve the existing Stop marker state; resume the original processes after the backup according to that state.

    data="$PWD/.data"
    backup_parent=/path/to/existing/backup-parent
    restore_parent=/path/to/existing/disposable-parent
    bundle="$backup_parent/weather-edge-backup"
    restored="$restore_parent/weather-edge-restore"

    # Use the commit or staged tree that identifies the source you actually built.
    source_id=$(git write-tree)
    .venv/bin/python -m weather_edge.archive_backup measure "$data" --volume "$backup_parent" --volume "$restore_parent"
    .venv/bin/python -m weather_edge.archive_backup backup "$data" "$bundle" --quiescent --source-id "$source_id" --build "$PWD/dist"
    # Add --venue-receipt ID, repeatable, to attest additional exact raw receipts.

    .venv/bin/python -m weather_edge.archive_backup verify "$bundle"
    .venv/bin/python -m weather_edge.archive_backup restore "$bundle" "$restored"
    .venv/bin/python -m weather_edge.archive_backup verify "$bundle" --restored "$restored"

    .venv/bin/python -m weather_edge.capture_index --directory "$restored/captures"
    .venv/bin/python -m weather_edge.capture_index --directory "$restored/fixtures" --fixture
    .venv/bin/python -m weather_edge.server --no-collect --data "$restored" --port 18765

Complete the preflight before rebuilding or booting. Startup can attest model availability, and the ledger reconciler starts under `--no-collect` and may publish fills and payouts from retained evidence. For an offline drill, also block server outbound access and avoid manual provider actions such as **Fetch public book**. Collection Stop and ledger reconciliation are independent.

The tool refuses held collector or reconciler locks and checks the listing and metadata of source files before and after copying. The operator still has to stop standalone writers. A read transaction pins each store while the SQLite backup API copies committed WAL contents. The tool checks schema and logical records, and produces a self-contained database in DELETE journal mode. If resuming operational writes, restore the prior journal mode where needed after preflight. The manifest records store cutoffs, venue receipt IDs and decompressed body digests, a book-batch cutoff, exact ledger and reconciliation summaries, both accounting namespaces, and saved run IDs. Committed ledger runs and their parents must reopen and reproduce, and a saved proposal must reproduce and match its order. File hashes are for the backup bytes; a SQLite copy need not hash to the live main file.

A live read of one SQLite database can be consistent while its neighbors move. Record separate acquisition cutoffs for any test seed assembled from live reads, and confirm the exact cross-file cases. That seed does not represent a coherent whole archive snapshot.

## A failed operation

The command stages work in a sibling directory called `.<target>.incomplete-…` and publishes by rename. Verify rejects unfinished work. Keep the error log and retry to a fresh destination, leaving the source and known-good bundle untouched. Compare the reported manifest SHA-256 with your separate record to detect substitution of the whole manifest.

For a damaged restore, use the verified bundle and a fresh destination. For isolated ledger recovery, stop the disposable app, put back the exact artifact named in the diagnostic from a verified copy, and restart. If a simulated position is missing its entry run, the payout remains pending and its commitment stays accounted for. A run embedded in an unpublished proposal can instead be recreated from the proposal.

This work does not change app schemas. For a future migration, keep the pre-migration bundle and its matching build. If the migration fails, restore the bundle to a new root and use the matching code; leave the failed candidate available for diagnosis. Use separate physical storage for a backup intended to survive disk loss.

The [Slice 7 verification record](audit-archive-recovery-verification.md) identifies the restored population and the cases it proved.
