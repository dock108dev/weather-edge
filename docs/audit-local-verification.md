# Slice 9 · Local verification

Run these commands on a machine with loopback access and Google Chrome. The source export uses the staged tree, so it works on the local repository before the first commit. With a committed checkout, use its Git tree. The two listeners below use disposable data. Keep the normal listener and collector running.

## Clean export and locked build

From the repository root:

```sh
repo=$PWD
scratch=$(mktemp -d "${TMPDIR:-/tmp}/weather-edge-verify.XXXXXX")
mkdir -p "$scratch/source" "$scratch/evidence"
tree=$(git write-tree)
printf '%s\n' "$tree" > "$scratch/evidence/source-tree.txt"
git archive "$tree" | tar -xf - -C "$scratch/source"
cd "$scratch/source"

uv sync --locked
npm ci
npm run build
npm install --prefix "$scratch/browser-tools" --no-save --package-lock=false \
  playwright-core@1.64.0-beta-1791568591000

uv run --locked python --version
uv --version
node --version
npm --version
printf 'Verification directory %s\n' "$scratch"
```

The installs may contact registries. Playwright Core is a pinned scratch tool for installed Chrome. The app uses the existing Python and npm locks. The Chrome script prints the actual version and compares the received JavaScript with the emitted file.

Choose free loopback ports in place of 18765 and 18766 if needed. In one terminal, run the listener from the export root. Set the same `scratch` path in each terminal.

```sh
cd "$scratch/source"
export UV_OFFLINE=1
export PYTHONPATH="$PWD/scripts/offline:$PWD"
uv run --locked python -m weather_edge.server --no-collect --data "$PWD/.data" --port 18765 \
  2>&1 | tee "$scratch/evidence/server.log"
```

The server starts its paper reconciler against this export's `.data`. Keep that directory disposable. The Python startup hook reports that the socket audit is active and rejects non-loopback DNS or socket calls. Chrome blocks and counts external page requests.

## Empty archive, capture, save and rerun

In a second terminal:

```sh
cd "$scratch/source"
export UV_OFFLINE=1
export PYTHONPATH="$PWD/scripts/offline:$PWD"
export PLAYWRIGHT_CORE="$scratch/browser-tools/node_modules/playwright-core"
evidence="$scratch/evidence"

node scripts/verify-local-chrome.cjs empty 18765 "$evidence"
uv run --locked python scripts/verify-local-http.py 18765 "$PWD/.data"
uv run --locked python -m weather_edge.capture --fixture --duration-seconds 30 \
  > "$evidence/capture.log" 2>&1
cat "$evidence/capture.log"
node scripts/verify-local-chrome.cjs backtest 18765 "$evidence"

run_id=$(cat "$evidence/run-id.txt")
artifact=".data/backtests/fixture/paper-runs/$run_id.json.gz"
shasum -a 256 "$artifact" > "$evidence/fixture-before.sha256"
uv run --locked python -m weather_edge.paper_runs --data "$PWD/.data" rerun "$run_id" \
  > "$evidence/fixture-rerun.json" 2> "$evidence/fixture-rerun.stderr"
shasum -a 256 -c "$evidence/fixture-before.sha256"
cat "$evidence/fixture-rerun.json"

node scripts/verify-local-chrome.cjs compatibility 18765 "$evidence"
node scripts/verify-local-chrome.cjs focus 18765 "$evidence"
uv run --locked pytest -q
```

The browser step opens `/?view=backtest&fixture=1`, clicks **Run and save backtest**, reads the POST response, and revisits `/?view=backtest&fixture=1&run=<returned ID>`. It matches the reopened GET and visible saved ID to the POST. The fixture capture makes the synthetic session available to Weather and Trade. Fixture Backtest builds its deterministic tape in the handler and freezes it in the returned result. The CLI checks that the frozen input digest generates the ID and that recomputation matches the entire saved result. The artifact checksum checks that the rerun leaves the original bytes intact.

The HTTP script seeds small synthetic research evidence files after checking the empty archive. Use a fresh data root to repeat the empty and boundary steps. The forecast and Trade refresh checks intercept responses after reading the real fixture. They inject controlled availability clocks and receipt changes. The Python suite also exercises the historical decision and legitimate local model loading over HTTP.

## Separate retained-data controls

From the original repository, copy only the two retained radar frames and the original Miami saved run. This optional branch of the procedure requires those local artifacts. Run it for full Slice 9 verification when they are available. It uses a second listener and a separate data root.

```sh
retained="$scratch/retained"
mkdir -p "$retained/radar" "$retained/backtests/live/paper-runs"
cp "$repo"/.data/radar/20261112{17,18}-f000.json "$retained/radar/"
miami=40ee74c6230a6dd3846fcbb7
cp "$repo/.data/backtests/live/paper-runs/$miami.json.gz" "$retained/backtests/live/paper-runs/"
```

Start this server from the export root in another terminal with the same offline environment:

