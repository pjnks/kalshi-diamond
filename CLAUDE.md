# DIAMOND — Kalshi Unusual Volume Tracker

## Project Purpose
Real-time anomaly detection on Kalshi prediction markets with **two-stage detection engine** (2 triggers + 8 scorers), event-aware conviction tracking, portfolio intelligence, and automated live trading. Places real orders on Kalshi when anomalies are detected with order-book-aware pricing.

Part of the gemstone-named trading sub-project family (AGATE, BERYL, CITRINE, DIAMOND). **Standalone repo** — separate from HMM-Trader. GitHub: `pjnks/kalshi-diamond` (private).

## Architecture
```
Kalshi WebSocket ──► Stream Processor ──► Two-Stage Engine ──► Alert Engine
     (trade channel)    (asyncio)         (2 triggers +        (Pushover/SQLite)
                                           8 scorers)
         │                                     │                      │
         └──► REST Poller ──► Market Cache      │               Dashboard (:8080)
              (order book,     (5min)           ▼                      │
               market info)              Conviction Tracker            ▼
                                         (event grouping,       Live Trading Engine
                                          decayed sums,         (book-aware pricing,
                                          BLOCK/FLIP)            adaptive execution)
                                               │                      │
                                               ▼                      ▼
                                         Portfolio Intelligence  Kalshi REST API
                                         (category limits,      (real orders via
                                          event caps,            /portfolio/orders)
                                          burst throttle)
                                               │
                                               ▼
                                         Self-Learning Pipeline
                                         (feature attribution,
                                          adaptive weights)
```

### Market Discovery
The Kalshi `/markets` list endpoint returns `volume_fp=0` for ALL markets (broken as of March 2026). Instead, `refresh_markets()` discovers active tickers from **recent trades** via `/markets/trades` (3 pages), then fetches individual market details. This is the only reliable way to find active markets.

### Category Derivation (Sprint 11, April 2026)
The Kalshi API has **no `category` field** in market responses. Category is derived from the ticker prefix via `_category_from_ticker()` in `diamond_monitor.py`. Longest-match lookup against `_TICKER_CATEGORY_MAP` (e.g., `KXNCAAMB` → "NCAA MBB", `KXNBA` → "NBA", `KXBTC` → "Crypto"). Fallback: strip digits/hyphens from prefix. This was previously broken (all trades recorded as `category=NULL`), which prevented the Dynamic Threshold Manager (CTM) from functioning.

### Warmup Period
The monitor has a **3-minute warmup** (`WARMUP_SEC = 180`) after startup. During warmup, trades are scored and anomalies recorded to SQLite, but alert dispatch (Pushover/macOS) and trade placement are suppressed. This prevents false positive floods during cold start when profiles are empty.

### Display Names
`_market_display_name()` in `diamond_monitor.py` builds human-readable names:
- **Regular markets:** `"LA L at Houston Winner? — Houston"` (title + yes_sub_title)
- **MVE/parlays:** `"Parlay: PSG, Real Madrid, Bodoe/Glimt"` (cleaned from `"yes X,yes Y"` format)
- Stored in both `anomalies.title` and `market_profiles.title` columns

### Single-Instance Guard
`diamond_monitor.py` uses `fcntl.flock()` on `.diamond_monitor.lock` to prevent duplicate instances. The kernel-level lock auto-releases on process death (including `kill -9`), so there's no stale lock file problem. Combined with systemd's `Restart=always`, this guarantees exactly one monitor process.

### Dynamic Threshold Manager (`src/diamond_threshold_manager.py`)
Context-aware ALERT threshold adjustment based on two rolling dimensions:
- **Category performance:** Rolling win rate per market category (NBA, ATP Tennis, etc.). Categories with positive edge get lower thresholds (easier entry), negative edge get higher thresholds.
- **Time-of-day performance:** Rolling win rate per UTC hour. Replaces the hardcoded 5-7pm ET score penalty with data-driven threshold adjustment.
- **Additive combination:** `effective = base + category_adj + hour_adj`, clamped to [FLOOR, CEIL].
- **Cold start:** Buckets with < CTM_MIN_N trades use the base threshold (no adjustment).
- **Score stays clean:** Adjustments go into the threshold, not the score. Raw anomaly scores in the DB are uncontaminated.
- **Config:** `CTM_ENABLED`, `CTM_WINDOW_N=100`, `CTM_MIN_N=30`, `CTM_SENSITIVITY=0.20`, `CTM_MAX_CAT_ADJ=0.08`, `CTM_MAX_HOUR_ADJ=0.06`, `CTM_THRESHOLD_FLOOR=0.40`, `CTM_THRESHOLD_CEIL=0.72`.
- **Refresh:** Re-loads from DB every 5 min via `maybe_refresh()`. Incremental updates via `on_settlement()` after each trade settles.
- **Dashboard:** `get_dashboard_state()` exposes per-category and per-hour thresholds for visualization.

### Live Trading Engine (`src/diamond_paper.py`)
Auto-places real Kalshi orders when anomalies reach ALERT level or above:
- **Order-book-aware pricing:** Fetches fresh order book, prices at best ask + cross margin
- **Adaptive execution tiers:** CRITICAL: ask+3¢, High ALERT: ask+2¢, Low ALERT: ask+1¢
- **Dynamic max spread:** CRITICAL: 25¢, High ALERT: 20¢, Low ALERT: 15¢
- **Min price filter:** Skips trades at or below `PAPER_MIN_PRICE_CENTS` (default 5¢)
- **Kill switch:** Stops new trades when total daily P&L drops below -$20
- **Dedup:** One position per ticker per 24 hours
- **Aggressive auto-cancel:** Unfilled orders cancelled after 15s (ALERT) / 30s (CRITICAL) to prevent adverse selection. Alpha decays in seconds on prediction markets — resting orders past signal half-life are liquidity donations. Legacy 1-hour fallback still exists in poll loop.
- **Config:** `PAPER_CANCEL_SEC_ALERT=15`, `PAPER_CANCEL_SEC_CRITICAL=30`
- **Settlement polling:** Checks order fills and market settlements every 2 minutes
- **Handles Kalshi `executed` status:** Orders on settled markets properly cleaned up
- **Notifications:** Pushover alerts for fills, settlements, flips, kill switch, order failures
- **Session recycling:** aiohttp session auto-recreates every 30 min; force-recycles on connection errors
- **Tiered contract sizing:** CRITICAL (≥0.78): 3 contracts, High ALERT (0.65–0.77): 2 contracts, Low ALERT (0.55–0.64): 1 contract. Kill switch estimate uses same tiered count.
- **Skipped trade logging:** All trade rejections (dedup, conviction block, min price, burst throttle, kill switch, category/event limits) logged to `skipped_trades` table with reason and detail for dashboard visibility

