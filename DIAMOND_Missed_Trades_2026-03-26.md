# DIAMOND — Estimated Missed Trades: March 26, 2026

## Context

The WebSocket crash loop (float/str type bug in `_normalize_trade()`) prevented all trade placement from ~21:54 UTC March 25 through 01:49 UTC March 26. During this window, the anomaly detector continued scoring trades and logging ALERT-level signals to SQLite, but the trading engine could not place orders.

This document estimates what trades **would have been placed** and their probable outcomes.

## Estimated Trades (14 ALERT signals, ~8 after filters)

The raw ALERT anomaly stream produced 14 unique tickers. However, the dedup, conviction, and min-price filters would have reduced this. Notes on what the filters would have caught:

- **Conviction BLOCK:** ATL-DET fired on both sides (DET at 01:22, ATL at 01:36) — one would be blocked
- **Conviction BLOCK:** HOU-MIN fired on both sides (HOU at 01:38, MIN at 02:12) — one would be blocked
- **Conviction BLOCK:** PAU-FIL fired on both sides (FIL at 00:54, PAU at 02:32) — one would be blocked
- **Min price filter:** BTC 15-min contract at 1.1c — would be skipped (below 5c threshold)
- **Min price filter:** OKC winner at 6c — borderline, would pass current 5c filter

### Trade-by-Trade Estimate

| # | Time (UTC) | Market | Entry | Outcome | Est P&L | Notes |
|---|-----------|--------|-------|---------|---------|-------|
| 1 | 00:11 | SF Giants (MLB) | 48c | **LOSS** (settled 1c) | -48c | SF lost to NYY |
| 2 | 00:23 | Club Guarani (soccer) | 74c | **LOSS** (settled 1c) | -74c | Guarani lost |
| 3 | 00:54 | Arthur Fils (tennis) | 63c | OPEN (45c) | ~-18c* | Match in progress, Fils trailing |
| 4 | 00:56 | Buffalo Sabres (NHL) | 61c | OPEN (80c) | ~+19c* | Buffalo leading |
| 5 | 01:22 | Detroit Pistons (NBA) | 65c | **LOSS** (settled 1c) | -65c | Detroit lost to Atlanta |
| 6 | 01:35 | OKC-BOS Over 218.5 | 95c | OPEN (95c) | ~0c* | High entry, near settlement |
| 7 | 01:36 | Cleveland Cavs (NBA) | 44c | **LOSS** (settled 1c) | -44c | Cleveland lost to Miami |
| 8 | 01:36 | OKC Thunder (NBA) | 6c | OPEN (4c) | ~-2c* | OKC losing, low price |
| 9 | 01:36 | Atlanta Hawks (NBA) | 47c | **WIN** (settled 99c) | +53c | Atlanta beat Detroit |
| 10 | 01:38 | Houston Rockets (NBA) | 55c | OPEN (45c) | ~-10c* | Game in progress |
| 11 | ~~01:43~~ | ~~BTC 15-min~~ | ~~1c~~ | — | — | *Skipped: below min price filter* |
| 12 | 02:12 | Minnesota (NBA) | 54c | OPEN (54c) | ~0c* | *Likely BLOCKED by conviction (HOU fired first)* |
| 13 | 02:30 | Brooklyn Nets (NBA) | 12c | OPEN (13c) | ~+1c* | Game in progress |
| 14 | 02:32 | Tommy Paul (tennis) | 55c | OPEN (58c) | ~+3c* | *Likely BLOCKED by conviction (FIL fired first)* |

\* Open market P&L is mark-to-market, not settlement. Final outcomes pending.

### Conviction Filter Adjustments

After conviction blocks and min-price filter, the likely placed trades would have been **~9-10 orders** (trades 1-10, 13), not all 14.

## Settled Outcomes Summary (of the 5 settled markets)

| Outcome | Count | P&L |
|---------|-------|-----|
| WIN | 1 | +53c |
| LOSS | 4 | -231c |
| **Net** | **5** | **-178c (-$1.78)** |

**Win rate on settled: 20% (1/5)**

## Analysis

The missed trades would have been **net negative** on the settled portion. The 4 losses (SF, Guarani, Detroit, Cleveland) all followed the pattern identified in the status report: the detector found volume anomalies on sides that turned out to be noise flow, not informed flow.

The one winner (Atlanta at 47c → 99c, +53c) did not offset the losses.

**Paradoxically, the crash loop may have saved ~$1.78 today** on settled trades. However, 9 markets remain open and could shift the final tally. This is also a single day with small N — no conclusions should be drawn about whether the system should or shouldn't trade.

## Caveat

These estimates assume:
- Entry at approximate market price at signal time (from trade stream, not order book)
- The actual entry would have been best-ask + cross margin (typically 1-3c worse)
- Conviction decisions are approximated — the actual signal decay math may have produced different BLOCK/ALLOW decisions
- Fill rate historically is 91.3% — some of these orders may not have filled

---

*Generated March 26, 2026. Markets marked OPEN will settle within hours.*
