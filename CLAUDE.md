# DIAMOND — Kalshi Unusual Volume Tracker

## 🔴 RETIRED (2026-05-13)

**Strategy autopsy: [reports/2026_05_13_diamond_strategy_autopsy.html](reports/2026_05_13_diamond_strategy_autopsy.html)**

DIAMOND has been **shut down and archived** after the post-Sprint-14h clean cohort produced a five-way diagnostic agreement that the underlying hypothesis (Kalshi orderbook microstructure contains tradeable alpha at retail scale) is falsified.

**Final state:**
- Tier-1 cumulative kill switch latched at −$51.52 on 2026-05-08 (1,575 settled, 53.78% WR all-time)
- N=305 post-14h clean cohort: Brier=0.2000, AUC=0.750 (degrading), 3/9 features survived null-permutation test
- `diamond-monitor` + `diamond-dashboard` services stopped + disabled on OCI
- DB archived to `diamond_trades.db.shutdown-2026-05-13` (VM) and `~/Documents/quant/diamond/archive/diamond_trades.db.shutdown-2026-05-13.gz` (Mac)
- 3 positions remain open (no manual exit per training-distribution rule); will not be polled for settlement

The infrastructure built for DIAMOND (PiT-purged CV, three-layer data integrity, two-tier kill switch, task-death watchdog, cohort-burn discipline) transfers to BERYL/CITRINE. See § "Institutional Assets" at end of file.

The documentation below is preserved for historical/forensic reference. Active operational guidance (deploy.sh, --restart commands, monitoring) is no longer current.

---

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
- **Kelly sizing (Sprint 14c, DORMANT):** Scaffolded in `src/diamond_kelly.py`. Feature-flagged OFF (`KELLY_SIZING_ENABLED=false`). Formula: `f* = E/(1-p)` with Half-Kelly safety multiplier (0.5), slippage-adjusted hurdle (`MIN_EDGE_HURDLE=0.02` — matches measured +0.77¢ adverse slippage), 5% per-trade cap for tail-event survival. Self-check harness passes 6 test cases on import. Activation requires: (a) N≥500 post-Sprint-11, (b) ML shadow model passes Brier<0.25 AND Jaccard≥0.70, (c) backtest validates vs flat sizing via `backtest_kelly.py`. Critical nuance: ML edge must also be wired into ENTRY gate (not just sizing) — else Kelly sizes trades the composite-score gate admits, which Sprint 13b proved has near-zero IC.
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
| `src/diamond_kelly.py` | **DORMANT Kelly sizing utility** (Sprint 14c). `kelly_fraction()` + `kelly_contracts()`. Half-Kelly default, slippage-adjusted hurdle 0.02, 5% per-trade cap. Self-check harness on import. NOT imported by production. |
| `src/bounded_dict.py` | Memory-bounded dictionary for market profile caching |
| `diamond_monitor.py` | Main async loop (entry point) — runs everything, PID lock guard |
| `diamond_dashboard.py` | Dash visualization at :8080 (futuristic terminal aesthetic) |
| `diamond_dashboard_lite.py` | Lightweight fallback dashboard |
| `diamond_backtest.py` | Historical replay + grid search + DSR output |
| `diamond_ml_train.py` | ML model training script |
| `backtest_kelly.py` | **Kelly vs flat sizing harness** (Sprint 14c). 3-phase: Oracle / Sensitivity sweep / PiT empirical. Event-cluster block bootstrap, ledger invariant assertion. |
| `reconcile_fills.py` | Utility to reconcile fill records with Kalshi API |
| `reconcile_stuck_trades.py` | Sprint 14: drain orphan `status='filled'` rows via forced settlement queries (Kalshi taxonomy-drift fix) |
| `backfill_orphan_fills.py` | Sprint 14: recover fill_price from Kalshi API for orphan rows, drive settlement |
| `migrations/apply_check_constraint.py` | Sprint 14b: Layer 3 schema migration — rebuild `paper_trades` with `check_valid_fill` CHECK. Reusable, `--apply` gate, preflight + smoke test. |
| `migrations/README_schema_check_constraint.md` | Sprint 14b runbook + rollback procedure (DEPLOYED 2026-04-20) |
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

## Current Status (May 13, 2026) — 🔴 STRATEGY RETIRED

**DIAMOND has been shut down following five-way diagnostic agreement that the underlying hypothesis is falsified.** See `reports/2026_05_13_diamond_strategy_autopsy.html` for the full autopsy.

### Final tally

| Metric | Value |
|---|---|
| **Status** | 🔴 RETIRED |
| Services on OCI | `diamond-monitor`: stopped + disabled. `diamond-dashboard`: stopped + disabled. |
| Total settled (all-time) | 1,575 |
| All-time WR | 53.78% |
| **Cumulative P&L** | **−$51.52** (Tier-1 kill switch latched 2026-05-08) |
| Post-14h clean cohort | 305 settled, 60.33% WR, −$19.65 P&L |
| Final Brier (N=300, PiT-purged) | 0.2000 ± 0.0295 |
| Final AUC (N=300) | 0.750 (degrading from 0.787 at N=200) |
| Features surviving null-permutation | 3 of 9 |
| Pre-registered N=300 verdict | **0.20-0.22 = Inconclusive** — landed on dividing line between salvageable signal and dead hypothesis |
| Open positions at shutdown | 3 (KXALIENS-27 no@78c, KXITFMATCH-…GUO no@49c, KXTRUMPOUT27 yes@8c — Trump Aug 2027 tail) |

### The five-way diagnostic agreement

1. **Brier plateau** at 0.20 across N=150 → 300 (no descending slope — model has stopped learning)
2. **AUC degrading** with N (0.787 → 0.750) — signature of small-sample overfit melting away
3. **Feature count collapse** at N=300 (5 → 3, null-permutation test rejected Sprint 14e absorption variants)
4. **Persistent U-shape monotonicity inversion** (intrinsically orthogonal features did not break base-rate conflation)
5. **OOS WR CI [18.6%, 49.9%]** includes 50% — cannot reject null hypothesis of no edge

### Dual verdict (execution + evaluation)

| Layer | Result |
|---|---|
| Execution | −$51.52 cumulative, Tier-1 kill switch caught the bleed at the structural risk tolerance |
| Evaluation | Brier 0.20 plateau + AUC degrading + 3/9 features survive null test = no scalable IC |

Both layers independently arrived at the same conclusion. The hypothesis — "Kalshi orderbook microstructure contains tradeable alpha at retail scale via flat-sized longshot-biased anomaly detection" — is empirically falsified.

### DB archive

- **VM**: `/home/ubuntu/kalshi-diamond/diamond_trades.db.shutdown-2026-05-13` (398MB plain, 107MB gzipped)
- **Mac**: `~/Documents/quant/diamond/archive/diamond_trades.db.shutdown-2026-05-13.gz` (107MB)
- 1,787 paper_trades, 89,376 anomalies, 1,275,176 trades, 126,265 book_snapshots, 9,383 skipped_trades, 20,980 market_profiles
- SQLite `integrity_check`: ok

---

## Historical sprint state (preserved for forensic reference)

The sections below document the full Sprint 14 series that led to the autopsy. Operationally these are no longer relevant (daemon is stopped), but they capture the institutional knowledge that transfers to BERYL/CITRINE.

### Pre-shutdown state (May 6, 2026 — 13:25 UTC, 11h post-Sprint-14h)
**Was live trading active on OCI — Sprints 14/14b/14c/14d/14e/14f/14g/14h all DEPLOYED + VERIFIED. State at that snapshot: 1,364 settled, 53.15% WR, −$34.99 cumulative P&L. Today (May 6 UTC, partial): 101 settled, 67 wins (66.3% WR), −$3.92. Sprint 14h (2026-05-06 02:12:12 UTC, epoch `1778033532`) fixed silent task death from `asyncio.CancelledError`. Three patches: (1) explicit CancelledError handling in `orderbook_poll_loop` + `metadata_refresh_loop` with heartbeat logging, (2) top-level task death watchdog that fires Pushover CRITICAL on unexpected task death, (3) N=500 cohort reset (393 contaminated trades purged). Verified healthy: 651 heartbeats logged, 0 errors, 0 task deaths, absorption pipeline back to 0.00% stale + 99.98% nonzero across 16,921 anomalies in last 6h. Post-14h cohort velocity: 94 settled in first 11h = ~205/day initially, sustained at ~145/day over 24h. Cohort completed at N=305 when Tier-1 cumulative kill switch latched on May 8.**