```sh
uv run --locked python -m weather_edge.server --no-collect --data "$scratch/retained" --port 18766 \
  2>&1 | tee "$scratch/evidence/retained-server.log"
```

From the export root in the terminal with `PLAYWRIGHT_CORE`:

```sh
miami=40ee74c6230a6dd3846fcbb7
original="$scratch/retained/backtests/live/paper-runs/$miami.json.gz"
printf '%s  %s\n' 6ac624aae8685257aca1378b28a043ad72ca4d5ca5bc162db19e3b1a4a573af4 \
  "$original" > "$evidence/miami-original.sha256"
shasum -a 256 -c "$evidence/miami-original.sha256"
uv run --locked python -m weather_edge.paper_runs --data "$scratch/retained" rerun "$miami"
shasum -a 256 -c "$evidence/miami-original.sha256"
node scripts/verify-local-chrome.cjs retained 18766 "$evidence"
node scripts/verify-local-chrome.cjs radar 18766 "$evidence"
node scripts/verify-local-chrome.cjs fallback 18766 "$evidence"
```

Inspect the audited command logs for the startup marker and for any `OFFLINE BLOCK` lines. The successful browser commands print the external request count. Stop both disposable listeners with Ctrl-C when finished.

## Negative controls

Run these while the disposable listeners are up. The browser controls must exit nonzero for the named assertion. Use the diagnostic and the preceding successful check to distinguish a reproduced failure from a setup error.

```sh
node scripts/verify-local-chrome.cjs broken-focus 18765 "$evidence"
node scripts/verify-local-chrome.cjs leaked-clock 18765 "$evidence"
node scripts/verify-local-chrome.cjs broken-shader 18766 "$evidence"

uv run --locked python - <<'PY'
import pytest
from weather_edge import market

market.scan = lambda *_args, **_kwargs: []
raise SystemExit(pytest.main([
    "-q", "tests/test_market_capture.py",
    "-k", "display_prices_do_not or source_clock_regression or missing_model_allows",
]))
PY
```

The focus control suppresses the replacement control's focus restoration. The clock control makes an intentionally late observation appear available at the origin. The shader control injects a GLSL error before compilation, lets Canvas 2D draw, and then requires a successful WebGL link anyway. The scanner control must fail all three exact-side checks. Transport and model-loading tests are included in the consolidated run.

For the original browser compatibility failure, set `BASELINE_BUNDLE` to the Slice 8 JavaScript file and run `compatibility` on port 18765. Chrome removes only `Array.prototype.toReversed` in that run. The old code fails during the live capture-list render.

## Verified November 17, 2026

The entry point above was run on Apple Silicon macOS. The baseline was the Slice 8 index `abec06bea353c0d1a82804dca94c1e1e5fc39437`. The tested source export was `2c427891c0a0d1a2e1336ba2fe5e48d8c55c1866`, including the verification scripts. The final index adds the results and link corrections in these audit docs. The final tree fingerprint is in the [local staged-tree receipt](../.data/browser-verification/slice9-closeout/staged-tree.txt). The separate working-tree README edit was preserved.

| Tool | Exercised version |
| --- | --- |
| Python | CPython 3.13.16 |
| uv | 0.13.1 (`62faed335`, aarch64 Apple Darwin) |
| Node | v24.22.0 |
| npm | 11.19.1 |
| Google Chrome | 157.0.8103.79, headless |
| Playwright Core | 1.64.0-beta-1791568591000, scratch install |

Both lockfiles match Slice 8. The normal `npm ci` registry audit summary reported zero vulnerabilities and npm emitted an installation policy warning for the existing esbuild postinstall script. The build completed. The separate review of both locked sets is scheduled for Slice 11.

### Fixture and HTTP

- The empty export rendered Trade, Weather, Backtest, Market Archive and Research. Trade showed Ready 0, Weather explained that it was awaiting the first capture, and the model card showed the training guidance. The real listener returned an empty uncached live session and null legacy/study reports.
- The fixture capture wrote `20261115T171030-0480c621.json`, with one event, four rounds and 24 depth images. Chrome saved and reopened Backtest `55f689d562ca07642aec5996`. The POST and GET responses matched, with **5 signals, 1 fill, 3 skips, 1 awaiting recheck and 1 unresolved payout**. CLI rerun matched the full frozen result under `book-recheck-v3`. The gzip remained at SHA-256 `c17f2eed3d5921e4e3a3ab6fb14cb990313f5066fa0a64144f80025a3c0e4162`. Saved ID and screenshot are in the [HTTP result](../.data/browser-verification/slice9-closeout/fixture-backtest.json) and [Chrome capture](../.data/browser-verification/slice9-closeout/fixture-backtest.png).
- Port 18765 accepted both loopback Host spellings and matched POST Origins, including a 4,096-byte valid JSON request. Nonlocal Host and a localhost Origin against the numeric loopback Host returned 403. The 4,097-byte body, empty body, malformed JSON, array and wrong media type returned 400. Every checked JSON response had `Cache-Control: no-store`. Rejected Backtest writes left the Backtest, run, paper ledger, and collector file lists and bytes identical. Valid CSV and JSON exports worked, and invalid filters returned 400. The seeded in-root research download matched its bytes, and missing or out-of-root targets, including a symlink, returned 404.
- The focused Python run passed 42 tests. The consolidated export run passed **213 tests**. The trusted model load, corrupt optional artifact recovery, and historical decision assertions used the existing seeded HTTP tests. The empty-scanner control failed all three expected case assertions. The executable receipt at 17:10:24Z is distinct from the display-only quote timestamp at 17:10:30Z in those fixtures.
- Python server, capture, reruns and tests reported an active socket audit and zero non-loopback DNS or connection attempts. Chrome's passing runs observed zero external page requests. Installation happened before the offline audit.

