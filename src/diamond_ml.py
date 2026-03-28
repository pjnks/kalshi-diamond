"""
diamond_ml.py
─────────────
Machine-learned anomaly scorer for DIAMOND.

Tiered pipeline:
  Tier 1: Logistic Regression with L1 (Lasso) — baseline, auto-zeroes noise features
  Tier 2: GradientBoosting — only if it beats Lasso's Brier Score in time-series CV

Predicts trade EDGE (P(win) - market_implied_prob), not raw win/loss.
NO Platt scaling at N < 500 — raw probabilities are ordinal (ranking), not cardinal.
Null importance testing drops spurious features before training.
All comparison metrics use ONLY held-out CV fold predictions (never in-sample).
"""

from __future__ import annotations

import json
import logging
import pickle
import time
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss, roc_auc_score
from sklearn.preprocessing import StandardScaler

log = logging.getLogger(__name__)

# ── Raw feature names (drop dead cross_market_correlation) ────────────
RAW_FEATURES = [
    "trade_size_zscore",
    "volume_spike_ratio",
    "order_book_imbalance",
    "taker_side_skew",
    "price_impact",
    "sweep_score",
    "trade_velocity",
    "size_concentration",
    "book_pressure_delta",
]

# ── Engineered feature names ─────────────────────────────────────────
ENGINEERED_FEATURES = [
    "n_features_firing",
    "max_feature_score",
    "entry_price_cents",
    "side_is_yes",
    # "composite_x_price" — REMOVED: composite is a linear combination of the
    # raw features, so composite*price introduces massive multicollinearity.
    # Wastes degrees of freedom and confuses feature importance attribution.
    "category_target_enc",
]

ALL_CANDIDATE_FEATURES = RAW_FEATURES + ENGINEERED_FEATURES

# Minimum N for Platt calibration (below this, use raw probabilities)
MIN_N_FOR_CALIBRATION = 500


def _wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a proportion."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    margin = z * np.sqrt((p * (1 - p) + z**2 / (4 * n)) / n) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