**Sprint 14h forensic timeline:**
- May 1 03:09 UTC: Sprint 14g restart. orderbook_poll_loop starts.
- May 1 18:14 UTC: aiohttp session recycle → CancelledError → task dies silently.
- May 1 18:17 UTC: last book_snapshot row written.
- May 3 18:27 UTC: 2-day pruning empties book_snapshots table.
- May 4-5: `_book_absorption_stale = 1.0` on 100% of anomalies (Sprint 14e canary firing correctly, but no human attention on it).
- May 5 02:00 UTC: user requests daily status report → audit catches the regression.
- May 6 02:12:12 UTC: Sprint 14h deployed.

**Pre-14h cohort (purged):** 393 settled trades opened Apr 29 03:04 → May 6 02:12. ~83% have zeroed absorption features.

**Total cohort cost across Sprints 14e/14f/14h:** 290 + 50 + 393 = **733 trades** purged. Each reset preserved stationarity rather than moving goalposts.

**All three data-integrity layers LIVE (continuous since Apr 16):**

| Layer | Mechanism | Firings since deploy |
|---|---|---|
| 1 — Code | `_cancel_after()` derives fill_price from Kalshi API cost fields | 0 corrupt fills |
| 2 — Application | `update_paper_fill` guard rejects corrupt writes, downgrades to `unfilled` | 0 firings |
| 3 — Schema | `check_valid_fill` CHECK constraint rebuilt `paper_trades` with NOT NULL invariant | 0 violations |

### Sprint 14 (2026-04-16/18, DEPLOYED) — Data integrity + execution layer hardening

**Symptoms observed:**
- 13 paper trades stuck in `status='filled'` for up to 16.5 days (oldest Mar 31)
- Post-drain forensic: 9 additional rows had `status='filled'` but `fill_price=None` — data integrity violation

**Root causes (three compounding):**

1. **Kalshi taxonomy drift** — API returns `'determined'` or `'finalized'` for some market settlement states, not `'settled'`. Our settlement matcher in `check_settlements()` only accepted `'settled'`, so these were perpetually skipped. Expanded to `{settled, determined, finalized}`.

2. **Silent exception swallowing** — `check_settlements()` caught all exceptions into `log.debug()`. Stack traces disappeared into noise; no Pushover alert. Upgraded to `log.error()` with Pushover on repeated failures.

3. **Fill-price corruption (line 544)** — `diamond_paper.py::_cancel_after()` had a literal hardcoded `None` where `fill_price` should have been parsed from the API response. This was NOT a race condition; the cost data (`maker_fill_cost_dollars`, `taker_fill_cost_dollars`, `fill_count_fp`) was in the response all along — just never read. Classic copy-paste bug.

**Three-layer defense-in-depth:**

1. **Layer 1 — Code fix**: Rewrote `_cancel_after()` signature to accept `limit_price` fallback. On partial-fill detection, derive `fill_price = round((maker_cost + taker_cost) * 100 / fill_count)`. Fallback to `limit_price` if API cost fields are absent/zero.

   ```python
   async def _cancel_after(self, order_id, row_id, ticker, delay_sec, limit_price):
       # ... fetch order status ...
       if fill_count > 0:
           try:
               maker_cost = float(order.get("maker_fill_cost_dollars", "0") or "0")
               taker_cost = float(order.get("taker_fill_cost_dollars", "0") or "0")
               total_cost = maker_cost + taker_cost
               fill_price = int(round(total_cost * 100 / fill_count)) if total_cost > 0 else int(limit_price)
           except (TypeError, ValueError):
               fill_price = int(limit_price)
           self._store.update_paper_fill(row_id, order_id, fill_price, fill_count, "filled")
   ```

2. **Layer 2 — Application guard in `diamond_store.py::update_paper_fill`**:

   ```python
   if status == "filled":
       is_corrupt = (
           fill_price is None
           or not isinstance(fill_price, (int, float))
           or fill_price <= 0
           or fill_count is None
           or not isinstance(fill_count, (int, float))
           or fill_count <= 0
       )
       if is_corrupt:
           log.critical(f"[STORE] REJECTED CORRUPT FILL: row_id={row_id} ...")
           status = "unfilled"
           fill_price = 0
           fill_count = 0
   ```

   **Semantic decision**: Downgrade to `unfilled`, NOT `stuck`. Rationale:
   - `stuck` = *unobservable outcome of a known execution* (right-censored, should recover)
   - `unfilled` = *known non-execution* (terminal state, no recovery)
   - If `fill_price` is missing, no execution was ever recorded → `unfilled` is the only provable claim
   - Using `stuck` would waste reconciliation budget hunting for fills that likely don't exist

3. **Layer 3 — Schema CHECK constraint (DEFERRED)**:

   ```sql
   CHECK (status NOT IN ('filled','settled','closed')
          OR (fill_price IS NOT NULL AND fill_count > 0))
   ```

   SQLite has no `ALTER TABLE ADD CONSTRAINT` — requires full table rebuild (create new, `INSERT INTO new SELECT * FROM old`, drop old, rename). On 1-OCPU VM with 125MB DB, this is a synchronous 30-60s operation that would block the asyncio event loop (Sprint 12 lessons: WebSocket keep-alive times out in minutes). Deferred to maintenance window with full runbook in `migrations/README_schema_check_constraint.md`.

**Orphan backfill** (`backfill_orphan_fills.py`, ~230 lines):
- Dry-run by default, `--apply` gate
- Per orphan row: queries `/portfolio/orders/{order_id}` + market details via Kalshi API
- Derives `fill_price` from `(maker_fill_cost_dollars + taker_fill_cost_dollars) × 100 / fill_count_fp`
- Computes P&L from market settlement value
- Drives settlement through existing `update_paper_settlement` / `mark_paper_voided` methods
- Preserves original `opened_at`; sets `filled_at = COALESCE(filled_at, opened_at)`
- Idempotent: stuck→settled transition removes row from future runs
- **Result**: All 9 orphans recovered, 6W/3L, net −23¢

**Validation** (2026-04-18, ~48h post-deploy):
- Guard firings: 0 (Layer 1 fix prevents Layer 2 from ever triggering — ideal state)
- Orphans remaining: 0
- Monitor uptime: 2 days stable, 154MB/600MB RAM
- Trade ingest healthy: 33k trades/24h, most recent anomaly <1 hour old

### Sprint 14b (2026-04-20, DEPLOYED) — Layer 3 schema constraint

Collaborator sign-off received to execute the deferred CHECK constraint migration during a low-frequency maintenance window.

**Migration execution (04:41–04:43 UTC, ~2 min downtime):**
1. `systemctl stop diamond-monitor` → clean SIGTERM shutdown
2. `cp diamond_trades.db diamond_trades.db.pre-check-constraint.bak` (346 MB backup)
3. Preflight: **0 violations** in current data (805 rows × `status IN ('filled','settled','voided') AND NULL fill_price` check)
4. `apply_check_constraint.py --apply` — 5-step table rebuild inside single transaction:
   - `CREATE TABLE paper_trades_new (... CONSTRAINT check_valid_fill CHECK (...))`
   - `INSERT INTO paper_trades_new SELECT * FROM paper_trades` (805 rows, **<0.1s** — much faster than 30-60s budgeted; scales with row count × row size, not DB file size)
   - `DROP TABLE paper_trades`
   - `ALTER TABLE paper_trades_new RENAME TO paper_trades`
   - `CREATE INDEX idx_paper_ticker, idx_paper_status`
5. **Smoke test (critical):** forced `INSERT` with `status='filled'` + `NULL fill_price` — confirmed `sqlite3.IntegrityError: CHECK constraint failed: check_valid_fill`. Schema enforcement verified, not just DDL trust.
6. `systemctl start diamond-monitor` → store reconnected to new schema, ML loaded, paper engine armed, WebSocket subscribed. **No integrity errors, no CHECK violations, 0 guard firings** post-restart.

**Live constraint definition:**
```sql
CONSTRAINT check_valid_fill CHECK (
    status NOT IN ('filled', 'settled', 'voided')
    OR (fill_price IS NOT NULL AND fill_price > 0
        AND fill_count IS NOT NULL AND fill_count > 0)
)
```

Artifacts: `migrations/apply_check_constraint.py` (reusable, `--apply` gate, preflight + smoke test), `migrations/README_schema_check_constraint.md` (status flipped DEFERRED→DEPLOYED), `diamond_trades.db.pre-check-constraint.bak` retained on VM.

Defense-in-depth now complete: three layers (code / application / schema) with non-overlapping failure domains. Simultaneous silent corruption reaches persistent storage only if all three fail independently.

