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


# ── Feature Orthogonalizer (Sprint 13b) ─────────────────────────────
# Scrubs the base-rate (price) correlation out of raw features.
# Raw features like taker_side_skew are mechanically correlated with
# price (thin $0.05 books produce extreme skew more easily than thick
# $0.85 books). This confound causes Ridge to squash all features when
# predicting the residual (outcome - price). Orthogonalization removes
# the price-correlated component, isolating pure anomaly signal.

class FeatureOrthogonalizer:
    """Regress each feature against implied_probability, keep residuals.

    For each feature f_i:
        f_i_orth = f_i - LinearRegression(price).predict(price)

    The orthogonalized feature represents anomaly magnitude *independent*
    of contract price. If sweep_score is 0.8 on a $0.05 contract and
    0.8 on a $0.80 contract, the raw values are identical but the
    orthogonalized values differ — the $0.80 contract's sweep is more
    surprising (harder to achieve on a thick book).
    """

    def __init__(self):
        self._models: dict[str, tuple[float, float]] = {}  # fname → (slope, intercept)

    def fit(self, X: pd.DataFrame, price_col: str = "entry_price_cents") -> "FeatureOrthogonalizer":
        """Fit OLS for each feature against price."""
        from sklearn.linear_model import LinearRegression

        if price_col not in X.columns:
            log.warning(f"[ORTHO] Price column '{price_col}' not in X — skipping orthogonalization")
            return self

        prices = X[price_col].values.reshape(-1, 1)
        feature_cols = [c for c in X.columns if c != price_col]

        for col in feature_cols:
            lr = LinearRegression()
            lr.fit(prices, X[col].values)
            self._models[col] = (float(lr.coef_[0]), float(lr.intercept_))

        log.info(f"[ORTHO] Fitted {len(self._models)} feature→price regressions")
        return self

    def transform(self, X: pd.DataFrame, price_col: str = "entry_price_cents") -> pd.DataFrame:
        """Replace features with their price-orthogonalized residuals."""
        X_out = X.copy()

        if price_col not in X.columns or not self._models:
            return X_out

        prices = X[price_col].values

        for col, (slope, intercept) in self._models.items():
            if col in X_out.columns:
                expected = slope * prices + intercept
                X_out[col] = X[col].values - expected

        return X_out

    def fit_transform(self, X: pd.DataFrame, price_col: str = "entry_price_cents") -> pd.DataFrame:
        return self.fit(X, price_col).transform(X, price_col)


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
    # ── Binned microstructural regime indicators (Sprint 11) ─────────
    # Disjoint one-hot features that let Lasso capture non-linear
    # price × side interactions without multicollinearity. These encode
    # the three regimes where edge behaves qualitatively differently.
    "is_longshot_yes",       # <30¢ YES — retail favorite speculation
    "is_favorite_no",        # >70¢ NO — informed contrarian flow
    "is_mid_yes_spike",      # 30-60¢ YES with high volume — noise apex
    # ── Probability-conditioned interaction (Sprint 13) ───────────
    # Continuous score × implied_prob interaction breaks the U-shape.
    # Lets the model learn: high score + low price = genuine anomaly
    # (profitable), high score + high price = noise about to revert
    # (unprofitable). Lasso is linear — can't learn this without an
    # explicit interaction term.
    "score_x_implied_prob",
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

    def extract_training_data(
        self, min_opened_at: float | None = None,
    ) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
        """Extract labeled training data from settled paper trades.

        Args:
            min_opened_at: If provided, only include trades opened after this
                epoch timestamp. Use to filter to post-penalty data regimes
                (e.g., post-Sprint 11) and prevent distribution shift from
                contaminating the model.

        Returns:
            X: Feature DataFrame
            y: Binary labels (1=win, 0=loss)
            meta: Metadata DataFrame (ticker, pnl_cents, entry_price, opened_at, settled_at)
        """
        import sqlite3

        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        query = """
            SELECT id, ticker, side, entry_price, fill_price, pnl_cents,
                   anomaly_score, features_json, opened_at, settled_at, category
            FROM paper_trades
            WHERE status = 'settled'
              AND features_json IS NOT NULL
              AND pnl_cents IS NOT NULL
              AND settled_at IS NOT NULL
        """
        params: tuple = ()
        if min_opened_at is not None:
            query += "      AND opened_at >= ?\n"
            params = (min_opened_at,)
        query += "    ORDER BY opened_at ASC"
        rows = conn.execute(query, params).fetchall()
        conn.close()

        if not rows:
            return pd.DataFrame(), pd.Series(dtype=float), pd.DataFrame()

        records = []
        meta_records = []
        labels = []
        n_skipped_json = 0

        for row in rows:
            try:
                feat = json.loads(row["features_json"])
            except (json.JSONDecodeError, TypeError):
                n_skipped_json += 1
                continue

            # Explicit None checks — avoid truthiness (0 is a valid price)
            price = row["fill_price"] if row["fill_price"] is not None else row["entry_price"]
            if price is None:
                n_skipped_json += 1
                continue
            pnl = row["pnl_cents"] if row["pnl_cents"] is not None else 0.0

            # Raw features (fill missing with 0)
            raw = {f: float(feat.get(f, 0.0)) for f in RAW_FEATURES}

            # Engineered features
            firing = [v for v in raw.values() if v > 0.01]
            raw["n_features_firing"] = len(firing)
            raw["max_feature_score"] = max(firing) if firing else 0.0
            raw["entry_price_cents"] = float(price)
            raw["side_is_yes"] = 1.0 if row["side"] == "yes" else 0.0
            raw["category_target_enc"] = 0.0  # Filled later

            # Binned microstructural regime indicators (disjoint, orthogonal)
            raw["is_longshot_yes"] = 1.0 if (price < 30 and row["side"] == "yes") else 0.0
            raw["is_favorite_no"] = 1.0 if (price > 70 and row["side"] == "no") else 0.0
            raw["is_mid_yes_spike"] = 1.0 if (
                30 <= price <= 60 and row["side"] == "yes"
                and raw.get("volume_spike_ratio", 0) > 0.8
            ) else 0.0

            # Probability-conditioned interaction (Sprint 13).
            # anomaly_score is the post-penalty composite. entry_price_cents
            # is the market's implied probability (in cents). Their product
            # lets a linear model learn conditional slopes: a 0.65 score at
            # 15¢ produces 0.65*0.15=0.098, while 0.65 at 85¢ produces
            # 0.65*0.85=0.553 — the model can assign a negative coefficient
            # to penalize the high-price regime.
            anomaly_score = float(row["anomaly_score"] or 0.0)
            raw["score_x_implied_prob"] = anomaly_score * (float(price) / 100.0)

            records.append(raw)
            meta_records.append({
                "id": row["id"],
                "ticker": row["ticker"],
                "pnl_cents": pnl,
                "entry_price": price,
                "opened_at": row["opened_at"],
                "settled_at": row["settled_at"],
                "category": row["category"] or "unknown",
                "anomaly_score": row["anomaly_score"],
            })
            labels.append(1.0 if pnl > 0 else 0.0)

        if n_skipped_json > 0:
            log.warning(f"[ML] Skipped {n_skipped_json}/{len(rows)} trades "
                        f"(unparseable features_json or missing price)")
            if n_skipped_json > len(rows) * 0.5:
                log.error("[ML] Over 50% of trades have corrupt data. "
                          "Possible database corruption.")

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
        # Sprint 13b: Pure L2 (Ridge) for null importance test.
        # Previous L1/Elastic Net zeroed out collinear interaction terms
        # (score_x_implied_prob) before permutation could evaluate them.
        # L2 distributes weight across correlated features, allowing the
        # permutation test to measure true discriminative power.
        model = LogisticRegression(solver="lbfgs", penalty="l2",
                                   C=1.0,
                                   max_iter=2000, random_state=42)
        model.fit(X_scaled, y)
        real_importance = np.abs(model.coef_[0])

        null_importances = np.zeros((n_iterations, X.shape[1]))
        rng = np.random.RandomState(42)
        for i in range(n_iterations):
            y_shuffled = y.values.copy()
            rng.shuffle(y_shuffled)
            model_null = LogisticRegression(solver="lbfgs", penalty="l2",
                                           C=1.0,
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

    def _time_series_cv(self, X: pd.DataFrame, y: pd.Series,
                        meta: pd.DataFrame,
                        model_factory: callable,
                        n_min_train: int = 100,
                        fold_size: int = 30) -> dict:
        """Expanding-window CV with Point-in-Time purging and nested feature selection.

        Fixes three critical look-ahead biases identified in peer review:
        1. PiT Purging: Only trains on trades that settled BEFORE the test fold
           opens, preventing future label leakage from overlapping contracts.
        2. Nested Null Importance: Feature selection runs inside each fold using
           only purged training data, preventing data snooping.
        3. Nested Target Encoding: Category encoding computed per-fold from
           purged training labels only.
        4. Nested Scaling: StandardScaler fit per-fold on training data only.

        Returns mean Brier, AUC, and per-sample held-out predictions.
        """
        n = len(y)
        briers, aucs = [], []
        oos_probs = np.full(n, np.nan)
        fold_features_log = []  # Track feature stability across folds
        folds_attempted = 0
        folds_completed = 0

        start = n_min_train
        while start + fold_size <= n:
            end = min(start + fold_size, n)
            folds_attempted += 1

            # ── 1. POINT-IN-TIME PURGE ──────────────────────────────
            # Only keep training samples whose outcome was known (settled)
            # strictly before the first trade in the test fold opened.
            test_start_time = meta.iloc[start]["opened_at"]
            pit_mask = meta.iloc[:start]["settled_at"] < test_start_time

            if pit_mask.sum() < 50:
                log.debug(f"[ML] Fold {folds_attempted}: insufficient resolved "
                          f"trades after purge ({pit_mask.sum()}/50). Skipping.")
                start = end
                continue

            # Use positional indexing with np.where to prevent silent index
            # misalignment if DataFrames ever have non-default indices.
            pit_positions = np.where(pit_mask.values)[0]
            X_train = X.iloc[pit_positions].copy()
            y_train = y.iloc[pit_positions].copy()
            X_test = X.iloc[start:end].copy()
            y_test = y.iloc[start:end].copy()
            cats_train = meta.iloc[pit_positions]["category"]
            cats_test = meta.iloc[start:end]["category"]

            # Skip folds with homogeneous labels (all wins or all losses
            # after PiT purge) — Lasso coefficients would be all zeros,
            # making null importance test meaningless.
            if len(y_train.unique()) < 2:
                log.debug(f"[ML] Fold {folds_attempted}: homogeneous labels "
                          f"after PiT purge ({y_train.mean():.0%} win rate). "
                          f"Skipping.")
                start = end
                continue

            # ── 2. NESTED TARGET ENCODING ───────────────────────────
            # Point-in-time win rate for unseen categories (fixes Medium #4)
            fold_wr = y_train.mean() if len(y_train) > 0 else 0.5
            smoothing = 10.0
            fold_enc = {}
            for cat in cats_train.unique():
                cat_mask = cats_train == cat
                wins = y_train[cat_mask.values].sum()
                fold_enc[cat] = (wins + fold_wr * smoothing) / (cat_mask.sum() + smoothing)

            if "category_target_enc" in X_train.columns:
                X_train["category_target_enc"] = cats_train.map(
                    lambda c, fe=fold_enc, fw=fold_wr: fe.get(c, fw)
                ).values
                X_test["category_target_enc"] = cats_test.map(
                    lambda c, fe=fold_enc, fw=fold_wr: fe.get(c, fw)
                ).values

            # ── 3. NESTED NULL IMPORTANCE ───────────────────────────
            # Feature selection using only purged training data
            keep_features = self.null_importance_test(X_train, y_train,
                                                      n_iterations=50)
            fold_features_log.append(set(keep_features))
            X_train_filtered = X_train[keep_features].values
            X_test_filtered = X_test[keep_features].values

            # ── 4. NESTED SCALING ───────────────────────────────────
            fold_scaler = StandardScaler()
            X_train_scaled = fold_scaler.fit_transform(X_train_filtered)
            X_test_scaled = fold_scaler.transform(X_test_filtered)

            # ── 5. MODEL FIT ────────────────────────────────────────
            try:
                m = model_factory()
                m.fit(X_train_scaled, y_train.values)
                proba = m.predict_proba(X_test_scaled)[:, 1]
                oos_probs[start:end] = proba
                briers.append(brier_score_loss(y_test, proba))
                if len(np.unique(y_test)) > 1:
                    aucs.append(roc_auc_score(y_test, proba))
                folds_completed += 1
            except (ValueError, np.linalg.LinAlgError) as e:
                log.warning(f"[ML] CV fold {folds_attempted} failed: {e}")

            start = end

        # Log feature stability across folds via Jaccard similarity
        # (quant review, March 2026). Jaccard < 0.50 for 3 consecutive days
        # = halt shadow model and review data feed.
        jaccard = 0.0
        if len(fold_features_log) >= 2:
            all_sets = fold_features_log
            intersection = set.intersection(*all_sets)
            union = set.union(*all_sets)
            jaccard = len(intersection) / len(union) if union else 0.0
            log.info(f"[ML] Feature Jaccard stability across {len(all_sets)} folds: "
                     f"{jaccard:.2f} ({len(intersection)}/{len(union)} features "
                     f"selected in all folds)")
            if jaccard < 0.5:
                log.warning("[ML] Low feature stability (Jaccard < 0.50) — signals may be "
                            "statistical mirages. Review per-fold feature logs.")

        if folds_completed == 0:
            log.error(f"[ML] CV completed with ZERO valid folds out of "
                      f"{folds_attempted} attempted. PiT purge may be too "
                      f"aggressive or all folds raised exceptions.")

        return {
            "brier_mean": np.mean(briers) if briers else 1.0,
            "brier_std": np.std(briers) if briers else 0.0,
            "auc_mean": np.mean(aucs) if aucs else 0.5,
            "auc_std": np.std(aucs) if aucs else 0.0,
            "n_folds": folds_completed,
            "n_folds_attempted": folds_attempted,
            "oos_probs": oos_probs,
            "jaccard_stability": jaccard,
            "fold_features": fold_features_log,
        }

    # ── Training ──────────────────────────────────────────────────────

    def train(self, min_samples: int = 100, min_opened_at: float | None = None) -> dict:
        """Train tiered model pipeline.

        Args:
            min_samples: Minimum settled trades to proceed with training.
            min_opened_at: If provided, only train on trades opened after this
                epoch timestamp. Critical for post-penalty regime isolation.

        Returns metrics dict with model_type, brier, auc, feature_importance, etc.
        All comparison metrics use ONLY held-out CV predictions with PiT purging.
        """
        X, y, meta = self.extract_training_data(min_opened_at=min_opened_at)

        if len(y) < min_samples:
            msg = f"Not enough samples ({len(y)} < {min_samples})"
            log.warning(f"[ML] {msg}")
            return {"error": msg, "n_samples": len(y)}

        log.info(f"[ML] Training on {len(y)} samples "
                 f"(win rate: {y.mean():.1%})")

        # Step 1: Model factories (CV handles feature selection internally)
        # Sprint 13b: Pure L2 (Ridge) regularization.
        # Elastic Net (L1 component) was amputating the score_x_implied_prob
        # interaction term due to collinearity with entry_price_cents. L2
        # distributes weight proportionally across correlated features,
        # allowing the model to learn the conditional slope that breaks
        # the U-shape (high score + low price = genuine anomaly, high
        # score + high price = noise). See quant audit April 12, 2026.
        def lasso_factory():
            return LogisticRegression(
                solver="lbfgs", penalty="l2", C=0.5,
                max_iter=3000, random_state=42, class_weight="balanced",
            )

        def gbm_factory():
            return GradientBoostingClassifier(
                n_estimators=80, max_depth=3, learning_rate=0.05,
                min_samples_leaf=10, subsample=0.8, max_features=0.8,
                random_state=42,
            )

        # Step 2: Tier 1 — Lasso (PiT-purged CV with nested feature selection)
        lasso_cv = self._time_series_cv(X, y, meta, lasso_factory)
        log.info(f"[ML] Lasso CV: Brier={lasso_cv['brier_mean']:.4f}±{lasso_cv['brier_std']:.4f}, "
                 f"AUC={lasso_cv['auc_mean']:.3f}±{lasso_cv['auc_std']:.3f} "
                 f"({lasso_cv['n_folds']}/{lasso_cv.get('n_folds_attempted', '?')} folds)")

        # Refuse to proceed if CV produced zero valid folds
        if lasso_cv["n_folds"] == 0:
            msg = (f"CV completed with zero valid folds "
                   f"({lasso_cv.get('n_folds_attempted', 0)} attempted). "
                   f"PiT purge may be too aggressive for N={len(y)}.")
            log.error(f"[ML] {msg}")
            return {"error": msg, "n_samples": len(y)}

        # Step 3: Tier 2 — GradientBoosting (only if enough data)
        gb_cv = {"brier_mean": 1.0, "auc_mean": 0.5, "n_folds": 0,
                 "oos_probs": np.full(len(y), np.nan)}
        if len(y) >= 1000:
            gb_cv = self._time_series_cv(X, y, meta, gbm_factory)
            log.info(f"[ML] GBM CV:   Brier={gb_cv['brier_mean']:.4f}±{gb_cv['brier_std']:.4f}, "
                     f"AUC={gb_cv['auc_mean']:.3f}±{gb_cv['auc_std']:.3f} "
                     f"({gb_cv['n_folds']}/{gb_cv.get('n_folds_attempted', '?')} folds)")

        # Step 4: Pick winner by Brier Score
        if gb_cv["brier_mean"] < lasso_cv["brier_mean"] - 0.005:
            log.info("[ML] Winner: GradientBoosting (lower Brier)")
            base_model = gbm_factory()
            winner_cv = gb_cv
            model_type = "gradient_boosting"
        else:
            log.info("[ML] Winner: Lasso (GBM didn't beat it)")
            base_model = lasso_factory()
            winner_cv = lasso_cv
            model_type = "lasso"

        # Step 5: Final feature selection on all data (for production model)
        # Apply global category encoding BEFORE feature selection so the
        # null importance test evaluates real per-category values, not
        # the constant placeholder (which would always be dropped as zero-variance).
        X_for_selection = X.copy()
        if "category_target_enc" in X_for_selection.columns:
            X_for_selection["category_target_enc"] = meta["category"].map(
                lambda c: self._category_encoding.get(c, self._global_win_rate)
            )
        keep_features = self.null_importance_test(X_for_selection, y)
        X_filtered = X_for_selection[keep_features].copy()

        # Step 6: Train final model on all data
        # Global scaler fit on ALL data is correct here — this is the production
        # model, not CV evaluation. Per-fold scaling above was for unbiased metrics.
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X_filtered)

        # Platt scaling with TimeSeriesSplit (fixes Critical #3: no random shuffle)
        if len(y) >= MIN_N_FOR_CALIBRATION:
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.model_selection import TimeSeriesSplit
            tscv = TimeSeriesSplit(n_splits=5)
            final_model = CalibratedClassifierCV(base_model, method="sigmoid", cv=tscv)
            log.info(f"[ML] N={len(y)} >= {MIN_N_FOR_CALIBRATION}: applying "
                     f"Time-Series Platt calibration (5 splits)")
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
        oos_wins = int(y.values[oos_edge_pos].sum()) if oos_n > 0 else 0
        oos_pnl = float(meta["pnl_cents"].values[oos_edge_pos].sum()) if oos_n > 0 else 0.0
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
            # Structural stability (quant review, March 2026)
            "jaccard_stability": winner_cv.get("jaccard_stability", 0.0),
            "l1_ratio": 0.0,  # Pure Ridge (L2) — Sprint 13b
            # OOS-only edge metrics (fixes CRO flaw #1)
            "oos_edge_trades": oos_n,
            "oos_edge_wins": oos_wins,
            "oos_edge_win_rate": oos_wr,
            "oos_edge_win_rate_ci_95": [oos_wr_lo, oos_wr_hi],
            "oos_edge_pnl_cents": oos_pnl,
            "oos_total_held_out": int(oos_mask.sum()),
        }

        # ── Monotonicity validation ────────────────────────────────────
        # Bin OOS predicted probabilities into deciles and check that
        # actual win rate increases monotonically. If the U-shape persists
        # after adding interaction terms, the feature space is mis-specified.
        mono_bins = []
        if oos_mask.sum() >= 50:
            oos_p = oos_probs[oos_mask]
            oos_y = y.values[oos_mask]
            try:
                n_bins = min(10, max(3, int(oos_mask.sum() / 15)))
                bin_edges = np.percentile(oos_p, np.linspace(0, 100, n_bins + 1))
                bin_edges = np.unique(bin_edges)  # deduplicate tied edges
                for i in range(len(bin_edges) - 1):
                    lo, hi = bin_edges[i], bin_edges[i + 1]
                    if i == len(bin_edges) - 2:
                        mask = (oos_p >= lo) & (oos_p <= hi)
                    else:
                        mask = (oos_p >= lo) & (oos_p < hi)
                    n_bin = mask.sum()
                    if n_bin > 0:
                        wr_bin = oos_y[mask].mean()
                        mono_bins.append({
                            "range": f"[{lo:.3f},{hi:.3f})",
                            "n": int(n_bin),
                            "win_rate": float(wr_bin),
                        })

                # Check monotonicity: each bin's WR should be >= previous
                wrs = [b["win_rate"] for b in mono_bins]
                inversions = sum(1 for i in range(1, len(wrs)) if wrs[i] < wrs[i-1])
                is_monotonic = inversions == 0
                metrics["monotonicity_bins"] = mono_bins
                metrics["monotonicity_inversions"] = inversions
                metrics["is_monotonic"] = is_monotonic

                log.info(f"[ML] Monotonicity check ({len(mono_bins)} bins, "
                         f"{inversions} inversions):")
                for b in mono_bins:
                    log.info(f"  {b['range']:>18s}  N={b['n']:>4d}  "
                             f"WR={b['win_rate']:.1%}")
                if is_monotonic:
                    log.info("[ML] ✓ Predicted probabilities are monotonic with actual WR")
                else:
                    log.warning(f"[ML] ✗ {inversions} monotonicity inversions — "
                                f"U-shape may persist. Do NOT lower CRITICAL threshold.")
            except Exception as e:
                log.warning(f"[ML] Monotonicity check failed: {e}")

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

    # ── Target Residualization (Phase 2 — Sprint 13b) ────────────────

    def train_residual(
        self,
        min_samples: int = 100,
        min_opened_at: float | None = None,
    ) -> dict:
        """Train a Ridge regression model to predict alpha residuals.

        Instead of predicting binary outcomes (Win=1, Loss=0), predicts the
        residual: y_residual = outcome - implied_probability. This strips the
        base rate (entry_price) of its dominant importance, forcing the model
        to find edge in the anomaly features alone.

        Positive residual = positive expected value (alpha).
        Negative residual = negative expected value (adverse selection).

        This is the Phase 2 architecture, ready to deploy if the classification
        approach fails its monotonicity check at N=500.

        Returns metrics dict compatible with train() output format.
        """
        from sklearn.linear_model import RidgeCV
        from sklearn.model_selection import TimeSeriesSplit
        from sklearn.metrics import mean_squared_error

        X, y_binary, meta = self.extract_training_data(min_opened_at=min_opened_at)

        if len(y_binary) < min_samples:
            msg = f"Not enough samples ({len(y_binary)} < {min_samples})"
            log.warning(f"[ML-RESIDUAL] {msg}")
            return {"error": msg, "n_samples": len(y_binary)}

        # Transform target: residual = outcome - implied_probability
        # Win at 30¢ → residual = +0.70 (huge alpha)
        # Loss at 30¢ → residual = -0.30 (expected loss)
        # Win at 80¢ → residual = +0.20 (small alpha)
        # Loss at 80¢ → residual = -0.80 (huge adverse selection)
        implied_probs = meta["entry_price"].values / 100.0
        y_residual = y_binary.values - implied_probs

        log.info(f"[ML-RESIDUAL] Training on {len(y_residual)} samples")
        log.info(f"[ML-RESIDUAL] Residual stats: mean={y_residual.mean():+.4f}, "
                 f"std={y_residual.std():.4f}, "
                 f"range=[{y_residual.min():+.3f}, {y_residual.max():+.3f}]")

        # Step 0: Orthogonalize features against price.
        # Raw features are mechanically correlated with price (thin books
        # produce extreme skew/sweep). This confound causes Ridge to squash
        # all features when predicting the residual. Orthogonalization
        # removes the price-correlated component, isolating pure signal.
        ortho = FeatureOrthogonalizer()
        X_orth = ortho.fit_transform(X, price_col="entry_price_cents")
        log.info(f"[ML-RESIDUAL] Orthogonalized {len(ortho._models)} features against price")

        # Null importance test — Ridge regression version for continuous target.
        # Cannot reuse null_importance_test() which uses LogisticRegression.
        from sklearn.linear_model import Ridge
        log.info("[ML-RESIDUAL] Running null importance test (50 iterations)...")
        scaler_null = StandardScaler()
        X_scaled_null = scaler_null.fit_transform(X_orth)
        model_real = Ridge(alpha=1.0)
        model_real.fit(X_scaled_null, y_residual)
        real_importance = np.abs(model_real.coef_)

        n_null_iter = 50
        null_importances = np.zeros((n_null_iter, X.shape[1]))
        rng = np.random.RandomState(42)
        for i in range(n_null_iter):
            y_shuf = y_residual.copy()
            rng.shuffle(y_shuf)
            m_null = Ridge(alpha=1.0)
            m_null.fit(X_scaled_null, y_shuf)
            null_importances[i] = np.abs(m_null.coef_)

        p95 = np.percentile(null_importances, 95, axis=0)
        keep_features = []
        for j, fname in enumerate(X_orth.columns):
            passed = real_importance[j] > p95[j]
            status = "KEEP" if passed else "DROP"
            log.info(f"  {fname:30s}  real={real_importance[j]:.4f}  "
                     f"null_p95={p95[j]:.4f}  → {status}")
            if passed:
                keep_features.append(fname)

        if len(keep_features) < 1:
            msg = "No features survived null importance test for residual model"
            log.warning(f"[ML-RESIDUAL] {msg}")
            return {"error": msg, "n_samples": len(y_residual)}

        X_filtered = X_orth[keep_features]
        log.info(f"[ML-RESIDUAL] Features kept: {len(keep_features)}/{len(X_orth.columns)}: "
                 f"{keep_features}")

        # Time-series CV for Ridge regression
        scaler = StandardScaler()
        tscv = TimeSeriesSplit(n_splits=5)
        oos_preds = np.full(len(y_residual), np.nan)
        fold_rmses = []

        for fold_idx, (train_idx, test_idx) in enumerate(tscv.split(X_filtered)):
            X_train = X_filtered.iloc[train_idx]
            y_train = y_residual[train_idx]
            X_test = X_filtered.iloc[test_idx]
            y_test = y_residual[test_idx]

            X_train_s = scaler.fit_transform(X_train)
            X_test_s = scaler.transform(X_test)

            model = RidgeCV(
                alphas=np.logspace(-3, 2, 50),
                scoring="neg_mean_squared_error",
                cv=3,
            )
            model.fit(X_train_s, y_train)
            preds = model.predict(X_test_s)
            oos_preds[test_idx] = preds

            rmse = np.sqrt(mean_squared_error(y_test, preds))
            fold_rmses.append(rmse)
            log.info(f"[ML-RESIDUAL] Fold {fold_idx}: RMSE={rmse:.4f}, "
                     f"alpha={model.alpha_:.4f}, N_train={len(train_idx)}, "
                     f"N_test={len(test_idx)}")

        oos_mask = ~np.isnan(oos_preds)
        if oos_mask.sum() < 20:
            msg = f"Too few OOS predictions ({oos_mask.sum()}) for evaluation"
            log.warning(f"[ML-RESIDUAL] {msg}")
            return {"error": msg}

        oos_p = oos_preds[oos_mask]
        oos_y = y_residual[oos_mask]
        oos_pnl = meta["pnl_cents"].values[oos_mask]

        # Rank correlation: do higher predicted residuals correspond to better outcomes?
        from scipy import stats as sp_stats
        rho, p_val = sp_stats.spearmanr(oos_p, oos_y)

        # Decile P&L: rank by predicted residual, check if top deciles profit
        n_bins = min(5, max(3, int(oos_mask.sum() / 20)))
        edges = np.percentile(oos_p, np.linspace(0, 100, n_bins + 1))
        edges = np.unique(edges)

        decile_results = []
        log.info(f"[ML-RESIDUAL] OOS Residual Decile Analysis ({len(edges)-1} bins):")
        for i in range(len(edges) - 1):
            lo, hi = edges[i], edges[i + 1]
            if i == len(edges) - 2:
                mask = (oos_p >= lo) & (oos_p <= hi)
            else:
                mask = (oos_p >= lo) & (oos_p < hi)
            n_bin = mask.sum()
            if n_bin == 0:
                continue
            avg_residual = oos_y[mask].mean()
            total_pnl = oos_pnl[mask].sum()
            avg_pnl = oos_pnl[mask].mean()
            decile_results.append({
                "range": f"[{lo:+.3f}, {hi:+.3f})",
                "n": int(n_bin),
                "avg_residual": float(avg_residual),
                "total_pnl": float(total_pnl),
                "avg_pnl": float(avg_pnl),
            })
            log.info(f"  {decile_results[-1]['range']:>20s}  N={n_bin:>4d}  "
                     f"AvgResidual={avg_residual:+.4f}  "
                     f"ΣP&L={total_pnl:+.0f}¢  AvgP&L={avg_pnl:+.1f}¢")

        # Check if top bucket has positive P&L (the critical validation)
        top_bucket_pnl = decile_results[-1]["total_pnl"] if decile_results else 0
        bottom_bucket_pnl = decile_results[0]["total_pnl"] if decile_results else 0

        metrics = {
            "model_type": "ridge_residual",
            "n_samples": len(y_residual),
            "features_kept": keep_features,
            "n_features": len(keep_features),
            "rmse_mean": float(np.mean(fold_rmses)),
            "rmse_std": float(np.std(fold_rmses)),
            "spearman_rho": float(rho),
            "spearman_pval": float(p_val),
            "spearman_significant": p_val < 0.05,
            "oos_total": int(oos_mask.sum()),
            "decile_results": decile_results,
            "top_bucket_pnl": float(top_bucket_pnl),
            "bottom_bucket_pnl": float(bottom_bucket_pnl),
            "spread": float(top_bucket_pnl - bottom_bucket_pnl),
        }

        log.info(f"[ML-RESIDUAL] RMSE: {metrics['rmse_mean']:.4f} ± {metrics['rmse_std']:.4f}")
        log.info(f"[ML-RESIDUAL] Spearman ρ={rho:+.4f} (p={p_val:.4f})")

        if metrics["spearman_significant"]:
            log.info("[ML-RESIDUAL] ✓ Predicted residuals rank-correlate with actual residuals")
        else:
            log.warning("[ML-RESIDUAL] ✗ Predicted residuals have NO significant "
                        "rank correlation with actual residuals")

        if top_bucket_pnl > 0 and bottom_bucket_pnl < 0:
            log.info(f"[ML-RESIDUAL] ✓ Top bucket +{top_bucket_pnl:.0f}¢, "
                     f"bottom bucket {bottom_bucket_pnl:.0f}¢ — "
                     f"spread={metrics['spread']:.0f}¢")
        else:
            log.warning(f"[ML-RESIDUAL] ✗ Top/bottom bucket P&L not monotonic — "
                        f"residual model lacks discriminative power")

        return metrics

    # ── Prediction ────────────────────────────────────────────────────

    def predict(self, features: dict, entry_price: int,
                taker_side: str = "") -> float:
        """Predict trade edge = P(win) - market_implied_probability.

        NOTE: At N < 500, probabilities are NOT calibrated (Platt scaling disabled).
        The edge score is ordinal (useful for ranking) but NOT cardinal (don't
        interpret magnitude as true probability difference).

        Args:
            features: Feature dict from FeatureEngine.compute()
            entry_price: Entry price in cents (1-99)
            taker_side: "yes" or "no" — for side_is_yes feature (default "" → 0.5)

        Returns:
            Edge score (positive = favorable). -999 if model not ready.
        """
        if not self.is_ready:
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
                    if taker_side:
                        raw[fname] = 1.0 if taker_side == "yes" else 0.0
                    else:
                        raw[fname] = 0.5  # Fallback if side unknown
                elif fname == "category_target_enc":
                    raw[fname] = self._global_win_rate
                elif fname == "score_x_implied_prob":
                    # Interaction: composite_score × implied_probability
                    # features["composite"] is set before predict() is called
                    # (diamond_features.py line 523). This is the same post-penalty
                    # anomaly_score stored in paper_trades at training time.
                    composite = float(features.get("composite", 0.0))
                    raw[fname] = composite * (float(entry_price) / 100.0)
                elif fname == "is_longshot_yes":
                    is_yes = taker_side == "yes" if taker_side else False
                    raw[fname] = 1.0 if (entry_price < 30 and is_yes) else 0.0
                elif fname == "is_favorite_no":
                    is_no = taker_side == "no" if taker_side else False
                    raw[fname] = 1.0 if (entry_price > 70 and is_no) else 0.0
                elif fname == "is_mid_yes_spike":
                    is_yes = taker_side == "yes" if taker_side else False
                    vol_spike = float(features.get("volume_spike_ratio", 0))
                    raw[fname] = 1.0 if (30 <= entry_price <= 60 and is_yes and vol_spike > 0.8) else 0.0
                else:
                    raw[fname] = 0.0

            X = np.array([[raw[f] for f in self._feature_names]])
            X_scaled = self._scaler.transform(X)
            prob_win = self._model.predict_proba(X_scaled)[0, 1]

            market_implied = entry_price / 100.0
            edge = prob_win - market_implied

            return float(edge)

        except Exception as e:
            log.error(f"[ML] Prediction failed: {e}", exc_info=True)
            return -999.0

    # ── Comparison (OOS-only — fixes CRO flaw #1) ────────────────────

    def compare_with_handtuned(self) -> dict:
        """Compare ML edge scorer vs hand-tuned composite.

        CRITICAL: All ML metrics use ONLY held-out CV fold predictions with
        Point-in-Time purging and nested feature selection. The final model
        (trained on all data) is NEVER used for comparison.
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

        # ML: re-run PiT-purged CV to get held-out predictions
        def model_factory():
            if self._model_type == "gradient_boosting":
                return GradientBoostingClassifier(
                    n_estimators=80, max_depth=3, learning_rate=0.05,
                    min_samples_leaf=10, subsample=0.8, max_features=0.8,
                    random_state=42,
                )
            return LogisticRegression(
                solver="lbfgs", penalty="l2", C=0.5,
                max_iter=3000, random_state=42, class_weight="balanced",
            )

        cv_result = self._time_series_cv(X, y, meta, model_factory)
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
                wins = int(y.values[mask].sum())
                pnl = float(meta["pnl_cents"].values[mask].sum())
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

    # ── Shadow Model Evaluation (Quant Review, March 2026) ───────────

    def evaluate_shadow_model(
        self,
        y_true: np.ndarray,
        y_pred_shadow: np.ndarray,
        current_active_features: set,
        new_shadow_features: set,
    ) -> dict:
        """Evaluate shadow model's calibration and structural stability.

        To be run daily before manual promotion of ML_SCORER_ACTIVE.
        Checks both predictive quality (Brier score) and structural
        stability (Jaccard similarity of active feature set).

        Quant review: tracking Brier alone is insufficient. Feature set
        turnover is the canary for concept drift whipsawing — where a
        temporary market anomaly causes the Elastic Net to zero out
        historically robust features.

        Args:
            y_true: Actual outcomes (0/1 array from settled trades).
            y_pred_shadow: Shadow model predicted probabilities.
            current_active_features: Feature set from current production model.
            new_shadow_features: Feature set from newly trained shadow model.

        Returns:
            Dict with brier_score, jaccard_similarity, features_dropped,
            features_added, warnings, and promotion_eligible flag.
        """
        brier = brier_score_loss(y_true, y_pred_shadow)

        # Jaccard similarity: measures feature set stability across retrains
        intersection = current_active_features & new_shadow_features
        union = current_active_features | new_shadow_features
        jaccard = len(intersection) / len(union) if union else 0.0

        features_dropped = current_active_features - new_shadow_features
        features_added = new_shadow_features - current_active_features

        warnings = []
        if jaccard < 0.70:
            warnings.append(
                f"High feature turnover (Jaccard: {jaccard:.2f}). "
                f"Model may be fitting to noise. "
                f"Dropped: {features_dropped}, Added: {features_added}"
            )
        if brier > 0.25:
            warnings.append(f"Poor calibration (Brier: {brier:.4f}).")

        # Promotion gate: eligible only if both calibration and stability pass
        promotion_eligible = brier < 0.25 and jaccard >= 0.70

        for w in warnings:
            log.warning(f"[ML-SHADOW] {w}")

        if promotion_eligible:
            log.info(
                f"[ML-SHADOW] Model eligible for promotion: "
                f"Brier={brier:.4f}, Jaccard={jaccard:.2f}"
            )
        else:
            log.info(
                f"[ML-SHADOW] Model NOT eligible for promotion: "
                f"Brier={brier:.4f}, Jaccard={jaccard:.2f}"
            )

        return {
            "brier_score": brier,
            "jaccard_similarity": jaccard,
            "features_dropped": features_dropped,
            "features_added": features_added,
            "warnings": warnings,
            "promotion_eligible": promotion_eligible,
        }