### Chrome and retained controls

- Chrome removed `toReversed`, `toSorted`, and `findLastIndex` through a new-document hook before loading app scripts. On the delivered build, the real fixture yielded one capture option below **Latest data**. The controlled 82-session response plus a duplicate showed the newest 79 followed by the original earliest one. With the optional airport series absent, the station rendered without an airport row. With the series present, the later-listed equal-clock 11 °C observation won and the 80 °C receipt at 17:10:31Z was withheld. The temperature chart drew 22 confidence-band vertices, with the upper path ascending in time and the lower path descending over the same times. The monthly rain chart selected the last edition at an equal clock and clamped before the first report.
- Forecast ahead kept 17:10:30Z as the decision origin while showing 18:00Z valid time and the eligible 51 °F guidance. It withheld both the 177 °F forecast and the 80 °C airport observation first available at 17:10:31Z. The negative clock control changed the supplied receipt time to the origin and the assertion exposed the 80 °C leak. With only `toReversed` removed, the Slice 8 bundle displayed **Could not read the local cache … toReversed is not a function** before filling the capture menu.
- The exact fixture YES ticket for `tc-temp-nychigh-2026-11-15-gte49lt50f` returned a 12¢ displayed offer for 45 contracts at 17:10:00Z. A controlled live-shaped refresh changed receipt #1 to #2. The replacement **Book evidence** summary retained focus and its disclosure stayed open. Suppressing the focus restoration on the replacement made the test fail with receipt #2 visible.
- Separate port 18766 served two original radar frames. At 18:01Z and 18:08Z, WebGL linked through SwiftShader and drew the selected frames with Radar enabled. Central Park showed 1.10 mm and 4.62 mm, published at 17:02:42Z and 18:02:44Z respectively. With WebGL disabled, Canvas 2D painted the same inputs. The 18:08Z zero cell showed **0 mm** and the missing cell showed **Unavailable** in both renderers. The injected shader compile failure still produced a Canvas paint but failed the WebGL link assertion. This covers Chrome's software WebGL and Canvas paths.
- Original Miami Backtest `40ee74c6230a6dd3846fcbb7` reopened with **3 signals and 1 fill** on the separate listener. Its `book-recheck-v2` rerun passed and matched the original gzip hash pinned above. Chrome also reopened that record on the normal listener after installation.

### Normal installation

The verified bundle is `app-BOFYDPMO.js`, SHA-256 `4986161ffab5c23b4de87cea900b974465d7cad7ea6eee51dab3fb8b18eb753f`. Port 8765 served the exact nine build files, including six fonts, `index.html` (SHA-256 `54c5360db50f7c05f93df1cee61e6370f692578f74d352d80b61099c3cdc03b3`), and `app-2N6NKZ4L.css` (SHA-256 `e820674f6094a65f050581b799771833324729924bdd2b23aa5b155754335d9f`). Chrome verified the bytes of the JavaScript response it loaded.

Only static files changed in the installed runtime. The content-hashed files were copied first and the HTML was replaced atomically on the existing listener. Listener and reconciler PID 63535 retained the reconciliation lock. Collector PID 1709 stayed enabled and running with its worker lock, started at 03:33:30.428867Z. Before and after hashes matched for the live ledger, seven frozen live paper records, the frozen Miami artifact, and the collector worker record. [Installation receipt](../.data/browser-verification/slice9-closeout/normal-install.json) · [Installed Chrome check](../.data/browser-verification/slice9-closeout/installed-chrome.log) · [Protected hash comparison](../.data/browser-verification/slice9-closeout/protected-after.log)

The [local evidence directory](../.data/browser-verification/slice9-closeout) contains command logs, browser screenshots, the generated run response, rerun outputs and retained negative failures. The first installed Chrome command used an undefined smoke mode after verifying the bundle bytes. The existing `retained` mode then verified the installed bundle and opened Miami on 8765. Operating guide work belongs to Slice 10. Integrated dependency advisories and live qualification belong to Slice 11.