### Sprint 14c (2026-04-20, STAGED DORMANT) — Kelly sizing + backtest harness

**Motivation:** Phase 1 audit diagnosed flat tiered sizing (CRITICAL=3 / ALERT=2 / ALERT=1) as the root cause of "picking up pennies in front of a steamroller" — favorite-bucket losses swamp longshot-bucket wins. Kelly's edge-proportional sizing mathematically neutralizes this when fed a calibrated edge estimate. Requires ML validation (N=500) before going live; code staged in anticipation.

**Shipped:**

1. **`src/diamond_kelly.py`** — dormant utility, 9.6 KB
   - Formula: `f* = E / (1 - p)` (binary YES contract Kelly, verified from first principles)
   - Half-Kelly default (`safety_fraction=0.5`)
   - **Slippage-adjusted hurdle**: `MIN_EDGE_HURDLE=0.02` (not 0.01) — reflects measured +0.77¢/trade adverse slippage + spread cost
   - **5% per-trade cap** (`MAX_PER_TRADE_FRACTION`) to survive Over-Kelly ruin (raw Kelly at E=0.15, p=0.80 → f*=0.75)
   - Two-tier API: `kelly_fraction()` (pure math) → `KellyAllocation` dataclass; `kelly_contracts()` adds bankroll/price → integer contract count
   - Explicit `rejected` + `reject_reason` on failures (telemetry-ready when activated)
   - `_self_check()` harness with 6 test cases, runs on `python src/diamond_kelly.py`
   - Extensive docstring with full activation runbook

2. **`diamond_config.py`** — 4 new flags (all OFF by default):
   ```python
   KELLY_SIZING_ENABLED = False  # master kill switch
   KELLY_SAFETY_FRACTION = 0.5   # Half-Kelly
   KELLY_MIN_EDGE_HURDLE = 0.02
   KELLY_MAX_PER_TRADE = 0.05
   ```
   Not imported by any production code path; import graph is clean.

