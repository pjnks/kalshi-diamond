# DIAMOND System Report — 2026-03-25

## Executive Summary

DIAMOND has been live-trading on Kalshi prediction markets for 5 days (since March 21). The system has placed 218 orders, achieving a 91.3% fill rate with 197 settled trades. Cumulative P&L is **-$2.62** on $1 contract sizing — essentially flat, which is a meaningful result for a detection-first system operating without calibrated edge estimates or Kelly sizing.

The critical finding: **the system has a winner entry-price problem.** Winners enter at 61c avg; losers enter at 37c avg. This implies the detector is finding real signal in high-probability markets but bleeding on low-probability longshots where the base rate overwhelms the anomaly signal.

---

## Performance Metrics (N=197 settled)

| Metric | Value |
|--------|-------|
| Win Rate | 46.2% (91W / 105L / 1 push) |
| Avg Win | +39.16c |
| Avg Loss | -36.44c |
| Win/Loss Ratio | 1.075 |
| Expectancy | -1.33c/trade |
| Cumulative P&L | -262c (-$2.62) |
| Fill Rate | 91.3% (199/218) |
| Sharpe (daily, N=5) | ~0.05 (insufficient data) |
| Max Daily Drawdown | -$4.71 (March 24) |
| Best Single Trade | +83c (Iowa ML, entry 17c) |
| Worst Single Trade | -95c (Iowa ML, entry 94c) |
| Kill Switch Trigger | None (threshold -$20) |

**Edge decomposition:** Win rate of 46.2% with win/loss ratio of 1.075 yields negative expectancy of -1.33c/trade. The system needs either (a) win rate > 48.2% at current payoff ratio, or (b) win/loss ratio > 1.165 at current win rate, to break even before fees.

---

## Daily P&L Trajectory

```
Date        Trades  Settled  Wins  P&L (cents)  Cumulative
2026-03-21      44       41    21     +187         +187
2026-03-22     110      102    49      +14         +201
2026-03-23      21       17     7     -109          +92
2026-03-24      37       31    10     -471         -379
2026-03-25       6        6     4     +117         -262
```

March 24 accounts for the entirety of the negative cumulative. Investigate what happened — likely a correlated loss cluster on a single sports slate. The 10/31 win rate (32.3%) that day is >2 sigma below the overall mean.

---

## Signal Funnel

```
Raw Trades Observed     →  ~150,000 (5 days)
├── LOG anomalies       →  113,840  (75.9%)
├── NOTABLE anomalies   →   26,833  (17.9%)
├── ALERT anomalies     →    8,429   (5.6%)
├── CRITICAL anomalies  →      239   (0.16%)
│
├── Orders Placed       →      218   (2.6% of ALERT+)
│   ├── Filled          →      199   (91.3% fill rate)
│   ├── Unfilled        →       18   (8.3%)
│   └── Cancelled       →        1   (0.5%)
│
└── Settled Trades      →      197
    ├── Winners         →       91   (46.2%)
    └── Losers          →      105   (53.3%)
```

**Observation:** 8,668 ALERT+ anomalies generated only 218 orders (2.5% conversion). The gap is the dedup filter (one position per ticker per 24h), burst throttle, conviction blocks, and min-price filter doing their jobs. This is correct behavior — the anomaly stream is intentionally noisy to avoid missing signal, and the execution layer is intentionally selective.

---

## Alert Level Performance

| Level | N | Win Rate | Avg P&L | Total P&L | E[edge] |
|-------|---|----------|---------|-----------|---------|
| ALERT | 193 | 45.1% | -2.20c | -$4.24 | Negative |
| CRITICAL | 4 | 100.0% | +40.50c | +$1.62 | Positive* |

*N=4 is far too small for inference. The 100% win rate on CRITICAL is encouraging but statistically meaningless — need N>30 before drawing conclusions. However, the directional signal is consistent with the thesis: higher composite scores should predict higher win rates.

**Action item:** The system needs to generate more CRITICAL-level trades. Current threshold (0.78) may be too tight — only 239 CRITICAL anomalies in 5 days, converting to 4 trades. Consider a modest reduction to 0.72-0.75 after N=50 CRITICALs.

---

## The Entry Price Problem

