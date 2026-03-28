"""
diamond_dashboard.py
────────────────────
DIAMOND — Live Unusual Volume Dashboard.

Standalone Dash app that auto-refreshes every 30 seconds.
Reads from diamond_trades.db (trades, anomalies, market_profiles, book_snapshots).

Run:
  PYTHONPATH=. python diamond_dashboard.py   # opens at http://127.0.0.1:8080

Port 8080 to avoid collision with AGATE (:8060), CITRINE (:8070), backtest (:8050).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import dash
from dash import html, dcc, dash_table
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, State

from diamond_config import (
    ALERT_THRESHOLD_LOG,
    ALERT_THRESHOLD_NOTABLE,
    ALERT_THRESHOLD_ALERT,
    ALERT_THRESHOLD_CRITICAL,
    DASHBOARD_PORT,
    FEATURE_WEIGHTS,
    PAPER_TRADING_ENABLED,
    PAPER_MAX_UNREALIZED_CENTS,
)

ROOT = Path(__file__).parent

# ── Colour palette (futuristic terminal aesthetic) ────────────────────────────
BG       = "#080b16"
PANEL    = "rgba(16, 22, 36, 0.75)"
PANEL_SOLID = "#101624"  # For Plotly (can't use rgba)
BORDER   = "rgba(0, 240, 255, 0.08)"
BORDER_SOLID = "#0e1a2a"  # For Plotly gridlines
TEXT     = "#e0e4f0"
TEXT_DIM = "#6b7394"
GREEN    = "#00e676"
RED      = "#ff1744"
YELLOW   = "#EAB308"
BLUE     = "#448aff"
PURPLE   = "#8B5CF6"
ORANGE   = "#F59E0B"
CYAN     = "#00F0FF"

LEVEL_COLORS = {
    "LOG": TEXT_DIM,
    "NOTABLE": PURPLE,
    "ALERT": ORANGE,
    "CRITICAL": RED,
}

# Font families
FONT_BODY = "'Inter', 'Segoe UI', sans-serif"
FONT_MONO = "'JetBrains Mono', 'Fira Code', monospace"

DB_PATH = ROOT / "diamond_trades.db"
EST = timezone(timedelta(hours=-5))


# ── Data helpers ──────────────────────────────────────────────────────────────


def _get_conn():
    """Open a read-only connection to the DIAMOND database."""
    if not DB_PATH.exists():
        return None
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _load_recent_anomalies(limit: int = 500, level_filter: str | list = "ALL") -> pd.DataFrame:
    """Load recent anomalies with parsed feature scores.

    Args:
        limit: Max rows to return.
        level_filter: "ALL", a list of levels like ["ALERT","CRITICAL"],
                      legacy "NOTABLE+"/"ALERT+" strings, or a single level name.
    """
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        level_clause = ""
        # Multi-select: list of levels
        if isinstance(level_filter, list) and level_filter:
            safe = [l for l in level_filter if l in ("LOG", "NOTABLE", "ALERT", "CRITICAL")]
            if safe and len(safe) < 4:
                quoted = ",".join(f"'{l}'" for l in safe)
                level_clause = f"WHERE alert_level IN ({quoted})"
        # Legacy string filters
        elif level_filter == "NOTABLE+":
            level_clause = "WHERE alert_level IN ('NOTABLE','ALERT','CRITICAL')"
        elif level_filter == "ALERT+":
            level_clause = "WHERE alert_level IN ('ALERT','CRITICAL')"
        elif level_filter in ("LOG", "NOTABLE", "ALERT", "CRITICAL"):
            level_clause = f"WHERE alert_level = '{level_filter}'"

        df = pd.read_sql_query(
            f"SELECT * FROM anomalies {level_clause} ORDER BY ts DESC LIMIT ?",
            conn, params=(limit,),
        )
        if not df.empty and "features" in df.columns:
            feat_dicts = df["features"].apply(json.loads)
            feat_df = pd.json_normalize(feat_dicts)
            # Drop columns that already exist in df to avoid duplicates
            overlap = set(feat_df.columns) & set(df.columns)
            feat_df = feat_df.drop(columns=overlap, errors="ignore")
            df = pd.concat([df.drop(columns=["features"]), feat_df], axis=1)
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def _load_db_stats() -> dict:
    """Get database statistics."""
    conn = _get_conn()
    if conn is None:
        return {"trades": 0, "anomalies": 0, "market_profiles": 0, "book_snapshots": 0}
    try:
        stats = {}
        for table in ["trades", "anomalies", "market_profiles", "book_snapshots"]:
            row = conn.execute(f"SELECT COUNT(*) as n FROM {table}").fetchone()
            stats[table] = row["n"] if row else 0
        return stats
    except Exception:
        return {"trades": 0, "anomalies": 0, "market_profiles": 0, "book_snapshots": 0}
    finally:
        conn.close()


def _load_alert_counts(hours_back: int = 24) -> dict:
    """Count anomalies by alert level in the last N hours."""
    conn = _get_conn()
    if conn is None:
        return {}
    try:
        cutoff = time.time() - hours_back * 3600
        rows = conn.execute(
            "SELECT alert_level, COUNT(*) as cnt FROM anomalies "
            "WHERE ts >= ? GROUP BY alert_level",
            (cutoff,),
        ).fetchall()
        return {r["alert_level"]: r["cnt"] for r in rows}
    except Exception:
        return {}
    finally:
        conn.close()


def _load_top_markets(limit: int = 15) -> pd.DataFrame:
    """Load most active markets by recent trade count."""
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        cutoff = time.time() - 3600  # Last hour
        df = pd.read_sql_query(
            "SELECT t.ticker, mp.title, COUNT(*) as trades_1h, SUM(t.count) as volume_1h "
            "FROM trades t "
            "LEFT JOIN market_profiles mp ON t.ticker = mp.ticker "
            "WHERE t.ts >= ? "
            "GROUP BY t.ticker ORDER BY volume_1h DESC LIMIT ?",
            conn, params=(cutoff, limit),
        )
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def _load_volume_timeline(hours_back: int = 6) -> pd.DataFrame:
    """Load hourly volume buckets across all markets."""
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        cutoff = time.time() - hours_back * 3600
        df = pd.read_sql_query(
            "SELECT "
            "  CAST(ts / 300 AS INTEGER) * 300 as bucket_ts, "
            "  COUNT(*) as trade_count, "
            "  SUM(count) as total_volume, "
            "  COUNT(DISTINCT ticker) as active_tickers "
            "FROM trades WHERE ts >= ? "
            "GROUP BY bucket_ts ORDER BY bucket_ts",
            conn, params=(cutoff,),
        )
        if not df.empty:
            df["time"] = pd.to_datetime(df["bucket_ts"], unit="s", utc=True).dt.tz_convert("US/Eastern")
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def _load_anomaly_timeline(hours_back: int = 6) -> pd.DataFrame:
    """Load anomaly counts per 5-min bucket."""
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        cutoff = time.time() - hours_back * 3600
        df = pd.read_sql_query(
            "SELECT "
            "  CAST(ts / 300 AS INTEGER) * 300 as bucket_ts, "
            "  alert_level, "
            "  COUNT(*) as cnt "
            "FROM anomalies WHERE ts >= ? "
            "GROUP BY bucket_ts, alert_level ORDER BY bucket_ts",
            conn, params=(cutoff,),
        )
        if not df.empty:
            df["time"] = pd.to_datetime(df["bucket_ts"], unit="s", utc=True).dt.tz_convert("US/Eastern")
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def _load_market_profiles() -> pd.DataFrame:
    """Load all market profiles."""
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        return pd.read_sql_query(
            "SELECT * FROM market_profiles ORDER BY volume_24h DESC", conn
        )
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def _load_paper_stats() -> dict:
    """Load paper trading aggregate stats including activity metrics."""
    conn = _get_conn()
    if conn is None:
        return {}
    try:
        # Check if table exists
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "paper_trades" not in tables:
            return {"total": 0, "filled": 0, "settled": 0, "wins": 0,
                    "win_rate": 0, "total_pnl_cents": 0, "open_positions": 0,
                    "daily_spend_cents": 0, "unrealized_pnl_cents": 0,
                    "total_daily_pnl_cents": 0, "trades_1h": 0, "trades_24h": 0,
                    "avg_pnl_cents": 0, "best_trade_cents": 0, "worst_trade_cents": 0,
                    "total_deployed_cents": 0, "return_pct": 0.0,
                    "losses": 0, "best_market": "—", "worst_market": "—"}

        total = conn.execute("SELECT COUNT(*) as n FROM paper_trades").fetchone()["n"]
        if total == 0:
            return {"total": 0, "filled": 0, "settled": 0, "wins": 0,
                    "win_rate": 0, "total_pnl_cents": 0, "open_positions": 0,
                    "daily_spend_cents": 0, "unrealized_pnl_cents": 0,
                    "total_daily_pnl_cents": 0, "trades_1h": 0, "trades_24h": 0,
                    "avg_pnl_cents": 0, "best_trade_cents": 0, "worst_trade_cents": 0,
                    "total_deployed_cents": 0, "return_pct": 0.0,
                    "losses": 0, "best_market": "—", "worst_market": "—"}

        # Get core stats from store (includes unrealized P&L)
        from src.diamond_store import DiamondStore
        store = DiamondStore()
        store.connect()
        stats = store.get_paper_stats()
        store.close()

        # ── Activity metrics ──────────────────────────────────────
        now = time.time()
        trades_1h = conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE opened_at >= ?",
            (now - 3600,)).fetchone()["n"]
        trades_24h = conn.execute(
            "SELECT COUNT(*) as n FROM paper_trades WHERE opened_at >= ?",
            (now - 86400,)).fetchone()["n"]
        stats["trades_1h"] = trades_1h
        stats["trades_24h"] = trades_24h

        # ── P&L breakdown ─────────────────────────────────────────
        settled = stats.get("settled", 0)
        if settled > 0:
            avg_pnl = conn.execute(
                "SELECT AVG(pnl_cents) as avg FROM paper_trades WHERE status='settled'"
            ).fetchone()["avg"] or 0
            best = conn.execute(
                "SELECT MAX(pnl_cents) as mx FROM paper_trades WHERE status='settled'"
            ).fetchone()["mx"] or 0
            worst = conn.execute(
                "SELECT MIN(pnl_cents) as mn FROM paper_trades WHERE status='settled'"
            ).fetchone()["mn"] or 0
            losses = conn.execute(
                "SELECT COUNT(*) as n FROM paper_trades WHERE status='settled' AND pnl_cents <= 0"
            ).fetchone()["n"]

            # Best/worst market names
            best_row = conn.execute(
                "SELECT title, ticker FROM paper_trades WHERE status='settled' ORDER BY pnl_cents DESC LIMIT 1"
            ).fetchone()
            worst_row = conn.execute(
                "SELECT title, ticker FROM paper_trades WHERE status='settled' ORDER BY pnl_cents ASC LIMIT 1"
            ).fetchone()
            stats["avg_pnl_cents"] = avg_pnl
            stats["best_trade_cents"] = best
            stats["worst_trade_cents"] = worst
            stats["losses"] = losses
            stats["best_market"] = (best_row["title"] or best_row["ticker"])[:30] if best_row else "—"
            stats["worst_market"] = (worst_row["title"] or worst_row["ticker"])[:30] if worst_row else "—"
        else:
            stats.update({"avg_pnl_cents": 0, "best_trade_cents": 0,
                          "worst_trade_cents": 0, "losses": 0,
                          "best_market": "—", "worst_market": "—"})

        # ── Total capital deployed (sum of entry_price * fill_count for all filled/settled) ──
        deployed = conn.execute(
            "SELECT COALESCE(SUM(fill_price * fill_count), 0) as d FROM paper_trades "
            "WHERE status IN ('filled', 'settled') AND fill_price > 0"
        ).fetchone()["d"]
        stats["total_deployed_cents"] = int(deployed)
        total_pnl = stats.get("total_pnl_cents", 0) + stats.get("unrealized_pnl_cents", 0)
        stats["return_pct"] = (total_pnl / deployed * 100) if deployed > 0 else 0.0

        return stats
    except Exception:
        return {}
    finally:
        conn.close()


def _load_paper_trades(limit: int = 100) -> pd.DataFrame:
    """Load paper trade history."""
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "paper_trades" not in tables:
            return pd.DataFrame()
        return pd.read_sql_query(
            "SELECT * FROM paper_trades ORDER BY opened_at DESC LIMIT ?",
            conn, params=(limit,))
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def _load_paper_pnl_timeline() -> pd.DataFrame:
    """Load cumulative P&L over time for settled paper trades."""
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "paper_trades" not in tables:
            return pd.DataFrame()
        df = pd.read_sql_query(
            "SELECT settled_at, pnl_cents FROM paper_trades "
            "WHERE status='settled' ORDER BY settled_at", conn)
        if not df.empty:
            df["time"] = pd.to_datetime(df["settled_at"], unit="s", utc=True).dt.tz_convert("US/Eastern")
            df["cumulative_pnl"] = df["pnl_cents"].cumsum()
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def _build_pnl_chart(pnl_df: pd.DataFrame) -> go.Figure:
    """Build cumulative P&L line chart."""
    fig = go.Figure()
    if pnl_df.empty:
        fig.add_annotation(
            text="No settled trades yet",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=14, color=TEXT_DIM),
        )
    else:
        color = GREEN if pnl_df["cumulative_pnl"].iloc[-1] >= 0 else RED
        fig.add_trace(go.Scatter(
            x=pnl_df["time"], y=pnl_df["cumulative_pnl"],
            mode="lines+markers", line=dict(color=color, width=2),
            marker=dict(size=5, color=[GREEN if p > 0 else RED for p in pnl_df["pnl_cents"]]),
            hovertemplate="P&L: %{y:+.0f}¢<br>%{x}<extra></extra>",
        ))
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="JetBrains Mono, monospace", color=TEXT, size=10),
        margin=dict(l=50, r=10, t=10, b=30),
        height=200,
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)", title=dict(text="Cumulative P&L (¢)", font=dict(size=9))),
    )
    return fig


def _paper_trades_table(paper_df: pd.DataFrame) -> html.Div:
    """Build paper trades table."""
    if paper_df.empty:
        return html.Div(
            "No paper trades yet — enable PAPER_TRADING_ENABLED in .env",
            style={"color": TEXT_DIM, "textAlign": "center", "padding": "20px"})

    display = paper_df.head(100).copy()
    display["time"] = (
        pd.to_datetime(display["opened_at"], unit="s", utc=True)
        .dt.tz_convert("US/Eastern").dt.strftime("%m/%d %H:%M"))
    display["market"] = display.apply(
        lambda r: (r["title"] if r.get("title") else r["ticker"])[:40], axis=1)
    display["price"] = display["fill_price"].apply(
        lambda x: f"{x}¢" if pd.notna(x) and x > 0 else "—")
    display["pnl"] = display["pnl_cents"].apply(
        lambda x: f"{x:+.0f}¢" if pd.notna(x) else "—")
    display["score_fmt"] = display["anomaly_score"].apply(
        lambda x: f"{x:.2f}" if pd.notna(x) else "—")

    cols = ["time", "market", "side", "price", "status", "settlement", "pnl", "score_fmt"]
    col_names = {"time": "Time", "market": "Market", "side": "Side", "price": "Entry",
                 "status": "Status", "settlement": "Result", "pnl": "P&L", "score_fmt": "Score"}

    return dash_table.DataTable(
        data=display[cols].to_dict("records"),
        columns=[{"name": col_names.get(c, c), "id": c} for c in cols],
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": PANEL_SOLID, "color": TEXT_DIM,
            "fontWeight": "600", "fontSize": "0.7rem", "fontFamily": FONT_BODY,
            "textTransform": "uppercase", "border": f"1px solid {BORDER_SOLID}",
            "letterSpacing": "0.05em",
        },
        style_cell={
            "backgroundColor": BG, "color": TEXT,
            "fontFamily": FONT_MONO, "fontSize": "0.8rem",
            "border": f"1px solid {BORDER_SOLID}", "padding": "6px 10px", "textAlign": "left",
        },
        style_data_conditional=[
            {"if": {"filter_query": '{status} = "settled" && {pnl} contains "+"'},
             "color": GREEN, "fontWeight": "600"},
            {"if": {"filter_query": '{status} = "settled" && {pnl} contains "-"'},
             "color": RED, "fontWeight": "600"},
            {"if": {"filter_query": '{status} = "filled"'},
             "color": YELLOW},
            {"if": {"filter_query": '{status} = "unfilled"'},
             "color": TEXT_DIM},
        ],
        page_size=10,
        sort_action="native",
        style_as_list_view=True,
    )


def _load_skipped_trades(limit: int = 200) -> pd.DataFrame:
    """Load skipped trade history."""
    conn = _get_conn()
    if conn is None:
        return pd.DataFrame()
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "skipped_trades" not in tables:
            return pd.DataFrame()
        return pd.read_sql_query(
            "SELECT * FROM skipped_trades ORDER BY ts DESC LIMIT ?",
            conn, params=(limit,))
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


SKIP_REASON_COLORS = {
    "conviction_block": "#8b5cf6",  # violet
    "dedup": "#64748b",  # slate
    "event_limit": "#f59e0b",  # amber
    "category_limit": "#f59e0b",
    "min_price": "#6b7280",  # gray
    "burst_throttle": "#ef4444",  # red
    "kill_switch": "#ef4444",
    "max_positions": "#f59e0b",
}


def _skipped_trades_table(skipped_df: pd.DataFrame) -> html.Div:
    """Build skipped trades table."""
    if skipped_df.empty:
        return html.Div(
            "No skipped trades recorded yet",
            style={"color": TEXT_DIM, "textAlign": "center", "padding": "20px"})

    display = skipped_df.head(200).copy()
    display["time"] = (
        pd.to_datetime(display["ts"], unit="s", utc=True)
        .dt.tz_convert("US/Eastern").dt.strftime("%m/%d %H:%M"))
    display["market"] = display.apply(
        lambda r: (str(r["title"]) if r.get("title") and str(r.get("title")) != "nan" else r["ticker"])[:40], axis=1)
    display["price_fmt"] = display["price_cents"].apply(
        lambda x: f"{int(x)}¢" if pd.notna(x) and x > 0 else "—")
    display["score_fmt"] = display["anomaly_score"].apply(
        lambda x: f"{x:.2f}" if pd.notna(x) else "—")
    display["reason_fmt"] = display["skip_reason"].apply(
        lambda x: str(x).replace("_", " ").title())

    cols = ["time", "market", "side", "price_fmt", "anomaly_level", "score_fmt", "reason_fmt", "detail"]
    col_names = {"time": "Time", "market": "Market", "side": "Side", "price_fmt": "Price",
                 "anomaly_level": "Level", "score_fmt": "Score", "reason_fmt": "Skip Reason", "detail": "Detail"}

    return dash_table.DataTable(
        data=display[cols].to_dict("records"),
        columns=[{"name": col_names.get(c, c), "id": c} for c in cols],
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": PANEL_SOLID, "color": TEXT_DIM,
            "fontWeight": "600", "fontSize": "0.7rem", "fontFamily": FONT_BODY,
            "textTransform": "uppercase", "border": f"1px solid {BORDER_SOLID}",
            "letterSpacing": "0.05em",
        },
        style_cell={
            "backgroundColor": BG, "color": TEXT,
            "fontFamily": FONT_MONO, "fontSize": "0.8rem",
            "border": f"1px solid {BORDER_SOLID}", "padding": "6px 10px", "textAlign": "left",
        },
        style_data_conditional=[
            {"if": {"filter_query": '{reason_fmt} = "Conviction Block"'},
             "color": "#8b5cf6"},
            {"if": {"filter_query": '{reason_fmt} = "Kill Switch"'},
             "color": RED, "fontWeight": "600"},
            {"if": {"filter_query": '{reason_fmt} = "Burst Throttle"'},
             "color": RED},
            {"if": {"filter_query": '{reason_fmt} = "Event Limit"'},
             "color": YELLOW},
            {"if": {"filter_query": '{reason_fmt} = "Dedup"'},
             "color": TEXT_DIM},
        ],
        page_size=10,
        sort_action="native",
        style_as_list_view=True,
    )


# ── Chart builders ────────────────────────────────────────────────────────────


def _build_volume_timeline(vol_df: pd.DataFrame, anom_df: pd.DataFrame) -> go.Figure:
    """Build combined volume + anomaly timeline."""
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.08,
        subplot_titles=["Trade Volume (5-min)", "Anomalies"],
    )

    if vol_df.empty:
        fig.add_annotation(
            text="No trade data yet — start the monitor",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=16, color=TEXT_DIM),
        )
    else:
        # Volume bars with cyan-to-violet gradient
        n_bars = len(vol_df)
        bar_colors = [f"rgba({int(0 + (139-0)*i/max(n_bars-1,1))},{int(240 + (92-240)*i/max(n_bars-1,1))},{int(255 + (246-255)*i/max(n_bars-1,1))},0.75)" for i in range(n_bars)]
        fig.add_trace(go.Bar(
            x=vol_df["time"], y=vol_df["total_volume"],
            marker_color=bar_colors,
            name="Volume",
            hovertemplate="%{x}<br>Volume: %{y:,.0f}<extra></extra>",
        ), row=1, col=1)

        # Active tickers line
        fig.add_trace(go.Scatter(
            x=vol_df["time"], y=vol_df["active_tickers"],
            mode="lines", line=dict(color=CYAN, width=1),
            name="Active Tickers",
            yaxis="y3",
        ), row=1, col=1)

    # Anomaly bars stacked by level
    if not anom_df.empty:
        for level in ["LOG", "NOTABLE", "ALERT", "CRITICAL"]:
            level_data = anom_df[anom_df["alert_level"] == level]
            if not level_data.empty:
                fig.add_trace(go.Bar(
                    x=level_data["time"], y=level_data["cnt"],
                    marker_color=LEVEL_COLORS.get(level, TEXT_DIM),
                    name=level, opacity=0.85,
                    hovertemplate=f"{level}: " + "%{y}<extra></extra>",
                ), row=2, col=1)

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="JetBrains Mono, monospace", color=TEXT, size=11),
        margin=dict(l=50, r=10, t=60, b=10),
        height=400,
        barmode="stack",
        showlegend=True,
        legend=dict(orientation="h", y=1.22, x=0, font=dict(size=9)),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)", title=None),
        yaxis2=dict(gridcolor="rgba(255,255,255,0.05)", title=None),
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
        xaxis2=dict(gridcolor="rgba(255,255,255,0.05)"),
    )
    # Update subplot title colors
    for annotation in fig.layout.annotations:
        annotation.font = dict(size=11, color=TEXT_DIM)

    return fig


def _build_feature_radar(anomaly_row: pd.Series | None) -> go.Figure:
    """Build radar chart of 6 feature scores for a single anomaly."""
    feature_names = list(FEATURE_WEIGHTS.keys())
    display_names = [
        "Trade Size", "Vol Spike", "Book Imbal",
        "Taker Skew", "Price Impact", "Cross Mkt",
    ]

    fig = go.Figure()

    if anomaly_row is not None:
        values = [anomaly_row.get(f, 0) for f in feature_names]
        values.append(values[0])  # Close the polygon
        display = display_names + [display_names[0]]

        level = anomaly_row.get("alert_level", "LOG")
        color = LEVEL_COLORS.get(level, BLUE)

        fig.add_trace(go.Scatterpolar(
            r=values,
            theta=display,
            fill="toself",
            fillcolor=f"rgba(0,240,255,0.10)",
            line=dict(color=CYAN, width=2.5),
            marker=dict(size=4, color=CYAN),
            name=f"{anomaly_row.get('ticker', '?')}",
        ))
    else:
        fig.add_annotation(
            text="Select an anomaly to see feature breakdown",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=12, color=TEXT_DIM),
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, sans-serif", color=TEXT_DIM, size=10),
        margin=dict(l=40, r=40, t=20, b=30),
        height=320,
        polar=dict(
            bgcolor="rgba(0,0,0,0)",
            radialaxis=dict(
                visible=True, range=[0, 1],
                gridcolor="rgba(255,255,255,0.06)", linecolor="rgba(255,255,255,0.06)",
                tickfont=dict(size=8, color=TEXT_DIM),
            ),
            angularaxis=dict(
                gridcolor="rgba(255,255,255,0.06)",
                linecolor="rgba(255,255,255,0.06)",
            ),
        ),
        showlegend=False,
    )
    return fig


def _build_market_volume_heatmap(markets_df: pd.DataFrame) -> go.Figure:
    """Build horizontal bar chart of top markets by volume."""
    fig = go.Figure()

    if markets_df.empty:
        fig.add_annotation(
            text="No market data",
            xref="paper", yref="paper", x=0.5, y=0.5,
            showarrow=False, font=dict(size=14, color=TEXT_DIM),
        )
    else:
        # Use title if available, fall back to ticker
        display_tickers = []
        for _, row in markets_df.iterrows():
            name = str(row.get("title") or row.get("ticker") or "")
            display_tickers.append(name[:45] + "..." if len(name) > 45 else name)

        # Gradient colors from cyan to violet
        n_bars = len(display_tickers)
        bar_colors = [f"rgba({int(0 + (139-0)*i/max(n_bars-1,1))},{int(240 + (92-240)*i/max(n_bars-1,1))},{int(255 + (246-255)*i/max(n_bars-1,1))},0.8)" for i in range(n_bars)]
        fig.add_trace(go.Bar(
            y=display_tickers[::-1],
            x=markets_df["volume_1h"][::-1],
            orientation="h",
            marker_color=bar_colors[::-1],
            hovertemplate="%{y}<br>Volume: %{x:,.0f}<extra></extra>",
        ))

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Inter, sans-serif", color=TEXT, size=9),
        margin=dict(l=180, r=10, t=10, b=10),
        height=280,
        xaxis=dict(gridcolor="rgba(255,255,255,0.05)", title=None),
        yaxis=dict(gridcolor="rgba(255,255,255,0.05)", title=None, tickfont=dict(size=8),
                   automargin=True),
    )
    return fig


# ── UI Components ─────────────────────────────────────────────────────────────


def _metric_card(title: str, value: str, color: str = TEXT) -> dbc.Card:
    # Build glow shadow matching the accent color
    try:
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        glow = f"0 0 20px rgba({r},{g},{b},0.25)"
    except (ValueError, IndexError):
        glow = "none"
    return dbc.Card(
        dbc.CardBody([
            html.P(title, className="card-title",
                   style={"fontSize": "0.65rem", "color": TEXT_DIM,
                          "marginBottom": "6px", "textTransform": "uppercase",
                          "letterSpacing": "0.08em", "whiteSpace": "nowrap",
                          "overflow": "hidden", "textOverflow": "ellipsis",
                          "fontFamily": FONT_BODY}),
            html.H4(value, style={"color": color, "fontFamily": FONT_MONO,
                                   "fontWeight": "700", "marginBottom": "0",
                                   "whiteSpace": "nowrap", "fontSize": "2rem",
                                   "textShadow": glow}),
        ], style={"padding": "12px 14px"}),
        className="glass-card",
        style={"border": "none"},
    )


def _alert_level_badge(level: str) -> html.Span:
    """Colored badge for alert level."""
    color = LEVEL_COLORS.get(level, TEXT_DIM)
    return html.Span(
        level,
        style={
            "backgroundColor": f"rgba({','.join(str(int(color.lstrip('#')[i:i+2], 16)) for i in (0,2,4))},0.2)",
            "color": color,
            "padding": "2px 8px",
            "borderRadius": "4px",
            "fontSize": "0.75rem",
            "fontWeight": "700",
            "fontFamily": "monospace",
        },
    )


def _anomaly_table(anom_df: pd.DataFrame) -> html.Div:
    """Build the live anomaly feed table."""
    if anom_df.empty:
        return html.Div(
            "No anomalies detected yet",
            style={"color": TEXT_DIM, "textAlign": "center", "padding": "40px"},
        )

    # Prepare display data
    display_df = anom_df.head(200).copy()
    display_df["time"] = (
        pd.to_datetime(display_df["ts"], unit="s", utc=True)
        .dt.tz_convert("US/Eastern")
        .dt.strftime("%H:%M:%S ET")
    )

    # Use title if available, fall back to ticker — keep full name for tooltips
    if "title" in display_df.columns:
        display_df["full_market"] = display_df.apply(
            lambda r: r["title"] if r.get("title") else r["ticker"], axis=1,
        )
        display_df["market"] = display_df["full_market"].apply(
            lambda m: m[:50] + "..." if len(m or "") > 50 else m
        )
    else:
        display_df["full_market"] = display_df["ticker"]
        display_df["market"] = display_df["ticker"].apply(
            lambda t: t[:45] + "..." if len(t) > 45 else t
        )

    cols = ["time", "market", "alert_level", "score"]
    for feat in ["trade_size_zscore", "volume_spike_ratio", "order_book_imbalance",
                 "taker_side_skew", "price_impact", "cross_market_correlation"]:
        if feat in display_df.columns:
            display_df[feat] = display_df[feat].apply(
                lambda x: f"{x:.2f}" if pd.notna(x) and isinstance(x, (int, float)) else "—"
            )
            cols.append(feat)

    display_df["score"] = display_df["score"].apply(lambda x: f"{x:.3f}")

    col_names = {
        "time": "Time (ET)", "market": "Market", "alert_level": "Level",
        "score": "Score", "trade_size_zscore": "Size",
        "volume_spike_ratio": "Spike", "order_book_imbalance": "Book",
        "taker_side_skew": "Skew", "price_impact": "Impact",
        "cross_market_correlation": "XMkt",
    }

    # Build tooltip data — show full market name on hover
    tooltip_data = [
        {"market": {"value": row.get("full_market", row.get("market", "")), "type": "text"}}
        for _, row in display_df.head(200).iterrows()
    ]

    return dash_table.DataTable(
        data=display_df[cols].to_dict("records"),
        columns=[{"name": col_names.get(c, c), "id": c} for c in cols],
        tooltip_data=tooltip_data,
        tooltip_delay=0,
        tooltip_duration=None,
        css=[{
            "selector": ".dash-table-tooltip",
            "rule": f"background-color: {PANEL_SOLID}; color: {TEXT}; font-family: {FONT_MONO};"
                    f" font-size: 0.8rem; border: 1px solid rgba(0,240,255,0.15); border-radius: 10px;"
                    f" padding: 8px 12px; max-width: 500px; width: auto;"
                    f" box-shadow: 0 4px 16px rgba(0,0,0,0.6), 0 0 8px rgba(0,240,255,0.08);"
                    f" backdrop-filter: blur(12px);",
        }],
        style_table={"overflowX": "auto"},
        style_header={
            "backgroundColor": PANEL_SOLID, "color": TEXT_DIM,
            "fontWeight": "600", "fontSize": "0.7rem", "fontFamily": FONT_BODY,
            "textTransform": "uppercase", "letterSpacing": "0.05em",
            "border": f"1px solid {BORDER_SOLID}",
        },
        style_cell={
            "backgroundColor": BG, "color": TEXT,
            "fontFamily": FONT_MONO, "fontSize": "0.8rem",
            "border": f"1px solid {BORDER_SOLID}",
            "padding": "6px 10px", "textAlign": "left",
            "minWidth": "50px", "maxWidth": "250px",
        },
        style_data_conditional=[
            {"if": {"filter_query": '{alert_level} = "CRITICAL"'},
             "color": RED, "fontWeight": "700",
             "backgroundColor": "rgba(255,23,68,0.10)",
             "borderLeft": f"3px solid {RED}"},
            {"if": {"filter_query": '{alert_level} = "ALERT"'},
             "color": ORANGE, "fontWeight": "700",
             "backgroundColor": "rgba(245,158,11,0.08)",
             "borderLeft": f"3px solid {ORANGE}"},
            {"if": {"filter_query": '{alert_level} = "NOTABLE"'},
             "color": PURPLE, "fontWeight": "400"},
            {"if": {"filter_query": '{alert_level} = "LOG"'},
             "color": TEXT_DIM, "fontWeight": "400"},
        ],
        page_size=20,
        sort_action="native",
        style_as_list_view=True,
    )


def _threshold_indicator() -> html.Div:
    """Visual indicator of current threshold settings."""
    levels = [
        ("LOG", ALERT_THRESHOLD_LOG, TEXT_DIM),
        ("NOTABLE", ALERT_THRESHOLD_NOTABLE, YELLOW),
        ("ALERT", ALERT_THRESHOLD_ALERT, ORANGE),
        ("CRITICAL", ALERT_THRESHOLD_CRITICAL, RED),
    ]
    items = []
    for name, val, color in levels:
        items.append(html.Div([
            html.Span(f"{name}", style={"color": color, "fontWeight": "600",
                                         "width": "70px", "display": "inline-block",
                                         "fontFamily": FONT_MONO}),
            html.Span(f"≥ {val:.2f}", style={"color": TEXT_DIM, "fontFamily": FONT_MONO}),
        ], style={"fontSize": "0.75rem", "marginBottom": "4px"})
        )
    return html.Div(items, style={"padding": "6px 0"})


# ── App ───────────────────────────────────────────────────────────────────────

app = dash.Dash(
    __name__,
    external_stylesheets=[
        dbc.themes.CYBORG,
        "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700;800&display=swap",
    ],
    title="DIAMOND — Unusual Volume Tracker",
)

# Inject custom CSS for glassmorphism, hover effects, animated border
app.index_string = '''<!DOCTYPE html>
<html>
<head>
{%metas%}
<title>{%title%}</title>
{%favicon%}
{%css%}
<style>
  body {
    background: linear-gradient(135deg, #080b16 0%, #0d1117 50%, #0a0e1a 100%) !important;
    font-family: 'Inter', 'Segoe UI', sans-serif !important;
  }
  .glass-card {
    background: rgba(16, 22, 36, 0.75) !important;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(0, 240, 255, 0.08);
    border-radius: 14px;
    transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
  }
  .glass-card:hover {
    transform: translateY(-2px);
    border-color: rgba(0, 240, 255, 0.2);
    box-shadow: 0 8px 24px rgba(0, 240, 255, 0.06);
  }
  .glass-panel {
    background: rgba(16, 22, 36, 0.75) !important;
    backdrop-filter: blur(12px);
    -webkit-backdrop-filter: blur(12px);
    border: 1px solid rgba(0, 240, 255, 0.08);
    border-radius: 14px;
  }
  @keyframes gradient-shift {
    0% { background-position: 0% 50%; }
    50% { background-position: 100% 50%; }
    100% { background-position: 0% 50%; }
  }
  .header-border {
    height: 2px;
    background: linear-gradient(90deg, #00F0FF, #8B5CF6, #00F0FF, #8B5CF6);
    background-size: 300% 100%;
    animation: gradient-shift 4s ease infinite;
  }
  .glow-text-cyan {
    text-shadow: 0 0 20px rgba(0, 240, 255, 0.4), 0 0 40px rgba(0, 240, 255, 0.15);
  }
  .neon-red { box-shadow: inset 3px 0 0 #ff1744, 0 0 8px rgba(255, 23, 68, 0.15); }
  .neon-amber { box-shadow: inset 3px 0 0 #F59E0B, 0 0 6px rgba(245, 158, 11, 0.10); }
  /* Dash table overrides */
  .dash-table-container .dash-spreadsheet-container {
    border-radius: 10px !important;
    overflow: hidden;
  }
</style>
</head>
<body>
{%app_entry%}
<footer>
{%config%}
{%scripts%}
{%renderer%}
</footer>
</body>
</html>'''

app.layout = html.Div([
    dcc.Interval(id="refresh-interval", interval=30_000, n_intervals=0),
    dcc.Store(id="level-filter-store", data="ALL"),
    html.Div(id="header-container"),
    html.Div(id="body-container",
             style={"padding": "20px", "minHeight": "100vh"}),
], style={"fontFamily": FONT_BODY})


@app.callback(
    [Output("header-container", "children"),
     Output("body-container", "children")],
    [Input("refresh-interval", "n_intervals")],
    [State("level-filter-store", "data")],
)
def update_dashboard(_n, current_filter):
    """Rebuild entire dashboard on each interval tick."""
    current_filter = current_filter or "ALL"
    now_str = datetime.now(EST).strftime("%Y-%m-%d %H:%M:%S EST")

    # Load data
    stats = _load_db_stats()
    alert_counts = _load_alert_counts(hours_back=24)
    anom_df = _load_recent_anomalies(limit=500, level_filter=current_filter)
    top_markets = _load_top_markets(limit=12)
    vol_timeline = _load_volume_timeline(hours_back=6)
    anom_timeline = _load_anomaly_timeline(hours_back=6)
    profiles_df = _load_market_profiles()

    # Compute metrics
    total_trades = stats.get("trades", 0)
    total_anomalies = stats.get("anomalies", 0)
    active_markets = stats.get("market_profiles", 0)
    notable_24h = alert_counts.get("NOTABLE", 0)
    alert_24h = alert_counts.get("ALERT", 0)
    critical_24h = alert_counts.get("CRITICAL", 0)
    log_24h = alert_counts.get("LOG", 0)

    anomaly_rate = (
        f"{total_anomalies * 100 / total_trades:.1f}%"
        if total_trades > 0 else "—"
    )

    # ── Header ─────────────────────────────────────────────────────────────
    header = html.Div([
        html.Div(className="header-border"),
        html.Div([
            html.Div([
                html.H3("DIAMOND", className="glow-text-cyan", style={
                    "color": CYAN, "fontWeight": "800", "marginBottom": "0",
                    "letterSpacing": "0.15em", "fontFamily": FONT_MONO,
                }),
                html.P("Kalshi Unusual Volume Tracker", style={
                    "color": TEXT_DIM, "fontSize": "0.85rem", "marginTop": "2px",
                    "fontFamily": FONT_BODY,
                }),
            ], style={"flex": "1"}),
            html.Div([
                html.Span(f"Last refresh: {now_str}", style={
                    "color": TEXT_DIM, "fontSize": "0.75rem", "fontFamily": FONT_MONO,
                }),
            ], style={"textAlign": "right"}),
        ], style={
            "display": "flex", "justifyContent": "space-between",
            "alignItems": "center", "padding": "16px 20px",
        }),
    ], className="glass-panel", style={
        "borderRadius": "0", "borderTop": "none",
    })

    # ── Metric Cards ───────────────────────────────────────────────────────
    alert_color = ORANGE if alert_24h > 0 else TEXT_DIM
    crit_color = RED if critical_24h > 0 else TEXT_DIM

    metrics_row = dbc.Row([
        dbc.Col(_metric_card("Trades", f"{total_trades:,}"), width=2),
        dbc.Col(_metric_card("Markets", str(active_markets)), width=2),
        dbc.Col(_metric_card("Anom Rate", anomaly_rate), width=2),
        dbc.Col(_metric_card("Logged", str(log_24h), TEXT_DIM), width=2),
        dbc.Col(_metric_card("Alert", str(alert_24h), alert_color), width=2),
        dbc.Col(_metric_card("Critical", str(critical_24h), crit_color), width=2),
    ], className="g-2 mb-3")

    # ── Charts Row ─────────────────────────────────────────────────────────
    vol_chart = _build_volume_timeline(vol_timeline, anom_timeline)

    # Radar: show the most recent notable+ anomaly
    radar_row_data = None
    if not anom_df.empty:
        high_anom_df = anom_df[anom_df["alert_level"].isin(["NOTABLE", "ALERT", "CRITICAL"])]
        if not high_anom_df.empty:
            radar_row_data = high_anom_df.iloc[0]
        else:
            radar_row_data = anom_df.iloc[0]
    radar_chart = _build_feature_radar(radar_row_data)

    radar_title = "Feature Breakdown"
    if radar_row_data is not None:
        ticker_short = radar_row_data.get("ticker", "")[:25]
        level = radar_row_data.get("alert_level", "")
        radar_title = f"{ticker_short} [{level}]"

    charts_row = dbc.Row([
        dbc.Col([
            html.Div(
                dcc.Graph(figure=vol_chart, config={"displayModeBar": False}),
                className="glass-panel", style={"padding": "8px"},
            ),
        ], width=8),
        dbc.Col([
            html.Div([
                html.P(radar_title, style={
                    "fontSize": "0.75rem", "color": TEXT_DIM, "marginBottom": "4px",
                    "textTransform": "uppercase", "letterSpacing": "0.05em",
                    "padding": "4px 8px",
                }),
                dcc.Graph(figure=radar_chart, config={"displayModeBar": False}),
            ], className="glass-panel"),
        ], width=4),
    ], className="g-2 mb-3")

    # ── Middle Row: Market Volume + Weight Config ──────────────────────────
    market_chart = _build_market_volume_heatmap(top_markets)

    weight_items = []
    for feat, weight in FEATURE_WEIGHTS.items():
        short = {
            "trade_size_zscore": "Trade Size",
            "volume_spike_ratio": "Vol Spike",
            "order_book_imbalance": "Book Imbal",
            "taker_side_skew": "Taker Skew",
            "price_impact": "Price Impact",
            "cross_market_correlation": "Cross Mkt",
        }.get(feat, feat)
        pct = int(weight * 100)
        bar_width = pct * 2
        weight_items.append(html.Div([
            html.Div([
                html.Span(short, style={"color": TEXT, "fontSize": "0.75rem", "width": "90px",
                                         "display": "inline-block", "fontFamily": FONT_BODY}),
                html.Span(f"{pct}%", style={"color": TEXT_DIM, "fontSize": "0.75rem",
                                             "width": "30px", "display": "inline-block",
                                             "textAlign": "right", "fontFamily": FONT_MONO}),
            ], style={"display": "flex", "justifyContent": "space-between",
                      "marginBottom": "2px"}),
            html.Div(
                html.Div(style={
                    "width": f"{bar_width}px", "height": "4px",
                    "backgroundColor": CYAN, "borderRadius": "2px",
                }),
                style={"backgroundColor": BORDER, "borderRadius": "2px", "height": "4px"},
            ),
        ], style={"marginBottom": "8px"}))

    middle_row = dbc.Row([
        dbc.Col([
            html.Div([
                html.P("TOP MARKETS (1H VOLUME)", style={
                    "fontSize": "0.7rem", "color": TEXT_DIM, "marginBottom": "4px",
                    "textTransform": "uppercase", "letterSpacing": "0.08em", "fontFamily": FONT_BODY,
                    "padding": "8px 8px 0",
                }),
                dcc.Graph(figure=market_chart, config={"displayModeBar": False}),
            ], className="glass-panel"),
        ], width=8),
        dbc.Col([
            html.Div([
                html.P("FEATURE WEIGHTS", style={
                    "fontSize": "0.7rem", "color": TEXT_DIM, "marginBottom": "12px",
                    "textTransform": "uppercase", "letterSpacing": "0.08em",
                }),
                html.Div(weight_items),
                html.Hr(style={"borderColor": BORDER, "margin": "12px 0"}),
                html.P("ALERT THRESHOLDS", style={
                    "fontSize": "0.7rem", "color": TEXT_DIM, "marginBottom": "8px",
                    "textTransform": "uppercase", "letterSpacing": "0.08em",
                }),
                _threshold_indicator(),
            ], className="glass-panel", style={"padding": "12px"}),
        ], width=4),
    ], className="g-2 mb-3")

    # ── Anomaly Feed Table ─────────────────────────────────────────────────
    # current_filter is "ALL" or a list like ["ALERT", "CRITICAL"]
    active_levels = set()
    is_all = current_filter == "ALL" or not current_filter
    if isinstance(current_filter, list):
        active_levels = set(current_filter)
    elif current_filter == "ALL":
        active_levels = {"LOG", "NOTABLE", "ALERT", "CRITICAL"}

    def _filter_btn(label, value, color, is_active=False):
        try:
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            bg = f"rgba({r},{g},{b},{'0.20' if is_active else '0.06'})"
            shadow = f"0 0 10px rgba({r},{g},{b},0.25)" if is_active else "none"
        except (ValueError, IndexError):
            bg = "rgba(100,100,100,0.1)"
            shadow = "none"
        return html.Button(label, id={"type": "level-filter", "value": value}, n_clicks=0,
            style={
                "backgroundColor": bg, "color": color,
                "border": f"1px solid {'rgba(' + str(r) + ',' + str(g) + ',' + str(b) + ',0.5)' if is_active else BORDER_SOLID}",
                "borderRadius": "8px", "padding": "5px 14px", "marginRight": "6px",
                "fontSize": "0.7rem", "fontWeight": "600", "fontFamily": FONT_MONO,
                "cursor": "pointer", "textTransform": "uppercase",
                "boxShadow": shadow, "backdropFilter": "blur(8px)",
                "transition": "all 0.2s ease",
            })

    filter_bar = html.Div([
        _filter_btn("All", "ALL", CYAN, is_active=is_all),
        _filter_btn(f"Critical ({critical_24h})", "CRITICAL", RED, is_active=("CRITICAL" in active_levels and not is_all)),
        _filter_btn(f"Alert ({alert_24h})", "ALERT", ORANGE, is_active=("ALERT" in active_levels and not is_all)),
        _filter_btn(f"Notable ({notable_24h})", "NOTABLE", YELLOW, is_active=("NOTABLE" in active_levels and not is_all)),
        _filter_btn(f"Log ({log_24h})", "LOG", TEXT_DIM, is_active=("LOG" in active_levels and not is_all)),
    ], style={"marginBottom": "10px", "display": "flex", "alignItems": "center"})

    table_section = html.Div([
        html.Div([
            html.P("LIVE ANOMALY FEED", style={
                "fontSize": "0.7rem", "color": TEXT_DIM, "marginBottom": "0",
                "textTransform": "uppercase", "letterSpacing": "0.08em",
            }),
            html.P(f"{stats.get('anomalies', 0)} total anomalies", style={
                "fontSize": "0.65rem", "color": TEXT_DIM, "marginBottom": "8px",
            }),
        ]),
        filter_bar,
        html.Div(id="anomaly-table-container", children=_anomaly_table(anom_df)),
    ], className="glass-panel", style={"padding": "12px"})

    # ── Portfolio / Paper Trading Section (ALWAYS visible) ────────────────
    paper_stats = _load_paper_stats()
    has_trades = paper_stats and paper_stats.get("total", 0) > 0

    # Status indicator
    paper_enabled = PAPER_TRADING_ENABLED
    if paper_enabled and has_trades:
        status_dot = html.Span("\u25cf", style={"color": GREEN, "marginRight": "6px"})
        status_text = "LIVE"
    elif paper_enabled:
        status_dot = html.Span("\u25cf", style={"color": YELLOW, "marginRight": "6px"})
        status_text = "AWAITING SIGNALS"
    else:
        status_dot = html.Span("\u25cf", style={"color": TEXT_DIM, "marginRight": "6px"})
        status_text = "DISABLED"

    # Core numbers
    total_trades = paper_stats.get("total", 0) if paper_stats else 0
    open_pos = paper_stats.get("open_positions", 0) if paper_stats else 0
    settled = paper_stats.get("settled", 0) if paper_stats else 0
    pnl_cents = paper_stats.get("total_pnl_cents", 0) if paper_stats else 0
    unrealized = paper_stats.get("unrealized_pnl_cents", 0) if paper_stats else 0
    total_pnl = pnl_cents + unrealized
    return_pct = paper_stats.get("return_pct", 0.0) if paper_stats else 0.0
    wr = paper_stats.get("win_rate", 0) if paper_stats else 0
    trades_1h = paper_stats.get("trades_1h", 0) if paper_stats else 0
    trades_24h = paper_stats.get("trades_24h", 0) if paper_stats else 0
    avg_pnl = paper_stats.get("avg_pnl_cents", 0) if paper_stats else 0
    best_trade = paper_stats.get("best_trade_cents", 0) if paper_stats else 0
    worst_trade = paper_stats.get("worst_trade_cents", 0) if paper_stats else 0

    pnl_color = GREEN if total_pnl >= 0 else RED
    return_color = GREEN if return_pct >= 0 else RED
    wr_color = GREEN if wr >= 0.5 else RED if settled > 0 else TEXT_DIM

    # Row 1: Key portfolio metrics
    portfolio_row1 = dbc.Row([
        dbc.Col(_metric_card("Status", status_text,
                             GREEN if status_text == "LIVE" else YELLOW if status_text == "AWAITING SIGNALS" else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Total P&L",
                             f"{total_pnl:+.0f}¢" if has_trades else "—", pnl_color if has_trades else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Return %",
                             f"{return_pct:+.1f}%" if has_trades else "—", return_color if has_trades else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Open Positions", str(open_pos), YELLOW if open_pos > 0 else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Win Rate",
                             f"{wr:.0%}" if settled > 0 else "—", wr_color), width=2),
        dbc.Col(_metric_card("Settled / Total",
                             f"{settled}/{total_trades}" if has_trades else "0/0"), width=2),
    ], className="g-2 mb-2")

    # Row 2: Activity + P&L details
    portfolio_row2 = dbc.Row([
        dbc.Col(_metric_card("Trades (1h)", str(trades_1h), CYAN if trades_1h > 0 else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Trades (24h)", str(trades_24h), CYAN if trades_24h > 0 else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Realized P&L", f"{pnl_cents:+.0f}¢" if settled > 0 else "—",
                             GREEN if pnl_cents > 0 else RED if pnl_cents < 0 else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Unrealized P&L", f"{unrealized:+.0f}¢" if open_pos > 0 else "—",
                             GREEN if unrealized > 0 else RED if unrealized < 0 else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Best Trade", f"{best_trade:+.0f}¢" if settled > 0 else "—",
                             GREEN if best_trade > 0 else TEXT_DIM), width=2),
        dbc.Col(_metric_card("Worst Trade", f"{worst_trade:+.0f}¢" if settled > 0 else "—",
                             RED if worst_trade < 0 else TEXT_DIM), width=2),
    ], className="g-2 mb-3")

    # P&L chart + trades table (only if trades exist)
    paper_details = html.Div()
    if has_trades:
        pnl_timeline = _load_paper_pnl_timeline()
        pnl_chart = _build_pnl_chart(pnl_timeline)
        paper_trades_df = _load_paper_trades(limit=100)

        skipped_df = _load_skipped_trades(limit=200)
        skip_count = len(skipped_df)

        paper_details = html.Div([
            html.Div([
                dcc.Graph(figure=pnl_chart, config={"displayModeBar": False}),
            ], className="glass-panel", style={"padding": "8px", "marginBottom": "12px"}),
            html.Div([
                _paper_trades_table(paper_trades_df),
            ], className="glass-panel", style={"padding": "12px", "marginBottom": "12px"}),
            html.Div([
                html.Div([
                    html.P("SKIPPED TRADES", style={
                        "fontSize": "0.7rem", "color": "#8b5cf6", "marginBottom": "0",
                        "textTransform": "uppercase", "letterSpacing": "0.08em",
                    }),
                    html.P(f"{skip_count} signals filtered out by risk gates", style={
                        "fontSize": "0.65rem", "color": TEXT_DIM, "marginBottom": "8px",
                    }),
                ]),
                _skipped_trades_table(skipped_df),
            ], className="glass-panel", style={"padding": "12px"}),
        ])

    daily_pnl = paper_stats.get("total_daily_pnl_cents", 0) if paper_stats else 0
    kill_pct = min(abs(daily_pnl) / PAPER_MAX_UNREALIZED_CENTS * 100, 100) if PAPER_MAX_UNREALIZED_CENTS > 0 else 0
    kill_color = GREEN if daily_pnl >= 0 else (YELLOW if kill_pct < 50 else (ORANGE if kill_pct < 80 else RED))

    paper_section = html.Div([
        html.Div([
            html.Div([
                status_dot,
                html.Span("PORTFOLIO STATUS", style={
                    "fontSize": "0.7rem", "color": CYAN, "marginBottom": "0",
                    "textTransform": "uppercase", "letterSpacing": "0.08em",
                }),
            ], style={"display": "flex", "alignItems": "center"}),
            html.Div([
                html.Span(f"Daily P&L: {daily_pnl:+d}¢ / -{PAPER_MAX_UNREALIZED_CENTS}¢ kill-switch",
                          style={"fontSize": "0.65rem", "color": kill_color, "marginRight": "12px"}),
                html.Span(f"Avg P&L/trade: {avg_pnl:+.0f}¢" if settled > 0 else "",
                          style={"fontSize": "0.65rem", "color": TEXT_DIM}),
            ], style={"marginTop": "2px"}),
        ], style={"marginBottom": "8px"}),
        portfolio_row1,
        portfolio_row2,
        paper_details,
    ], style={"marginBottom": "16px"})

    body = html.Div([
        metrics_row,
        charts_row,
        middle_row,
        paper_section,
        table_section,
    ])

    return header, body


@app.callback(
    [Output("anomaly-table-container", "children", allow_duplicate=True),
     Output("level-filter-store", "data")],
    [Input({"type": "level-filter", "value": dash.ALL}, "n_clicks")],
    [State("level-filter-store", "data")],
    prevent_initial_call=True,
)
def filter_anomaly_table(n_clicks_list, current_filter):
    """Filter anomaly table — multi-select toggle for individual levels."""
    # When update_dashboard rebuilds the body, new buttons are created with
    # n_clicks=0.  Ignore those phantom triggers — only act on real clicks.
    if not any(n_clicks_list):
        return dash.no_update, dash.no_update
    ctx = dash.callback_context
    if not ctx.triggered:
        return dash.no_update, dash.no_update

    # Parse which button was clicked
    import re
    triggered_id = ctx.triggered[0]["prop_id"]
    try:
        match = re.search(r'"value":"([^"]+)"', triggered_id)
        clicked = match.group(1) if match else "ALL"
    except Exception:
        clicked = "ALL"

    # "ALL" resets to show everything
    if clicked == "ALL":
        new_filter = "ALL"
    else:
        # Build current active set
        if isinstance(current_filter, list):
            active = set(current_filter)
        else:
            active = set()  # Was "ALL", first specific click starts fresh

        # Toggle the clicked level
        if clicked in active:
            active.discard(clicked)
        else:
            active.add(clicked)

        # If empty or all 4 selected, revert to ALL
        all_levels = {"LOG", "NOTABLE", "ALERT", "CRITICAL"}
        if not active or active == all_levels:
            new_filter = "ALL"
        else:
            new_filter = sorted(active, key=lambda x: ["CRITICAL","ALERT","NOTABLE","LOG"].index(x))

    anom_df = _load_recent_anomalies(limit=500, level_filter=new_filter)
    return _anomaly_table(anom_df), new_filter


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"DIAMOND Dashboard starting at http://127.0.0.1:{DASHBOARD_PORT}")
    app.run(debug=False, port=DASHBOARD_PORT, host="0.0.0.0")