3. **`backtest_kelly.py`** — 3-phase historical validation harness (~350 lines)
   - **Phase A (Oracle):** Feeds actual outcomes as `q_true` → proves the code works given perfect info. Unit test for the formula.
   - **Phase B (Sensitivity):** Corrupts oracle edges with `N(0, σ)` for σ ∈ {0.01, 0.02, 0.05, 0.10}, biased clip to [0.01, 0.99]. **Output: σ_max tolerance curve** — this derives the ML promotion gate (Brier/Jaccard threshold) numerically instead of by intuition. Per collaborator ruling 2026-04-20: **no Mills-ratio correction** — biased clip is the honest stress test (live ML won't self-center).
   - **Phase C (Empirical):** PiT price-bucket empirical edge (`bucket_WR − implied_prob`). Lower bound on real Kelly advantage. Warmup skip at `N < MIN_ACTIVE_N=10` per bucket (matches sparse orthogonalizer pattern from Sprint 13c).

4. **Key design invariants:**
   - **PiT gate: `settled_at < opened_at`** (NOT `opened_at < opened_at`). The subtle leakage: opened-before is historical but outcome-after is unknowable. Using `opened_at` as cutoff would leak contemporaneous-open outcomes.
   - **Ledger invariant asserted every tick**: `cash + locked + realized == initial_bankroll`. Breaks → bug, not strategy underperformance.
   - **Kelly base = `cash_available`** (not `total_wealth`) — matches Kalshi margin semantics; greedy approximation of Simultaneous Kelly (gap <10% at our 2-4 concurrent position regime, subsumed by Half-Kelly safety).
   - **Event-cluster block bootstrap** (not IID) — preserves sibling ticker correlation (±1 for mutually exclusive contracts). Blocks defined by `event_id` only, NOT temporal duration (per collaborator ruling).
   - **PiT edges computed ONCE on original cohort**, then bootstrap resamples operate on `(trade, precomputed_edge)` pairs. Separates estimator uncertainty from strategy uncertainty.

**NOT shipped (deliberately):** Kelly is not wired into `diamond_paper.py`. Live execution still uses flat tiered sizing. No code path currently imports `diamond_kelly.py` from production. Activation requires explicit config flip + ML integration work (see below).

**Activation path (4 sequential gates — tightened 2026-04-21, regime cutoff updated 2026-04-29):**
1. **N ≥ 500 post-Sprint-14f settled trades** (Unix epoch ≥ `1777431874` = 2026-04-29 03:04:34 UTC; measured tempo ~70/day → **ETA ~May 6**). The Sprint 14e cutoff (`1777346853`) was obsoleted by the Sprint 14f orderbook-poll fix; trades collected between the two have broken absorption features. See Sprint 14f regime-cutoff rationale.
2. ML retrain produces validated model: **Brier < 0.05 AND Jaccard ≥ 0.70** across CV folds
   (tightened from Brier < 0.25 after Phase B; see Sprint 14c-addendum below)
3. **Wire ML edge into entry gate** (not just sizing) — add `ml_edge < MIN_EDGE_HURDLE` pre-entry rejection to `_execute_trade()`. Sprint 13b proved composite score has near-zero IC; Phase C empirically confirmed at −$477/55% DD what happens when Kelly sizes composite-admitted noise.
4. Backtest validates: run `backtest_kelly.py` on post-N=500 (post-14e) data. Confirm Phase C max-drawdown shrinks vs flat with the new ML-backed edge estimator. Then flip `KELLY_SIZING_ENABLED=true`.

### Sprint 14c-addendum (2026-04-21, HARNESS EXECUTED) — Phase ABC findings

Executed the staged backtest with 3 material code fixes + 1 invariant fix during the run:

**Bug fixes landed in `backtest_kelly.py`:**
- **Ledger invariant was wrong** (`cash + locked + realized = initial` would double-count gains on every settlement). Corrected to `cash + locked - realized = initial`.
- **Terminal bankroll double-counted** (was `cash + realized` = `initial + 2·realized`; now just `cash`).
- **MaxDD was stub-only** (TODO in scaffold). Implemented time-indexed equity curve emitted per settlement; MaxDD computed as peak-to-trough.
- **Warmup N-matching** (collaborator diagnosis, Phase C trap): when PiT bucket edge returns `None` (bucket N < MIN_ACTIVE_N=10), the trade is dropped from BOTH Kelly and Flat cohorts before metric comparison. Without this, Kelly runs on a different universe than Flat and the comparison is dominated by survival bias.

**Phase A (Oracle, N=289):** Terminal +5050% return, zero drawdown (perfect info → Kelly skips every loser + sub-hurdle winner ≥98¢). Flat: −$1.47 (matches live reality). Harness verified correct.

**Phase B (σ sweep with biased clip, N=289):**

| σ | Kelly P&L | MaxDD % | Brier-equivalent (σ²) |
|---|---|---|---|
| 0.00–0.05 | +$51-52k | **0.00%** | ≤0.0025 |
| 0.07 | +$49.8k | 1.44% | 0.005 |
| 0.10 | +$43.8k | 5.03% | 0.01 |
| 0.20 | +$25.4k | 4.33% | 0.04 |
| 0.30 | +$16.9k | 9.59% | 0.09 |
| 0.50 | +$7.1k | 13.27% | 0.25 (random) |

**σ_max (zero-DD regime): 0.05** → Brier < 0.0025, effectively oracle.
**σ_max (< 5% DD): ~0.08** → Brier < 0.006.
**σ_max (Kelly > Flat P&L, any DD): > 0.50** → biased-clip happy path; Kelly stays profitable across entire sweep because winners always clip to 0.99 and only losers can flip to false-positive edge.

**ML gate tightened to Brier < 0.05** (σ < 0.22, <5% DD). Prior gate of Brier < 0.25 was σ≈0.50 / 13% DD territory — too permissive.

**Phase C (PiT bucket empirical, matched N=227, warmup dropped 62):**

| Strategy | P&L | MaxDD $ | MaxDD % |
|---|---|---|---|
| Flat matched | −$0.29 | $6.27 | **0.62%** |
| Kelly (bucket edge) matched | **−$477.50** | $580.41 | **55.22%** |

Bootstrap (event-cluster, N=10,000): P&L diff mean −$473 [p05: −$685, p95: −$188]. DD diff mean −$637 [p05: −$835, p95: −$440]. **Both intervals entirely negative across all 10,000 draws.**

**15-24¢ STEAMROLLER TOMBSTONE:** 72% of Kelly's total loss (−$344.86 of −$477.50) came from the 15-24¢ bucket alone. Mechanism: early trades in that bucket happened to win, creating a transient bucket WR above the implied-probability range. PiT estimator read this as positive edge. Because 15-24¢ contracts are cheap, Kelly's 5% bankroll cap allowed *thousands* of contracts per trade. When the bucket's true base rate manifested (5% Kelly WR out of 20 takes), concentrated positions in 20¢ contracts got annihilated.

**Interpretation:** Empirical validation of Sprint 13b's theoretical IC finding. Raw price-bucket WR has near-zero post-price IC; Kelly-sizing that noise produces leverage into tail outcomes rather than dampening risk. **Do NOT use bucket-WR as a Kelly edge estimator.** A properly calibrated per-trade ML model is mandatory.

**Gap identified → closed in Sprint 14d** (below): Bayesian shrinkage with k=20+3√N pseudocount, compression and limits measured.

### Sprint 14d (2026-04-21, HARNESS CLOSED OUT) — Bayesian shrinkage + Two-Gate Stack epistemology

Closed out Sprint 14 with the Laplace-smoothed bucket estimator and an empirical audit of what shrinkage can and cannot do.

**`src/diamond_shrinkage.py` — now complete, dormant pending ML integration:**
- `shrunk_wr(wins, n, prior_mean, prior_strength)` — Laplace smoothing, self-checked on 5 hand-computed cases (phase C steamroller at k=20 → edge=0.04; large-N regime dominates prior; input validation; etc.)
- `prior_strength_default(n, p)` = `20 + 3√N`
  - **k_base=20 is analytically derived** (not heuristic): the minimum pseudocount that squashes a 1σ noise event at N=5 below the 5% per-trade Kelly cap. For wins=2, n=5, p=0.20: shrunk_edge = (W − N·p)/(N+k) = 1/(5+k) ≤ 0.04 requires k ≥ 20.
  - **α√N floor** is isomorphic to the standard error of a proportion — prior weight fades in lockstep with the data's statistical precision. Principled Empirical Bayes.
  - Schedule: N=0 → k=20 (100% prior), N=25 → k=35 (58% prior), N=100 → k=50 (33% prior), N=400 → k=80 (17% prior).
- Two convenience wrappers: `shrunk_bucket_edge()` for Phase C use, `shrunk_category_wr()` for future ML target encoding.

**Phase C re-run results (matched N=227, bootstrap 10,000):**

| Run | Kelly P&L | Kelly MaxDD | Boot mean P&L diff | Boot p95 P&L diff |
|---|---|---|---|---|
| Naive bucket (legacy tombstone) | −$477.50 | 55.22% | −$473 | −$188 |
| Shrunk (k=20+3√N, matched) | −$277.58 | 34.11% | −$277 | −$8 |
| **Shrunk (no warmup cutoff, full N=289)** | **−$221.25** | **34.09%** | **−$216** | **+$83** |

Shrinkage compressed the loss by 42%, compressed MaxDD by 38%, and — critically — pushed the bootstrap p95 across zero. 5% of draws now show Kelly **beating** Flat. The naive run never had a positive bootstrap draw.

**The 15-24¢ steamroller — partially exhumed:**

| | Naive | Shrunk |
|---|---|---|
| Trades Kelly took | 20 | 10 |
| Kelly P&L contribution | −$344.86 | −$140.32 |

Shrinkage halved the bucket's exposure and cut its P&L contribution by 59%. The `k ≥ 20` analytical bound held — the math protected against the steamroller exactly as derived. But 10 trades still leaked through because they cleared a sub-hurdle shrunk edge with hundreds of prior observations; once Kelly is admitted, its allocation is driven by bucket size × contract price, not shrinkage magnitude.

**min_active_n hard cutoff retired (2026-04-21):** The `--min-active-n` CLI flag and associated logic were deleted after the ablation proved shrinkage handles warmup dynamically. Hard cutoffs introduce step-function discontinuities (at N=9 reject; at N=10 size dynamically); shrinkage replaces them with a mathematically smooth taper from 0 to f* as sample accumulates. Default Phase C is now `shrink=True`; `--no-shrink` remains available for tombstone reproduction only.

**The Fundamental Theorem of Alpha (stated informally):**
> *"Shrinkage is a variance-reducer, not an information-creator."*

Empirical proof: a zero-IC estimator + optimal shrinkage still produces negative expected value. The bucket-WR estimator post-Sprint-13b has near-zero post-price IC; shrinkage makes it safer (smaller tails, more selective) but cannot manufacture edge. Residual −$277 loss is information-theoretic, not sizing-theoretic.

**The Two-Gate Stack (Kelly activation epistemology):**

Kelly sizing is an *amplifier*. Shrinkage is a *dampener*. An ML model with real IC is the *engine*. All three are required:

1. **Gate A — Information:** ML produces calibrated edge with Brier < 0.05 AND Jaccard ≥ 0.70
2. **Gate B — Variance control:** Shrinkage wraps the ML output with k=20+3√N pseudocount

Only after both gates pass is `KELLY_SIZING_ENABLED=true` safe. Flipping one without the other reproduces either Phase C naive (variance disaster) or a zero-EV strategy sized correctly.

### Sprint 13a (2026-04-09, DEPLOYED) — L1 collinearity trap fix
Lasso (`l1_ratio=1.0`) was zeroing out correlated discriminative features arbitrarily — Jaccard < 0.30 across CV folds (target ≥ 0.70). Switched shadow ML to **Ridge L2** (`l1_ratio=0.0`). Sparsity sacrificed for stability; feature subset now consistent across folds. Live execution unaffected (ML still shadow mode).

### Sprint 13b (2026-04-11, DEPLOYED) — Signal audit + base-rate confound
Composite score showed inverse U-shape WR vs entry price — pure base-rate artifact (longshots structurally win less, favorites structurally win more, score rides along). Conditional-on-price IC analysis revealed **6 of 8 scorers have |IC| < 0.03** after controlling for entry price. Diagnosis: scorers measure trade *activity*, not directional *edge*. Built `FeatureOrthogonalizer` to residualize features against price for ML training.

### Sprint 13c (2026-04-13, DEPLOYED) — Intrinsically orthogonal features
Two new shadow ML features designed to be price-independent by construction:

1. **`flow_acceleration`** — 2nd derivative of trade velocity (Δ rate over consecutive 30s windows). Temporal leakage exclusion: window shifted to `[T-31s, T-1s]` and `[T-61s, T-31s]` so the triggering trade at T is excluded from its own measurement.

2. **`event_relative_flow`** — Ticker's share of event-level total volume vs uniform distribution. Event manifold normalization: removes absolute-volume-vs-price coupling.

3. **Sparse-aware orthogonalizer** (`src/diamond_ml.py::FeatureOrthogonalizer`) — Critical mathematical fix. Naive OLS on zero-inflated features hallucinates a deterministic negative correlation with price (synthetic test: −0.59 spurious slope). New version fits OLS on `val != 0` rows only; transforms `val != 0` rows only; zeros stay exact zeros. `MIN_ACTIVE_N=10` skip guard. `SPARSE_FEATURES = {"flow_acceleration", "event_relative_flow"}`.

4. **Sibling staleness guard** — `event_relative_flow` reads `market_profiles.volume_24h` which is batch-refreshed every `METADATA_REFRESH_SEC=300` (NOT per-trade). Threshold: 600s (2× refresh interval). Stale → emit `event_relative_flow=0` and set `_event_rel_flow_stale=1` flag. Kill-switch: stale flag > 20% of activations.

5. **Shadow mode discipline** — New features added to `RAW_FEATURES` in `diamond_ml.py` for ML training only. NOT in `SCORER_WEIGHTS`. Composite score and live execution untouched. `FEATURE_ENABLED["flow_acceleration"] = True` and `FEATURE_ENABLED["event_relative_flow"] = True` in `diamond_config.py`.

6. **`evaluate_model.py` extension** — Added `Sparse Orthogonal Feature Evaluation` block. Reports activation rate, price correlation (kill at >0.30), conditional IC, residual IC, top quintile edge. Kill conditions: `abs(price_corr) > 0.30` OR `abs(cond_ic) < 0.05` at N≥50.

### Sprint 14e (2026-04-26 → 2026-04-28, DEPLOYED) — WebSocket subscription fix + book absorption metrics + REGIME CUTOFF

**The most expensive 30-line bug in DIAMOND's history.** Three compounding flaws in the WebSocket subscription path were silently dropping ~60-90% of the eligible market universe since startup. Discovered by a "why is Kalshi so quiet?" diagnostic on 2026-04-26.

**Phase 1 — Subscription pipeline fix (deployed 2026-04-26 22:13 UTC):**
- `diamond_monitor.py:602`: `BATCH_SIZE = 100` capped startup subscriptions; `remaining = tickers[BATCH_SIZE:]` was captured but never used.
- `KalshiWSClient.connect()`: only subscribed to the initial tickers; reconnect re-used the same set, ignoring any updates.
- `metadata_refresh_loop()`: updated `market_cache` for the dashboard but never told the WebSocket about new tickers.
- **Fix**: added `_desired_tickers: set[str]` instance state to `KalshiWSClient`, restored on reconnect; new `sync_subscriptions()` method computes add/remove deltas; new `resubscribe_loop()` task in monitor diffs `market_cache.keys()` against subscribed set every `METADATA_REFRESH_SEC=300`.
- **Verified live**: subscription went from 67 → 128 → 257 tickers post-fix. Trade-stream throughput recovered from ~0-1/min to 500-1300/min (the system's design baseline).

**Phase 3 — Order-book absorption metrics (deployed 2026-04-28 03:27:33 UTC):**
- New `book_absorption_metrics()` in `src/diamond_features.py` (~140 lines). Single shared data extraction (book_now + book_prev + trades_in_60s_window) feeds 4 candidate variants:
  - `book_absorption_static` — gross flow / depth at t₀
  - `book_absorption_depletion` — net depth change rate (cents/sec)
  - `book_absorption_sided` — side-weighted (yes_pressure − no_pressure)
  - `book_absorption_replenish` — defended vs eaten ratio (Variant D, most information-rich)
- Variant D edge case: if total_volume ≥ depth_at_t₀, force `-1.0` ("book swept / toxic flow" sentinel).
- Window: 60s (matches anomaly cadence; ruled by collaborator 2026-04-26).
- `_book_absorption_stale` telemetry flag fires when prior-snapshot is missing. Kill-switch: alert if firing rate > 20% of activations long-term.
- **Shadow mode discipline**: 4 variants added to `RAW_FEATURES` in `diamond_ml.py`. NOT in `SCORER_WEIGHTS`. Composite score and live execution untouched.

**REGIME CUTOFF — N=500 GATE RESET TO 2026-04-28 03:27:33 UTC (Sprint 14e Deployment):**
- The Phase 1 fix changed the data-generating distribution by expanding the observed universe from 67 to 257+ tickers. The new universe includes intraday sports, crypto, and rapid-resolution binary contracts with entirely different volatility profiles than the constrained pre-fix universe.
- The 290 trades opened pre-2026-04-28 03:27:33 UTC are the **Pre-14e Cohort** — purged from the final ML training dataset. Retained for historical analysis only.
- The N=500 ML retrain gate is **reset to count only trades opened on or after 2026-04-28 03:27:33 UTC** (Unix epoch `1777346853`).
- Phase 3 absorption metrics are zero for the entire Pre-14e cohort (the feature didn't exist yet) — another reason to purge them. Mixing cohorts would cause Ridge regression to learn that absorption is meaningless (because it's structurally zero in 290 of 500 rows).
- ETA at sustained ~50/day tempo: **~10 days** (early-to-mid May 2026).

**Empirical evidence forcing the cutoff** (learning curve sweep, 2026-04-27):
| N (post-Sprint-11) | Brier | AUC | Δ vs naive |
|---|---|---|---|
| 250 | 0.1923 | 0.761 | −0.058 |
| 290 | 0.2087 | 0.744 | −0.041 |
| 340 | 0.2291 | 0.714 | −0.020 |

Brier is monotonically rising; AUC is monotonically falling; gap to naive collapsing. Diagnosed as regime-mixing contamination, not feature-IC failure. Sprint 14e cohort isolation is the corrective action.

**Monitoring protocol (during the new patience window):**
1. **48h check** (~2026-04-30): ✅ PASSED — `_book_absorption_stale` rate dropped to **0.0%** (9/47,101) at +24h post-Sprint-14f. Nonzero absorption values at 98.8%. See Sprint 14f verification results in Current Status.
2. **N=100 check** (~2026-05-01 at revised 70/day tempo): **PENDING** — post-14f cohort was at 67/500 as of Apr 30. Run `PYTHONPATH=. python learning_curve.py --grid 50,100` when cohort crosses 100. Pre-registered thresholds: Brier < 0.18 = absorption working; 0.20-0.22 = needs more N; > 0.23 = concern. Note: `--min-opened-at` default in `learning_curve.py` already set to Sprint 14f cutoff (`1777431874`).
3. **Do NOT touch execution thresholds.** Composite score firing rate, ALERT/CRITICAL thresholds, tiered sizing all unchanged.
4. **Do NOT lower the Brier < 0.05 ML promotion gate.** Two-Gate Stack policy stands.

### Sprint 14f (2026-04-29 03:04:34 UTC, DEPLOYED) — Orderbook poll universe expansion + N=500 reset

**Bug:** `orderbook_poll_loop` had a hardcoded `tickers_to_poll = active_tickers[:30]` cap from the early sandbox era when the universe was ~30 tickers. Combined with `get_all_active_tickers()` returning historical (alphabetically-ordered) tickers, the poll was capturing book snapshots ONLY for ~30 long-dated political/entertainment markets (KXAMERICANIDOL, KXARREST, KXARTISTSTREAMS) that almost never fire anomalies. The high-velocity sports markets that actually fired anomalies (MLB, NBA, NHL, soccer, tennis) had **zero book snapshots ever recorded**.

**Symptom (caught by Phase 3 telemetry within 23 hours of Sprint 14e deploy):** `_book_absorption_stale` flag firing on **100% of 34,960 anomalies**. The `_book_absorption_stale` canary worked as designed — surfaced what would otherwise have been a 9-day silent failure ending in a mysterious null-importance result on the absorption variants.

**Fix:**
- `orderbook_poll_loop`: switched poll target from `store.get_all_active_tickers()[:30]` to `list(market_cache.keys())` (the actual current active universe per the metadata refresh loop).
- Per-request sleep tightened from `0.5s` → `0.1s` (10 req/sec, 50% safety buffer under Kalshi's 20 req/sec limit).
- Math: ~250 tickers × 0.1s = 25s per full sweep. With `REST_POLL_INTERVAL_SEC` between sweeps, every ticker gets polled ~every 30-60s — comfortably inside the 60s absorption window lookback.

**REGIME CUTOFF — N=500 GATE RESET TO 2026-04-29 03:04:34 UTC (Sprint 14f Deployment):**
- The 50 trades opened in the post-14e / pre-14f window (2026-04-28 03:27:33 to 2026-04-29 03:04:34) all have all-zero absorption features (the data conduit was broken). Including them would cause Ridge regression to learn that absorption variants don't predict outcomes — false null-importance, false negative on potentially valuable features.
- The N=500 ML retrain gate is **reset to count only trades opened on or after 2026-04-29 03:04:34 UTC** (Unix epoch `1777431874`).
- Pre-14f Cohort (50 trades, broken absorption) joins the Pre-14e Cohort (290 trades, no absorption code) in the "purged from training set" pile.
- ETA at sustained ~70/day (revised up from 50/day after Sprint 14e universe expansion): **~May 6** (67/500 as of Apr 30).

**Cost of the regime reset:** 50 trades + ~22 hours of patience window. **Cost of NOT resetting:** false-negative null importance on the absorption features → 9-day patience window wasted → another retrain cycle needed → 3-4 more weeks of total delay. The reset is the cheap option.

**Verification protocol (post-deploy) — ALL PASSED:**
1. +10 min: snapshot history populated for 99 distinct tickers (was 33 pre-fix). ✅
2. +10 min: stale rate on post-14f anomalies = 11.5% (warmup, brand-new tickers without 60s-prior history). ✅ Expected.
3. +24h (Apr 30): stale rate = **0.0%** (9/47,101). Nonzero absorption values = **98.8%** (46,548/47,101). Snapshot breadth = 125 tickers. ✅ Absorption pipeline fully operational.
4. Weekly: re-check stale rate. Alert if it climbs above 5% sustained (would indicate Kalshi rate limiting or poll-loop degradation).

### Sprint 14g (2026-05-01 03:09:02 UTC, DEPLOYED) — Kill switch SQL aggregation bug + two-tier cap

**The third sandbox-era artifact found in 5 days.** After Sprint 14e (subscription truncation `[:100]`) and Sprint 14f (orderbook poll cap `[:30]`), Sprint 14g surfaces another archetype: an **unbounded SQL aggregation**.

**Bug:** `src/diamond_store.py:get_paper_stats()` had:
```sql
SELECT COALESCE(SUM(pnl_cents), 0) FROM paper_trades WHERE status = 'settled'
```
…with no date filter. The result was assigned to a variable named `total_pnl` and used as `total_daily_pnl_cents` downstream. The daily kill switch in `diamond_paper.py:_execute_trade` checked that "daily" value, which was actually all-time cumulative. The discrepancy was invisible until cumulative P&L crossed −$20 on 2026-04-29; from that moment, every entry attempt was rejected with `kill_switch: P&L -2195¢ < -$20`.

**Symptom (caught 2026-05-01 by Sprint 14e telemetry methodology):** 0 entries opened in the entire UTC day Apr 30 despite 2,841 ALERT-level anomalies firing. Skip-reasons query showed **1,765 `kill_switch` rejections in 24h** while today's actual realized P&L was +$0.00.

**Fix (two-tier kill switch):**
- `src/diamond_store.py`: added separate `today_realized_pnl` query with `settled_at >= strftime('%s', 'now', 'start of day')` filter. Returns both `total_pnl_cents` (cumulative) and `today_realized_pnl_cents` (daily) so callers don't have to guess.
- `diamond_paper.py:_execute_trade`: split into two checks, evaluated in order:
  - **Tier 1 (cumulative hard halt):** `cumulative_pnl < -PAPER_MAX_CUMULATIVE_CENTS` ($50). NEVER auto-resets. Hitting this means strategy must be re-validated before trading. Skip reason: `kill_switch_cumulative`.
  - **Tier 2 (daily soft halt):** `today_realized + unrealized + estimated_cost < -PAPER_MAX_UNREALIZED_CENTS` ($20). Resets at UTC midnight. Skip reason: `kill_switch_daily`.
- `diamond_config.py`: `PAPER_MAX_CUMULATIVE_CENTS = 5000` (env-overridable).

**Recovery:** With cumulative at -$20.96 (well above the new -$50 cap) and today's daily P&L at +$0.00, the cumulative check passes. The daily check now correctly evaluates today_realized + unrealized = $0.00, well above -$20 — entries should resume immediately on first post-deploy ALERT.

**Sandbox artifact pattern (now 3 confirmed):**
| Sprint | Artifact | Failure mode |
|---|---|---|
| 14e | `BATCH_SIZE = 100; first_batch = tickers[:BATCH_SIZE]` | Subscription truncated; 60% of universe deaf |
| 14f | `tickers_to_poll = active_tickers[:30]` | Orderbook polled only 30 historical tickers; sports markets had 0 snapshots |
| 14g | `SUM(pnl_cents) WHERE status='settled'` (no date filter) | Daily kill switch silently checked cumulative |
| 14h | `except Exception` in `orderbook_poll_loop` (CancelledError = BaseException, not Exception) | aiohttp session recycle raised CancelledError mid-request; bypassed handler; task died silently |

**Common archetype:** code that was correct at small N, never revisited as the system grew, silently failing in production under conditions the original author didn't anticipate.

**Backlog item (formal Sandbox Artifact Audit):** systematic grep of the codebase for unbounded `SELECT SUM/COUNT` queries, hardcoded `[:N]` slices, `except Exception` around await calls (CancelledError can escape), and similar small-N assumptions.

### Sprint 14h (2026-05-06 02:12:12 UTC, DEPLOYED) — CancelledError catch + task death watchdog + N=500 reset

**The fourth sandbox-era artifact found in 9 days.** Pattern continues: code correct at small N, broken at production scale, fails silently.

**Bug:** `orderbook_poll_loop` (and `metadata_refresh_loop`) used `except Exception:` around `await rest.get_orderbook()` calls. Every 30 minutes (`SESSION_RECYCLE_SEC = 1800`) the aiohttp session is recycled — `_ensure_session()` calls `await self._session.close()` which cancels in-flight requests, raising `asyncio.CancelledError` inside the awaited HTTP request. **`CancelledError` is a `BaseException` subclass, NOT `Exception`** (this is intentional in Python 3.8+, so cancellation can't be swallowed by generic handlers). It bypassed both inner and outer `except Exception:` blocks and killed the task. `asyncio.gather(return_exceptions=True)` in `monitor()` then suppressed the death notification.

**Symptom (caught 2026-05-05 by audit triggered by user inquiry):** `book_snapshots` table empty for 4+ days, despite the daemon being up and trading actively. `_book_absorption_stale` rate at 100% on 9,151 anomalies in last 6h. Strace of running process showed **zero outbound HTTP syscalls** across all 5 threads. py-spy dump showed only the WebSocket thread active.

**Forensic timeline:**
1. **2026-05-01 03:09:02 UTC:** Sprint 14g restart. `orderbook_poll_loop` task starts. Continuous polling: 250 tickers × 0.1s sleep ≈ in-flight every iteration.
2. **2026-05-01 18:14:00 UTC:** `[ERROR] asyncio: Unclosed client session` + `Unclosed connector` in log — session was force-recycled while orderbook_poll_loop was mid-request. CancelledError raised, bypassed `except Exception`, task died silently.
3. **2026-05-01 18:17 UTC onward:** No `book_snapshots` rows written. Other loops (metadata_refresh, status_report, paper.poll, resubscribe) survived because none had in-flight requests at the cancellation moment.
4. **2026-05-03 18:17:08 UTC:** Last NONZERO prune (466 rows). The 2-day pruning window caught up to the May 1 18:17 "wall."
5. **2026-05-03 18:27:21 UTC:** First ZERO prune. Table now empty.
6. **2026-05-04 onward:** `_book_absorption_stale = 1.0` on 100% of anomalies. The Sprint 14e canary fired correctly — but no one was watching the stale-rate alarm until the user asked for a daily status report.

**Cohort impact (purged):** Of 393 settled trades in the post-14f cohort:
- ~68 (Apr 29) had clean absorption telemetry
- ~325 (May 1+) have all-zero contaminated absorption features
- **~83% contamination** — feeding this to Ridge regression would mathematically anchor the absorption feature weights to zero, immunizing the model against learning anything about order book dynamics. The 393 trades join the 290+50 pre-14e/pre-14f purged cohorts.

**Three-patch fix (deployed simultaneously to avoid mid-deploy regime mixing):**

1. **Patch 1 — `orderbook_poll_loop` CancelledError handling + heartbeat** (`diamond_monitor.py:399`):
   - Inner `try/except` now catches `asyncio.CancelledError` separately, logs warning, continues
   - Outer `try/except` distinguishes "real shutdown cancellation" (running=False, propagate) from "stray cancel while running" (log, continue)
   - Final `except BaseException` catches *anything* else (KeyboardInterrupt, SystemExit) to prevent silent death
   - Startup banner: `[ORDERBOOK_POLL] Loop started`
   - Heartbeat every 10 iterations (~5 min): `[ORDERBOOK_POLL] iter=N written=W errors=E cancels=C tickers=T`
   - Absence of heartbeat is now an observable failure signal

2. **Patch 2 — Top-level task death watchdog** (`diamond_monitor.py:642`):
   - `_task_death_logger(name)` returns a `done_callback` that fires CRITICAL Pushover alert if a task dies with an exception
   - Distinguishes clean cancel (info log), unexpected death (critical log + Pushover priority=1), and unexpected clean exit (error log)
   - `_spawn(coro, name)` helper wraps `asyncio.create_task()` and attaches the callback in one step
   - All 6 tasks (`metadata_refresh_loop`, `orderbook_poll_loop`, `status_report_loop`, `paper_poll_loop`, `ml_retrain_loop`, `ws_with_subscribe`, `resubscribe_loop`) now use `_spawn`
   - Structurally guarantees: silent task death is impossible; future bugs of this class are detected within seconds

3. **Patch 3 — `metadata_refresh_loop` CancelledError handling** (`diamond_monitor.py:370`):
   - Same vulnerability (HTTP calls via `refresh_markets` → `rest._request`), same fix pattern
   - Startup banner + same three-tier exception handling as Patch 1

**Verification (post-deploy):**
1. Wait for `[ORDERBOOK_POLL] Loop started` log line (~30s post-restart)
2. Wait for first heartbeat `[ORDERBOOK_POLL] iter=1 written=N` (~60-90s post-restart)
3. Confirm `book_snapshots` rows accumulating: `SELECT COUNT(*) FROM book_snapshots WHERE ts >= strftime('%s','now','-5 minutes')` should be > 100 within ~5 min
4. After 30 min, look for `[ORDERBOOK_POLL] Request cancelled for X (likely aiohttp session recycle); continuing` — first session recycle should now be survived gracefully

**REGIME CUTOFF — N=500 GATE RESET TO 2026-05-06 02:12:12 UTC (Sprint 14h Deployment):**
- The 393 settled trades opened between Apr 29 03:04 UTC and May 6 02:12 UTC are the **Pre-14h Cohort**. Most have all-zero absorption features (the data conduit was broken May 1 18:14 onward). Purged from final training set; retained for historical analysis.
- The N=500 ML retrain gate is **reset to count only trades opened on or after 2026-05-06 02:12:12 UTC** (Unix epoch `1778033532`).
- Gate cohort epoch in `learning_curve.py` updated: default `--min-opened-at = 1778033532.0`.
- Cost: 7 days lost (Apr 29 → May 6). Benefit: clean training data + immortal task watchdog that prevents this exact failure class permanently.

**Activation timeline reset:**
- N=500 ETA at *measured* 145/day tempo (148 settled in first 24h post-deploy; 11h burst rate of 205/day did not sustain): **~3 days from May 6** → **~May 9, 2026**.
- Full Kelly activation timeline shifts BACK to roughly the original window: late May / early June for Two-Gate Stack validation + entry-gate wiring + flip.
- Two-Gate Stack policy unchanged: Brier < 0.05 AND Jaccard ≥ 0.70 required to flip `KELLY_SIZING_ENABLED=true`.

**N=100 checkpoint lesson (2026-05-07, retired):** The pre-registered N=100 Brier checkpoint was overoptimistic — it assumed CV would produce a valid fold at N=100, but the empirical PiT-purger threshold is `pit_mask.sum() >= 50` per fold (`src/diamond_ml.py:488`). With 5-fold CV at N=148, fold sizes are ~30 each; the first fold has fewer than 50 prior-settled trades, so all folds skip with "0 valid out of 0 attempted." The PiT purger is doing exactly what it was engineered to do in Sprint 13 — actively protecting against lookahead bias on samples too small to evaluate. **The first valid fold materializes at N≈250-300, not N=100.** Pre-registered thresholds rolled forward to N=300.

**Revised N=300 Brier matrix:**
- Brier < 0.18 → Signal (absorption variants adding IC)
- Brier 0.20-0.22 → Inconclusive (need more N)
- Brier > 0.23 → Systemic contamination (regime-mixing concern, investigate before N=500)

**Revised checkpoint plan:**
- **N=300** (~May 8 evening at 145/day): first valid PiT-purged fold expected → first real Brier signal. If signal/contamination, act before N=500. If inconclusive, hold to N=500.
- **N=500** (~May 9): ML retrain trigger per existing roadmap.

### Patience Discipline (CRITICAL — UPDATED 2026-05-06)
**Hold ML retrain until N=500 post-Sprint-14h settled trades accumulate** (Unix epoch ≥ `1778033532`, ETA **~May 8-9** at measured 205/day tempo). The Sprint 14f cutoff (`1777431874`) is OBSOLETE — Sprint 14h's orderbook_poll_loop revival represents the third regime change in 9 days. Pre-14h cohorts (290 pre-14e + 50 post-14e/pre-14f + 393 post-14f/pre-14h) all retained for historical analysis but excluded from the final training set.

**Total cohort cost summary (transparency for the audit log):** 290 + 50 + 393 = **733 trades** purged from the training set across Sprints 14e/14f/14h. Each cohort reset preserved dataset stationarity rather than moving goalposts. The alternative — feeding mixed-regime data to Ridge regression — would have produced a permanently broken model and required even longer recovery.

This is now the THIRD cohort reset in 9 days. The pattern of repeated sandbox-artifact discoveries justifies the formal Sandbox Artifact Audit (now scheduled post-Sprint-14h stabilization).

The shadow telemetry (`flow_acceleration`, `event_relative_flow`, `book_absorption_*`) will accumulate cleanly during this window — do NOT rush the retrain.

---
**Historical sprints (pre-Sprint 13):**
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
- **Stuck Settlement Loop Drained ✓** (13 orphan `status='filled'` rows from Kalshi taxonomy drift [`determined`/`finalized`] + silent `log.debug` exception swallowing — `reconcile_stuck_trades.py --apply` — Sprint 14, April 2026)
- **Fill-Price Corruption Fix ✓** (line 544 of `diamond_paper.py::_cancel_after()` hardcoded `None` — now parses from `maker_fill_cost_dollars + taker_fill_cost_dollars / fill_count` — Sprint 14, April 2026)
- **Application-Layer Integrity Guard ✓** (`update_paper_fill` rejects corrupt fills, downgrades to `unfilled` — Sprint 14, April 2026)
- **Schema CHECK Constraint DEPLOYED ✓** (Layer 3: `check_valid_fill` CHECK constraint on `paper_trades` — 805 rows rebuilt in <0.1s — smoke test confirmed constraint rejects NULL fill_price on filled rows — Sprint 14b, 2026-04-20)
- **Orphan Backfill ✓** (9 rows recovered via `backfill_orphan_fills.py --apply`, 6W/3L/-23¢ — Sprint 14, April 2026)
- **3-Layer Defense-in-Depth ✓** (code fix + app guard + schema constraint, non-overlapping failure domains — Sprint 14, April 2026)
- **Layer 3 CHECK Constraint LIVE ✓** (Sprint 14b, 2026-04-20 — 805-row rebuild in <0.1s, smoke-test verified, ~2 min downtime)
- **Dormant Kelly Utility ✓** (`src/diamond_kelly.py` — Half-Kelly default, 0.02 slippage-adjusted hurdle, 5% cap, feature-flagged off — Sprint 14c, April 2026)
- **Kelly Backtest Harness ✓** (`backtest_kelly.py` — 3-phase Oracle/Sensitivity/PiT, event-cluster bootstrap, ledger invariant — Sprint 14c, April 2026)

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

## Performance Summary (as of April 30, 2026)
- **Settled trades:** 903 (+174 since Apr 20, driven by Sprint 14e universe expansion)
- **Win rate:** 51.1%
- **Cumulative P&L:** −$20.32 (bleed accelerated from −$10.87 as wider universe captured more mid-price convexity-trap trades)
- **Apr 29 UTC day:** 83 settled, 56.6% WR, −$5.19 P&L (convexity-trap signature: winning small on favorites, losing big on rare mid-price losers)
- **Post-14f cohort (opened ≥ Apr 29):** 67/500 at 70/day → ETA N=500 ~May 6. First cohort with real absorption telemetry.
- **Sprint 14f verification:** `_book_absorption_stale` = 0.0% (9/47,101), nonzero absorption = 98.8%, snapshot breadth = 125 tickers. Pipeline fully operational.
- **Fill rate:** ~91% of placed orders fill
- **By alert level:** CRITICAL dramatically outperforms ALERT
- **Tiered sizing deployed:** CRITICAL=3 contracts, High ALERT=2, Low ALERT=1
- **ML model:** AUC=0.809, Brier=0.180. Needs retrain post N=500 on post-14f cohort. Shadow mode only.
- **WebSocket health:** stable post-Sprint-14e (250+ tickers subscribed)
- **Data integrity:** All three layers (code/app/schema) — zero firings since Apr 16.
- **Activation timeline:** ~4 weeks from Apr 30 (late May / early June) for full Kelly activation. Requires ML retrain passing Brier < 0.05 + Jaccard ≥ 0.70 — first retrain likely near-miss, realistic activation after second retrain at N=750-1000.

## Historical Performance Snapshots
- **April 4**: 472 settled, 49.0% WR, -$8.52 cumulative
- **April 14**: 698 settled, 50.9% WR, -$10.55 cumulative
- **April 18**: 720 settled, 51.0% WR, -$10.98 cumulative
- **April 20**: 729 settled, 50.9% WR, -$10.87 cumulative (Layer 3 deploy day)
- **April 30**: 903 settled, 51.1% WR, -$20.32 cumulative (Sprint 14f verification passed, absorption pipeline operational)

## Next Steps / Roadmap

### Active state (patience window, N=500 gate ~May 6)

- **[ONGOING] Passive monitoring** — watch for three signals, all of which should stay at zero:
  - `[STORE] REJECTED CORRUPT FILL` (Layer 2 guard firing → Layer 1 regression)
  - `sqlite3.IntegrityError: CHECK constraint failed: check_valid_fill` (Layer 3 enforcing → Layer 1+2 both regressed)
  - Orphan rows in `paper_trades` WHERE status='filled' AND fill_price IS NULL
  - Any of these firing is a P0 investigation trigger
- **[ONGOING] Accumulate post-14f settled trades** — 67/500 as of Apr 30. Trade tempo **~70/day** → gate unlocks ~May 6. Do NOT touch thresholds or attempt to accelerate flow.
- **[ONGOING] Track absorption pipeline health** — `_book_absorption_stale` rate should stay near 0%. Alert if sustained above 5%.
- **[PENDING] N=100 learning curve checkpoint** — run `PYTHONPATH=. python learning_curve.py --grid 50,100` when post-14f cohort crosses 100 settled. Pre-registered thresholds: Brier < 0.18 = absorption hypothesis supported; 0.20-0.22 = needs more N; > 0.23 = regime contamination concern.
- **[BACKLOG] Sandbox artifact audit** — grep codebase for unbounded `SELECT SUM/COUNT` (no date filter), hardcoded `[:N]` slices, `LIMIT N` constants. Three found in 5 days (14e/14f/14g); likely more exist.

### Completed (Phase ABC executed, Sprint 14c-addendum)

- ✅ **Phase A (Oracle):** Harness verified. Terminal +5050%, zero DD. Flat: −$1.47.
- ✅ **Phase B (σ sweep):** σ_max for <5% DD ≈ 0.08 (Brier < 0.006). ML gate tightened to Brier < 0.05.
- ✅ **Phase C (PiT bucket):** Kelly −$477 / 55% DD on naive bucket edge. Shrunk (k=20+3√N): −$277 / 34% DD. Fundamental Theorem: shrinkage reduces variance but cannot create IC. ML model required.
- ✅ **Sprint 14d (Bayesian shrinkage):** `src/diamond_shrinkage.py` complete + dormant. Two-Gate Stack epistemology codified.

### Gated (requires N ≥ 500 post-14f, ETA ~May 6)

- **[GATE 1: N=500] Retrain ML model** — `PYTHONPATH=. python diamond_ml_train.py` on the post-14f cohort (first with real absorption features). Must pass: **Brier < 0.05** (tightened from 0.25 per Phase B σ sweep) AND **Jaccard ≥ 0.70** across CV folds. First retrain likely near-miss — realistic activation after second retrain at N=750-1000.
- **[GATE 2: ML validated] Wire ML edge into ENTRY gate** — modify `diamond_paper.py::_execute_trade` to add `ml_edge < KELLY_MIN_EDGE_HURDLE` as pre-entry rejection. Without this, Kelly just sizes bad entries differently.
- **[GATE 3: Entry-gate wired] Flip `KELLY_SIZING_ENABLED=true`** — wire `kelly_contracts()` into `_execute_trade()` sizing path, replacing the flat tiered logic.
- **[GATE 4: Backtest re-validates]** — run `backtest_kelly.py` Phase C on post-14f data with ML-backed edge estimator. MaxDD must compress vs flat. Then flip `KELLY_SIZING_ENABLED=true`.

### Do NOT

- **[DO NOT] Touch execution thresholds.** Composite score has near-zero IC after price-controlling (Sprint 13b). Lowering ALERT threshold produces negative EV (validated Mar 29, −$5.37/day).
- **[DO NOT] Intervene on 95¢ trades or other in-steamroller-bucket trades.** The ML retrain MUST see these losses to learn they're toxic. Censoring the right tail breaks training distribution.
- **[DO NOT] Flip `CTM_ENABLED=true`** — Keep disabled until N > 1,500 settled trades. Bias-variance trap on thin stratification. Categories backfilled for data integrity and ML features, NOT for CTM activation.
- **[DO NOT] Enable Kelly without all 3 gates.** Kelly sizing bad entries is still bad. Execution-layer ML veto must come first.

### Operational wishlist (no specific gate)

- **Set up git on OCI** — replace scp-based `deploy.sh` with `git pull` workflow.
- **Hetzner migration consideration** — current 1-OCPU/956MB VM is at the edge. If ML retrain grows or trade tempo increases, migrate to Hetzner CCX23 (~$25/mo, 4GB RAM, 2 vCPU).
- **Dashboard updates** — consider adding Layer 2/3 guard firing counter to :8080 as a health card.

## Milestone Reports

End-of-sprint / phase / postmortem HTML summaries live in `~/Documents/quant/diamond/reports/YYYY_MM_DD_<slug>.html`. DIAMOND's `.gitignore` does not block HTML files, so reports are tracked by default. See `~/Documents/quant/CLAUDE.md` § Milestone Reports for the full cross-project standard (required sections, palette, triggers).

**Reports filed:**
- `2026_04_21_kelly_phase_abc.html` — Sprint 14c/14d Kelly backtest (Phase A/B/C + shrinkage)
- `2026_04_23_d1_status_update.html` — Mid-Sprint-14 status update
- `2026_04_26_ml_learning_curve.html` — Sprint 14e diagnostic, early learning curve sweep
- **`2026_05_13_diamond_strategy_autopsy.html`** — Final strategy autopsy + shutdown record

## Institutional Assets (Transferable to BERYL/CITRINE)

DIAMOND failed at the trading layer. The infrastructure built to test the hypothesis is the lasting asset. The following components are designed to carry over directly:

### Quantitative methodology

| Asset | Source | Transferability to BERYL/CITRINE |
|---|---|---|
| **Point-in-Time CV** | `src/diamond_ml.py:488` | Direct — port to BERYL's ML scoring layer if/when one is built |
| **Null-importance permutation test** | `src/diamond_ml.py` | Direct — applies to any feature-selection layer |
| **Cohort-burn discipline** | Sprints 14e/14f/14h | Conceptual — any time a regime change occurs (config change, data fix), reset the training cohort |
| **Two-Gate Stack** | Sprint 14d | Direct — IC gate AND variance gate are universal for activating Kelly sizing |
| **Pre-registered falsifiable thresholds** | N=300 Brier matrix | Cultural — commit thresholds before running the experiment |
| **Bayesian shrinkage estimator** | `src/diamond_shrinkage.py` | Reusable — Laplace smoothing with k=20+3√N pseudocount applies anywhere bucket-WR is the input |

### Execution / infrastructure

| Asset | Source | Transferability |
|---|---|---|
| **Three-layer data integrity** | Sprint 14/14b | Conceptual — code fix + app guard + schema CHECK on any DB-persisted state |
| **Two-tier kill switch** (daily soft + cumulative hard) | Sprint 14g | Direct — port the two-tier pattern to BERYL/CITRINE if not already present |
| **Task-death watchdog** | Sprint 14h `_task_death_logger` | Conceptual — BERYL/CITRINE use synchronous Python (1d bars), not asyncio, so the exact code doesn't apply. The PATTERN — silent failures must alert — does apply. |
| **CancelledError-aware exception handling** | Sprint 14h | Conceptual — any long-running asyncio loop should explicitly catch CancelledError |
| **Telemetry against absence** (`_book_absorption_stale` canary pattern) | Sprint 14e | Direct — design canaries that fire when the system is silently broken, not just when it's loudly broken |
| **PiT CV cohort cutoff epochs** | learning_curve.py `--min-opened-at` | Direct — preserves the ability to retrain on a specific data regime |

### Operator-side lessons

1. **When the comfortable explanation doesn't fit the facts, the explanation is wrong, not the facts.** (Lesson from Sprint 14g narrative-fallacy diagnosis.)
2. **Monitor for absence, not just presence.** "Is the service running?" is useless. "Is the service doing real work in the last N minutes?" is the right check.
3. **Burnout is a real failure mode.** 4 sandbox-era bugs in 9 days (14e/f/g/h) suggests the operator cadence was unsustainable. Build margin into future projects.
4. **Separate the expected value of the strategy from the expected value of the engineer.** Strategy can fail; engineer can succeed. DIAMOND was this.

## Operational Notes (Post-Retirement)

If for some reason DIAMOND needs to be revived for further research:

```bash
# Restore from archive
ssh ubuntu@129.158.40.51
cd /home/ubuntu/kalshi-diamond
cp diamond_trades.db.shutdown-2026-05-13 diamond_trades.db.research

# Re-enable services (NOT recommended unless pivoting to new strategy)
sudo systemctl enable diamond-monitor diamond-dashboard
sudo systemctl start diamond-monitor diamond-dashboard
```

The Kalshi API credentials remain in `.env` and the systemd unit files are unchanged — only the `enabled` state was flipped to `disabled`. EMERALD still reads DIAMOND's `.env` for Kalshi REST access; this remains intact.

⚠️ **Do not revive DIAMOND for live trading.** The hypothesis is empirically falsified. Any further work should be research-only (e.g., post-hoc feature analysis on the archived dataset), or should constitute a new strategy with a new repo/name.
