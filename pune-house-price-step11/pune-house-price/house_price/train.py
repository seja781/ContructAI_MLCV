"""Step 6: train 4 models and compare them with 5-fold cross-validation on the training rows.

    python -m house_price.train

Reads   data/split/train.csv           (test.csv is NOT used here; it is kept for step 8)
Writes  reports/models/model_comparison.md + .csv + chart

Models (all predict log(price); errors are reported in INR lakh after converting back):
  0. Baseline       similar-listing median INR/sqft x area (rule-based comparable listings)
  1. Ridge          linear regression: one weight per input, added up            (step 6b)
  2. Random forest  200 decision trees built independently, answers averaged     (step 6a)
  3. LightGBM       600 small trees built one after another, each fixing the
                    previous trees' mistakes (gradient boosting)                 (step 6b)

Cross-validation: near-identical listings (same locality, BHK, property type and 25 sqft area band)
stay in the same fold. Locality price levels are learned from each training fold only.
"""
from __future__ import annotations

import json
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import sklearn
from lightgbm import LGBMRegressor
from sklearn.base import BaseEstimator, RegressorMixin, clone
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.model_selection import KFold, StratifiedGroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, OrdinalEncoder, StandardScaler, TargetEncoder

from .features import CATEGORICAL_FEATURES, NUMERIC_FEATURES, TARGET
from .paths import CONFIG_FILE, PROJECT_ROOT
from .split import TRAIN_FILE

FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES
BASELINE_FEATURES = ["locality", *FEATURES]
REPORT_DIR = PROJECT_ROOT / "reports" / "models"


def load_settings() -> dict:
    s = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("models")
    if not s:
        raise ValueError("data/config.json has no 'models' section")
    return s


class LocalityRateBaseline(BaseEstimator, RegressorMixin):
    """Rule-based price from similar listings, with locality/BHK and city fallback rates."""

    def __init__(self, area_bandwidth: float = 0.08, prior_strength: float = 5.0):
        self.area_bandwidth = area_bandwidth
        self.prior_strength = prior_strength

    def fit(self, X, y):
        rate = np.exp(y) / np.exp(X["log_area"])
        listings = pd.DataFrame({
            "locality": X["locality"].to_numpy(),
            "bedrooms": X["bedrooms"].to_numpy(),
            "property_type": X["property_type"].to_numpy(),
            "log_area": X["log_area"].to_numpy(),
            "rate": rate.to_numpy(),
        })
        self.comparables_ = {
            key: (group["log_area"].to_numpy(), group["rate"].to_numpy())
            for key, group in listings.groupby(["locality", "bedrooms", "property_type"], sort=False)
        }
        self.locality_bedroom_rates_ = listings.groupby(["locality", "bedrooms"])["rate"].median().to_dict()
        self.locality_rates_ = listings.groupby("locality")["rate"].median().to_dict()
        self.city_rate_ = float(listings["rate"].median())
        return self

    def predict(self, X):
        predicted_rates = np.empty(len(X), dtype=float)
        queries = pd.DataFrame({
            "locality": X["locality"].to_numpy(),
            "bedrooms": X["bedrooms"].to_numpy(),
            "property_type": X["property_type"].to_numpy(),
            "log_area": X["log_area"].to_numpy(),
        }, index=np.arange(len(X)))
        for key, indices in queries.groupby(
            ["locality", "bedrooms", "property_type"], sort=False
        ).groups.items():
            idx = np.asarray(list(indices))
            area = queries.loc[idx, "log_area"].to_numpy()
            local_prior = self.locality_bedroom_rates_.get(
                key[:2], self.locality_rates_.get(key[0], self.city_rate_)
            )
            comparable = self.comparables_.get(key)
            if comparable is None:
                predicted_rates[idx] = local_prior
                continue

            comparable_area, comparable_rates = comparable
            weights = np.exp(-0.5 * ((area[:, None] - comparable_area[None, :]) / self.area_bandwidth) ** 2)
            order = np.argsort(comparable_rates)
            sorted_weights = weights[:, order]
            cumulative_weights = np.cumsum(sorted_weights, axis=1)
            median_indices = (cumulative_weights >= cumulative_weights[:, -1, None] / 2).argmax(axis=1)
            local_medians = comparable_rates[order[median_indices]]
            total_weight = weights.sum(axis=1)
            denominator = total_weight + self.prior_strength
            predicted_rates[idx] = np.divide(
                total_weight * local_medians + self.prior_strength * local_prior,
                denominator,
                out=np.full_like(total_weight, local_prior),
                where=denominator > 0,
            )
        return np.log(predicted_rates * np.exp(X["log_area"].to_numpy()))


