"""
Lightweight DIAMOND dashboard — minimal version without blocking callbacks.
Just reads from DB and displays live without complex Dash callbacks.
"""
import sqlite3
import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import dash
from dash import html, dcc, dash_table
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, clientside_callback

from diamond_config import DASHBOARD_PORT

ROOT = Path(__file__).parent
DB = ROOT / "diamond_trades.db"

# Colors
BG = "#0d0f14"
PANEL = "#141820"
BORDER = "#1e2330"
TEXT = "#e0e4f0"
RED = "#e84c3d"
GREEN = "#4caf50"
YELLOW = "#ffb703"
ORANGE = "#ff9800"
CYAN = "#00bcd4"
TEXT_DIM = "#6e7681"

app = dash.Dash(__name__, external_stylesheets=[dbc.themes.CYBORG])

def get_db():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn

def get_stats():
    """Get real-time stats from DB"""
    conn = get_db()
    trades = conn.execute("SELECT COUNT(*) as n FROM trades").fetchone()["n"]
    markets = conn.execute("SELECT COUNT(DISTINCT ticker) as n FROM trades").fetchone()["n"]
    anomalies = conn.execute("SELECT COUNT(*) as n FROM anomalies").fetchone()["n"]
    
    paper = conn.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) as filled,
               SUM(CASE WHEN status='settled' THEN 1 ELSE 0 END) as settled,
               SUM(CASE WHEN status='settled' AND pnl_cents > 0 THEN 1 ELSE 0 END) as wins
        FROM paper_trades
    """).fetchone()
    
    conn.close()
    return {
        "trades": trades,
        "markets": markets,
        "anomalies": anomalies,
        "paper_total": paper["total"] or 0,
        "paper_filled": paper["filled"] or 0,
        "paper_settled": paper["settled"] or 0,
        "paper_wins": paper["wins"] or 0,
    }

app.layout = dbc.Container([
    dbc.Row([
        dbc.Col([
            html.H1("DIAMOND", style={"color": CYAN, "fontFamily": "monospace", "fontWeight": "bold"}),
            html.P("Kalshi Unusual Volume Tracker", style={"color": TEXT_DIM}),
        ]),
        dbc.Col([
            html.P(f"Last refresh: {datetime.now().strftime('%H:%M:%S')}", 
                   style={"color": TEXT_DIM, "fontSize": "0.8rem", "textAlign": "right"}),
            dcc.Interval(id="refresh", interval=30000, n_intervals=0),  # 30s refresh
        ], width=6),
    ], className="mb-3"),
    
    dbc.Row([
        dbc.Col(html.Div(id="stats", children="Loading..."), width=12),
    ], className="mb-3"),
    
    dbc.Row([
        dbc.Col([
            html.H5("PAPER TRADING", style={"color": CYAN, "fontSize": "0.8rem"}),
            html.Div(id="paper-stats", children="No trades yet"),
        ], className="p-3", style={"backgroundColor": PANEL, "borderRadius": "8px", "border": f"1px solid {BORDER}"}),
    ]),
], fluid=True, style={"backgroundColor": BG, "color": TEXT, "padding": "20px", "minHeight": "100vh"})

@app.callback(
    Output("stats", "children"),
    Input("refresh", "n_intervals")
)
def update_stats(n):
    stats = get_stats()
    return html.Div([
        dbc.Row([
            dbc.Col([
                html.Div([
                    html.P("TRADES", style={"color": TEXT_DIM, "fontSize": "0.7rem", "margin": "0"}),
                    html.H3(f"{stats['trades']:,}", style={"color": TEXT, "margin": "5px 0"}),
                ], style={"backgroundColor": PANEL, "padding": "15px", "borderRadius": "4px", "border": f"1px solid {BORDER}"}),
            ], width=2),
            dbc.Col([
                html.Div([
                    html.P("MARKETS", style={"color": TEXT_DIM, "fontSize": "0.7rem", "margin": "0"}),
                    html.H3(f"{stats['markets']}", style={"color": TEXT, "margin": "5px 0"}),
                ], style={"backgroundColor": PANEL, "padding": "15px", "borderRadius": "4px", "border": f"1px solid {BORDER}"}),
            ], width=2),
            dbc.Col([
                html.Div([
                    html.P("ANOMALIES", style={"color": TEXT_DIM, "fontSize": "0.7rem", "margin": "0"}),
                    html.H3(f"{stats['anomalies']:,}", style={"color": TEXT, "margin": "5px 0"}),
                ], style={"backgroundColor": PANEL, "padding": "15px", "borderRadius": "4px", "border": f"1px solid {BORDER}"}),
            ], width=2),
            dbc.Col([
                html.Div([
                    html.P("PAPER TOTAL", style={"color": TEXT_DIM, "fontSize": "0.7rem", "margin": "0"}),
                    html.H3(f"{stats['paper_total']}", style={"color": YELLOW, "margin": "5px 0"}),
                ], style={"backgroundColor": PANEL, "padding": "15px", "borderRadius": "4px", "border": f"1px solid {BORDER}"}),
            ], width=2),
            dbc.Col([
                html.Div([
                    html.P("FILLED", style={"color": TEXT_DIM, "fontSize": "0.7rem", "margin": "0"}),
                    html.H3(f"{stats['paper_filled']}", style={"color": GREEN, "margin": "5px 0"}),
                ], style={"backgroundColor": PANEL, "padding": "15px", "borderRadius": "4px", "border": f"1px solid {BORDER}"}),
            ], width=2),
            dbc.Col([
                html.Div([
                    html.P("SETTLED", style={"color": TEXT_DIM, "fontSize": "0.7rem", "margin": "0"}),
                    html.H3(f"{stats['paper_settled']}", style={"color": CYAN, "margin": "5px 0"}),
                ], style={"backgroundColor": PANEL, "padding": "15px", "borderRadius": "4px", "border": f"1px solid {BORDER}"}),
            ], width=2),
        ], className="g-2"),
    ])

@app.callback(
    Output("paper-stats", "children"),
    Input("refresh", "n_intervals")
)
def update_paper(n):
    stats = get_stats()
    if stats['paper_total'] == 0:
        return html.P("No trades placed yet", style={"color": TEXT_DIM})
    
    win_rate = (stats['paper_wins'] / stats['paper_settled'] * 100) if stats['paper_settled'] > 0 else 0
    return html.Div([
        html.P(f"Total: {stats['paper_total']} | Filled: {stats['paper_filled']} | Settled: {stats['paper_settled']} | Wins: {stats['paper_wins']} ({win_rate:.0f}%)", 
               style={"color": TEXT, "fontSize": "0.9rem", "margin": "0"})
    ])

if __name__ == "__main__":
    print(f"DIAMOND Lite Dashboard at http://127.0.0.1:{DASHBOARD_PORT}")
    app.run(debug=False, port=DASHBOARD_PORT, host="127.0.0.1")
