"""Step 8: final test. Train on train.csv, then score on the fixed random holdout.

    python -m house_price.evaluate

Reads   data/split/train.csv, data/split/test.csv
        data/config.json -> models (incl. lightgbm_tuned from step 7), evaluation
Writes  reports/evaluation/  evaluation_report.md, test_predictions.csv, 4 charts

Training-only cross-validation keeps near-identical listings in one fold. The existing test split
is a random stratified holdout and can contain listings similar to training rows.
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.base import clone

from .paths import CONFIG_FILE, PROJECT_ROOT
from .split import TEST_FILE, TRAIN_FILE
from .train import BASELINE_FEATURES, FIT_PARAMS, TARGET, PriceBlendRegressor, build_models, cross_validate, scores

REPORT_DIR = PROJECT_ROOT / "reports" / "evaluation"
FINAL = "LightGBM + Random forest (blended)"
TUNED = "LightGBM (tuned)"
LIGHTGBM_WEIGHT = 0.7


def load_config() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if "lightgbm_tuned" not in cfg.get("models", {}):
        raise ValueError("config.json has no models.lightgbm_tuned - run step 7 first: python -m house_price.tune")
    if "evaluation" not in cfg:
        raise ValueError("config.json has no 'evaluation' section")
    return cfg


def all_models(m_cfg: dict) -> dict:
    """The step-6 models, tuned LightGBM, and the tuned LightGBM/random-forest blend."""
    models = build_models(m_cfg)
    tuned = {k: v for k, v in m_cfg["lightgbm_tuned"].items() if not k.startswith("_")}
    models[TUNED] = build_models({**m_cfg, "lightgbm": tuned})["LightGBM"]
    models[FINAL] = PriceBlendRegressor(
        models[TUNED],
        models["Random forest"],
        lightgbm_weight=LIGHTGBM_WEIGHT,
    )
    return models


def _metrics(y_true, y_pred) -> dict:
    return {k: round(v, 2) for k, v in scores(y_true, y_pred).items()}


def _group_table(df: pd.DataFrame, by: str, order=None) -> pd.DataFrame:
    t = (df.groupby(by, observed=True)
         .apply(lambda g: pd.Series({"Test flats": len(g), "Median price (lakh)": round(g.price_lakh.median(), 1),
                                     "MAPE (%)": round(g.ape.mean(), 1), "Within 20% (%)": round((g.ape <= 20).mean() * 100, 1)}),
                include_groups=False))
    t["Test flats"] = t["Test flats"].astype(int)
    return t.loc[order] if order is not None else t


def evaluate() -> dict:
    cfg = load_config()
    m_cfg, e_cfg = cfg["models"], cfg["evaluation"]
    train, test = pd.read_csv(TRAIN_FILE), pd.read_csv(TEST_FILE)
    X_tr, y_tr, X_te, y_te = train[BASELINE_FEATURES], train[TARGET], test[BASELINE_FEATURES], test[TARGET]
    print(f"Train on {len(train):,} flats -> evaluate on {len(test):,} random holdout rows\n")

    rows, final_model = [], None
    for name, model in all_models(m_cfg).items():
        fit_params = FIT_PARAMS.get("LightGBM" if name.startswith("LightGBM") else name, {})
        m = clone(model).fit(X_tr, y_tr, **fit_params)
        r = {"Model": name, **_metrics(y_te, m.predict(X_te))}
        rows.append(r)
        print(f"  {name:<32} test MAPE {r['MAPE (%)']:5.1f}%  R2 {r['R2']:.3f}  within 20% {r['Within 20% (%)']:.1f}%")
        if name == FINAL:
            final_model = m
    table = pd.DataFrame(rows)

    # final model in detail
    pred_log = final_model.predict(X_te)
    out = test[["locality", "locality_group", "bedrooms", "price_lakh"]].copy()
    out["predicted_lakh"] = np.exp(pred_log).round(2)
    out["error_lakh"] = (out.predicted_lakh - out.price_lakh).round(2)
    out["ape"] = (out.error_lakh.abs() / out.price_lakh * 100).round(2)
    train_fit = _metrics(y_tr, final_model.predict(X_tr))           # how well it fits rows it has seen
    cv = cross_validate({FINAL: all_models(m_cfg)[FINAL]}, X_tr, y_tr, m_cfg, verbose=False).iloc[0]

    out["bhk"] = out.bedrooms.astype(int).astype(str) + " BHK"
    bands = e_cfg["price_bands_lakh"]
    labels = [f"{a}-{b}" for a, b in zip(bands[:-1], bands[1:])]
    out["price_band"] = pd.cut(out.price_lakh, bands, labels=labels)
    by_bhk = _group_table(out, "bhk")
    by_band = _group_table(out, "price_band", labels)
    loc = _group_table(out[out.locality_group != "Other"], "locality_group")
    loc = loc[loc["Test flats"] >= e_cfg["min_test_rows_per_locality"]].sort_values("MAPE (%)")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out.drop(columns=["bhk"]).to_csv(REPORT_DIR / "test_predictions.csv", index=False)
    final = table.set_index("Model").loc[FINAL]
    base = table.set_index("Model").iloc[0]
    summary = {"table": table, "final": final, "baseline": base, "train_fit": train_fit,
               "cv_mae": float(cv["MAE (lakh)"]), "cv_rmse": float(cv["RMSE (lakh)"]),
               "cv_mape": float(cv["MAPE (%)"]),
               "price_adjustment_factor": float(cfg["price_adjustment"]["factor"]),
               "price_source_period": cfg["price_adjustment"]["data_period"],
               "price_target_period": cfg["price_adjustment"]["target_period"],
               "by_bhk": by_bhk, "by_band": by_band, "best_localities": loc.head(10), "worst_localities": loc.tail(10),
               "n_localities": len(loc), "median_abs_error_lakh": float(out.error_lakh.abs().median()),
               "bias_pct": float((out.error_lakh / out.price_lakh).median() * 100)}
    _charts(out, by_bhk, by_band)
    _report(summary, len(train), len(test), e_cfg)
    return summary


def _charts(out: pd.DataFrame, by_bhk: pd.DataFrame, by_band: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(5.8, 5.4))
    ax.scatter(out.price_lakh, out.predicted_lakh, s=3, alpha=0.25, color="#3b6ea5")
    lim = [out[["price_lakh", "predicted_lakh"]].min().min() * 0.9, out[["price_lakh", "predicted_lakh"]].max().max() * 1.1]
    ax.plot(lim, lim, color="black", lw=1, label="perfect prediction")
    for f, ls in ((1.2, "--"), (1 / 1.2, "--")):
        ax.plot(lim, [v * f for v in lim], color="#c0504d", lw=0.8, ls=ls)
    ax.set(xscale="log", yscale="log", xlim=lim, ylim=lim, xlabel="Real price (INR lakh)",
           ylabel="Predicted price (INR lakh)", title="Test set: predicted vs real (red lines = ±20%)")
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "01_predicted_vs_real.png", dpi=110)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(6.5, 3.8))
    pct = (out.error_lakh / out.price_lakh * 100).clip(-60, 60)
    ax.hist(pct, bins=60, color="#3b6ea5")
    ax.axvline(0, color="black", lw=1)
    ax.set(title="Test set: % error per flat (negative = predicted too low)", xlabel="% error (capped at ±60)",
           ylabel="Flats")
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "02_error_histogram.png", dpi=110)
    plt.close(fig)

    for name, t, title in (("03_error_by_bhk.png", by_bhk, "by BHK"),
                           ("04_error_by_price_band.png", by_band, "by price band (INR lakh)")):
        fig, ax = plt.subplots(figsize=(6.5, 3.6))
        ax.bar(t.index.astype(str), t["MAPE (%)"], color="#3b6ea5")
        for i, (v, n) in enumerate(zip(t["MAPE (%)"], t["Test flats"])):
            ax.text(i, v + 0.3, f"{v}%\n({n:,})", ha="center", fontsize=8)
        ax.set(title=f"Test set: average % error {title}", ylabel="MAPE %", ylim=(0, t["MAPE (%)"].max() * 1.3))
        ax.grid(alpha=0.3, axis="y")
        fig.tight_layout()
        fig.savefig(REPORT_DIR / name, dpi=110)
        plt.close(fig)


def _md(t: pd.DataFrame, index_name: str) -> list[str]:
    t = t.reset_index().rename(columns={"index": index_name})
    cols = list(t.columns)
    return ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols),
            *["| " + " | ".join(str(v) for v in r) + " |" for r in t.itertuples(index=False)]]


def _report(s: dict, n_train: int, n_test: int, e_cfg: dict) -> None:
    f, b, tf = s["final"], s["baseline"], s["train_fit"]
    lines = ["# Step 8: final evaluation on the test set", "",
             f"Trained on {n_train:,} flats; evaluated on {n_test:,} separate rows in the existing random holdout. "
             "Grouped cross-validation is the stricter check for similar listings.", "",
             "## All models on the test set", *_md(s["table"].set_index("Model"), "Model"), "",
             f"**Final model ({FINAL}): {f['MAPE (%)']}% average error, R² {f['R2']}, "
             f"{f['Within 20% (%)']}% of flats within 20%** (baseline: {b['MAPE (%)']}%). "
             f"Median error per flat: {s['median_abs_error_lakh']:.1f} lakh.", "",
             f"Absolute error metrics are in {s['price_source_period']} price units. The app applies a "
             f"x{s['price_adjustment_factor']} adjustment for {s['price_target_period']}; this would scale "
             f"holdout MAE to about {f['MAE (lakh)'] * s['price_adjustment_factor']:.2f} lakh and RMSE to "
             f"{f['RMSE (lakh)'] * s['price_adjustment_factor']:.2f} lakh. MAPE is unchanged; this adjustment "
             "is not a validation against recent sale prices.", "",
             "## Is it over-fitting?",
             "| Dataset | MAE (lakh) | RMSE (lakh) | MAPE (%) |", "|---|---:|---:|---:|",
             f"| Training flats (seen) | {tf['MAE (lakh)']} | {tf['RMSE (lakh)']} | {tf['MAPE (%)']} |",
             f"| Grouped 5-fold CV (train only) | {s['cv_mae']:.2f} | {s['cv_rmse']:.2f} | {s['cv_mape']:.2f} |",
             f"| Random holdout rows | {f['MAE (lakh)']} | {f['RMSE (lakh)']} | {f['MAPE (%)']} |", "",
             "CV groups listings by locality, BHK, property type and a 25 sqft area band. "
             "The training-to-validation gap is visible here; model selection uses validation, not training error. "
             "The random holdout can contain listings similar to training rows.", "",
             f"Typical bias: median error {s['bias_pct']:+.1f}% (negative = predicts slightly low).", "",
             "## By BHK", *_md(s["by_bhk"], "BHK"), "",
             "## By price band (INR lakh)", *_md(s["by_band"], "Price band"), "",
             f"## Localities (with {e_cfg['min_test_rows_per_locality']}+ test flats: {s['n_localities']})",
             "### 10 most accurate", *_md(s["best_localities"], "Locality"), "",
             "### 10 least accurate", *_md(s["worst_localities"], "Locality"), "",
             "![pred vs real](01_predicted_vs_real.png)", "![errors](02_error_histogram.png)",
             "![bhk](03_error_by_bhk.png)", "![band](04_error_by_price_band.png)"]
    (REPORT_DIR / "evaluation_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = evaluate()
    f, tf = s["final"], s["train_fit"]
    print(f"\nFinal model: {FINAL}")
    print(f"  Test MAE {f['MAE (lakh)']} lakh | RMSE {f['RMSE (lakh)']} lakh | "
          f"MAPE {f['MAPE (%)']}% | R2 {f['R2']} | "
          f"within 10% {f['Within 10% (%)']}% | "
          f"within 20% {f['Within 20% (%)']}% | median error {s['median_abs_error_lakh']:.1f} lakh")
    print(f"  MAE/RMSE/MAPE: train {tf['MAE (lakh)']}/{tf['RMSE (lakh)']}/{tf['MAPE (%)']} | "
          f"grouped CV {s['cv_mae']:.2f}/{s['cv_rmse']:.2f}/{s['cv_mape']:.2f} | "
          f"holdout {f['MAE (lakh)']}/{f['RMSE (lakh)']}/{f['MAPE (%)']}")
    print("\nBy BHK:\n" + s["by_bhk"].to_string())
    print("\nBy price band (lakh):\n" + s["by_band"].to_string())
    print("\nLeast accurate localities:\n" + s["worst_localities"].tail(5).to_string())
    print(f"\nReport {REPORT_DIR / 'evaluation_report.md'}")