class PriceBlendRegressor(BaseEstimator, RegressorMixin):
    """Blend boosted-tree and random-forest predictions on the real-price scale."""

    def __init__(self, lightgbm, forest, lightgbm_weight: float = 0.7):
        self.lightgbm = lightgbm
        self.forest = forest
        self.lightgbm_weight = lightgbm_weight

    def fit(self, X, y, model__categorical_feature=None):
        categories = model__categorical_feature
        if categories is None:
            categories = list(range(len(NUMERIC_FEATURES), len(FEATURES)))
        fit_params = {"model__categorical_feature": categories}
        self.lightgbm_ = clone(self.lightgbm).fit(X, y, **fit_params)
        self.forest_ = clone(self.forest).fit(X, y)
        return self

    def predict(self, X):
        lightgbm_price = np.exp(self.lightgbm_.predict(X))
        forest_price = np.exp(self.forest_.predict(X))
        price = self.lightgbm_weight * lightgbm_price + (1 - self.lightgbm_weight) * forest_price
        return np.log(price)


def _target_encoder(seed: int) -> TargetEncoder:
    """Locality -> its average log(price), learned with internal 5-fold cross-fitting (no leakage).
    Newer scikit-learn takes the shuffling as cv=KFold(...); older versions take shuffle/random_state."""
    major, minor = (int(x) for x in sklearn.__version__.split(".")[:2])
    if (major, minor) >= (1, 9):
        return TargetEncoder(target_type="continuous", cv=KFold(5, shuffle=True, random_state=seed))
    return TargetEncoder(target_type="continuous", random_state=seed)


def build_models(s: dict) -> dict:
    seed = s["random_state"]

    # Ridge: fill empty numbers with the median (+ "was missing" flags) and put all numbers on the same
    # scale; each locality / property type becomes its own 0/1 column ("one-hot"), so it gets its own weight
    ridge = Pipeline([
        ("prep", ColumnTransformer([
            ("num", Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True)),
                              ("scale", StandardScaler())]), NUMERIC_FEATURES),
            ("cat", OneHotEncoder(handle_unknown="ignore"), CATEGORICAL_FEATURES)])),
        ("model", Ridge(**s["ridge"]))])

    # Random forest: fill empty numbers with the median (+ "was missing" flags),
    # turn locality / property type into their average log(price), learned without leakage
    forest = Pipeline([
        ("prep", ColumnTransformer([
            ("num", SimpleImputer(strategy="median", add_indicator=True), NUMERIC_FEATURES),
            ("cat", _target_encoder(seed), CATEGORICAL_FEATURES)])),
        ("model", RandomForestRegressor(**s["random_forest"], n_jobs=1, random_state=seed))])

    # LightGBM: handles empty values by itself, so numbers pass straight through; locality / property
    # type are turned into category numbers and LightGBM is told they are categories (see FIT_PARAMS)
    lgbm = Pipeline([
        ("prep", ColumnTransformer([
            ("num", "passthrough", NUMERIC_FEATURES),
            ("cat", OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1), CATEGORICAL_FEATURES)])),
        ("model", LGBMRegressor(**s["lightgbm"], random_state=seed, verbose=-1))])

    return {"Baseline (similar-listing rate x area)": LocalityRateBaseline(), "Ridge regression": ridge,
            "Random forest": forest, "LightGBM": lgbm}


# extra arguments for .fit() per model: tell LightGBM which columns are categories (the last two)
FIT_PARAMS: dict = {"LightGBM": {"model__categorical_feature": list(range(len(NUMERIC_FEATURES), len(FEATURES)))}}


def scores(y_log_true, y_log_pred) -> dict:
    """Errors on the real price scale (INR lakh)."""
    true, pred = np.exp(y_log_true), np.exp(y_log_pred)
    err = np.abs(pred - true)
    return {"MAE (lakh)": float(err.mean()), "RMSE (lakh)": float(np.sqrt(np.mean((pred - true) ** 2))),
            "MAPE (%)": float((err / true).mean() * 100),
            "Median APE (%)": float(np.median(err / true) * 100),
            "R2": float(1 - ((true - pred) ** 2).sum() / ((true - true.mean()) ** 2).sum()),
            "Within 10% (%)": float((err / true <= 0.10).mean() * 100),
            "Within 20% (%)": float((err / true <= 0.20).mean() * 100)}


def _validation_groups(X: pd.DataFrame) -> np.ndarray:
    locality = X["locality"] if "locality" in X else X["locality_group"]
    keys = pd.DataFrame({
        "locality": locality.to_numpy(),
        "bedrooms": X["bedrooms"].to_numpy(),
        "property_type": X["property_type"].to_numpy(),
        "area_25_sqft_band": np.floor(np.exp(X["log_area"].to_numpy()) / 25).astype(np.int64),
    })
    return pd.MultiIndex.from_frame(keys).factorize()[0]


