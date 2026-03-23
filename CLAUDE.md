# DIAMOND — Kalshi Unusual Volume Tracker

## Project Purpose
Real-time anomaly detection on Kalshi prediction markets with **10-feature detection engine**, event-aware conviction tracking, portfolio intelligence, and automated live trading. Places real orders on Kalshi when anomalies are detected with order-book-aware pricing.

Part of the gemstone-named trading sub-project family (AGATE, BERYL, CITRINE, DIAMOND). **Standalone repo** — separate from HMM-Trader. GitHub: `pjnks/kalshi-diamond` (private).

## Architecture
```
Kalshi WebSocket ──► Stream Processor ──► 10-Feature Engine ──► Alert Engine
     (trade channel)    (asyncio)         (10 detectors)       (Pushover/SQLite)
         │                                     │                      │
         └──► REST Poller ──► Market Cache      │               Dashboard (:8080)
              (order book,     (5min)           ▼                      │
               market info)              Conviction Tracker            ▼
                                         (event grouping,       Paper Trading Engine
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

### Warmup Period
The monitor has a **3-minute warmup** (`WARMUP_SEC = 180`) after startup. During warmup, trades are scored and anomalies recorded to SQLite, but alert dispatch (Pushover/macOS) and trade placement are suppressed. This prevents false positive floods during cold start when profiles are empty.

### Display Names
`_market_display_name()` in `diamond_monitor.py` builds human-readable names:
- **Regular markets:** `"LA L at Houston Winner? — Houston"` (title + yes_sub_title)
- **MVE/parlays:** `"Parlay: PSG, Real Madrid, Bodoe/Glimt"` (cleaned from `"yes X,yes Y"` format)
- Stored in both `anomalies.title` and `market_profiles.title` columns

### Live Trading Engine (`src/diamond_paper.py`)
Auto-places real Kalshi orders when anomalies reach ALERT level or above:
- **Order-book-aware pricing:** Fetches fresh order book, prices at best ask + cross margin
- **Adaptive execution tiers:** CRITICAL: ask+3¢, High ALERT: ask+2¢, Low ALERT: ask+1¢
- **Dynamic max spread:** CRITICAL: 25¢, High ALERT: 20¢, Low ALERT: 15¢
- **Min price filter:** Skips trades at or below `PAPER_MIN_PRICE_CENTS` (default 5¢)
- **Kill switch:** Stops new trades when total daily P&L drops below -$20
- **Dedup:** One position per ticker per 24 hours (was 30 min)
- **Stale order cancel:** GTC orders auto-cancelled after 1 hour
- **Settlement polling:** Checks order fills and market settlements every 2 minutes
- **Handles Kalshi `executed` status:** Orders on settled markets properly cleaned up
- **Notifications:** Pushover alerts for fills, settlements, flips, kill switch, order failures
- **Session recycling:** aiohttp session auto-recreates every 30 min; force-recycles on connection errors

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
- **Activation:** Requires 50+ settled trades (run manually or via scheduled task)

### REST Client Resilience (`src/kalshi_client.py`)
- **Session recycling:** `SESSION_RECYCLE_SEC = 1800` — recreates aiohttp session every 30 minutes to prevent stale connections
- **Force-recycle on errors:** Connection errors (`ClientConnectorError`, `ServerDisconnectedError`, `OSError`) trigger immediate session recreation before retry
- **WebSocket version compat:** Auto-detects websockets version and uses `additional_headers` (v11+) or `extra_headers` (v10)

## Key Files
| File | Purpose |
|------|---------|
| `diamond_config.py` | API keys, thresholds, feature weights, trading config |
| `src/kalshi_client.py` | WebSocket + REST API client (incl. order placement, session recycling) |
| `src/diamond_store.py` | SQLite storage + rolling aggregates + paper trade tracking |
| `src/diamond_features.py` | 10 detection features + composite score |
| `src/diamond_conviction.py` | Event-aware conviction tracking (BLOCK/FLIP) |
| `src/diamond_analytics.py` | Self-learning: feature attribution + adaptive weights |
| `src/diamond_alerts.py` | Alert engine with cooldowns |
| `src/diamond_paper.py` | **Live trading engine** — auto-bet on anomalies, min price filter |
| `diamond_monitor.py` | Main async loop (entry point) — runs everything |
| `diamond_dashboard.py` | Dash visualization at :8080 (futuristic terminal aesthetic) |
| `diamond_dashboard_lite.py` | Lightweight fallback dashboard |
| `diamond_backtest.py` | Historical replay + grid search |
| `deploy.sh` | Deploy to OCI: `./deploy.sh` (sync) or `./deploy.sh --restart` |
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
./deploy.sh --restart    # Sync + restart monitor & dashboard
```

