# Live Trade qualification and recheck

The live board considers exact open market sides. A posted side can have a listing price or an offer from a retained ladder. Ready requires qualifying exact depth, origin evidence, suitable exchange image age, supported model odds, verified fees, and positive estimated net EV. An exact ticket revalidates the side and budget independently. A paper order needs its own delayed receipt before the simulator will fill it.

## Identical cached copies

Polymarket US can return a new HTTP receipt carrying the same unchanged book. Trade keeps the earlier origin receipt when the newer copy agrees on native image clock, open state and both ladders, and there is no intervening contradictory evidence. The earlier origin receipt still determines expiry. A cached copy cannot create a Ready quote by itself or qualify as a delayed recheck.

At 9:28:27 p.m. EST on November 15, the origin recorded Miami daily high 81–82°F YES. The public feed returned the same image again at 9:28:44 with HTTP Age 16 seconds. At the reproduced 9:28:48 ticket clock, the older policy blocked the preview. The revised ticket priced 15.91 contracts with $1.3433 maximum loss and about nine seconds of evidence validity remaining. At 9:28:58 the original receipt was already outside the 30-second cutoff and both policies blocked. The receipts around the later recheck window were cached or lacked a fresh image. This case produced zero fills.

The exact ticket now loads ahead of the expanded Weather timeline. Both can contend for the capture lock. A cold Weather-first loopback comparison delayed the ticket to 2.97 seconds, compared with 0.05 seconds alone. This was one competing-load example. The ticket also moves its side out of Ready when it sees a newer blocker and displays the updated receipt.

## Paired replay

The 02:24–02:40 UTC November 16 slice contains 3,024 captured Polymarket US high side appearances and 1,701 posted ladder appearances. Each appearance is a side at a completed capture clock and the replay checks its exact preview five seconds later. The five seconds reproduce the observed Miami board-to-ticket gap. The recheck uses the normal 30-second reaction, 15-second recheck window, exchange-age requirement, and frozen order limits.

| Policy | Ready | Eligible preview | Eligible recheck | Simulated fill |
| --- | ---: | ---: | ---: | ---: |
| Latest receipt | 3 | 3 | 0 | 0 |
| Unexpired origin with matching copies | 7 | 6 | 1 | 1 |

The filled appearance was the same Miami YES contract at an earlier 9:24 p.m. EST window. The 9:28 p.m. walkthrough expired without a fill. A legacy strategy replay keyed to HTTP receipt time produced one unresolved simulated position at 30 seconds. The current strategy replay keys decisions to first app availability and produced one unresolved simulated position at 15 seconds. Both sensitivity runs used the same Miami side. The live ticket control uses its own decision clock and still rechecked at the normal 30-second setting. [Slice 11 timing comparison](audit-integrated-verification.md#same-tape-size-and-delay)

Sampled board counts separately track live API snapshots and exact ticket GETs. A contract can appear many times. Kalshi public REST ladders support indicative previews with local receipt clocks. The book payload lacks an exchange image timestamp, so its local receipt cannot qualify as a paper recheck. Kalshi documents authenticated sequenced orderbook updates as a separate interface. [Kalshi public book](https://docs.kalshi.com/api-reference/market/get-market-orderbook) · [Kalshi orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates) · [Polymarket US public book](https://docs.polymarket.us/api-reference/markets/get-market-book)

## Market rollover

The November 15 daily-high selections expired together at 11:59 p.m. EST. The previous discovery already contained November 16 events, but the collector had kept only the four selected events. The next discovery ran at 12:03:30 a.m. and the first November 16 high book arrived at 12:03:33.932503. Each book cycle now selects from the same verified candidate catalog using the current clock and existing four-event limit. The collector test reproduces a date change using retained discovery and verifies that the next-day books are requested before the next discovery.

The old Miami ticket kept appearing open after its last capture left the recent buffer. Its market listing carried a 5 a.m. EST scheduled expiry, while its retained event listing showed the 11:59 p.m. event cutoff. Trade applies the retained event cutoff to old listings as well and labels the reason. The distinction is about the listed window and leaves the native close and payout evidence available in Market Archive.

At 1:05 a.m. EST the same live snapshot counted 280 Polymarket US sides with the prior open-status logic and 204 with the retained event clock. All 76 excluded sides belonged to ended listed windows. The revised browser ticket kept the exact archive link and hid the public-book refresh action.

The live Kalshi Central Park 41–42°F daily-low ticket for November 16 initially showed the previous weather selection while loading and then joined the November 15 station day. The venue timeline now matches both station and contract date. Trade starts the exact preview first and Weather shows a loading state until the matching timeline arrives. The November 16 date, Kalshi bucket and YES side survived the Trade → Weather → Trade round trip and a browser reload. The ticket still showed the same public-feed recheck blocker.

The 05:25–05:59 UTC November 16 tape added a separate Central Park case. The two-sided rule found 25 signals at both budgets. Recheck delays of 15, 30 and 60 seconds found zero, ten and six eligible rechecks, respectively. Both longer variants produced two unresolved positions. At a $25 cap, the first $24.913 position left only $0.087 for the second. At $10 per order, the same 30-second variant used $9.9996 and $8.0467. The buffered 30-second variant produced one unresolved position. Trade offers a reprice to the cents left under the existing paper balance and station-day cap. In the browser fixture, $13.016 room offered a $13.01 budget and a $12.974 preview.

The next two hours of tape, 06:00–08:00 UTC, contained 47 two-sided signals at each budget. The 15-, 30- and 60-second delays found zero, 25 and 24 eligible rechecks. Both longer delays again produced two unresolved positions, on the same two Central Park sides as the earlier interval. The buffered rule produced one unresolved position. The additional window tests persistence of the recheck pattern on the same market, with zero settled outcomes and no independent station replication.

The 08:00–09:38 UTC window added Miami and a third Central Park side. At both budgets, the 15-, 30- and 60-second settings found one, 57 and 48 eligible rechecks from 108 signals. The two longer variants generated five unresolved positions each. The 30-second buffered rule generated three. At 30 seconds the Central Park third position used $0.003 at the $25 budget and $9.9995 at the $10 budget under the same station-day cap. Order budget affected dollar allocation even where it left the position count unchanged. There were zero settled positions across these variants.

## Reproduce

```sh
.venv/bin/python -m weather_edge.trade_audit replay --start 2026-11-16T02:24:00Z \
  --through 2026-11-16T02:40:00Z --output .data/paired-trade-funnel.json
.venv/bin/python -m weather_edge.trade_audit sensitivity --start 2026-11-16T02:24:00Z \
  --through 2026-11-16T02:40:00Z --output .data/trade-sensitivity.json
```

Real backtests freeze the relevant model availability receipt with each model estimate used. Retraining records a local publication time after writing the artifact. Earlier frozen trials can attest an exact digest at their recorded save time. Current artifacts with no earlier witness receive a local observation timestamp. Reopening existing saved results uses the original frozen inputs.
