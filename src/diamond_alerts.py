"""
diamond_alerts.py
─────────────────
Alert engine for DIAMOND — Pushover + macOS notifications with cooldowns.

Reuses notification patterns from HMM-Trader's src/notifier.py.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import urllib.parse
import urllib.request

from diamond_config import (
    ALERT_COOLDOWN_SEC,
    PUSHOVER_APP_TOKEN,
    PUSHOVER_USER_KEY,
)

log = logging.getLogger(__name__)

# Track last alert time + level per ticker to enforce cooldown
_last_alert: dict[str, dict] = {}  # ticker → {"ts": float, "level": str}

# Cooldowns per alert level (lower severity = longer cooldown)
_LEVEL_COOLDOWNS = {
    "NOTABLE": ALERT_COOLDOWN_SEC,       # 5 min
    "ALERT": ALERT_COOLDOWN_SEC // 2,    # 2.5 min
    "CRITICAL": 30,                       # 30 sec — never want to miss these
}

# Alert severity for escalation comparison
_LEVEL_SEVERITY = {"NOTABLE": 1, "ALERT": 2, "CRITICAL": 3}


# ── Notification Channels ─────────────────────────────────────────────


def _macos_notify(title: str, message: str, sound: str = "default") -> bool:
    """Send macOS native notification via osascript."""
    try:
        # Escape quotes for AppleScript
        title_esc = title.replace('"', '\\"')
        message_esc = message.replace('"', '\\"')
        script = f'display notification "{message_esc}" with title "{title_esc}" sound name "{sound}"'
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
        return True
    except Exception as e:
        log.warning(f"macOS notification failed: {e}")
        return False


def _pushover(title: str, message: str, priority: int = 0) -> bool:
    """Send Pushover push notification."""
    if not PUSHOVER_USER_KEY or not PUSHOVER_APP_TOKEN:
        log.debug("Pushover not configured — skipping")
        return False

    try:
        params = {
            "token": PUSHOVER_APP_TOKEN,
            "user": PUSHOVER_USER_KEY,
            "title": title,
            "message": message,
            "priority": priority,
            "sound": "siren" if priority >= 1 else "pushover",
        }
        if priority == 2:
            params["retry"] = 60
            params["expire"] = 3600

        data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(
            "https://api.pushover.net/1/messages.json", data=data, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get("status") == 1
    except Exception as e:
        log.warning(f"Pushover failed: {e}")
        return False


def _terminal_bell():
    print("\a" * 3, end="", flush=True)


# ── Cooldown Management ──────────────────────────────────────────────


def _check_cooldown(ticker: str, alert_level: str) -> bool:
    """
    Returns True if ok to alert. Level-aware:
    - Uses per-level cooldown times (CRITICAL = 30s, ALERT = 2.5min, NOTABLE = 5min)
    - Always allows escalation (e.g., NOTABLE → ALERT bypasses cooldown)
    """
    last = _last_alert.get(ticker)
    if last is None:
        return True

    elapsed = time.time() - last["ts"]
    cooldown = _LEVEL_COOLDOWNS.get(alert_level, ALERT_COOLDOWN_SEC)

    # Always allow escalation to higher severity
    last_severity = _LEVEL_SEVERITY.get(last["level"], 0)
    new_severity = _LEVEL_SEVERITY.get(alert_level, 0)
    if new_severity > last_severity:
        return True

    return elapsed >= cooldown


def _record_alert(ticker: str, alert_level: str):
    """Record that we just sent an alert for this ticker."""
    _last_alert[ticker] = {"ts": time.time(), "level": alert_level}


# ── Public API ────────────────────────────────────────────────────────


def dispatch_alert(
    ticker: str,
    alert_level: str,
    composite_score: float,
    features: dict,
    market_title: str = "",
):
    """
    Dispatch an alert based on level. Enforces per-ticker cooldown.

    Alert levels:
      LOG      → SQLite only (handled by caller)
      NOTABLE  → macOS notification
      ALERT    → macOS + Pushover
      CRITICAL → macOS + Pushover emergency + terminal bell
    """
    if alert_level == "NONE" or alert_level == "LOG":
        return  # Caller handles LOG-level storage

    if not _check_cooldown(ticker, alert_level):
        log.debug(f"Alert cooldown active for {ticker}, skipping {alert_level}")
        return

    title_prefix = {
        "NOTABLE": "DIAMOND Notable",
        "ALERT": "DIAMOND ALERT",
        "CRITICAL": "DIAMOND CRITICAL",
    }.get(alert_level, "DIAMOND")

    # Build concise message
    title = f"{title_prefix}: {ticker}"
    top_features = sorted(
        [(k, v) for k, v in features.items() if k not in ("composite", "alert_level")],
        key=lambda x: x[1],
        reverse=True,
    )[:3]
    feature_str = " | ".join(f"{k}: {v:.2f}" for k, v in top_features)
    name_part = f" ({market_title})" if market_title else ""
    message = f"Score: {composite_score:.2f}{name_part}\n{feature_str}"

    log.info(f"[{alert_level}] {ticker} score={composite_score:.2f}")

    if alert_level == "NOTABLE":
        _macos_notify(title, message, sound="Pop")

    elif alert_level == "ALERT":
        _macos_notify(title, message, sound="Basso")
        _pushover(title, message, priority=0)

    elif alert_level == "CRITICAL":
        _macos_notify(title, message, sound="Basso")
        _pushover(title, message, priority=1)
        _terminal_bell()

    _record_alert(ticker, alert_level)


def notify_trade_placed(ticker: str, side: str, price: int, title: str = ""):
    """Send notification when a paper trade is placed and filled."""
    name_part = f" ({title})" if title else ""
    msg_title = f"DIAMOND Trade: {ticker}"
    message = f"Bought {side} @ {price}¢{name_part}"
    _pushover(msg_title, message, priority=0)
    _macos_notify(msg_title, message, sound="Purr")
    log.info(f"[PAPER] Notified trade placed: {ticker} {side} @ {price}¢")


def notify_trade_settled(ticker: str, pnl_cents: float, title: str = ""):
    """Send notification when a paper trade settles."""
    name_part = f" ({title})" if title else ""
    won = pnl_cents > 0
    msg_title = f"DIAMOND {'Win' if won else 'Loss'}: {ticker}"
    message = f"P&L: {pnl_cents:+.0f}¢{name_part}"
    _pushover(msg_title, message, priority=0 if won else -1)
    _macos_notify(msg_title, message, sound="Glass" if won else "Basso")
    log.info(f"[PAPER] Notified settlement: {ticker} pnl={pnl_cents:+.0f}¢")


def notify_trade_flipped(
    ticker_exited: str,
    ticker_entered: str,
    exit_pnl_cents: float,
    entry_price: int,
    conviction_old: float,
    conviction_new: float,
    title: str = "",
):
    """Send notification when conviction system flips a position."""
    name_part = f" ({title})" if title else ""
    msg_title = f"DIAMOND Flip: {ticker_exited} → {ticker_entered}"
    message = (
        f"Exited {ticker_exited} (P&L: {exit_pnl_cents:+.0f}¢)\n"
        f"Entering {ticker_entered} @ {entry_price}¢{name_part}\n"
        f"Conviction: {conviction_old:.2f} → {conviction_new:.2f}"
    )
    _pushover(msg_title, message, priority=0)
    _macos_notify(msg_title, message, sound="Submarine")
    log.info(f"[PAPER] Notified flip: {ticker_exited} → {ticker_entered}")


def test_alert():
    """Send a test alert through all channels."""
    print("Testing DIAMOND alert channels...\n")

    print("1. macOS notification...")
    ok = _macos_notify("DIAMOND Test", "Test unusual volume alert", sound="Pop")
    print(f"   {'OK' if ok else 'FAILED'}")

    print("2. Pushover...")
    if PUSHOVER_USER_KEY and PUSHOVER_APP_TOKEN:
        ok = _pushover("DIAMOND Test", "Test unusual volume alert", priority=0)
        print(f"   {'OK' if ok else 'FAILED'}")
    else:
        print("   SKIPPED (not configured)")

    print("3. Terminal bell...")
    _terminal_bell()
    print("   OK")

    print("\nDone!")


if __name__ == "__main__":
    test_alert()