## Deployment — OCI Compute Instance
- **Host:** `129.158.40.51` (Oracle Cloud Infrastructure)
- **SSH:** `ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51`
- **Python:** `/home/ubuntu/miniconda3/bin/python` (Python 3.13)
- **Code:** `/home/ubuntu/kalshi-diamond/`
- **Deploy:** `./deploy.sh --restart` from local Mac (uses scp, not git)
- **Processes:** Monitor + Dashboard run as nohup background processes
- **Logs:** `diamond_monitor.log`, `diamond_dashboard.log` on OCI
- **Note:** Git not yet set up on OCI. Using scp via `deploy.sh` instead.

## Tech Stack
- **Mac:** Python 3.9 (system anaconda), websockets v10.3 (`extra_headers=`)
- **OCI:** Python 3.13 (miniconda), websockets v16 (`additional_headers=`)
- **Version compat:** `kalshi_client.py` auto-detects websockets version for header kwarg
- HTTP: `aiohttp` (with 30-min session recycling)
- Storage: SQLite (`diamond_trades.db`)
- Dashboard: Dash 4.0 + Plotly 6.6 + dash-bootstrap-components + Google Fonts (Inter, JetBrains Mono)
- Notifications: Pushover + macOS native
- Source control: GitHub (`pjnks/kalshi-diamond`, private), `gh` CLI

## Conventions
- Config in `diamond_config.py` (not scattered imports)
- All `.env` secrets loaded via `python-dotenv`
- **CRITICAL:** RSA private key MUST be stored as separate `.pem` file — NEVER paste inline in `.env`. `python-dotenv` cannot parse multiline PEM and will silently corrupt the key path.
- `.env` must contain `KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem` (just the filename, NOT the key content)
- Notification pattern reused from HMM-Trader's `src/notifier.py`
- Gemstone naming: prefix files with `diamond_` for project-specific modules

## Current Status
**All 8 steps complete + live trading active on OCI.**
- Steps 1-8: Core system ✅
- Dashboard Redesign ✅ (futuristic terminal aesthetic, glassmorphism, Inter + JetBrains Mono)
- Live Trading Engine ✅ (auto-bet on ALERT+, GTC orders, kill switch, settlement tracking)
- OCI Deployment ✅ (monitor + dashboard running 24/7)
- Session Resilience ✅ (30-min recycling, force-recycle on errors, Pushover on failures)
- Min Price Filter ✅ (skip trades ≤ 5¢)
- GitHub Repo ✅ (`pjnks/kalshi-diamond`, private)

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
```

### Trading Rules
- Every ALERT+ signal (score ≥ 0.55) places a real order
- Min price filter: skips contracts ≤ 5¢ to avoid longshot bleed
- GTC limit orders with +3-8¢ slippage adjustment for fill improvement
- One position per ticker (dedup)
- Kill switch at -$20 total daily P&L (realized losses + unrealized losses on open positions)
- All positions ride to settlement (no early exits)
- Resets daily at midnight
- Pushover notification on order placement failures (silent failures no longer possible)

## Tuning Summary
**Alert distribution:** NONE ~86%, LOG ~11%, NOTABLE ~2%, ALERT ~1%.
**Feature weights (v2, tuned March 22):** skew=0.20, sweep=0.15, zscore=0.12, velocity=0.12, imbalance=0.12, spike=0.10, book_delta=0.06, impact=0.05, concentration=0.05, correlation=0.03.
**Composite scoring:** Capped weight redistribution (max 1.5×, 1.3× for <3 features).

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
- **Volume + anomaly timeline:** 5-min buckets, last 6 hours, cyan→violet gradient bars
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
- **Black screen after laptop sleep/wake:** Dashboard process stays alive but Dash stops rendering. Fix: restart with `pkill -f diamond_dashboard && PYTHONPATH=. nohup python diamond_dashboard.py &`
- **Filter button reset:** Pattern-matching callbacks can reset on refresh when buttons are recreated. Guard added but may have edge cases.
- **NaN market titles:** Some markets have NULL/NaN titles in DB. Fixed with `str()` wrapping in `_build_market_volume_heatmap()`.

## Database Schema
SQLite at `diamond_trades.db`. Key tables:
- `trades` — raw trade stream
- `anomalies` — detected anomalies with `title TEXT` column (human-readable name)
- `market_profiles` — per-market stats with `title TEXT` column
- `book_snapshots` — order book snapshots
- `paper_trades` — live trading orders (ticker, side, entry_price, status, order_id, pnl)

Schema migrations handled via `ALTER TABLE ADD COLUMN` in `DiamondStore._migrate()`.
Pruning: book snapshots at 2 days, trades/anomalies at 30 days.
DB index on `anomalies(ts)` for cross_market query performance.

### Paper Trades Table
```sql
paper_trades (
  id, ticker, title, side, action, count, entry_price, fill_count,
  anomaly_score, anomaly_level, features, client_order_id, order_id,
  status,  -- pending | filled | settled | unfilled | cancelled
  pnl, opened_at, settled_at
)
```

## Kalshi API Notes
- REST base: `https://api.elections.kalshi.com/trade-api/v2`
- WS: `wss://api.elections.kalshi.com/trade-api/ws/v2`
- Auth: RSA-PSS SHA-256 signature over `{timestamp_ms}{METHOD}{path}`
- Rate limit: 20 req/sec (Basic tier) — 429s common when paginating
- **Deprecated (March 2026):** `volume` field → use `volume_24h_fp` (string format, parse with `float()`)
- `/markets` list endpoint returns `volume_fp=0` for ALL markets — do NOT rely on it for volume filtering
- Individual markets return `status: "active"` not `"open"` — filter must check both
- WS trade format: `market_ticker` (not `ticker`), `count_fp` (string), `yes/no_price_dollars` (string)
- Orderbook format: `orderbook_fp.yes_dollars` / `no_dollars`: `[[price_str, qty_str], ...]`
- Order placement: POST `/portfolio/orders` with `type: "limit"`, `time_in_force: "good_till_canceled"`
- Order response includes `order_id` for tracking fills/cancellations

