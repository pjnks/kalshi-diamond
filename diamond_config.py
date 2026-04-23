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
ALERT_THRESHOLD_ALERT = 0.55    # Pushover push + triggers trade (reverted from 0.50 — lower threshold bled -$5.37/day)
ALERT_THRESHOLD_CRITICAL = 0.78 # Emergency Pushover (repeat)

# ── Two-Stage Feature Pipeline (PhD review, March 2025) ─────────────────
# Stage 1 — Trigger features: boolean gates that must BOTH fire (score > 0)
# for scoring to activate. These fire on 94-99% of ALERTs and have zero
# discriminative power within the ALERT set — they are necessary conditions,
# not predictors. Keeping them in the weighted model wastes 22% of weight
# on near-constants that suppress truly discriminative features.
TRIGGER_FEATURES = ("trade_size_zscore", "volume_spike_ratio")

# Stage 2 — Scorer weights: 8 discriminative features (sum to 1.0)
# Only evaluated when BOTH trigger features fire.
# Renormalized from original 10-feature weights (÷ 0.78 = sum of these 8).
SCORER_WEIGHTS = {
    "order_book_imbalance": 0.154,      # selective (9.6%), very strong when fires (0.91)
    "taker_side_skew": 0.256,           # fires 70%, high signal (0.79)
    "price_impact": 0.064,              # rarely fires (1.6%), weak signal
    "cross_market_correlation": 0.038,  # rare but meaningful
    "sweep_score": 0.192,               # selective (16%), strong (0.73)
    "trade_velocity": 0.154,            # selective (28%), extremely strong (0.98)
    "size_concentration": 0.064,        # fires weak (0.28), needs more data
    "book_pressure_delta": 0.077,       # needs to contribute once fixed
}

# Legacy alias — all 10 feature names (triggers at weight 0 + scorers).
# Kept for backward compatibility with diamond_backtest.py and diamond_analytics.py.
FEATURE_WEIGHTS = {f: 0.0 for f in TRIGGER_FEATURES}
FEATURE_WEIGHTS.update(SCORER_WEIGHTS)

# Per-feature enable/disable toggles
FEATURE_ENABLED = {name: True for name in FEATURE_WEIGHTS}
# Orthogonal features (Sprint 13): ML-only, not in composite score weights.
# Structurally price-independent by construction (acceleration + relative flow).
FEATURE_ENABLED["flow_acceleration"] = True
FEATURE_ENABLED["event_relative_flow"] = True

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
PRUNE_DAYS = 2                  # Auto-prune trades/anomalies older than this (was 7→2; feature engine only uses 24h, DB was bloating to 400MB+ causing OOM kills)

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
PAPER_STALE_ORDER_SEC = int(os.getenv("PAPER_STALE_ORDER_SEC", "3600"))  # Cancel GTC orders older than 1 hour (legacy fallback)
PAPER_CANCEL_SEC_ALERT = int(os.getenv("PAPER_CANCEL_SEC_ALERT", "15"))       # Cancel unfilled ALERT orders after 15s
PAPER_CANCEL_SEC_CRITICAL = int(os.getenv("PAPER_CANCEL_SEC_CRITICAL", "30")) # Cancel unfilled CRITICAL orders after 30s

# ── Portfolio Intelligence ──────────────────────────────────────────
PAPER_MAX_PER_CATEGORY = int(os.getenv("PAPER_MAX_PER_CATEGORY", "15"))
PAPER_MAX_PER_EVENT = int(os.getenv("PAPER_MAX_PER_EVENT", "1"))
PAPER_MAX_TRADES_PER_5MIN = int(os.getenv("PAPER_MAX_TRADES_PER_5MIN", "8"))

# ── Conviction System (Event-Aware Trade Management) ───────────────
CONVICTION_ENABLED = os.getenv("CONVICTION_ENABLED", "false").lower() == "true"
CONVICTION_HALF_LIFE_SEC = float(os.getenv("CONVICTION_HALF_LIFE_SEC", "420"))  # 7 minutes
CONVICTION_FLIP_THRESHOLD = float(os.getenv("CONVICTION_FLIP_THRESHOLD", "0.4"))

