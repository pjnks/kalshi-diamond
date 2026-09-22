# DIAMOND Session Handoff — 2026-04-26

**Status:** Sprint 14e in progress. Two phases deployed today (Apr 26), one phase
staged uncommitted local awaiting off-hours deploy.

---

## Today's deliveries (Apr 26)

### Phase 1 — DEPLOYED ✓ (subscription pipeline fix, 22:13 UTC)

- **Bug found:** `diamond_monitor.py:602` had `BATCH_SIZE = 100` cap on initial
  WebSocket subscription. `remaining = tickers[BATCH_SIZE:]` was captured but
  never used. New markets discovered post-startup were added to `market_cache`
  but never subscribed.
- **Symptom:** monitor saw 153 active markets but only subscribed to 67.
  Trade flow was 99% below baseline (~0-1 trades/min vs typical 500-1300/min).
- **Fix:** Three changes, all deployed and verified live:
  1. `src/kalshi_client.py` — added `_desired_tickers: set[str]` instance state,
     `sync_subscriptions()` method that diffs against target and emits add/remove
     deltas, restored full desired set on reconnect (was only re-subscribing
     to original startup batch).
  2. `diamond_monitor.py` — removed `BATCH_SIZE` cap; now subscribes to ALL
     discovered tickers at startup. `KalshiWSClient` chunks at 500 internally.
  3. Added `resubscribe_loop()` task that calls `ws.sync_subscriptions(market_cache.keys())`
     every `METADATA_REFRESH_SEC=300` to catch new markets appearing post-startup.
- **Verification:** post-restart log `22:13:50 [INFO] src.kalshi_client: Subscribed
  to ['trade'] for 128 tickers` (was 67 pre-fix).
- **Files modified:** committed in working tree, NOT yet git-committed locally
  (per Sprint 14 hands-off discipline — diamond/ has accumulated local changes
  awaiting batch commit).

### Phase 2 — DIAGNOSTIC COMPLETE 🟡 (learning curve)

- **Tool:** `learning_curve.py` — read-only ML training sweep at expanding
  window sizes [50, 100, 150, 200, 250, 290]. Subclasses `DiamondMLScorer` with
  no-op `save_model()` override and truncated `extract_training_data()` to
  enforce read-only constraint by construction.
- **Constraint verification:** no `diamond_ml_model.pkl` written anywhere.
  DB mtime unchanged. Output only to `reports/2026_04_26_ml_learning_curve.{html,json}`.
