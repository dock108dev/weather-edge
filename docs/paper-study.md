# November 15 live paper study

The same saved Polymarket US book evidence covers **1:56:42–2:17:33 a.m. EST** on November 15, 2026. It contains **1,248 book receipts** over five station-days. The engine accepted 157 as eligible signal-time book images and had model odds for 83 of those. There were 18 positive signal observations across three distinct market sides. All five scenarios start with $500 and cap a single order and station-day exposure at $25 all-in. Rechecks use the first eligible response in the 15 seconds after the configured delay. Quotes are public venue evidence and fills are simulations.

| Reaction delay | Eligible rechecks | Still positive | Paper fills | Open cost | Settled station-days |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 5 sec | 0 | 0 | 0 | $0.00 | 0 |
| 15 sec | 1 | 1 | 1 | $25.00 | 0 |
| 30 sec | 8 | 8 | 2 | $30.62 | 0 |
| 45 sec | 0 | 0 | 0 | $0.00 | 0 |
| 60 sec | 6 | 6 | 1 | $25.00 | 0 |

The 30-second run opened one Central Park and one Miami position. Its cash is $469.38 with $30.62 committed and zero realized P&L. The delay changes where the recheck window lands relative to cache refreshes. A representative public GET returned an HTTP cache hit with 17 seconds of `Age` and a 30-second `max-age` despite the request's `no-cache` directive. Exchange, HTTP, and local receipt timestamps are kept separately. The engine retains its fresh-depth requirement for signal and recheck. [Polymarket US market book](https://docs.polymarket.us/api-reference/markets/get-market-book), [HTTP caching semantics](https://www.rfc-editor.org/rfc/rfc9111.html).

The extended snapshot through **2:28:15 a.m. EST** has 2,232 book receipts, 251 eligible images, and model odds for 176 of those. The five-second trial recorded 21 signals across four sides, with zero eligible follow-ups. The 30-second trial retained the same two open fills, eight eligible follow-ups, and $30.62 committed. The saved IDs are `ecd402e5bbd0faa43788dd1b` at five seconds and `3489a65b5d720effd8bab8fe` at 30 seconds. Both capture the exact model vintage.

The larger historical price comparison used 33 settled station-days and 990 matched exact contract buckets. Market YES display prices scored 0.05817 Brier and NOAA scored 0.07638. This price-only comparison provides zero historical simulated fills. Neither November 15 paper fill in the saved run has a verified outcome. Repeatable P&L and weekly dollars require independent settled runs.

## Inspect and replay

```sh
uv run --locked python -m weather_edge.paper_runs rerun d5b6f8c9e18bbfea9e304c92
uv run --locked python -m weather_edge.paper_runs rerun 4988baf9c3305752d65f881c
```

The five-second run is `d5b6f8c9e18bbfea9e304c92`. The 30-second counterpart is `4988baf9c3305752d65f881c`. Both open in **Saved trials**. The saved run records the books and their reception clocks, the forecasts and observations available by the decision, the probability by event and timestamp, the fee schedule, timing policy, allocations, outcomes, and per-position cash timeline.

New runs capture the exact model vintage. Earlier runs retain their original model role and frozen probabilities. A later payout receipt creates a child run and preserves the original entry cost, fill time, and model. Fixture tests exercise favorable and adverse price moves, a missing recheck, a win, a loss, a fractional payout, duplicate session import, and cash reconciliation.

Review decision: Five-second rechecks | Public gateway cache age limited fresh follow-up evidence in this sample | Preserved eligibility and compared longer delays on the same tape.