# ── ML Scorer (Tiered: Lasso → GradientBoosting) ──────────────────
ML_SCORER_ENABLED = os.getenv("ML_SCORER_ENABLED", "false").lower() == "true"   # Log ML scores alongside hand-tuned
ML_SCORER_ACTIVE = os.getenv("ML_SCORER_ACTIVE", "false").lower() == "true"     # Use ML score for trade decisions
ML_MODEL_PATH = Path(__file__).parent / "diamond_ml_model.pkl"
ML_MIN_SAMPLES = int(os.getenv("ML_MIN_SAMPLES", "100"))

# ── Dynamic Threshold Manager (Category + Time-of-Day) ────────────
# Adjusts ALERT entry threshold based on rolling category and hour performance.
# Score stays clean — adjustments go into the threshold, not the score.
CTM_ENABLED = os.getenv("CTM_ENABLED", "false").lower() == "true"
CTM_BASE_THRESHOLD = ALERT_THRESHOLD_ALERT                        # 0.55 — start from static config
CTM_WINDOW_N = int(os.getenv("CTM_WINDOW_N", "100"))              # Rolling window per bucket (trade count)
CTM_MIN_N = int(os.getenv("CTM_MIN_N", "30"))                     # Cold-start floor: use base below this
CTM_BASELINE_WIN_RATE = 0.487                                      # From live data (374 trades, 48.7% WR)
CTM_SENSITIVITY = float(os.getenv("CTM_SENSITIVITY", "0.20"))     # 10pp edge → 0.02 threshold shift
CTM_MAX_CAT_ADJ = float(os.getenv("CTM_MAX_CAT_ADJ", "0.08"))    # Max ±0.08 from category
CTM_MAX_HOUR_ADJ = float(os.getenv("CTM_MAX_HOUR_ADJ", "0.06"))  # Max ±0.06 from time-of-day
CTM_THRESHOLD_FLOOR = float(os.getenv("CTM_THRESHOLD_FLOOR", "0.40"))
CTM_THRESHOLD_CEIL = float(os.getenv("CTM_THRESHOLD_CEIL", "0.72"))  # Below CRITICAL (0.78)
CTM_REFRESH_INTERVAL_SEC = int(os.getenv("CTM_REFRESH_INTERVAL_SEC", "300"))  # 5 min DB reload

# ── Kelly Sizing (Sprint 14c, DORMANT) ────────────────────────────
# Edge-proportional position sizing using Half-Kelly formula.
# DORMANT until ML shadow model passes validation gates.
# See src/diamond_kelly.py for full activation runbook.
# DO NOT flip on without: (a) N >= 500 post-Sprint-11, (b) validated ML model,
# (c) backtest comparison vs flat tiered sizing, (d) ML edge wired into ENTRY gate.
KELLY_SIZING_ENABLED = os.getenv("KELLY_SIZING_ENABLED", "false").lower() == "true"
KELLY_SAFETY_FRACTION = float(os.getenv("KELLY_SAFETY_FRACTION", "0.5"))   # Half-Kelly default
KELLY_MIN_EDGE_HURDLE = float(os.getenv("KELLY_MIN_EDGE_HURDLE", "0.02"))  # Slippage-adjusted floor
KELLY_MAX_PER_TRADE = float(os.getenv("KELLY_MAX_PER_TRADE", "0.05"))      # 5% bankroll cap

# ML promotion gate — tightened from Brier<0.25 → <0.05 after Phase B backtest
# (2026-04-21). Phase B established σ_max=0.05 for the zero-drawdown regime;
# Brier ≈ σ² so Brier<0.05 maps to σ<0.22, preserving <5% max drawdown.
# Brier=0.25 corresponds to constant-0.5 prediction (literally random noise).
KELLY_BRIER_PROMOTION_THRESHOLD = float(os.getenv("KELLY_BRIER_PROMOTION_THRESHOLD", "0.05"))
KELLY_JACCARD_PROMOTION_THRESHOLD = float(os.getenv("KELLY_JACCARD_PROMOTION_THRESHOLD", "0.70"))