### Conviction System (`src/diamond_conviction.py`)
Event-aware trade management preventing both-sides positions:
- **Event grouping:** `ticker.rsplit("-", 1)[0]` identifies sibling markets
- **Conviction formula:** `Σ (score_i × e^(-age_i / half_life))` — decayed sum per side
- **Decisions:** ALLOW (no opposing signals), BLOCK (opposing side stronger), FLIP (new side exceeds old + threshold)
- **Persistence:** Signals saved to SQLite `conviction_signals` table, restored on startup
- **Config:** `CONVICTION_HALF_LIFE_SEC=420` (7 min), `CONVICTION_FLIP_THRESHOLD=0.4`

### Portfolio Intelligence
- **Category limits:** Max 15 open positions per market category
- **Event caps:** Max 1 position per event (prevents both-sides trading)
- **Burst throttle:** Max 8 trades per 5-minute window
- **Config:** `PAPER_MAX_PER_CATEGORY`, `PAPER_MAX_PER_EVENT`, `PAPER_MAX_TRADES_PER_5MIN`

### Self-Learning Pipeline (`src/diamond_analytics.py`)
- **Feature attribution:** Per-feature win rate, avg P&L, primary driver analysis
- **Co-occurrence patterns:** Which feature combinations predict outcomes
- **Adaptive weights:** Blends current weights with settlement-derived optimal weights
- **Brier decomposition:** Murphy (1973) decomposition into Reliability (calibration), Resolution (discrimination), Uncertainty (base rate). Diagnoses whether model failures are fixable (recalibration) or fundamental (need new features).
- **Slippage tracking:** Post-fill slippage analysis (fill_price - signal_price) broken down by alert level and win/loss outcome. Detects adverse selection when losers have systematically higher slippage than winners.
- **Deflated Sharpe Ratio (DSR):** Bailey & López de Prado (2014) multiple testing penalty. Computes probability that best grid search config exceeds expected max Sharpe of noise. Integrated into `diamond_backtest.py gridsearch` output. DSR < 0.50 = likely overfit.
- **Feature co-firing audit:** Detects multicollinear feature pairs from co-activation rates across settled trades. Flags pairs with >70% co-firing rate and recommends weight masking. Run via `print_cofiring_report(store)` after 200+ settlements.
- **Activation:** Requires 50+ settled trades (run manually or via scheduled task)

### ML Scoring Layer (`src/diamond_ml.py`)
Optional tiered ML pipeline running in shadow mode (logging, not acting) until validated:
- **Feature set:** 9 raw features + 4 engineered (n_features_firing, max_feature_score, entry_price_cents, side_is_yes, category_target_enc). `composite_x_price` was REMOVED due to multicollinearity with raw inputs.
- **Null importance test:** 100-iteration permutation test; features kept only if real importance > 95th percentile of null distribution
- **Tier 1 (Elastic Net):** L1+L2 regularized logistic regression (l1_ratio=0.7) — achieves sparsity while sharing weight among correlated features for day-to-day stability. Upgraded from pure Lasso (l1_ratio=1.0) per quant review (March 2026).
- **Tier 2 (GBM):** GradientBoosting — requires **1000+ samples** (raised from 150) and must beat Lasso by >0.005 Brier. At current trade rates, GBM is effectively disabled for months.
- **Edge prediction:** `edge = P_calibrated(win) - P_market(win)` — positive edge = favorable mispricing
- **Kelly sizing:** NOT YET IMPLEMENTED — requires 60+ days of shadow-mode validation with calibration slope in [0.8, 1.2] before edge estimates can be trusted for sizing
- **Per-fold CV leakage fixes (peer review, March 2025):**
  - Category target encoding recomputed per CV fold (prevents test-label leakage)
  - StandardScaler fit per fold on training data only (prevents feature distribution leakage)
  - Global scaler/encoding retained for final production model fit only
- **Point-in-Time (PiT) purging (quant friend audit, March 2026):**
  - Training only on trades whose `settled_at < test_fold_opened_at` (prevents future label leakage from overlapping contracts)
  - Nested null importance testing inside CV folds using only purged data (prevents data snooping)
  - Time-series Platt scaling via `TimeSeriesSplit(n_splits=5)` (replaces random `StratifiedKFold`)
  - Nested target encoding per fold with PiT win rates for unseen categories
  - `penalty="elasticnet"` fix in null importance test (was silently running ridge/L2)
  - Zero-fold guard: refuses to save model when CV produces zero valid folds
  - Feature stability monitoring: logs set intersection/union across folds
  - Model factory pattern: `model_factory()` callables for clean fold independence
  - Latest retrain: 275 samples, 2/5 folds valid, AUC=0.809, Brier=0.180, 3 features kept
  - **Elastic Net upgrade (Sprint 10):** `l1_ratio=0.7` (was 1.0 pure Lasso). Shares weight among correlated features instead of arbitrarily zeroing one. Improves day-to-day model stability (Jaccard target > 0.70).
  - **Shadow model evaluator:** `evaluate_shadow_model()` checks both Brier calibration and Jaccard feature set stability before promoting to active. Promotion gate: Brier < 0.25 AND Jaccard ≥ 0.70.
  - **Needs retrain:** Saved model was trained with broken L2 (missing `penalty` kwarg) and pure L1. Run `PYTHONPATH=. python diamond_ml_train.py` for corrected Elastic Net model.

