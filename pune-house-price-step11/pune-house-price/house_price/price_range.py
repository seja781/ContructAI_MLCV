"""Step 10: give a price RANGE (low - high) instead of one number.

    python -m house_price.price_range

Reads   data/split/train.csv, data/split/test.csv
        data/config.json -> models.lightgbm_tuned, price_range
Writes  reports/price_range/  price_range_report.md + 3 charts

How it works (conformalised quantile regression):
  1. Two LightGBM models with the tuned settings, but trained to predict a LOW price (10th percentile)
     and a HIGH price (90th percentile) instead of the typical price.
  2. Calibration on train.csv only: 5-fold cross-validation shows how often real prices fall outside
     [low, high]; the range is widened (or narrowed) by one fixed amount so that 80% fall inside.
  3. Check once on test.csv: what share of unseen flats fall inside their range, and how wide it is.
The typical price comes from the blended LightGBM/random-forest model used in step 8.
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.model_selection import KFold

from .evaluate import FINAL, all_models
from .paths import CONFIG_FILE, PROJECT_ROOT
from .split import TEST_FILE, TRAIN_FILE
from .train import FEATURES, FIT_PARAMS, TARGET, build_models

REPORT_DIR = PROJECT_ROOT / "reports" / "price_range"


def load_config() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if "price_range" not in cfg:
        raise ValueError("data/config.json has no 'price_range' section")
    if "lightgbm_tuned" not in cfg.get("models", {}):
        raise ValueError("config.json has no models.lightgbm_tuned - run step 7 first")
    return cfg


def quantile_model(m_cfg: dict, alpha: float):
    """Tuned LightGBM settings, but aiming at the alpha-quantile of log(price) instead of the middle."""
    tuned = {k: v for k, v in m_cfg["lightgbm_tuned"].items() if not k.startswith("_")}
    return build_models({**m_cfg, "lightgbm": {**tuned, "objective": "quantile", "alpha": alpha}})["LightGBM"]


def fit(model, X, y):
    return clone(model).fit(X, y, **FIT_PARAMS["LightGBM"])


def calibration_shift(m_cfg: dict, r: dict, X: pd.DataFrame, y: pd.Series) -> tuple[float, float]:
    """Cross-validated amount (in log-price) to widen [low, high] so that `coverage` of real prices fall inside.
    Returns (shift, coverage before calibration)."""
    lo_m, hi_m = quantile_model(m_cfg, r["low_quantile"]), quantile_model(m_cfg, r["high_quantile"])
    lo, hi = np.empty(len(y)), np.empty(len(y))
    folds = KFold(r["cv_folds"], shuffle=True, random_state=r["random_state"])
    for k, (a, b) in enumerate(folds.split(X), start=1):
        lo[b] = fit(lo_m, X.iloc[a], y.iloc[a]).predict(X.iloc[b])
        hi[b] = fit(hi_m, X.iloc[a], y.iloc[a]).predict(X.iloc[b])
        print(f"  calibration fold {k}/{r['cv_folds']} done")
    yv = y.to_numpy()
    raw_cover = float(np.mean((yv >= lo) & (yv <= hi)))
    # how far each real price is outside its range (negative = inside); widen by the coverage-quantile of that
    outside = np.maximum(lo - yv, yv - hi)
    n = len(yv)
    level = min(1.0, np.ceil((n + 1) * r["coverage"]) / n)
    return float(np.quantile(outside, level)), raw_cover


def price_range() -> dict:
    cfg = load_config()
    m_cfg, r = cfg["models"], cfg["price_range"]
    train, test = pd.read_csv(TRAIN_FILE), pd.read_csv(TEST_FILE)
    X_tr, y_tr, X_te = train[FEATURES], train[TARGET], test[FEATURES]
    print(f"Calibrating an {r['coverage']:.0%} price range on {len(train):,} training flats "
          f"({r['cv_folds']}-fold cross-validation)\n")

    shift, raw_cover = calibration_shift(m_cfg, r, X_tr, y_tr)
    print(f"\n  before calibration {raw_cover:.1%} of real prices fell inside the range -> "
          f"range {'widened' if shift > 0 else 'narrowed'} by {abs((np.exp(shift) - 1) * 100):.1f}% on each side\n")

    mid = np.exp(fit(all_models(m_cfg)[FINAL], X_tr, y_tr).predict(X_te))
    lo = np.exp(fit(quantile_model(m_cfg, r["low_quantile"]), X_tr, y_tr).predict(X_te) - shift)
    hi = np.exp(fit(quantile_model(m_cfg, r["high_quantile"]), X_tr, y_tr).predict(X_te) + shift)
    lo, hi = np.minimum(lo, mid), np.maximum(hi, mid)            # the typical price always sits inside its range

    out = test[["locality", "locality_group", "bedrooms", "price_lakh"]].copy()
    out["low_lakh"], out["predicted_lakh"], out["high_lakh"] = lo.round(1), mid.round(1), hi.round(1)
    out["inside"] = (out.price_lakh >= lo) & (out.price_lakh <= hi)
    out["width_pct"] = ((hi - lo) / mid * 100).round(1)
    out["below_pct"], out["above_pct"] = ((1 - lo / mid) * 100).round(1), ((hi / mid - 1) * 100).round(1)

    bands = cfg["evaluation"]["price_bands_lakh"]
    out["price_band"] = pd.cut(out.price_lakh, bands, labels=[f"{a}-{b}" for a, b in zip(bands[:-1], bands[1:])])
    by_band = out.groupby("price_band", observed=True).agg(
        test_flats=("inside", "size"), inside_pct=("inside", lambda s: round(s.mean() * 100, 1)),
        median_width_pct=("width_pct", "median"))
    common = out.locality_group.value_counts()
    by_loc = (out[out.locality_group.isin(common[common >= 50].index)]
              .groupby("locality_group").agg(test_flats=("inside", "size"),
                                             inside_pct=("inside", lambda s: round(s.mean() * 100, 1)),
                                             median_width_pct=("width_pct", "median"))
              .sort_values("median_width_pct"))

    s = {"target": r["coverage"], "raw_cover": raw_cover, "shift_pct": float((np.exp(shift) - 1) * 100),
         "test_cover": float(out.inside.mean()), "median_width": float(out.width_pct.median()),
         "median_below": float(out.below_pct.median()), "median_above": float(out.above_pct.median()),
         "by_band": by_band, "narrowest": by_loc.head(5), "widest": by_loc.tail(5), "examples": _examples(out)}
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    _charts(out, by_band, s)
    _report(s, len(train), len(test))
    return s


def _examples(out: pd.DataFrame) -> pd.DataFrame:
    """Three typical test flats: a 1, 2 and 3 BHK closest to the median price of their BHK."""
    rows = []
    for b in (1, 2, 3):
        g = out[out.bedrooms == b]
        rows.append(g.iloc[int((g.price_lakh - g.price_lakh.median()).abs().argmin())])
    return pd.DataFrame(rows)[["locality", "bedrooms", "price_lakh", "low_lakh", "predicted_lakh", "high_lakh", "inside"]]


def _charts(out: pd.DataFrame, by_band: pd.DataFrame, s: dict) -> None:
    smp = out.sample(80, random_state=1).sort_values("predicted_lakh").reset_index(drop=True)
    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.vlines(smp.index, smp.low_lakh, smp.high_lakh, color="#9ec1e6", lw=4, label="predicted range")
    ax.scatter(smp.index, smp.predicted_lakh, s=10, color="#3b6ea5", label="predicted price", zorder=3)
    ax.scatter(smp.index, smp.price_lakh, s=14, marker="x", zorder=4,
               color=np.where(smp.inside, "#2e7d32", "#c62828"), label="real price (green inside, red outside)")
    ax.set(yscale="log", title=f"80 random test flats: range vs real price ({s['test_cover']:.0%} of all test flats inside)",
           xlabel="test flats, sorted by predicted price", ylabel="INR lakh")
    ax.legend(fontsize=8, loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "01_ranges_sample.png", dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    ax.bar(by_band.index.astype(str), by_band.inside_pct, color="#3b6ea5")
    ax.axhline(s["target"] * 100, color="#c0504d", ls="--", lw=1, label=f"target {s['target']:.0%}")
    for i, v in enumerate(by_band.inside_pct):
        ax.text(i, v + 1, f"{v}%", ha="center", fontsize=8)
    ax.set(title="Share of real prices inside their range, by price band", xlabel="price band (INR lakh)",
           ylabel="% inside", ylim=(0, 100))
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "02_coverage_by_price_band.png", dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 3.6))
    ax.hist(out.width_pct.clip(upper=120), bins=50, color="#3b6ea5")
    ax.axvline(s["median_width"], color="black", lw=1, label=f"median {s['median_width']:.0f}%")
    ax.set(title="How wide the ranges are (high - low, as % of predicted price)", xlabel="range width, % (capped at 120)",
           ylabel="test flats")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "03_range_width.png", dpi=110)
    plt.close(fig)


def _md(t: pd.DataFrame, idx: str) -> list[str]:
    t = t.reset_index().rename(columns={"index": idx})
    return ["| " + " | ".join(t.columns) + " |", "|" + "---|" * len(t.columns),
            *["| " + " | ".join(str(v) for v in row) + " |" for row in t.itertuples(index=False)]]


def _report(s: dict, n_train: int, n_test: int) -> None:
    lines = ["# Step 10: price range", "",
             f"Target: {s['target']:.0%} of real prices inside the range. Calibrated with cross-validation on "
             f"{n_train:,} training flats, checked once on {n_test:,} test flats.", "",
             "| | |", "|---|---|",
             f"| Inside the range before calibration (cross-validation) | {s['raw_cover']:.1%} |",
             f"| Calibration: range widened on each side by | {s['shift_pct']:+.1f}% |",
             f"| **Inside the range on unseen test flats** | **{s['test_cover']:.1%}** |",
             f"| Typical range | {s['median_below']:.0f}% below to {s['median_above']:.0f}% above the predicted price "
             f"(median width {s['median_width']:.0f}%) |", "",
             "## Examples (typical 1, 2 and 3 BHK test flats)", *_md(s["examples"].reset_index(drop=True), "#"), "",
             "## By price band", *_md(s["by_band"], "Price band (lakh)"), "",
             "## Narrowest ranges (localities with 50+ test flats)", *_md(s["narrowest"], "Locality"), "",
             "## Widest ranges", *_md(s["widest"], "Locality"), "",
             "Ranges are wider where prices vary more for similar flats (e.g. large mixed areas), and narrower in "
             "localities with many similar listings.", "",
             "![sample](01_ranges_sample.png)", "![coverage](02_coverage_by_price_band.png)",
             "![width](03_range_width.png)"]
    (REPORT_DIR / "price_range_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = price_range()
    print(f"Target {s['target']:.0%} inside -> on unseen test flats: {s['test_cover']:.1%} inside")
    print(f"Typical range: {s['median_below']:.0f}% below to {s['median_above']:.0f}% above the predicted price "
          f"(median width {s['median_width']:.0f}%)\n")
    print("Examples:")
    for r in s["examples"].itertuples():
        print(f"  {r.locality:<16} {int(r.bedrooms)} BHK  range {r.low_lakh:6.1f} - {r.high_lakh:6.1f} lakh "
              f"(predicted {r.predicted_lakh:6.1f}, real {r.price_lakh:6.1f}) {'inside' if r.inside else 'OUTSIDE'}")
    print("\nBy price band:\n" + s["by_band"].to_string())
    print(f"\nReport {REPORT_DIR / 'price_range_report.md'}")
