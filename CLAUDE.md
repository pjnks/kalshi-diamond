# DIAMOND — Kalshi Unusual Volume Tracker

## Project Purpose
Real-time anomaly detection on Kalshi prediction markets. Monitors trade sizes, volume spikes, order book imbalances, and cross-market correlations to identify potentially informed flow. **Now includes automated live trading** — places real orders on Kalshi when anomalies are detected.

Part of the gemstone-named trading sub-project family (AGATE, BERYL, CITRINE, DIAMOND). **Standalone repo** — separate from HMM-Trader.

## Architecture
```
Kalshi WebSocket ──► Stream Processor ──► Feature Engine ──► Alert Engine
     (trade channel)    (asyncio)         (6 detectors)     (Pushover/macOS/SQLite)
         │                                                        │
         └──► REST Poller ──► Market Metadata Cache               ▼
              (order book,     (refresh 5min)              Dashboard (:8080)
               market info)                                       │
                                                                  ▼
                                                         Paper Trading Engine
                                                         (auto-bet on ALERT+)
                                                              │
                                                              ▼
                                                         Kalshi REST API
                                                         (real orders via /portfolio/orders)
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
- **Order type:** GTC (good-till-canceled) limit orders with price slippage adjustment
- **Slippage:** Adjusts entry price by +8¢ above signal price to improve fill rate on thin books
- **Kill switch:** Stops new trades when total daily P&L (realized + unrealized) drops below -$20
- **Unrealized P&L:** Estimated using last trade price per ticker vs entry price
- **Dedup:** One position per ticker (won't re-enter same market)
- **Settlement polling:** Checks order fills and market settlements every 2 minutes
- **Notifications:** Pushover alerts for trade fills, settlements, and kill switch triggers

## Key Files
| File | Purpose |
|------|---------|
| `diamond_config.py` | API keys, thresholds, feature weights, trading config |
| `src/kalshi_client.py` | WebSocket + REST API client (incl. order placement) |
| `src/diamond_store.py` | SQLite storage + rolling aggregates + paper trade tracking |
| `src/diamond_features.py` | 6 detection features + composite score |
| `src/diamond_alerts.py` | Alert engine with cooldowns |
| `src/diamond_paper.py` | **Live trading engine** — auto-bet on anomalies |
| `diamond_monitor.py` | Main async loop (entry point) |
| `diamond_dashboard.py` | Dash visualization at :8080 |
| `diamond_dashboard_lite.py` | Lightweight fallback dashboard |
| `diamond_backtest.py` | Historical replay + grid search |
| `test_tuning.py` | Live 60s tuning harness |
| `test_live.py` | Live integration test |

## Running
```bash
PYTHONPATH=. python diamond_monitor.py                    # Monitor + live trading
PYTHONPATH=. python diamond_monitor.py --category politics  # Filter by category
PYTHONPATH=. python diamond_monitor.py --test              # Dry run (no alerts, no trades)
PYTHONPATH=. python diamond_dashboard.py                   # Dashboard at :8080
PYTHONPATH=. python test_tuning.py                         # 60s tuning session
PYTHONPATH=. python diamond_backtest.py replay --tickers 10 # Historical replay
PYTHONPATH=. python diamond_backtest.py gridsearch          # Grid search optimization
PYTHONPATH=. python diamond_backtest.py evaluate            # Precision evaluation
```

## Tech Stack
- Python 3.9 (system anaconda), asyncio
- WebSocket: `websockets` (v10.3 — uses `extra_headers=` not `additional_headers=`)
- HTTP: `aiohttp`
- Storage: SQLite (`diamond_trades.db`)
- Dashboard: Dash 4.0 + Plotly 6.6 + dash-bootstrap-components
- Notifications: Pushover + macOS native

## Conventions
- Config in `diamond_config.py` (not scattered imports)
- All `.env` secrets loaded via `python-dotenv`
- **CRITICAL:** RSA private key MUST be stored as separate `.pem` file — NEVER paste inline in `.env`. `python-dotenv` cannot parse multiline PEM and will silently corrupt the key path.
- `.env` must contain `KALSHI_PRIVATE_KEY_PATH=kalshi_private_key.pem` (just the filename, NOT the key content)
- Notification pattern reused from HMM-Trader's `src/notifier.py`
- Dashboard dark terminal UI pattern from HMM-Trader's `citrine_dashboard.py`
- Gemstone naming: prefix files with `diamond_` for project-specific modules

## Current Status
**All 8 steps complete + live trading active. Portfolio generating returns.**
- Steps 1-6: Scaffolding, API client, storage, features, alerts, main loop ✅
- Step 7: Dashboard ✅ (live at :8080, dark terminal UI, 30s auto-refresh)
- Step 8: Backtester ✅ (replay, grid search, precision evaluation)
- Dashboard Polish ✅ (metric cards, charts, legend alignment, EST timezone)
- Live Production Hardening ✅ (sensitivity tuning, market discovery fix, display names)
- **Live Trading Engine ✅** (auto-bet on ALERT+, GTC orders, kill switch, settlement tracking)
- **Portfolio: ~$108.90 from $100 deposit (as of March 19, 2026)** — early but positive

## Trading Configuration (`.env`)
```
PAPER_TRADING_ENABLED=true          # Enable/disable auto-trading
PAPER_MIN_ALERT_LEVEL=ALERT        # Minimum level to trigger trade (ALERT or CRITICAL)
PAPER_CONTRACTS_PER_TRADE=1        # Contracts per order
PAPER_MAX_POSITIONS=100            # Max simultaneous open positions
PAPER_MAX_UNREALIZED_CENTS=2000    # $20 kill switch (realized + unrealized P&L)
PAPER_POLL_INTERVAL_SEC=120        # Settlement check interval (2 min)
PAPER_NOTIFY_TRADES=true           # Pushover notifications for trades
```

### Trading Rules
- Every ALERT+ signal (score ≥ 0.65) places a real order — no price filtering
- GTC limit orders with +8¢ slippage adjustment for fill improvement
- One position per ticker (dedup)
- Kill switch at -$20 total daily P&L (realized losses + unrealized losses on open positions)
- All positions ride to settlement (no early exits)
- Resets daily at midnight

## Tuning Summary
**Alert distribution:** NONE ~86%, LOG ~11%, NOTABLE ~2%, ALERT ~1%.
**Feature weights:** zscore=0.30, spike=0.20, imbalance=0.20, skew=0.15, impact=0.10, correlation=0.05.
**Composite scoring:** Capped weight redistribution (max 1.5×, 1.3× for <3 features).

### Current Alert Thresholds
```
LOG      = 0.25   # SQLite only
NOTABLE  = 0.45   # macOS notification
ALERT    = 0.65   # Pushover push + triggers live trade
CRITICAL = 0.78   # Emergency Pushover (lowered from 0.85 to get 1-3/day)
```

### Current Feature Thresholds (tightened from original)
- `order_book_imbalance`: min depth **200** (was 100), threshold **0.7** (was 0.5)
- `taker_side_skew`: min trades **50** (was 30), threshold **0.8** (was 0.6)
- `cross_market_correlation`: min tickers **15** (was 5), required anomalies per ticker **3** (was 2)

### Cooldowns (level-aware)
CRITICAL=30s, ALERT=2.5min, NOTABLE=5min, with escalation bypass.

## Dashboard Features (`diamond_dashboard.py`)
- **6 metric cards:** Trades, Markets, Anom Rate, Logged, Alert, Critical
- **Volume + anomaly timeline:** 5-min buckets, last 6 hours, stacked by level
- **Feature radar chart:** Breakdown of most recent high-severity anomaly
- **Top markets bar chart:** 1h volume leaders (shows human-readable titles)
- **Feature weights & alert thresholds** reference panel
- **Live anomaly feed table:**
  - Sortable, color-coded by level (ALERT/CRITICAL highlighted, NOTABLE dimmed)
  - **Level filter buttons** (All, Critical, Alert+, Notable+, Log) with counts
  - Filter persists across 30s auto-refresh via `dcc.Store`
  - **Tooltip on hover** for truncated market names (shows full name)
  - 200 row limit, 20 per page
  - Shows human-readable market titles with ticker fallback
- Auto-refreshes every 30 seconds

### Known Dashboard Issues
- **Black screen after laptop sleep/wake:** Dashboard process stays alive but Dash stops rendering. Fix: restart with `pkill -f diamond_dashboard && PYTHONPATH=. nohup python diamond_dashboard.py &`
- **Filter button reset:** Pattern-matching callbacks can reset on refresh when buttons are recreated. Guard added but may have edge cases. Consider moving filter bar outside `body-container` or using `dcc.RadioItems`.

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
- `websockets` v10.3: uses `extra_headers=` not `additional_headers=`
- Rate limit 429 common when paginating all markets — has retry + delay
- `int("0.00")` crashes — use `float()` for Kalshi string numeric fields
- Pushover emergency priority (2) notifications repeat every 60s until acknowledged in-app
- Cross-market correlation can create feedback loop (more anomalies → higher score → more anomalies) — thresholds raised to prevent this
- Kalshi sports markets are naturally directional (lopsided order books, one-sided flow) — thresholds must account for this
- **Dashboard black screen:** After laptop sleep/wake, the Dash app's React frontend stops rendering while the Python process stays alive. HTTP 200 returns but browser shows black page. Must restart the dashboard process.
- **WebSocket 401 after sleep:** After laptop wake, WebSocket reconnects with stale auth. Monitor has retry logic that re-signs on each reconnect attempt, but may need manual restart if the key/API changed.
- **IOC orders don't fill:** Kalshi markets are thin. IOC (immediate-or-cancel) orders expire immediately if no liquidity. Switched to GTC with slippage adjustment.

## Mobile Monitoring Options
- **Pushover:** Already sends ALERT/CRITICAL events + trade fills to phone in real-time
- **ngrok:** `brew install ngrok && ngrok http 8080` — exposes dashboard to public URL for phone browser access (free tier = random URL each restart, $8/mo for stable subdomain)
- **Tailscale:** Private VPN mesh between Mac and phone — access `http://your-mac:8080` securely, free for personal use
- **Cloud deploy:** Heroku NOT recommended (ephemeral filesystem kills SQLite). Consider Fly.io or Render with persistent volumes.

## 24/7 Operation
Running on a laptop is not ideal — closing the lid kills the network connection.
- **`caffeinate -s`** keeps Mac awake on AC power
- **`pmset -c sleep 0`** disables sleep when plugged in
- **Cloud VPS** ($5-6/mo DigitalOcean/Hetzner) is the proper 24/7 solution
- Monitor self-heals on wake (WebSocket reconnects, REST re-authenticates)

## Next Steps / Roadmap
- **Collect 50+ settled trades** for meaningful win rate analysis (~1 week)
- **Feature attribution analysis:** Which of the 6 detectors predict price movement best?
- **Win rate by alert level:** Do CRITICALs outperform ALERTs?
- **Category breakdown:** Sports vs politics vs crypto performance
- **Event-level dedup:** Optional — limit positions per event (e.g., max 2 March Madness tickers)
- **Burst throttle:** Optional — max N new trades per 5-min window
- **Score-scaled sizing:** Once win rates known, increase bet size for higher-conviction signals
- **Kelly criterion sizing:** After sufficient settlement data, size by estimated edge

## Implementation Plan
See `/Users/perryjenkins/.claude/plans/staged-soaring-blum.md` for full 8-step architectural plan.