def cross_validate(models: dict, X: pd.DataFrame, y: pd.Series, s: dict, verbose=True) -> pd.DataFrame:
    groups = _validation_groups(X)
    price_bins = pd.qcut(y, q=10, labels=False, duplicates="drop")
    folds = StratifiedGroupKFold(n_splits=s["cv_folds"], shuffle=True, random_state=s["random_state"])
    rows = []
    for name, model in models.items():
        t0, fold_scores = time.time(), []
        for train_idx, valid_idx in folds.split(X, price_bins, groups):
            m = clone(model).fit(X.iloc[train_idx], y.iloc[train_idx], **FIT_PARAMS.get(name, {}))
            fold_scores.append(scores(y.iloc[valid_idx], m.predict(X.iloc[valid_idx])))
        f = pd.DataFrame(fold_scores)
        row = {"Model": name, **f.mean().round(2).to_dict(), "MAPE spread (+/-)": round(float(f["MAPE (%)"].std()), 2),
               "MAE spread (+/-)": round(float(f["MAE (lakh)"].std()), 2),
               "RMSE spread (+/-)": round(float(f["RMSE (lakh)"].std()), 2),
               "Seconds": round(time.time() - t0, 1)}
        rows.append(row)
        if verbose:
            print(f"  {name:<32} MAE/RMSE {row['MAE (lakh)']:6.2f}/{row['RMSE (lakh)']:6.2f} lakh  "
                  f"MAPE {row['MAPE (%)']:5.1f}%  R2 {row['R2']:.3f}  "
                  f"within 20% {row['Within 20% (%)']:.1f}%  ({row['Seconds']} s)")
    return pd.DataFrame(rows)


def run() -> pd.DataFrame:
    s = load_settings()
    train = pd.read_csv(TRAIN_FILE)
    X, y = train[BASELINE_FEATURES], train[TARGET]
    print(f"Training rows: {len(train):,} | features: {len(FEATURES)} | {s['cv_folds']}-fold cross-validation\n")
    results = cross_validate(build_models(s), X, y, s)
    _report(results, len(train), s)
    return results


def _report(r: pd.DataFrame, n: int, s: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    r.to_csv(REPORT_DIR / "model_comparison.csv", index=False)
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.6))
    best_i = r["MAPE (%)"].iloc[1:].idxmin()                        # best ML model (row 0 is the baseline)
    colors = ["#9e9e9e" if i == 0 else "#4f8a4f" if i == best_i else "#3b6ea5" for i in r.index]
    ax[0].barh(r.Model, r["MAPE (%)"], color=colors, xerr=r["MAPE spread (+/-)"])
    ax[0].set(title="Average % error (lower is better)", xlabel="MAPE %")
    ax[0].invert_yaxis()
    ax[1].barh(r.Model, r["Within 20% (%)"], color=colors)
    ax[1].set(title="Predictions within 20% of real price (higher is better)", xlabel="% of flats", xlim=(0, 100))
    ax[1].invert_yaxis()
    ax[1].set_yticklabels([])
    for a in ax:
        a.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "model_comparison.png", dpi=110)
    plt.close(fig)
    best = r.iloc[1:].sort_values("MAPE (%)").iloc[0]
    base = r.iloc[0]
    cols = [c for c in r.columns if c != "Model"]
    lines = ["# Step 6: model comparison", "",
             f"{n:,} training rows, {s['cv_folds']}-fold cross-validation (test rows not used).", "",
             "| Model | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols),
             *["| " + " | ".join(str(v) for v in row) + " |" for row in r.itertuples(index=False)], "",
             f"**Best: {best.Model}**: average error {best['MAPE (%)']}% vs {base['MAPE (%)']}% for the baseline "
             f"({base['MAPE (%)'] - best['MAPE (%)']:.1f} points better); "
             f"{best['Within 20% (%)']}% of flats predicted within 20%.", "",
             "![comparison](model_comparison.png)"]
    (REPORT_DIR / "model_comparison.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    res = run()
    print("\n" + res.drop(columns=["Seconds"]).to_string(index=False))
    best = res.iloc[1:].sort_values("MAPE (%)").iloc[0]
    print(f"\nBest model: {best.Model} ({best['MAPE (%)']}% average error vs {res.iloc[0]['MAPE (%)']}% for the baseline)")
    print(f"\nReport {REPORT_DIR / 'model_comparison.md'}")