class DiamondMLScorer:
    """Tiered ML scorer: Lasso baseline → GradientBoosting if it earns it."""

    def __init__(self, db_path: str | Path, model_path: str | Path | None = None):
        self._db_path = str(db_path)
        self._model_path = str(model_path or Path(db_path).parent / "diamond_ml_model.pkl")
        self._model = None
        self._scaler = None
        self._feature_names: list[str] = []
        self._category_encoding: dict[str, float] = {}
        self._global_win_rate: float = 0.5
        self._model_type: str = ""
        self._trained_at: float = 0.0

    # ── Data Extraction ───────────────────────────────────────────────

    def extract_training_data(self) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
        """Extract labeled training data from settled paper trades.

        Returns:
            X: Feature DataFrame
            y: Binary labels (1=win, 0=loss)
            meta: Metadata DataFrame (ticker, pnl_cents, entry_price, opened_at)
        """
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        query = """
            SELECT id, ticker, side, entry_price, fill_price, pnl_cents,
                   anomaly_score, features_json, opened_at, category
            FROM paper_trades
            WHERE status = 'settled'
              AND features_json IS NOT NULL
              AND pnl_cents IS NOT NULL
            ORDER BY opened_at ASC
        """
        rows = conn.execute(query).fetchall()
        conn.close()

        if not rows:
            return pd.DataFrame(), pd.Series(dtype=float), pd.DataFrame()

        records = []
        meta_records = []
        labels = []

        for row in rows:
            try:
                feat = json.loads(row["features_json"])
            except (json.JSONDecodeError, TypeError):
                continue

            price = row["fill_price"] or row["entry_price"] or 50
            pnl = row["pnl_cents"] or 0.0

            # Raw features (fill missing with 0)
            raw = {f: float(feat.get(f, 0.0)) for f in RAW_FEATURES}

            # Engineered features
            firing = [v for v in raw.values() if v > 0.01]
            raw["n_features_firing"] = len(firing)
            raw["max_feature_score"] = max(firing) if firing else 0.0
            raw["entry_price_cents"] = float(price)
            raw["side_is_yes"] = 1.0 if row["side"] == "yes" else 0.0
            raw["category_target_enc"] = 0.0  # Filled later

            records.append(raw)
            meta_records.append({
                "id": row["id"],
                "ticker": row["ticker"],
                "pnl_cents": pnl,
                "entry_price": price,
                "opened_at": row["opened_at"],
                "category": row["category"] or "unknown",
                "anomaly_score": row["anomaly_score"],
            })
            labels.append(1.0 if pnl > 0 else 0.0)

        X = pd.DataFrame(records)
        y = pd.Series(labels, name="is_win")
        meta = pd.DataFrame(meta_records)

        # NOTE: Target encoding is deferred to _time_series_cv() where it is
        # computed per-fold to prevent test-label leakage. The column is added
        # as a placeholder here and overwritten inside each CV fold.
        # Global encoding is still computed for the FINAL model fit (train on all data).
        self._global_win_rate = y.mean() if len(y) > 0 else 0.5
        self._compute_category_encoding(meta["category"], y)
        X["category_target_enc"] = self._global_win_rate  # placeholder

        return X, y, meta

    def _compute_category_encoding(self, categories: pd.Series, y: pd.Series,
                                   smoothing: float = 10.0):
        """Target encoding with global prior smoothing."""
        self._category_encoding = {}
        for cat in categories.unique():
            mask = categories == cat
            n = mask.sum()
            wins = y[mask].sum()
            smoothed = (wins + self._global_win_rate * smoothing) / (n + smoothing)
            self._category_encoding[cat] = smoothed

    # ── Null Importance Testing ───────────────────────────────────────

    def null_importance_test(self, X: pd.DataFrame, y: pd.Series,
                            n_iterations: int = 100) -> list[str]:
        """Identify features with real importance > 95th percentile of null distribution.

        Returns list of feature names to KEEP.
        """
        log.info(f"[ML] Running null importance test ({n_iterations} iterations)...")

        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        model = LogisticRegression(solver="saga", C=1.0, l1_ratio=1.0,
                                   max_iter=2000, random_state=42)
        model.fit(X_scaled, y)
        real_importance = np.abs(model.coef_[0])

        null_importances = np.zeros((n_iterations, X.shape[1]))
        rng = np.random.RandomState(42)
        for i in range(n_iterations):
            y_shuffled = y.values.copy()
            rng.shuffle(y_shuffled)
            model_null = LogisticRegression(solver="saga", C=1.0, l1_ratio=1.0,
                                           max_iter=2000, random_state=i)
            model_null.fit(X_scaled, y_shuffled)
            null_importances[i] = np.abs(model_null.coef_[0])

        p95 = np.percentile(null_importances, 95, axis=0)
        keep = []
        for j, fname in enumerate(X.columns):
            passed = real_importance[j] > p95[j]
            status = "KEEP" if passed else "DROP"
            log.info(f"  {fname:30s}  real={real_importance[j]:.4f}  "
                     f"null_p95={p95[j]:.4f}  → {status}")
            if passed:
                keep.append(fname)

        if not keep:
            log.warning("[ML] No features passed null test — keeping top 5 by importance")
            top_idx = np.argsort(real_importance)[-5:]
            keep = [X.columns[i] for i in top_idx]

        log.info(f"[ML] Null importance: keeping {len(keep)}/{X.shape[1]} features")
        return keep

    # ── Time-Series Cross-Validation ──────────────────────────────────

    def _time_series_cv(self, X: np.ndarray, y: np.ndarray,
                        model, n_min_train: int = 100,
                        fold_size: int = 30, *,
                        categories=None,
                        cat_enc_col_idx=None,
                        scale_per_fold: bool = False) -> dict:
        """Expanding-window time-series CV.

        Returns mean Brier, AUC, AND per-sample held-out predictions
        (critical: these are the ONLY valid predictions for comparison).

        If categories and cat_enc_col_idx are provided, the category target
        encoding is recomputed per-fold using ONLY training labels to prevent
        test-label leakage (CRO fix: peer review item #4).

        If scale_per_fold=True, StandardScaler is fit on training fold only
        and applied to test fold each iteration (prevents feature distribution
        leakage from test data into training standardization).
        """
        n = len(y)
        briers, aucs = [], []
        # Store held-out predictions indexed by original position
        oos_probs = np.full(n, np.nan)
        fold = 0

        start = n_min_train
        while start + fold_size <= n:
            end = min(start + fold_size, n)
            X_train, y_train = X[:start].copy(), y[:start]
            X_test, y_test = X[start:end].copy(), y[start:end]

            # Per-fold category target encoding (prevents label leakage)
            if categories is not None and cat_enc_col_idx is not None:
                cats_train = categories.iloc[:start]
                cats_test = categories.iloc[start:end]
                fold_wr = y_train.mean() if len(y_train) > 0 else 0.5
                smoothing = 10.0
                fold_enc = {}
                for cat in cats_train.unique():
                    mask = cats_train == cat
                    n_cat = mask.sum()
                    wins = y_train[mask.values].sum()
                    fold_enc[cat] = (wins + fold_wr * smoothing) / (n_cat + smoothing)
                # Apply fold-local encoding
                X_train[:, cat_enc_col_idx] = cats_train.map(
                    lambda c, fe=fold_enc, fw=fold_wr: fe.get(c, fw)
                ).values
                X_test[:, cat_enc_col_idx] = cats_test.map(
                    lambda c, fe=fold_enc, fw=fold_wr: fe.get(c, fw)
                ).values

            # Per-fold scaling: fit on train, transform both
            if scale_per_fold:
                fold_scaler = StandardScaler()
                X_train = fold_scaler.fit_transform(X_train)
                X_test = fold_scaler.transform(X_test)

            try:
                from sklearn.base import clone
                m = clone(model)
                m.fit(X_train, y_train)
                proba = m.predict_proba(X_test)[:, 1]
                # Store held-out predictions at their original indices
                oos_probs[start:end] = proba
                briers.append(brier_score_loss(y_test, proba))
                if len(np.unique(y_test)) > 1:
                    aucs.append(roc_auc_score(y_test, proba))
                fold += 1
            except Exception as e:
                log.warning(f"[ML] CV fold {fold} failed: {e}")

            start = end

        return {
            "brier_mean": np.mean(briers) if briers else 1.0,
            "brier_std": np.std(briers) if briers else 0.0,
            "auc_mean": np.mean(aucs) if aucs else 0.5,
            "auc_std": np.std(aucs) if aucs else 0.0,
            "n_folds": len(briers),
            "oos_probs": oos_probs,  # NaN for training-only samples
        }

    # ── Training ──────────────────────────────────────────────────────

    def train(self, min_samples: int = 100) -> dict:
        """Train tiered model pipeline.

        Returns metrics dict with model_type, brier, auc, feature_importance, etc.
        All comparison metrics use ONLY held-out CV predictions.
        """
        X, y, meta = self.extract_training_data()

        if len(y) < min_samples:
            msg = f"Not enough samples ({len(y)} < {min_samples})"
            log.warning(f"[ML] {msg}")
            return {"error": msg, "n_samples": len(y)}

        log.info(f"[ML] Training on {len(y)} samples "
                 f"(win rate: {y.mean():.1%})")

        # Step 1: Null importance test → feature selection
        keep_features = self.null_importance_test(X, y)
        X_filtered = X[keep_features].copy()

        # Step 2: Prepare unscaled features for per-fold CV
        # Scaling is done per-fold inside _time_series_cv to prevent
        # feature distribution leakage from test folds into training.
        # A global scaler is still fit on all data for the FINAL model only.
        X_unscaled = X_filtered.values

        # Locate category_target_enc column index for per-fold re-encoding
        _cat_col_idx = None
        _cats = None
        if "category_target_enc" in keep_features:
            _cat_col_idx = list(keep_features).index("category_target_enc")
            _cats = meta["category"]

        # Step 3: Tier 1 — Lasso Logistic Regression
        lasso = LogisticRegression(
            solver="saga", C=0.5, l1_ratio=1.0,
            max_iter=3000, random_state=42, class_weight="balanced",
        )
        lasso_cv = self._time_series_cv(X_unscaled, y.values, lasso,
                                        categories=_cats,
                                        cat_enc_col_idx=_cat_col_idx,
                                        scale_per_fold=True)
        log.info(f"[ML] Lasso CV: Brier={lasso_cv['brier_mean']:.4f}±{lasso_cv['brier_std']:.4f}, "
                 f"AUC={lasso_cv['auc_mean']:.3f}±{lasso_cv['auc_std']:.3f}")

        # Step 4: Tier 2 — GradientBoosting (only if enough data)
        # Raised from 150 to 1000: GBM with depth-3 trees and 80 estimators has
        # thousands of effective parameters. At < 1000 samples you're curve-fitting
        # noise. Lasso is the only defensible model until sufficient data accumulates.
        gb_cv = {"brier_mean": 1.0, "auc_mean": 0.5, "oos_probs": np.full(len(y), np.nan)}
        if len(y) >= 1000:
            gb = GradientBoostingClassifier(
                n_estimators=80, max_depth=3, learning_rate=0.05,
                min_samples_leaf=10, subsample=0.8, max_features=0.8,
                random_state=42,
            )
            gb_cv = self._time_series_cv(X_unscaled, y.values, gb,
                                        categories=_cats,
                                        cat_enc_col_idx=_cat_col_idx,
                                        scale_per_fold=True)
            log.info(f"[ML] GBM CV:   Brier={gb_cv['brier_mean']:.4f}±{gb_cv['brier_std']:.4f}, "
                     f"AUC={gb_cv['auc_mean']:.3f}±{gb_cv['auc_std']:.3f}")

        # Step 5: Pick winner by Brier Score
        if gb_cv["brier_mean"] < lasso_cv["brier_mean"] - 0.005:
            log.info("[ML] Winner: GradientBoosting (lower Brier)")
            base_model = GradientBoostingClassifier(
                n_estimators=80, max_depth=3, learning_rate=0.05,
                min_samples_leaf=10, subsample=0.8, max_features=0.8,
                random_state=42,
            )
            winner_cv = gb_cv
            model_type = "gradient_boosting"
        else:
            log.info("[ML] Winner: Lasso (GBM didn't beat it)")
            base_model = LogisticRegression(
                solver="saga", C=0.5, l1_ratio=1.0,
                max_iter=3000, random_state=42, class_weight="balanced",
            )
            winner_cv = lasso_cv
            model_type = "lasso"

        # Step 6: Train final model on all data
        # Global scaler fit on ALL data is correct here — this is the production
        # model, not CV evaluation. Per-fold scaling above was for unbiased metrics.
        scaler = StandardScaler()
        # Apply global category encoding for the final model
        X_final = X_filtered.copy()
        X_final["category_target_enc"] = meta["category"].map(
            lambda c: self._category_encoding.get(c, self._global_win_rate)
        ) if "category_target_enc" in X_final.columns else X_final.get("category_target_enc")
        X_scaled = scaler.fit_transform(X_final)

        # NO Platt scaling at N < 500 — probabilities are ordinal, not cardinal
        if len(y) >= MIN_N_FOR_CALIBRATION:
            from sklearn.calibration import CalibratedClassifierCV
            final_model = CalibratedClassifierCV(base_model, method="sigmoid", cv=3)
            log.info(f"[ML] N={len(y)} >= {MIN_N_FOR_CALIBRATION}: applying Platt calibration")
        else:
            final_model = base_model
            log.info(f"[ML] N={len(y)} < {MIN_N_FOR_CALIBRATION}: using raw probabilities "
                     f"(ordinal edge scores, NOT calibrated)")
        final_model.fit(X_scaled, y.values)

        # Step 7: Feature importance
        if model_type == "lasso":
            base_model.fit(X_scaled, y.values)
            importances = np.abs(base_model.coef_[0])
        else:
            base_model.fit(X_scaled, y.values)
            importances = base_model.feature_importances_

        feat_importance = dict(zip(keep_features, importances))
        feat_importance = dict(sorted(feat_importance.items(),
                                      key=lambda x: x[1], reverse=True))

        # Step 8: Save model artifacts
        self._model = final_model
        self._scaler = scaler
        self._feature_names = keep_features
        self._model_type = model_type
        self._trained_at = time.time()
        self.save_model()

        # Brier baseline
        base_rate = y.mean()
        naive_brier = brier_score_loss(y, np.full(len(y), base_rate))

        # Confidence intervals on win rate (Wilson score)
        n_wins = int(y.sum())
        wr_lo, wr_hi = _wilson_ci(n_wins, len(y))

        # OOS edge analysis (ONLY held-out predictions — fixes CRO flaw #1)
        oos_probs = winner_cv["oos_probs"]
        oos_mask = ~np.isnan(oos_probs)
        oos_edges = np.full(len(y), np.nan)
        if oos_mask.any():
            oos_edges[oos_mask] = oos_probs[oos_mask] - (meta["entry_price"].values[oos_mask] / 100.0)

        # OOS edge>0 stats (the ONLY valid comparison)
        oos_edge_pos = oos_mask & (oos_edges > 0)
        oos_n = int(oos_edge_pos.sum())
        oos_wins = int(y[oos_edge_pos].sum()) if oos_n > 0 else 0
        oos_pnl = float(meta.loc[oos_edge_pos, "pnl_cents"].sum()) if oos_n > 0 else 0.0
        oos_wr = oos_wins / oos_n if oos_n > 0 else 0.0
        oos_wr_lo, oos_wr_hi = _wilson_ci(oos_wins, oos_n)

        metrics = {
            "model_type": model_type,
            "n_samples": len(y),
            "win_rate": float(y.mean()),
            "win_rate_ci_95": [wr_lo, wr_hi],
            "calibrated": len(y) >= MIN_N_FOR_CALIBRATION,
            "n_features_kept": len(keep_features),
            "features_kept": keep_features,
            "features_dropped": [f for f in ALL_CANDIDATE_FEATURES if f not in keep_features],
            "cv_brier": winner_cv["brier_mean"],
            "cv_brier_std": winner_cv["brier_std"],
            "cv_auc": winner_cv["auc_mean"],
            "cv_auc_std": winner_cv["auc_std"],
            "naive_brier": naive_brier,
            "brier_improvement": naive_brier - winner_cv["brier_mean"],
            "feature_importance": feat_importance,
            "lasso_brier": lasso_cv["brier_mean"],
            "gbm_brier": gb_cv["brier_mean"],
            "n_folds": winner_cv["n_folds"],
            "trained_at": self._trained_at,
            # OOS-only edge metrics (fixes CRO flaw #1)
            "oos_edge_trades": oos_n,
            "oos_edge_wins": oos_wins,
            "oos_edge_win_rate": oos_wr,
            "oos_edge_win_rate_ci_95": [oos_wr_lo, oos_wr_hi],
            "oos_edge_pnl_cents": oos_pnl,
            "oos_total_held_out": int(oos_mask.sum()),
        }

        log.info(f"[ML] Model saved: {model_type}, "
                 f"Brier={winner_cv['brier_mean']:.4f} "
                 f"(naive={naive_brier:.4f}, improvement={metrics['brier_improvement']:+.4f}), "
                 f"AUC={winner_cv['auc_mean']:.3f}, "
                 f"features={len(keep_features)}")
        log.info(f"[ML] OOS edge>0: {oos_n} trades, "
                 f"WR={oos_wr:.1%} CI=[{oos_wr_lo:.1%}, {oos_wr_hi:.1%}], "
                 f"P&L={oos_pnl:.0f}¢")
        if oos_wr_lo <= 0.50:
            log.warning(f"[ML] WARNING: OOS win rate CI [{oos_wr_lo:.1%}, {oos_wr_hi:.1%}] "
                        f"includes 50% — cannot reject null hypothesis of no edge")

        return metrics

    # ── Prediction ────────────────────────────────────────────────────

    def predict(self, features: dict, entry_price: int) -> float:
        """Predict trade edge = P(win) - market_implied_probability.

        NOTE: At N < 500, probabilities are NOT calibrated (Platt scaling disabled).
        The edge score is ordinal (useful for ranking) but NOT cardinal (don't
        interpret magnitude as true probability difference).

        Args:
            features: Feature dict from FeatureEngine.compute()
            entry_price: Entry price in cents (1-99)

        Returns:
            Edge score (positive = favorable). -999 if model not ready.
        """
        if self._model is None or self._scaler is None:
            return -999.0

        try:
            raw = {}
            for fname in self._feature_names:
                if fname in RAW_FEATURES:
                    raw[fname] = float(features.get(fname, 0.0))
                elif fname == "n_features_firing":
                    firing = [float(features.get(f, 0.0))
                              for f in RAW_FEATURES if features.get(f, 0.0) > 0.01]
                    raw[fname] = float(len(firing))
                elif fname == "max_feature_score":
                    scores = [float(features.get(f, 0.0)) for f in RAW_FEATURES]
                    raw[fname] = float(max(scores)) if scores else 0.0
                elif fname == "entry_price_cents":
                    raw[fname] = float(entry_price)
                elif fname == "side_is_yes":
                    raw[fname] = 0.5
                elif fname == "category_target_enc":
                    raw[fname] = self._global_win_rate
                else:
                    raw[fname] = 0.0

            X = np.array([[raw[f] for f in self._feature_names]])
            X_scaled = self._scaler.transform(X)
            prob_win = self._model.predict_proba(X_scaled)[0, 1]

            market_implied = entry_price / 100.0
            edge = prob_win - market_implied

            return float(edge)

        except Exception as e:
            log.error(f"[ML] Prediction failed: {e}")
            return -999.0

    # ── Comparison (OOS-only — fixes CRO flaw #1) ────────────────────

    def compare_with_handtuned(self) -> dict:
        """Compare ML edge scorer vs hand-tuned composite.

        CRITICAL: All ML metrics use ONLY held-out CV fold predictions.
        The final model (trained on all data) is NEVER used for comparison.
        This prevents the in-sample bias identified in CRO review.
        """
        X, y, meta = self.extract_training_data()
        if len(y) < 50:
            return {"error": f"Not enough data ({len(y)} < 50)"}

        results = {
            "n_trades": len(y),
            "actual_win_rate": float(y.mean()),
        }

        # Hand-tuned baseline
        ht_scores = meta["anomaly_score"].fillna(0.5).values
        results["handtuned_brier"] = float(brier_score_loss(y, ht_scores))
        if len(np.unique(y)) > 1:
            results["handtuned_auc"] = float(roc_auc_score(y, ht_scores))
        results["handtuned_pnl_cents"] = float(meta["pnl_cents"].sum())

        # ML: generate held-out predictions via fresh CV (NOT using saved model)
        keep_features = self._feature_names
        if not keep_features:
            keep_features = self.null_importance_test(X, y)

        X_filtered = X[keep_features]
        X_unscaled = X_filtered.values

        # Re-run CV to get held-out predictions
        model = LogisticRegression(
            solver="saga", C=0.5, l1_ratio=1.0,
            max_iter=3000, random_state=42, class_weight="balanced",
        ) if self._model_type != "gradient_boosting" else GradientBoostingClassifier(
            n_estimators=80, max_depth=3, learning_rate=0.05,
            min_samples_leaf=10, subsample=0.8, max_features=0.8,
            random_state=42,
        )

        # Locate category_target_enc for per-fold re-encoding
        _cat_col_idx = None
        _cats = None
        if "category_target_enc" in keep_features:
            _cat_col_idx = list(keep_features).index("category_target_enc")
            _cats = meta["category"]

        cv_result = self._time_series_cv(X_unscaled, y.values, model,
                                         categories=_cats,
                                         cat_enc_col_idx=_cat_col_idx,
                                         scale_per_fold=True)
        oos_probs = cv_result["oos_probs"]
        oos_mask = ~np.isnan(oos_probs)

        if not oos_mask.any():
            results["ml_error"] = "No held-out predictions available"
            return results

        # OOS-only metrics
        oos_y = y.values[oos_mask]
        oos_p = oos_probs[oos_mask]
        results["ml_brier_oos"] = float(brier_score_loss(oos_y, oos_p))
        if len(np.unique(oos_y)) > 1:
            results["ml_auc_oos"] = float(roc_auc_score(oos_y, oos_p))
        results["n_held_out"] = int(oos_mask.sum())

        # OOS edge analysis
        entry_prices = meta["entry_price"].values
        oos_edges = np.full(len(y), np.nan)
        oos_edges[oos_mask] = oos_probs[oos_mask] - (entry_prices[oos_mask] / 100.0)

        for threshold in [0.0, 0.05, 0.10, 0.15]:
            mask = oos_mask & (oos_edges > threshold)
            n = int(mask.sum())
            if n > 0:
                wins = int(y[mask].sum())
                pnl = float(meta.loc[mask, "pnl_cents"].sum())
                wr = wins / n
                wr_lo, wr_hi = _wilson_ci(wins, n)
                results[f"oos_edge>{threshold:.2f}"] = {
                    "trades": n,
                    "wins": wins,
                    "pnl_cents": pnl,
                    "win_rate": wr,
                    "win_rate_ci_95": [wr_lo, wr_hi],
                    "ci_excludes_50": wr_lo > 0.50,
                }
            else:
                results[f"oos_edge>{threshold:.2f}"] = {
                    "trades": 0, "wins": 0, "pnl_cents": 0.0,
                    "win_rate": 0.0, "win_rate_ci_95": [0.0, 1.0],
                    "ci_excludes_50": False,
                }

        return results

    # ── Persistence ───────────────────────────────────────────────────

    def save_model(self):
        """Save model artifacts to disk."""
        artifacts = {
            "model": self._model,
            "scaler": self._scaler,
            "feature_names": self._feature_names,
            "category_encoding": self._category_encoding,
            "global_win_rate": self._global_win_rate,
            "model_type": self._model_type,
            "trained_at": self._trained_at,
        }
        with open(self._model_path, "wb") as f:
            pickle.dump(artifacts, f)
        log.info(f"[ML] Model saved to {self._model_path}")

    def load_model(self) -> bool:
        """Load model artifacts from disk. Returns True if successful."""
        try:
            with open(self._model_path, "rb") as f:
                artifacts = pickle.load(f)
            self._model = artifacts["model"]
            self._scaler = artifacts["scaler"]
            self._feature_names = artifacts["feature_names"]
            self._category_encoding = artifacts.get("category_encoding", {})
            self._global_win_rate = artifacts.get("global_win_rate", 0.5)
            self._model_type = artifacts.get("model_type", "unknown")
            self._trained_at = artifacts.get("trained_at", 0.0)

            age_hours = (time.time() - self._trained_at) / 3600
            log.info(f"[ML] Model loaded: {self._model_type}, "
                     f"{len(self._feature_names)} features, "
                     f"age={age_hours:.1f}h")
            return True
        except FileNotFoundError:
            log.info("[ML] No saved model found")
            return False
        except Exception as e:
            log.error(f"[ML] Failed to load model: {e}")
            return False

    @property
    def is_ready(self) -> bool:
        return self._model is not None and self._scaler is not None

    @property
    def model_age_hours(self) -> float:
        if self._trained_at <= 0:
            return float("inf")
        return (time.time() - self._trained_at) / 3600