### WebSocket Stale Detection (Sprint 8 — March 2026)
Diamond suffered daily 21+ hour staleness incidents where the WebSocket hung silently (no exception raised from `async for raw in ws`) while `status_report_loop()` kept logging every 60s, fooling the watchdog into thinking the service was healthy. **7-layer defense-in-depth fix:**

1. **Receive timeout** (`kalshi_client.py`): `asyncio.wait_for(ws.recv(), timeout=300)` — 5-minute silence triggers `TimeoutError` → automatic reconnect
2. **Data freshness tracking** (`diamond_monitor.py`): `last_trade_ts` global updated on every trade; `status_report_loop()` checks staleness every 60s
3. **Auto-reconnect on stale** (`diamond_monitor.py`): If no trades for 5min (`DATA_STALE_SEC`), cancels WebSocket task via `_ws_task_ref.cancel()` → `ws_with_subscribe()` catches `CancelledError` and restarts
4. **Stale-aware status logs** (`diamond_monitor.py`): Logs WARNING-level `STALE:` messages (not fake-healthy INFO) when no trades flowing — watchdog can distinguish
5. **Watchdog checks log file** (`watchdog.sh`): Diamond uses `StandardOutput=append` (not journald), so watchdog checks file mtime with 10-minute max staleness
6. **Unbuffered Python output** (`systemd`): `python -u` flag ensures log lines appear immediately (was buffered for minutes)
7. **Fast shutdown** (`systemd`): `TimeoutStopSec=15` + asyncio-safe `loop.add_signal_handler()` — restarts take 15s max instead of 90s

### REST Client Resilience (`src/kalshi_client.py`)
- **Fixed-point dollar API:** Orders use `yes_price_dollars`/`no_price_dollars` (string format like `"0.4500"`) and `count_fp` (string like `"1.00"`). This supports all market types including deci-cent parlays. The caller still passes integer cents — conversion to dollar strings happens internally.
- **Session recycling:** `SESSION_RECYCLE_SEC = 1800` — recreates aiohttp session every 30 minutes to prevent stale connections
- **Force-recycle on errors:** Connection errors (`ClientConnectorError`, `ServerDisconnectedError`, `OSError`) trigger immediate session recreation before retry
- **WebSocket version compat:** Auto-detects websockets version and uses `additional_headers` (v11+) or `extra_headers` (v10)
- **WebSocket crash loop detection:** After 10 consecutive errors, sends Pushover CRITICAL alert so trading outages don't go unnoticed

## Key Files
| File | Purpose |
|------|---------|
| `diamond_config.py` | API keys, thresholds, feature weights, trading config |
| `src/kalshi_client.py` | WebSocket + REST API client (order placement with dollar-string format, session recycling) |
| `src/diamond_store.py` | SQLite storage + rolling aggregates + paper trade tracking + skipped trades |
| `src/diamond_features.py` | Two-stage detection: 2 trigger features + 8 scorer features + composite score + convexity penalty |
| `src/diamond_threshold_manager.py` | Dynamic ALERT threshold by category + hour-of-day (rolling win rate) |
| `src/diamond_conviction.py` | Event-aware conviction tracking (BLOCK/FLIP) |
| `src/diamond_analytics.py` | Self-learning: feature attribution, Brier decomposition, DSR, co-firing audit, adaptive weights |
| `src/diamond_alerts.py` | Alert engine with level-aware cooldowns |
| `src/diamond_paper.py` | **Live trading engine** — auto-bet on anomalies, book-aware pricing, portfolio intelligence gates |
| `src/diamond_ml.py` | Shadow ML pipeline (Lasso + GBM), edge prediction, null importance test |
| `src/bounded_dict.py` | Memory-bounded dictionary for market profile caching |
| `diamond_monitor.py` | Main async loop (entry point) — runs everything, PID lock guard |
| `diamond_dashboard.py` | Dash visualization at :8080 (futuristic terminal aesthetic) |
| `diamond_dashboard_lite.py` | Lightweight fallback dashboard |
| `diamond_backtest.py` | Historical replay + grid search + DSR output |
| `diamond_ml_train.py` | ML model training script |
| `reconcile_fills.py` | Utility to reconcile fill records with Kalshi API |
| `deploy.sh` | Deploy to OCI via systemd: `./deploy.sh` (sync) or `./deploy.sh --restart` |
| `test_tuning.py` | Live 60s tuning harness |
| `test_live.py` | Live integration test |

## Running
```bash
# Local (Mac)
PYTHONPATH=. python diamond_monitor.py                    # Monitor + live trading
PYTHONPATH=. python diamond_monitor.py --category politics  # Filter by category
PYTHONPATH=. python diamond_monitor.py --test              # Dry run (no alerts, no trades)
PYTHONPATH=. python diamond_dashboard.py                   # Dashboard at :8080
PYTHONPATH=. python test_tuning.py                         # 60s tuning session
PYTHONPATH=. python diamond_backtest.py replay --tickers 10 # Historical replay
PYTHONPATH=. python diamond_backtest.py gridsearch          # Grid search optimization
PYTHONPATH=. python diamond_backtest.py evaluate            # Precision evaluation

# Deploy to OCI
./deploy.sh              # Sync files only
./deploy.sh --restart    # Sync + restart monitor & dashboard via systemd
```