- **Findings:**
  - N=50/100/150/200: PiT-purged CV produced zero valid folds (structural
    floor — purge requires settlement-time density we don't have at low N)
  - N=250: Brier=0.1923, AUC=0.761, 4 features kept
  - N=290: Brier=0.2087, AUC=0.744, 4 features kept (WORSE than 250)
  - Jaccard(N=250 → N=290) = 0.60 (40% feature turnover; NOT converging)
- **Verdict:** Brier <0.05 gate looks unreachable with current feature set.
  Brier curve is flat-to-rising at 0.20, far from 0.05. With only 2 valid
  data points, the trend is statistically weak — but the baseline is so
  far from the gate that "more data" alone won't bridge the gap.
- **Implication:** authorized feature engineering work (Phase 3 below) is
  the active path during patience window.

### Phase 3 — STAGED UNCOMMITTED LOCAL ⏸ (book absorption metrics)

- **Goal:** add genuinely new feature dimension to enrich vector space for
  the eventual N=500 ML retrain. Existing 12 features measure volume PATTERNS;
  absorption measures volume × book-depth INTERACTION.
- **Staged changes (4 files, all uncommitted local):**
  1. `src/diamond_features.py`: new `book_absorption_metrics()` function
     (lines ~411-540) returning 5-key dict (4 metric variants + 1 stale flag).
     Integrated into `evaluate_trade()` after `event_relative_flow` block.
     Uses 60s window (matches anomaly cadence). Single shared data extraction
     (book_now + book_prev + trades_in_window) feeds all 4 variants.
  2. `diamond_config.py`: `FEATURE_ENABLED["book_absorption"] = True` flag added.
     `SCORER_WEIGHTS` UNTOUCHED (verified byte-identical, 8 keys, weight 0.999).
  3. `src/diamond_ml.py`: 4 new entries appended to `RAW_FEATURES`:
     `book_absorption_static`, `book_absorption_depletion`,
     `book_absorption_sided`, `book_absorption_replenish`.
- **Variant definitions:**
  - **A (static):** total_volume / total_prev_depth → "flow per unit standing depth"
  - **B (depletion):** (total_prev - total_now) / window_sec → "net depth depletion rate cents/sec"
  - **C (sided):** yes_pressure - no_pressure → "side-weighted directional pressure"
  - **D (replenish):** total_now / (total_prev - total_volume) − 1 → "defended vs eaten ratio"
    + edge case: if total_volume ≥ total_prev_depth, force `-1.0` ("book swept / toxic flow")
- **Architecture rulings received from operator (2026-04-26):**
  - All 4 formulas approved; Variant D edge case (-1.0 sweep) explicitly
    approved as "non-linear discontinuous flag the Ridge regression can partition cleanly"
  - Window length 60s confirmed (do NOT tighten to 30s, do NOT widen to 5min)
  - Deploy timing: WAIT for off-hours maintenance window (~03:00 UTC) to
    avoid second WebSocket interruption within 90 minutes of Phase 1 deploy
- **Smoke test results (canned data):**
  ```
  Case 2 (informed flow, no replenishment): D=+0.000, A=+0.900, B=+0.300, C=+0.700
  Case 3 (replenishment defense):           D=+0.300, A=+0.500, B=+0.017, C=+0.500
  ```
  Variant D cleanest discriminator between informed and uninformed flow regimes.

---

## To deploy Phase 3 (run at off-hours, ~03:00 UTC):

```bash
cd ~/Documents/quant/diamond && ./deploy.sh --restart
```

Then verify (~3 min after restart):

```bash
# 1. Service health + new code loaded
ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51 \
  "tail -50 /home/ubuntu/kalshi-diamond/diamond_monitor.log | \
   grep -E 'Started DIAMOND|Subscribed to|ERROR'"

# 2. New keys appearing in features_json on next anomaly
ssh -i ~/.ssh/hmm-trader.key ubuntu@129.158.40.51 \
  "sqlite3 /home/ubuntu/kalshi-diamond/diamond_trades.db \
   \"SELECT features FROM anomalies WHERE created_at >= strftime('%s','now','-30 min') \
    ORDER BY created_at DESC LIMIT 1;\"" | grep -o 'book_absorption[^,]*' | head -5
```

Expected: 4 `book_absorption_*` keys with float values + `_book_absorption_stale` flag.

---

## What stays in patience window (DO NOT TOUCH)

- `KELLY_SIZING_ENABLED` — still false. N=500 + Brier <0.05 gate unchanged.
- `SCORER_WEIGHTS` — verified byte-identical post-Sprint-14e. Composite score
  and live entry/exit logic untouched.
- ALERT threshold (0.55), CRITICAL threshold (0.78), tiered sizing (3/2/1) — unchanged.
- `CTM_ENABLED` — still false. Wait for N>1500 settled trades.
- `KXTRUMPOUT27` 93¢/8¢ open positions — DELIBERATELY not intervened
  (preserves training-distribution right tail for ML retrain).

---

## Recurring tasks (weekly during patience window)

1. **Re-run learning curve:** `PYTHONPATH=. python learning_curve.py --db /tmp/diamond_trades_fresh.db`
   - Pull fresh DB first: `scp ubuntu@129.158.40.51:/home/ubuntu/kalshi-diamond/diamond_trades.db /tmp/diamond_trades_fresh.db`
   - Track the additional curve points as N grows beyond 290 — by N=400 we'd
     have 4 valid points, enough to see whether Brier is descending, flat, or rising.
2. **Verify guard invariants:** `sqlite3 ... "SELECT COUNT(*) FROM paper_trades WHERE status IN ('filled','settled','voided') AND fill_price IS NULL;"` should return 0.
3. **Confirm absorption shadow features accumulating:** post-deploy, check
   that `features_json` on recent anomalies contains the 4 new keys.

---

## Open positions (as of Apr 26 ~22:00 UTC)

1. KXNBAROY-26-CFLA  no @ 41¢  (Apr 13, long-dated)
2. KXPERUPRES-26-KFUJ  no @ 76¢  (Apr 13, long-dated)
3. KXTRUMPOUT27-27-26AUG01  no @ 93¢  (Apr 20, deliberately not intervened)
4. KXTRUMPOUT27-27-26AUG01  yes @ 8¢  (Apr 24, same ticker after dedup expired)
5. KXLLM1-26APR25-OPEN  yes @ 20¢  (Apr 20)

(Some may have settled between this note and the next session.)