## Gotchas
- **CRITICAL `.env` gotcha:** NEVER paste the RSA private key inline in `.env`. ALWAYS store it as a separate `.pem` file and reference via `KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem`. Pasting inline causes `python-dotenv` parse errors and `FileNotFoundError` pointing at `-----BEGIN RSA PRIVATE KEY-----` as a path. This has happened multiple times — always check `.env` first when debugging auth issues.
- **websockets version:** Mac uses v10 (`extra_headers=`), OCI uses v16 (`additional_headers=`). Auto-detected in `kalshi_client.py`.
- **Stale aiohttp sessions:** Long-running processes (2+ days) can have stale HTTP sessions that silently fail on order placement. Fixed with 30-min session recycling + force-recycle on connection errors.
- Rate limit 429 common when paginating all markets — has retry + delay
- `int("0.00")` crashes — use `float()` for Kalshi string numeric fields
- Pushover emergency priority (2) notifications repeat every 60s until acknowledged in-app
- Cross-market correlation can create feedback loop (more anomalies → higher score → more anomalies) — thresholds raised to prevent this
- Kalshi sports markets are naturally directional (lopsided order books, one-sided flow) — thresholds must account for this
- **Dashboard black screen:** After laptop sleep/wake, the Dash app's React frontend stops rendering while the Python process stays alive. HTTP 200 returns but browser shows black page. Must restart the dashboard process.
- **WebSocket 401 after sleep:** After laptop wake, WebSocket reconnects with stale auth. Monitor has retry logic that re-signs on each reconnect attempt, but may need manual restart if the key/API changed.
- **IOC orders don't fill:** Kalshi markets are thin. IOC (immediate-or-cancel) orders expire immediately if no liquidity. Switched to GTC with slippage adjustment.
- **OCI SSH background commands:** `pkill -f` sometimes doesn't work cleanly over SSH — may need `kill -9` or `killall -9 python`. Log files may be owned by root from previous runs.
- **NaN market titles:** Some market titles come back as NaN (float) from DB. Always use `str()` when displaying.

## OCI Deployment Details
- **Instance:** Oracle Cloud Infrastructure Compute (Ubuntu, hostname `hmm-trader`)
- **IP:** `129.158.40.51`
- **SSH key:** `~/.ssh/hmm-trader.key` (user: `ubuntu`)
- **Python:** `/home/ubuntu/miniconda3/bin/python` (3.13, miniconda)
- **Deploy script:** `./deploy.sh` syncs `.py` files via scp; `./deploy.sh --restart` also restarts processes
- **Manual restart:** `ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51 'bash -c "cd /home/ubuntu/kalshi-diamond && nohup /home/ubuntu/miniconda3/bin/python diamond_monitor.py > diamond_monitor.log 2>&1 &"'`
- **Git:** Not set up on OCI yet. GitHub repo exists at `pjnks/kalshi-diamond` but OCI uses scp.
- **Also running:** CITRINE dashboard on :8070 (separate project)

## 24/7 Operation
- **Primary:** OCI compute instance (always-on, no lid-close issues)
- **Secondary:** Mac runs local copy for development/testing
- Monitor self-heals on wake (WebSocket reconnects, REST re-authenticates)
- Session recycling prevents stale connection failures after long uptime
- Pushover alerts on order failures ensure silent failures are caught

## Next Steps / Roadmap
- **Collect 50+ settled trades** for meaningful win rate analysis (~1 week)
- **Feature attribution analysis:** Which of the 6 detectors predict price movement best?
- **Win rate by alert level:** Do CRITICALs outperform ALERTs?
- **Category breakdown:** Sports vs politics vs crypto performance
- **Event-level dedup:** Optional — limit positions per event (e.g., max 2 March Madness tickers)
- **Burst throttle:** Optional — max N new trades per 5-min window
- **Score-scaled sizing:** Once win rates known, increase bet size for higher-conviction signals
- **Kelly criterion sizing:** After sufficient settlement data, size by estimated edge
- **Set up git on OCI:** Replace scp deploy with git pull workflow