## Deployment — OCI Compute Instance
- **Host:** `129.158.40.51` (Oracle Cloud Infrastructure)
- **SSH:** `ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51`
- **Python:** `/home/ubuntu/miniconda3/bin/python` (Python 3.13)
- **Code:** `/home/ubuntu/kalshi-diamond/`
- **Deploy:** `./deploy.sh --restart` from local Mac (uses scp, not git)
- **Processes:** Monitor + Dashboard run as systemd services (`diamond-monitor.service`, `diamond-dashboard.service`)
- **Process management:** ALWAYS use `sudo systemctl restart diamond-monitor` — NEVER use nohup/pkill. Systemd has `Restart=always`, so manually launched processes will create duplicates. The monitor's `fcntl.flock()` PID lock prevents this, but systemd is the correct interface.
- **Memory limit:** 600MB via systemd drop-in (`/etc/systemd/system/diamond-monitor.service.d/memory-limit.conf`) — raised from 350MB after April 3 OOM crash loop. OOMScoreAdjust=-500.
- **Stop timeout:** 15s via same drop-in (`TimeoutStopSec=15`) — prevents 90s hang on restart
- **Consolidated dashboard:** Stopped and disabled (freed 93MB). Was running unbounded without MemoryMax on a 956MB VM.
- **Unbuffered output:** `python -u` in ExecStart — ensures logs appear immediately (not buffered)
- **Logs:** `tail -f /home/ubuntu/kalshi-diamond/diamond_monitor.log` (uses `StandardOutput=append`, NOT journald)
- **Note:** Git not yet set up on OCI. Using scp via `deploy.sh` instead.

## Tech Stack
- **Mac:** Python 3.9 (system anaconda), websockets v10.3 (`extra_headers=`)
- **OCI:** Python 3.13 (miniconda), websockets v16 (`additional_headers=`)
- **Version compat:** `kalshi_client.py` auto-detects websockets version for header kwarg
- HTTP: `aiohttp` (with 30-min session recycling)
- Storage: SQLite (`diamond_trades.db`)
- Dashboard: Dash 4.0 + Plotly 6.6 + dash-bootstrap-components + Google Fonts (Inter, JetBrains Mono)
- Notifications: Pushover + macOS native (`_IS_MACOS` platform guard skips `osascript` on Linux — no more log spam)
- Source control: GitHub (`pjnks/kalshi-diamond`, private), `gh` CLI

## Conventions
- Config in `diamond_config.py` (not scattered imports)
- All `.env` secrets loaded via `python-dotenv`
- **CRITICAL:** RSA private key MUST be stored as separate `.pem` file — NEVER paste inline in `.env`. `python-dotenv` cannot parse multiline PEM and will silently corrupt the key path.
- `.env` must contain `KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem` (just the filename, NOT the key content)
- Notification pattern reused from HMM-Trader's `src/notifier.py`
- Gemstone naming: prefix files with `diamond_` for project-specific modules

## Current Status (April 4, 2026)
**Live trading active on OCI — 472 settled trades, 49% win rate, -$8.52 cumulative P&L. Sprint 12 deployed.**
- Steps 1-8: Core system ✓
- Dashboard Redesign ✓ (futuristic terminal aesthetic, glassmorphism, Inter + JetBrains Mono)
- Live Trading Engine ✓ (auto-bet on ALERT+, GTC orders, kill switch, settlement tracking)
- OCI Deployment ✓ (monitor + dashboard running 24/7 via systemd)
- Session Resilience ✓ (30-min recycling, force-recycle on errors, Pushover on failures)
- Min Price Filter ✓ (skip trades ≤ 5¢)
- GitHub Repo ✓ (`pjnks/kalshi-diamond`, private)
- Two-Stage Pipeline ✓ (PhD review: triggers separated from scorers — March 2025)
- ~~Probability Penalty~~ → **Convexity Price Penalty ✓** (inverted: penalize favorites/mid, not longshots — Sprint 10, March 2026)
- ~~Time-of-Day Suppression~~ → **Dynamic Threshold Manager ✓** (replaces hardcoded penalty with rolling per-category + per-hour threshold adjustment — Sprint 10, March 2026)
- **lasso_factory L1 fix ✓** (was missing `penalty="elasticnet"`, silently trained L2 ridge — Sprint 10, March 2026)
- **side_is_yes inference fix ✓** (was hardcoded 0.5, now uses real taker side — Sprint 10, March 2026)
- **_exit_position return type fix ✓** (consistent bool returns — Sprint 10, March 2026)
- **sweep_score streak commit fix ✓** (final streak captured after loop — Sprint 10, March 2026)
- Peer Review Fixes ✓ (DSR, co-firing audit, CV leakage fixes — March 2025)
- Single-Instance Guard ✓ (`fcntl.flock` PID lock — March 2026)
- Systemd-Only Deploys ✓ (deploy.sh uses systemctl, no more nohup — March 2026)
- Fixed-Point Dollar API ✓ (parlay/deci-cent market compatibility — March 2026)
- Skipped Trade Logging ✓ (conviction blocks, min price, burst throttle visible in DB)
- WebSocket Crash Loop Alerts ✓ (Pushover after 10 consecutive errors)
- **7-Layer Stale Detection ✓ (recv timeout + data freshness + auto-reconnect + stale logs + watchdog file check + unbuffered output + fast shutdown — March 2026)**
- Platform Guard ✓ (osascript skipped on Linux — March 2026)
- DB Pruning Tightened ✓ (30d → 7d → 2d — March/April 2026)
- Memory Limit Raised ✓ (250MB → 350MB → 600MB — March/April 2026)
- Asyncio-Safe Signal Handlers ✓ (`loop.add_signal_handler` — March 2026)
- **ML Pipeline PiT Purging ✓ (point-in-time CV, nested null importance, nested encoding, time-series Platt — March 2026)**
- **Tiered Contract Sizing ✓ (CRITICAL=3, High ALERT=2, Low ALERT=1 — March 2026)**
- **ALERT Threshold Revert ✓ (0.50→0.55, lower threshold bled -$5.37/day — March 2026)**
- **Elastic Net Upgrade ✓** (l1_ratio=0.7, shares weight among correlated features — Sprint 10, March 2026)
- **Jaccard Stability Tracking ✓** (feature set turnover across CV folds + shadow model evaluator — Sprint 10, March 2026)
- **Category Derivation Fix ✓** (Kalshi API has no category field — derive from ticker prefix via `_category_from_ticker()`. Unblocks CTM — Sprint 11, April 2026)
- **Convexity Penalty Tightened ✓** (25-49¢ halted at ×0.50, 50-74¢ ×0.85, 75¢+ ×0.75. Based on N=441 forensic: 25-49¢ was -$13.06 / 27% WR — Sprint 11, April 2026)
- **YES-Side Retail Flow Penalty ✓** (×0.85 on YES-side trades. 92% of trades were YES at 41% WR — retail noise, not informed flow — Sprint 11, April 2026)
- **OOM Crash Loop Fix ✓** (14 OOM kills on April 3 — root causes: mmap cgroup inflation, oversized DB, OOMScoreAdjust=+300, consolidated-dashboard unbounded — Sprint 12, April 2026)
- **MemoryMax Raised ✓** (350MB → 600MB, OOMScoreAdjust=-500 — Sprint 12, April 2026)
- **Asyncio Event Loop Protection ✓** (ML retrain → asyncio.to_thread, inline VACUUM removed, profile updates limited to cache, cooperative yield — Sprint 12, April 2026)
- **Watchdog Fix ✓** (was checking journald, monitor uses StandardOutput=append to file — fixed to check log file mtime — Sprint 12, April 2026)
- **Consolidated Dashboard Disabled ✓** (stopped + disabled, 93MB freed — Sprint 12, April 2026)
- **DB Pruning Tightened ✓** (7d → 2d — Sprint 12, April 2026)
- **SQLite mmap_size=256MB ✓** (explicit enable — platform default is 0, disabling causes 3,500+ pread64/sec event loop starvation — Sprint 12, April 2026)

