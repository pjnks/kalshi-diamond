"""
diamond_config.py
─────────────────
Configuration for DIAMOND — Kalshi Unusual Volume Tracker.

API keys loaded from .env file. Thresholds and weights tunable here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── Kalshi API ────────────────────────────────────────────────────────────
KALSHI_API_KEY = os.getenv("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY_PATH = Path(__file__).parent / os.getenv("KALSHI_PRIVATE_KEY_PATH", "kalshi_private_key.pem")
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_REST_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# ── Notifications ─────────────────────────────────────────────────────────
PUSHOVER_USER_KEY = os.getenv("PUSHOVER_USER_KEY", "")
PUSHOVER_APP_TOKEN = os.getenv("PUSHOVER_APP_TOKEN", "")

# ── Alert Thresholds (composite score 0-1) ────────────────────────────────
# Raised to reduce false positives from Kalshi's naturally directional
# sports markets. Need 3+ strong features to reach ALERT.
ALERT_THRESHOLD_LOG = 0.25      # SQLite only
ALERT_THRESHOLD_NOTABLE = 0.45  # macOS notification
ALERT_THRESHOLD_ALERT = 0.55    # Pushover push + triggers trade
ALERT_THRESHOLD_CRITICAL = 0.78 # Emergency Pushover (repeat)

# ── Feature Weights (sum to 1.0) ──────────────────────────────────────────
# 10 features: original 6 + advanced 4 (sweep, velocity, concentration, book delta)
# Weights tuned based on firing rate analysis (March 22):
#   - zscore/vol_spike fire on 94-99% of ALERTs — not differentiating, weight reduced
#   - skew/velocity/sweep are selective and high-conviction — weight increased
FEATURE_WEIGHTS = {
    "trade_size_zscore": 0.12,     # Reduced: fires 94% — baseline, not differentiating
    "volume_spike_ratio": 0.10,    # Reduced: fires 99% — baseline, not differentiating
    "order_book_imbalance": 0.12,  # Kept: selective (9.6%), very strong when fires (0.91)
    "taker_side_skew": 0.20,       # Increased: fires 70%, high signal (0.79)
    "price_impact": 0.05,          # Reduced: rarely fires (1.6%), weak signal
    "cross_market_correlation": 0.03,  # Kept: rare but meaningful
    "sweep_score": 0.15,           # Increased: selective (16%), strong (0.73)
    "trade_velocity": 0.12,        # Increased: selective (28%), extremely strong (0.98)
    "size_concentration": 0.05,    # Kept: fires weak (0.28), needs more data
    "book_pressure_delta": 0.06,   # Increased: needs to contribute once fixed
}

# Per-feature enable/disable toggles
FEATURE_ENABLED = {name: True for name in FEATURE_WEIGHTS}

# ── Rolling Windows ──────────────────────────────────────────────────────
ROLLING_WINDOW_1H = 3600
ROLLING_WINDOW_4H = 14400
ROLLING_WINDOW_24H = 86400

# ── Rate Limiting ─────────────────────────────────────────────────────────
REST_POLL_INTERVAL_SEC = 30     # Order book polling
METADATA_REFRESH_SEC = 300      # Market metadata refresh (5 min)
ALERT_COOLDOWN_SEC = 300        # Per-market alert cooldown (5 min)

# ── Storage ───────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "diamond_trades.db"
PRUNE_DAYS = 30                 # Auto-prune trades older than this

# ── Dashboard ─────────────────────────────────────────────────────────────
DASHBOARD_PORT = 8080

# ── Paper Trading (Auto-Bet on Anomalies) ────────────────────────────────
PAPER_TRADING_ENABLED = os.getenv("PAPER_TRADING_ENABLED", "false").lower() == "true"
PAPER_MIN_ALERT_LEVEL = os.getenv("PAPER_MIN_ALERT_LEVEL", "ALERT")
PAPER_CONTRACTS_PER_TRADE = int(os.getenv("PAPER_CONTRACTS_PER_TRADE", "1"))
PAPER_MAX_POSITIONS = int(os.getenv("PAPER_MAX_POSITIONS", "100"))  # High to allow all signals
PAPER_MAX_UNREALIZED_CENTS = int(os.getenv("PAPER_MAX_UNREALIZED_CENTS", "2000"))  # $20 cap on open position costs
PAPER_POLL_INTERVAL_SEC = int(os.getenv("PAPER_POLL_INTERVAL_SEC", "120"))
PAPER_NOTIFY_TRADES = os.getenv("PAPER_NOTIFY_TRADES", "true").lower() == "true"
PAPER_MIN_PRICE_CENTS = int(os.getenv("PAPER_MIN_PRICE_CENTS", "5"))  # Skip trades ≤ this price
PAPER_STALE_ORDER_SEC = int(os.getenv("PAPER_STALE_ORDER_SEC", "3600"))  # Cancel GTC orders older than 1 hour

# ── Portfolio Intelligence ──────────────────────────────────────────
PAPER_MAX_PER_CATEGORY = int(os.getenv("PAPER_MAX_PER_CATEGORY", "15"))
PAPER_MAX_PER_EVENT = int(os.getenv("PAPER_MAX_PER_EVENT", "1"))
PAPER_MAX_TRADES_PER_5MIN = int(os.getenv("PAPER_MAX_TRADES_PER_5MIN", "8"))

# ── Conviction System (Event-Aware Trade Management) ───────────────
CONVICTION_ENABLED = os.getenv("CONVICTION_ENABLED", "false").lower() == "true"
CONVICTION_HALF_LIFE_SEC = float(os.getenv("CONVICTION_HALF_LIFE_SEC", "420"))  # 7 minutes
CONVICTION_FLIP_THRESHOLD = float(os.getenv("CONVICTION_FLIP_THRESHOLD", "0.4"))