This is the most important finding in the data.

| Outcome | Avg Entry Price | Avg Fill Price | N |
|---------|----------------|----------------|---|
| Winners | 60.76c | 60.84c | 91 |
| Losers | 36.90c | 36.81c | 105 |

**Interpretation:** The detector is systematically entering losing trades at low prices (longshots, ~37c implied probability) and winning trades at high prices (favorites, ~61c implied probability). This is the classic prediction market trap:

- At 37c entry, you need >37% win rate to profit. The system's overall 46.2% win rate suggests it *does* beat this threshold on average — but losses at 37c cost 63c (paying to 100c settlement), while wins pay only 63c. The asymmetry is unfavorable at low prices.
- At 61c entry, you need >61% win rate. The system likely exceeds this for high-price entries (these are favorites where the anomaly signal confirms the market direction).

**The anomaly detector fires on unusual volume, which is inherently directional in sports markets.** When the favorite sees a volume spike, the detector correctly identifies it and enters near 60c — these are winners. When the underdog sees a volume spike, the detector enters near 37c — these are noise traders, not informed flow, and the bets lose.

**Recommendation:** Implement a **price-conditional filter** or **asymmetric sizing**:
- Option A: Raise `PAPER_MIN_PRICE_CENTS` from 5 to 25-30c (cut longshots entirely)
- Option B: Score trades differently based on entry price — apply a penalty to low-price entries that lack multiple confirming features
- Option C: Use the ML layer's edge estimate (once validated) to gate low-price entries

---

## Side Analysis

| Side | N | Win Rate | Avg P&L | Total P&L |
|------|---|----------|---------|-----------|
| Yes | 167 | 45.5% | -0.57c | -$0.95 |
| No | 30 | 50.0% | -5.57c | -$1.67 |

Heavy yes-side bias (85% of trades). The "no" side has a higher win rate but worse avg P&L — small sample, but suggests the no-side entries are at unfavorable prices. The yes-side bias likely reflects Kalshi's market microstructure: retail flow is predominantly yes-side, so anomalous volume spikes are disproportionately on the yes book.

---

## Feature Weights (Current Production)

```
taker_side_skew        0.20  ████████████████████  (fires 70% of ALERTs, score 0.79)
sweep_score            0.15  ███████████████       (fires 16%, score 0.73)
trade_size_zscore      0.12  ████████████          (fires 94% — near-universal)
trade_velocity         0.12  ████████████          (fires 28%, score 0.98)
order_book_imbalance   0.12  ████████████          (fires 9.6%, score 0.91)
volume_spike_ratio     0.10  ██████████            (fires 99% — near-universal)
book_pressure_delta    0.06  ██████
price_impact           0.05  █████
size_concentration     0.05  █████
cross_market_corr      0.03  ███
```

**Weight efficiency concern:** `trade_size_zscore` (0.12) and `volume_spike_ratio` (0.10) fire on 94-99% of ALERT trades — they are necessary conditions but provide zero discriminative power within the alert set. Their weight allocation is dead weight for separating winners from losers. The adaptive weight system (requiring 50+ settled trades) can now run — it should shift weight toward features that actually predict outcomes, not just trigger alerts.

---

## System Stability

| Component | Status | Detail |
|-----------|--------|--------|
| Monitor | Running | PID 159323, 141MB RSS, systemd-managed |
| Dashboard | Running | Port 8080, 19h uptime |
| PID Lock | Active | fcntl flock, prevents duplicate instances |
| Deploy | Fixed | systemctl restart (was nohup, caused duplicates) |
| Memory | OK | 141MB / 250MB limit (56% headroom) |
| DB Size | ~150K anomalies | 5 days, pruning at 30 days |

**Incident (March 25):** Duplicate monitor processes discovered (one systemd-managed, one orphaned nohup). Root cause: `deploy.sh` used `pkill + nohup` while systemd auto-restarted the killed process. Fixed with: (1) deploy.sh now uses `systemctl restart`, (2) PID lock file via `fcntl.flock` prevents any second instance.

---

## ML Layer Status

The ML scoring layer (`src/diamond_ml.py`) is running in **shadow mode** — logging predictions but not gating trades. Current state:

- **Tier 1 (Lasso):** Active, logging edge estimates
- **Tier 2 (GBM):** Disabled (requires 1,000+ samples; current N=197)
- **Kelly sizing:** Not implemented (requires 60+ days shadow validation)
- **Sklearn warnings:** `X does not have valid feature names` — cosmetic, should suppress with `warnings.filterwarnings`

The sklearn warning is flooding logs and burying real output. Low priority but worth a one-line fix.

---

## Conviction System

The conviction tracker prevents both-sides positions on the same event:
- Half-life: 420s (7 min)
- FLIP threshold: 0.4
- No data on BLOCK/FLIP rates yet (not logged to a queryable table at trade level)

**Gap:** We have no visibility into how many trades the conviction system is blocking. If it's blocking high-quality signals, that's a hidden cost. Recommend adding a counter/log for conviction decisions.

---

## Category Analysis

Categories are currently split between NULL and empty string in the database — a data population bug. 102 trades have NULL category (53.9% win rate, +$5.15 P&L) and 95 have empty string (37.9% win rate, -$7.77 P&L). This needs to be fixed before category-level analysis is meaningful.

---

## Actionable Recommendations (Priority Order)

### 1. Price-Conditional Filter (High Impact, Low Effort)
Raise `PAPER_MIN_PRICE_CENTS` from 5 to 25. The data shows losing trades cluster at low entry prices. Cutting entries below 25c would eliminate the worst-performing segment. Backtest this against the 197 settled trades first.

### 2. Run Adaptive Weights (Medium Impact, Zero Effort)
N=197 exceeds the 50-trade threshold. Run the self-learning pipeline to compute feature attribution and update weights based on actual settlement outcomes. `zscore` and `volume_spike` should lose weight; discriminative features should gain.

### 3. Fix Category Logging (Low Impact, Low Effort)
Debug why some trades get NULL vs empty-string category. This blocks category-level performance analysis which is needed to decide if certain market types (sports vs politics vs crypto) should be weighted differently.

### 4. Suppress Sklearn Warnings (Low Impact, Trivial)
Add `warnings.filterwarnings("ignore", category=UserWarning, module="sklearn")` to the monitor entrypoint. The current log is unreadable.

### 5. Investigate March 24 Drawdown (Medium Impact, Research)
March 24 had 31 settled trades at 32.3% win rate, -$4.71 P&L. Was this a single correlated event (e.g., one sports slate going against the flow), or distributed losses? If correlated, the portfolio intelligence layer (category limits, event caps) may need tightening.

### 6. Lower CRITICAL Threshold (Medium Impact, Needs Data)
Wait for N=30+ CRITICAL trades before acting, but the directional signal (4/4 wins) suggests CRITICAL captures real informed flow. A modest reduction from 0.78 to 0.73 would generate more data faster.

---

## Theoretical Framework

DIAMOND operates as a **microstructure anomaly detector** on prediction markets. The core thesis:

> Unusual volume patterns on Kalshi indicate informed flow — participants with private information or superior models acting on mispriced contracts. By detecting these patterns in real-time and following the informed side, the system can extract edge from the market.

The 10-feature engine computes a composite anomaly score as a **weighted linear combination** of normalized feature scores:

```
S_composite = Σ(w_i × s_i) / Σ(w_i × 𝟙[s_i > 0])
```

where the denominator normalizes by active features only (capped redistribution at 1.5x to prevent single-feature domination).

The conviction system models **signal decay** as exponential:

```
C(side, t) = Σ (score_i × e^(-Δt_i / τ))
```

where τ = 420s (half-life). This captures the empirical reality that prediction market information incorporates within minutes — a signal from 10 minutes ago is worth ~25% of a fresh signal.

**Current regime:** The system is in the **data collection phase** of a three-phase lifecycle:
1. **Collection** (now): Flat sizing, broad signal acceptance, settle-and-learn. Goal is N>500 settled trades with diverse market conditions.
2. **Calibration** (next): Activate adaptive weights, validate ML edge estimates, implement price-conditional filtering.
3. **Optimization** (future): Kelly sizing, dynamic thresholds, category-specific models.

At the current rate (~40 trades/day on active days), Phase 1 should complete within 2 weeks.