## Trading Configuration (`.env`)
```
PAPER_TRADING_ENABLED=true          # Enable/disable auto-trading
PAPER_MIN_ALERT_LEVEL=ALERT        # Minimum level to trigger trade (ALERT or CRITICAL)
PAPER_CONTRACTS_PER_TRADE=1        # Contracts per order
PAPER_MAX_POSITIONS=100            # Max simultaneous open positions
PAPER_MAX_UNREALIZED_CENTS=2000    # $20 kill switch (realized + unrealized P&L)
PAPER_POLL_INTERVAL_SEC=120        # Settlement check interval (2 min)
PAPER_NOTIFY_TRADES=true           # Pushover notifications for trades
PAPER_MIN_PRICE_CENTS=5            # Skip trades at or below this price (avoid longshots)
PAPER_CANCEL_SEC_ALERT=15          # Auto-cancel unfilled ALERT orders after 15s
PAPER_CANCEL_SEC_CRITICAL=30       # Auto-cancel unfilled CRITICAL orders after 30s
PAPER_MAX_PER_CATEGORY=15          # Max open positions per market category
PAPER_MAX_PER_EVENT=1              # Max positions per event (prevents both-sides)
PAPER_MAX_TRADES_PER_5MIN=8        # Burst throttle
```

### Trading Rules
- Every ALERT+ signal (score ≥ 0.55) places a real order with **tiered sizing**: CRITICAL=3 contracts, High ALERT=2, Low ALERT=1
- Min price filter: skips contracts ≤ 5¢ to avoid longshot bleed
- GTC limit orders with book-aware pricing (best ask + cross margin by tier)
- **Aggressive auto-cancel:** Unfilled orders cancelled after 15s (ALERT) / 30s (CRITICAL) — prediction market alpha decays in seconds, resting orders past signal half-life are adverse selection bait
- One position per ticker (dedup) and per event (event cap)
- Kill switch at -$20 total daily P&L (realized losses + unrealized losses on open positions)
- All positions ride to settlement (no early exits, except conviction FLIPs)
- Resets daily at midnight
- Pushover notification on order placement failures (silent failures no longer possible)

## Tuning Summary
**Alert distribution:** NONE ~86%, LOG ~11%, NOTABLE ~2%, ALERT ~1%.

### Two-Stage Pipeline (PhD review, March 2025)
**Stage 1 — Triggers (boolean gates):** `trade_size_zscore` and `volume_spike_ratio` must BOTH fire (score > 0) to activate scoring. These fire on 94-99% of ALERTs — necessary conditions, not predictors. Removed from weighted model entirely (weight=0) to avoid suppressing discriminative features.
**Stage 2 — Scorer weights (8 features, sum=1.0):** skew=0.256, sweep=0.192, velocity=0.154, imbalance=0.154, book_delta=0.077, impact=0.064, concentration=0.064, correlation=0.038.
**Composite scoring:** Capped weight redistribution (max 1.5×, 1.3× for <3 discriminative features).
**Convexity price penalty (Sprint 11, April 2026):** Tightened from Sprint 10 based on N=441 forensic analysis. 25-49¢ bucket halted (×0.50) — was -$13.06 / 27% WR, the primary bleed source. 50-74¢ lightly penalized (×0.85) — marginal +$4.04 / 58% WR, strong signals only. 75¢+ penalized (×0.75) — expensive losers wipe out high WR gains. <25¢ unpenalized (only profitable bucket).
**YES-side retail flow penalty (Sprint 11, April 2026):** Additional ×0.85 penalty on YES-side trades. Live data: 92% of trades were YES-side at 41% WR — retail overwhelmingly buys YES on favorites/popular narratives. NO-side flow is rarer and more likely informed. Combined with price penalty, YES@25-49¢ is effectively impossible (needs raw score >1.29, capped at 1.0).
**Time-of-day suppression (Sprint 10, March 2026):** 25% score penalty during 21-23 UTC (5-7 PM ET) when pre-game sports volume surges trigger `volume_spike_ratio` seasonally (26-36% WR in this window). Bypassed if `cross_market_correlation > 0.8` (breaking news).
**Adaptive weights:** Blocked until N=500 settled trades (~2 weeks at current rates). PhD review: N=204 is insufficient for stable 8-parameter estimation. Do NOT activate early.

### Current Alert Thresholds
```
LOG      = 0.25   # SQLite only
NOTABLE  = 0.45   # macOS notification
ALERT    = 0.55   # Pushover push + triggers live trade (lowered from 0.65 on March 21)
CRITICAL = 0.78   # Emergency Pushover (lowered from 0.85 to get 1-3/day)
```

### Current Feature Thresholds (tightened from original)
- `order_book_imbalance`: min depth **200** (was 100), threshold **0.7** (was 0.5)
- `taker_side_skew`: min trades **50** (was 30), threshold **0.8** (was 0.6)
- `cross_market_correlation`: min tickers **15** (was 5), required anomalies per ticker **3** (was 2)

### Cooldowns (level-aware)
CRITICAL=30s, ALERT=2.5min, NOTABLE=5min, with escalation bypass.

## Dashboard Features (`diamond_dashboard.py`)
**Futuristic terminal aesthetic** with glassmorphism, Inter + JetBrains Mono fonts.

- **Animated gradient header border** (cyan↔violet shifting)
- **Glassmorphism panels** with `backdrop-filter: blur(12px)`, subtle cyan border glow
- **6 metric cards:** Trades, Markets, Anom Rate, Logged, Alert, Critical — with hover lift effect
- **Volume + anomaly timeline:** 5-min buckets, last 6 hours, cyan→violet gradient bars, gap-filled (reindexed to show zero bars during WebSocket outages instead of missing space)
- **Feature radar chart:** Cyan glowing stroke, translucent fill
- **Top markets bar chart:** Gradient fill (cyan → violet)
- **Feature weights & alert thresholds** reference panel
- **Portfolio section:** Status, P&L, win rate, open positions, trade activity metrics
- **Paper trades table:** With fill status, P&L, settlement results
- **Live anomaly feed table:**
  - Sortable, color-coded: CRITICAL=red, ALERT=amber, NOTABLE=violet, LOG=gray
  - **Level filter buttons** (All, Critical, Alert, Notable, Log) with counts + glassmorphism
  - Filter persists across 30s auto-refresh via `dcc.Store`
  - **Tooltip on hover** for truncated market names
  - **Time (ET)** column with timezone label
  - 200 row limit, 20 per page
- Auto-refreshes every 30 seconds

### Known Dashboard Issues
- **Black screen after laptop sleep/wake:** Dashboard process stays alive but Dash stops rendering. Fix: `sudo systemctl restart diamond-dashboard`
- **Filter button reset:** Pattern-matching callbacks can reset on refresh when buttons are recreated. Guard added but may have edge cases.
- **NaN market titles:** Some markets have NULL/NaN titles in DB. Fixed with `str()` wrapping in `_build_market_volume_heatmap()`.

## Database Schema
SQLite at `diamond_trades.db`. Key tables:
- `trades` — raw trade stream
- `anomalies` — detected anomalies with `title TEXT` column (human-readable name)
- `market_profiles` — per-market stats with `title TEXT` column
- `book_snapshots` — order book snapshots
- `paper_trades` — live trading orders (ticker, side, entry_price, status, order_id, pnl)
- `conviction_signals` — event-level conviction state, restored on startup
- `skipped_trades` — rejected trade attempts with reason/detail (dedup, conviction_block, min_price, burst_throttle, kill_switch, category_limit, event_limit, max_positions)

Schema migrations handled via `ALTER TABLE ADD COLUMN` in `DiamondStore._migrate()`.
Pruning: book snapshots at 2 days, trades/anomalies at 2 days (reduced from 7; feature engine only uses 24h), conviction signals at 2 hours.
DB index on `anomalies(ts)` for cross_market query performance.
**VACUUM:** Removed from inline code — it rewrites the entire DB file and blocks the asyncio event loop for minutes on a 1-OCPU VM. Run manually when needed: `sqlite3 diamond_trades.db "VACUUM"`.

### Paper Trades Table
```sql
paper_trades (
  id, ticker, title, side, action, count, entry_price, fill_count, fill_price,
  anomaly_score, anomaly_level, features, client_order_id, order_id,
  status,  -- pending | filled | settled | unfilled | cancelled
  pnl_cents, opened_at, settled_at, event_id, category
)
```

### Skipped Trades Table
```sql
skipped_trades (
  id, ticker, title, side, price_cents, anomaly_score, anomaly_level,
  skip_reason,  -- min_price | dedup | conviction_block | max_positions | category_limit | event_limit | burst_throttle | kill_switch
  detail, ts
)
```

## Kalshi API Notes
- REST base: `https://api.elections.kalshi.com/trade-api/v2`
- WS: `wss://api.elections.kalshi.com/trade-api/ws/v2`
- Auth: RSA-PSS SHA-256 signature over `{timestamp_ms}{METHOD}{path}`
- Rate limit: 20 req/sec (Basic tier) — 429s common when paginating
- **Deprecated (March 2026):** `volume` field → use `volume_24h_fp` (string format, parse with `float()`)
- **Deprecated (March 2026):** `yes_price` (integer cents) → use `yes_price_dollars` (string like `"0.4500"`) for order placement. Integer format silently fails on deci-cent markets.
- **Market price structures:** `price_level_structure` field: `linear_cent` (regular markets, 1¢ ticks) vs `deci_cent` (parlays/MVE, 0.1¢ ticks). Our `place_order()` handles both via dollar-string format.
- `/markets` list endpoint returns `volume_fp=0` for ALL markets — do NOT rely on it for volume filtering
- Individual markets return `status: "active"` not `"open"` — filter must check both
- WS trade format: `market_ticker` (not `ticker`), `count_fp` (string), `yes/no_price_dollars` (string)
- Orderbook format: `orderbook_fp.yes_dollars` / `no_dollars`: `[[price_str, qty_str], ...]`
- Order placement: POST `/portfolio/orders` with `type: "limit"`, `yes_price_dollars: "0.45"`, `count_fp: "1.00"`, `time_in_force: "good_till_canceled"`
- Order response includes `order_id` for tracking fills/cancellations
- Fill count fields: check `fill_count_fp` → `count_filled_fp` → `count_filled` → `fill_count` (API has multiple field names across versions)

## Gotchas
- **CRITICAL `.env` gotcha:** NEVER paste the RSA private key inline in `.env`. ALWAYS store it as a separate `.pem` file and reference via `KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem`. Pasting inline causes `python-dotenv` parse errors and `FileNotFoundError` pointing at `-----BEGIN RSA PRIVATE KEY-----` as a path. This has happened multiple times — always check `.env` first when debugging auth issues.
- **Parlay/MVE order failures:** Markets with `price_level_structure: deci_cent` reject orders using the legacy `yes_price` (integer cents) field. Must use `yes_price_dollars` (string). Fixed in `kalshi_client.py` (March 26, 2026). If you see 400 "invalid_parameters" errors on KXMVE* tickers, check that the dollar-string format is being used.
- **websockets version:** Mac uses v10 (`extra_headers=`), OCI uses v16 (`additional_headers=`). Auto-detected in `kalshi_client.py`.
- **Stale aiohttp sessions:** Long-running processes (2+ days) can have stale HTTP sessions that silently fail on order placement. Fixed with 30-min session recycling + force-recycle on connection errors.
- **Unclosed session warnings:** `asyncio: Unclosed client session` / `Unclosed connector` errors in logs are cosmetic — they occur during session recycling when the old session's connector has in-flight requests. Harmless but noisy.
- Rate limit 429 common when paginating all markets — has retry + delay
- `int("0.00")` crashes — use `float()` for Kalshi string numeric fields
- Pushover emergency priority (2) notifications repeat every 60s until acknowledged in-app
- Cross-market correlation can create feedback loop (more anomalies → higher score → more anomalies) — thresholds raised to prevent this
- Kalshi sports markets are naturally directional (lopsided order books, one-sided flow) — thresholds must account for this
- **Dashboard black screen:** After laptop sleep/wake, the Dash app's React frontend stops rendering while the Python process stays alive. HTTP 200 returns but browser shows black page. Must restart the dashboard process.
- **WebSocket keepalive timeouts:** `sent 1011 (internal error) keepalive ping timeout` appears every ~20 min in logs. This is normal — Kalshi's WS server occasionally misses pong responses. The client auto-reconnects within 5s with no trade data loss (trades are replayed on reconnect).
- **macOS osascript on OCI:** Fixed (Sprint 8): `_IS_MACOS` platform guard in `diamond_alerts.py` silently skips `osascript` on Linux. No more log spam.
- **IOC orders don't fill:** Kalshi markets are thin. IOC (immediate-or-cancel) orders expire immediately if no liquidity. Switched to GTC with slippage adjustment.
- **OCI process management:** Always use `sudo systemctl restart diamond-monitor` (not pkill + nohup) to avoid duplicate processes. Systemd auto-restarts killed services, so manual nohup launches will create duplicates. The fcntl PID lock is a safety net but systemd is the correct interface.
- **NaN market titles:** Some market titles come back as NaN (float) from DB. Always use `str()` when displaying.
- **SQLite mmap and cgroups:** On Linux with systemd, `PRAGMA mmap_size=N` causes file-backed mmap pages to count against MemoryMax. A 400MB DB can inflate cgroup to 300MB+ even with 72MB RSS. But `mmap_size=0` forces pread64 syscalls (3,500+/sec) which starves the asyncio event loop. Current solution: mmap_size=256MB + MemoryMax=600MB.
- **VACUUM blocks the event loop:** SQLite VACUUM rewrites the entire DB file synchronously. On a 1-OCPU VM this takes minutes. NEVER run VACUUM inline in the monitor — run it manually. The `_last_vacuum` attribute also resets to 0 on every restart, so periodic VACUUM timers fire on every startup.
- **Synchronous DB writes in async code:** Any `store.*()` call is synchronous SQLite. Loops over 100+ tickers with per-ticker DB writes will block the event loop for minutes. Always limit batch sizes or use `asyncio.to_thread()` for large operations.
- **Watchdog checks log file, not journald:** Diamond uses `StandardOutput=append` (writes to file), NOT journald. The watchdog must check the log file's mtime, not `journalctl` timestamps. Getting this wrong causes the watchdog to kill a healthy process every ~75 minutes.

## OCI Deployment Details
- **Instance:** Oracle Cloud Infrastructure Compute (Ubuntu, hostname `hmm-trader`)
- **IP:** `129.158.40.51`
- **SSH key:** `~/.ssh/hmm-trader.key` (user: `ubuntu`)
- **Python:** `/home/ubuntu/miniconda3/bin/python` (3.13, miniconda)
- **Deploy script:** `./deploy.sh` syncs `.py` files via scp; `./deploy.sh --restart` also restarts via systemd
- **Manual restart:** `ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51 'sudo systemctl restart diamond-monitor'`
- **Git:** Not set up on OCI yet. GitHub repo exists at `pjnks/kalshi-diamond` but OCI uses scp.
- **Also running:** CITRINE dashboard on :8070 (separate project)
- **Memory limit:** 600MB via systemd drop-in (`/etc/systemd/system/diamond-monitor.service.d/memory-limit.conf`), raised from 350MB after April 3 OOM crash loop. OOMScoreAdjust=-500 (protected from kernel OOM killer).

## 24/7 Operation
- **Primary:** OCI compute instance (always-on, no lid-close issues)
- **Secondary:** Mac runs local copy for development/testing
- Monitor self-heals on wake (WebSocket reconnects with fresh auth, REST re-authenticates)
- Session recycling prevents stale connection failures after long uptime
- **7-layer stale detection** prevents silent WebSocket hangs (see "WebSocket Stale Detection" section)
- Pushover alerts on order failures and WebSocket crash loops ensure silent failures are caught
- Single-instance guard (fcntl PID lock) prevents duplicate processes even if deployment goes wrong

### Asyncio Event Loop Protection (Sprint 12, April 2026)
Three independent sources of event loop starvation were discovered and fixed during the April 3 crash-loop investigation:
1. **ML retrain** — `scorer.train()` is synchronous sklearn (100 permutation fits, 10+ min on 1-OCPU). Now wrapped in `asyncio.to_thread()` to offload to thread pool.
2. **Inline VACUUM** — `prune_old_data()` had a 6-hour VACUUM timer, but `_last_vacuum` reset to 0 on every restart → VACUUM ran on every startup, blocking the event loop for minutes while rewriting the entire DB. Removed entirely (run manually when needed).
3. **1,212 profile updates** — `metadata_refresh_loop()` was calling `store.get_all_active_tickers()` which returned all historical tickers from DB, not just current market cache (~76-99 active). Each got a synchronous DB write. Fixed to use `list(market_cache.keys())`.
4. **Cooperative yield** — Added `await asyncio.sleep(0)` at end of `on_trade()` to prevent WebSocket message flood (500-1300 trades/min) from starving status_report_loop and paper_engine.poll_loop.

### SQLite Memory & Cgroup Accounting (Sprint 12, April 2026)
On Linux with systemd cgroups, `PRAGMA mmap_size=N` causes file-backed mmap pages to count against the process's MemoryMax budget. A 394MB DB + 273MB WAL inflated cgroup usage to 310MB even though actual process RSS was only 72MB. Current config:
- `PRAGMA mmap_size=268435456` (256MB) — enabled for performance (disabling caused 3,500+ pread64 syscalls/sec which also starved the event loop)
- `PRAGMA synchronous=NORMAL` — reduces disk I/O blocking
- `PRAGMA journal_mode=WAL` — concurrent read/write
- MemoryMax raised to 600MB to accommodate mmap overhead

## Performance Summary (as of April 4, 2026)
- **Settled trades:** 472
- **Win rate:** 49% (231W / 241L)
- **Cumulative P&L:** -$8.52
- **Fill rate:** ~91% of placed orders fill
- **By alert level:** CRITICAL: ~9 trades, 78% WR, +$2.18 P&L — ALERTs: ~460+ trades, 49% WR. CRITICALs dramatically outperform.
- **By price bucket:** <25¢: 56 trades, 18% WR, +$1.18 (ONLY profitable). **25-49¢: 135 trades, 27% WR, -$13.06 (halted in Sprint 11).** 50-74¢: 169 trades, 58% WR, +$4.04. 75¢+: 81 trades, 81% WR, -$0.63.
- **Score discrimination:** Zero. Win avg=0.598, loss avg=0.591 (composite score cannot distinguish winners from losers).
- **Threshold experiment:** Lowering ALERT from 0.55→0.50 (Mar 29) produced 51 trades at -$5.37/day. Reverted — marginal signals lack edge to overcome spread. Do NOT lower again without ML validation.
- **Tiered sizing deployed:** CRITICAL=3 contracts, High ALERT=2, Low ALERT=1.
- **ML model:** AUC=0.809, Brier=0.180. Needs retrain. Shadow mode only.
- **WebSocket health:** ~46 reconnects/day (keepalive timeouts), all auto-recovered
- **Sprint 11 impact (early signal, N=15):** Post-deployment (Apr 3+): 15 trades, 80% WR, +$0.48. Caveat: N=15 is not statistically significant (SE ≈ 0.26). Need 15+ days for valid assessment.
- **April 3 dark period:** 15-hour gap in paper trades (02:25–17:50 UTC) due to OOM crash loop. Raw trade data captured (254K trades) but paper trades cannot be retroactively simulated — anomaly scoring is state-dependent and path-dependent.

## Next Steps / Roadmap
- ✅ **[DONE] Monitor Sprint 11 penalty impact** — Post-deployment (Apr 3-4): 15 trades, 80% WR, +$0.48. Trade volume dropped as expected. N=15 is too small for conclusions (need 15+ days).
- ✅ **[DONE] Backfill categories on historical trades** — 493/493 paper_trades updated from NULL → derived category (April 2, 2026).
- ✅ **[DONE] OOM crash loop stabilization (Sprint 12)** — 7 independent fixes: MemoryMax 600MB, OOM=-500, inline VACUUM removed, ML→asyncio.to_thread, profile updates limited, watchdog→file check, consolidated-dashboard disabled.
- **[DO NOT] Flip `CTM_ENABLED=true`** — Keep disabled until **N > 1,500 settled trades** (~5-6 weeks). With ~472 trades across ~15 categories × 24 hours, most CTM buckets have N < 5. Optimizing on N < 30 per bucket guarantees extreme overfitting via bias-variance tradeoff. Categories are backfilled for data integrity and ML features, NOT for CTM activation.
- **[THIS WEEK] Retrain ML model** — `PYTHONPATH=. python diamond_ml_train.py` with corrected Elastic Net (`l1_ratio=0.7`) + 3 new binned interaction features (`is_longshot_yes`, `is_favorite_no`, `is_mid_yes_spike`). Run `--null-test` to validate the binned features survive permutation test. The composite score has zero discrimination; ML with disjoint regime indicators is the path.
- **[THIS WEEK] Deploy backtest fixes** (quant friend audit, confirmed present):
  - [CRITICAL] Directional precision evaluator — replace abs-move "hit" with MFE/MAE relative to trade side
  - [CRITICAL] EV-based grid search objective — replace cosmetic distribution penalty with expected value optimization
  - [HIGH] Mid-price edge anchoring — use order book mid-price instead of entry_price for ML edge calculation
  - [MEDIUM] Sweep/impact latency documentation — backtest doesn't account for post-trade price movement
- **[ONGOING] Collect post-Sprint 11 out-of-sample data** — Need 15+ days (until ~April 17) with paired t-test on daily P&L for valid assessment of penalty impact. Do NOT draw conclusions from N < 50 trades.
- **[ONGOING] Track Jaccard stability** — if feature set turnover > 0.50 for 3 consecutive retrains, halt shadow model.
- **[ONGOING] Shadow P&L on skipped trades** — The execution-layer hard block (YES@25-49¢) prevents observing outcomes for blocked trades. Must periodically compute hypothetical mark-to-market P&L on `skipped_trades` to detect if a blocked regime becomes profitable.
- **Kelly criterion sizing:** After ML shadow mode validated (Brier < 0.25, Jaccard ≥ 0.70), size by estimated edge
- **Set up git on OCI:** Replace scp deploy with git pull workflow
- **Consider Hetzner migration:** Current VM (1 OCPU, 956MB) is at the edge. If trade volume grows or ML retrain gets heavier, migrate to Hetzner CCX23 (~$25/mo, 4GB RAM, 2 vCPU).
