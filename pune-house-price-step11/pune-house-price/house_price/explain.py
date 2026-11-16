"""Step 9: explain the final model - which inputs drive the price, and how.

    python -m house_price.explain

Reads   data/split/train.csv (to train the blended model, exactly as in step 8)
        data/split/test.csv  (to measure importance on unseen flats; nothing is tuned here)
        data/config.json -> models.lightgbm_tuned, explain
Writes  reports/explain/  explain_report.md + 6 charts

Three ways of explaining, from simple to detailed:
  1. Input-group importance  shuffle all columns of one group (e.g. every floor column) in the test set and
                             measure how much worse the % error gets. Big jump = the model relies on it.
  2. Feature importance      the same, one column at a time (21 columns).
  3. SHAP values             for every flat, how much each input pushed its price up or down vs the average flat.
                             LightGBM computes these itself (pred_contrib=True), no extra library needed.
                             SHAP explains the LightGBM component of the blend; permutation scores explain the blend.
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.base import clone

from .evaluate import FINAL, all_models
from .paths import CONFIG_FILE, PROJECT_ROOT
from .split import TEST_FILE, TRAIN_FILE
from .train import FEATURES, FIT_PARAMS, TARGET

REPORT_DIR = PROJECT_ROOT / "reports" / "explain"


def load_config() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    for key in ("explain", "models"):
        if key not in cfg:
            raise ValueError(f"data/config.json has no '{key}' section")
    if "lightgbm_tuned" not in cfg["models"]:
        raise ValueError("config.json has no models.lightgbm_tuned - run step 7 first")
    missing = set(FEATURES) - {f for g in cfg["explain"]["groups"].values() for f in g}
    if missing:
        raise ValueError(f"explain.groups does not cover: {sorted(missing)}")
    return cfg


def mape(model, X, y_log) -> float:
    true, pred = np.exp(y_log), np.exp(model.predict(X))
    return float(np.mean(np.abs(pred - true) / true) * 100)


def permutation_importance(model, X, y_log, column_sets: dict, repeats: int, seed: int) -> pd.DataFrame:
    """For each set of columns: shuffle them together, re-score, report the increase in % error."""
    rng = np.random.default_rng(seed)
    base = mape(model, X, y_log)
    rows = []
    for name, cols in column_sets.items():
        rises = []
        for _ in range(repeats):
            Xs = X.copy()
            order = rng.permutation(len(X))
            Xs[cols] = X[cols].to_numpy()[order]          # shuffle rows of these columns together
            rises.append(mape(model, Xs, y_log) - base)
        rows.append({"name": name, "error_rise_points": round(float(np.mean(rises)), 2),
                     "spread": round(float(np.std(rises)), 2)})
    return pd.DataFrame(rows).sort_values("error_rise_points", ascending=False).reset_index(drop=True), base


def shap_values(fitted_pipeline, X: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """Per-flat LightGBM-component contribution to log(price), plus its average log(price)."""
    component = getattr(fitted_pipeline, "lightgbm_", fitted_pipeline)
    prep, booster = component.named_steps["prep"], component.named_steps["model"].booster_
    contrib = booster.predict(prep.transform(X), pred_contrib=True)
    names = [c.split("__", 1)[-1] for c in prep.get_feature_names_out()]   # 'num__log_area' -> 'log_area'
    return pd.DataFrame(contrib[:, :-1], columns=names, index=X.index), float(contrib[0, -1])


def pct(log_value):
    """A change in log(price) as a % change in price."""
    return (np.exp(log_value) - 1) * 100


def explain() -> dict:
    cfg = load_config()
    e = cfg["explain"]
    train, test = pd.read_csv(TRAIN_FILE), pd.read_csv(TEST_FILE)
    model = clone(all_models(cfg["models"])[FINAL]).fit(train[FEATURES], train[TARGET], **FIT_PARAMS["LightGBM"])
    X, y = test[FEATURES], test[TARGET]
    print(f"Blended model trained on {len(train):,} flats; explaining on {len(test):,} test flats\n")

    groups, base = permutation_importance(model, X, y, e["groups"], e["permutation_repeats"], e["random_state"])
    singles, _ = permutation_importance(model, X, y, {f: [f] for f in FEATURES}, e["permutation_repeats"],
                                        e["random_state"])

    sample = test.sample(min(e["shap_sample_rows"], len(test)), random_state=e["random_state"])
    shap, start = shap_values(model, sample[FEATURES])
    mean_abs = shap.abs().mean().sort_values(ascending=False)
    group_of = {f: g for g, fs in e["groups"].items() for f in fs}
    shap_groups = shap.T.groupby(group_of).sum().T.abs().mean().sort_values(ascending=False)

    ex = _example_flat(test, e["example_flat"])
    ex_shap, _ = shap_values(model, ex[FEATURES])
    ex_pred = float(np.exp(model.predict(ex[FEATURES]))[0])

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    summary = {"base_mape": round(base, 2), "groups": groups, "singles": singles, "mean_abs_shap": mean_abs,
               "shap_groups": shap_groups, "start_price": float(np.exp(start)), "example": ex.iloc[0],
               "example_shap": ex_shap.iloc[0], "example_pred": ex_pred, "sample_rows": len(sample)}
    _charts(summary, shap, sample)
    _report(summary, e)
    return summary


def _example_flat(test: pd.DataFrame, want: dict) -> pd.DataFrame:
    """A typical flat matching config.explain.example_flat (closest to the median price of that group)."""
    m = test
    for k, v in want.items():
        if (m[k] == v).any():
            m = m[m[k] == v]
    m = m.dropna(subset=["floor_number", "total_floors"]) if m.floor_number.notna().any() else m
    return m.iloc[[int((m.price_lakh - m.price_lakh.median()).abs().argmin())]]


def _barh(ax, labels, values, color="#3b6ea5", fmt="{:.1f}", errors=None):
    ax.barh(labels, values, color=color, xerr=errors)
    for i, v in enumerate(values):
        ax.text(v, i, " " + fmt.format(v), va="center", fontsize=8)
    ax.invert_yaxis()
    ax.grid(alpha=0.3, axis="x")


def _charts(s: dict, shap: pd.DataFrame, sample: pd.DataFrame) -> None:
    g = s["groups"]
    fig, ax = plt.subplots(figsize=(7, 3.4))
    _barh(ax, g.name, g.error_rise_points, errors=g.spread, fmt="+{:.1f} pts")
    ax.set(title=f"How much worse the model gets without each input group\n(test error {s['base_mape']}% "
                 "+ this many % points)", xlabel="increase in average % error (MAPE)")
    ax.set_xlim(0, g.error_rise_points.max() * 1.25)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "01_importance_by_input.png", dpi=110)
    plt.close(fig)

    f = s["singles"]
    fig, ax = plt.subplots(figsize=(7, 6))
    _barh(ax, f.name, f.error_rise_points, fmt="+{:.2f}")
    ax.set(title="Importance of each of the 21 model inputs (shuffle test)", xlabel="increase in average % error")
    ax.set_xlim(0, f.error_rise_points.max() * 1.2)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "02_feature_importance.png", dpi=110)
    plt.close(fig)

    m = pct(s["mean_abs_shap"])
    fig, ax = plt.subplots(figsize=(7, 6))
    _barh(ax, m.index, m.values, color="#4f8a4f", fmt="{:.1f}%")
    ax.set(title=f"SHAP: average effect of each input on a flat's price\n({s['sample_rows']:,} test flats; "
                 "how far it moves the price from the average flat)", xlabel="average |effect| on price, %")
    ax.set_xlim(0, m.max() * 1.2)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "03_shap_importance.png", dpi=110)
    plt.close(fig)

    for name, col, xlabel, title in (
            ("04_location_effect.png", "dist_city_centre_km", "distance to city centre (km)",
             "Location: effect of distance to the city centre on price"),
            ("05_floor_effect.png", "floor_number", "floor of the flat",
             "Floors: effect of the flat's floor on price")):
        x, eff = sample[col], pct(shap[col])
        ok = x.notna()
        fig, ax = plt.subplots(figsize=(6.5, 3.8))
        ax.scatter(x[ok], eff[ok], s=4, alpha=0.3, color="#3b6ea5")
        ax.axhline(0, color="black", lw=0.8)
        ax.set(title=title, xlabel=xlabel, ylabel="effect on price, %")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(REPORT_DIR / name, dpi=110)
        plt.close(fig)

    ex, es = s["example"], pct(s["example_shap"])
    top = es.reindex(es.abs().sort_values(ascending=False).index).head(10)
    fig, ax = plt.subplots(figsize=(7, 4.4))
    ax.barh(top.index, top.values, color=np.where(top.values >= 0, "#4f8a4f", "#c0504d"))
    for i, v in enumerate(top.values):
        ax.text(v, i, f" {v:+.1f}%", va="center", fontsize=8, ha="left" if v >= 0 else "right")
    ax.axvline(0, color="black", lw=0.8)
    ax.invert_yaxis()
    ax.set(title=f"Why this flat is priced at {s['example_pred']:.0f} lakh (average flat: {s['start_price']:.0f} lakh)\n"
                 f"{ex.locality}, {int(ex.bedrooms)} BHK, {np.exp(ex.log_area):.0f} sqft, "
                 f"floor {ex.floor_number:.0f} of {ex.total_floors:.0f}  ·  real price {ex.price_lakh:.0f} lakh",
           xlabel="effect on price, % (top 10 inputs)")
    lim = top.abs().max() * 1.35
    ax.set_xlim(-lim, lim)
    ax.grid(alpha=0.3, axis="x")
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "06_example_flat.png", dpi=110)
    plt.close(fig)


def _report(s: dict, e: dict) -> None:
    g, f = s["groups"], s["singles"]
    ex, es = s["example"], pct(s["example_shap"]).sort_values(key=abs, ascending=False)
    lines = ["# Step 9: explaining the model", "",
             f"LightGBM/random-forest blend, test error {s['base_mape']}%. Importance is measured on the unseen test flats.", "",
             "## 1. Which of the user's inputs matter (shuffle test, by group)",
             "Shuffling a group's columns breaks the link between those inputs and the price; the rise in error "
             "shows how much the model depends on them.", "",
             "| Input group | Error rises by (% points) | Columns |", "|---|---|---|",
             *[f"| {r.name} | +{r.error_rise_points} (±{r.spread}) | {', '.join(e['groups'][r.name])} |"
               for r in g.itertuples()], "",
             "## 2. All 21 inputs (shuffle test, one at a time)",
             "| Input | Error rises by (% points) |", "|---|---|",
             *[f"| {r.name} | +{r.error_rise_points} |" for r in f.itertuples()], "",
             "Inputs that carry the same information (e.g. latitude and distance to the centre) share credit, so "
             "each looks smaller alone than its group does in table 1.", "",
             f"## 3. SHAP: average effect on a flat's price ({s['sample_rows']:,} test flats)",
             f"Starting point = the average flat, about {s['start_price']:.0f} lakh. SHAP shows how far each input "
             "moves a flat's price away from it.", "",
             "| Input group | Average effect |", "|---|---|",
             *[f"| {k} | ±{pct(v):.1f}% |" for k, v in s["shap_groups"].items()], "",
             "## 4. Example flat",
             f"{ex.locality}, {int(ex.bedrooms)} BHK, {np.exp(ex.log_area):.0f} sqft, floor {ex.floor_number:.0f} "
             f"of {ex.total_floors:.0f}: predicted **{s['example_pred']:.1f} lakh** (real {ex.price_lakh:.1f} lakh). "
             f"Average flat {s['start_price']:.1f} lakh, then:", "",
             "| Input | Value | Effect on price |", "|---|---|---|",
             *[f"| {k} | {ex[k] if k in ex.index else ''} | {v:+.1f}% |" for k, v in es.head(10).items()], "",
             "![groups](01_importance_by_input.png)", "![features](02_feature_importance.png)",
             "![shap](03_shap_importance.png)", "![location](04_location_effect.png)",
             "![floor](05_floor_effect.png)", "![example](06_example_flat.png)"]
    (REPORT_DIR / "explain_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = explain()
    print(f"Test error with all inputs: {s['base_mape']}%\n")
    print("Error rise when an input group is shuffled (bigger = more important):")
    for r in s["groups"].itertuples():
        print(f"  {r.name:<32} +{r.error_rise_points:5.1f} points")
    print("\nTop 8 single inputs:")
    for r in s["singles"].head(8).itertuples():
        print(f"  {r.name:<22} +{r.error_rise_points:5.2f} points")
    ex = s["example"]
    print(f"\nExample: {ex.locality}, {int(ex.bedrooms)} BHK, {np.exp(ex.log_area):.0f} sqft, floor "
          f"{ex.floor_number:.0f}/{ex.total_floors:.0f} -> predicted {s['example_pred']:.1f} lakh "
          f"(real {ex.price_lakh:.1f}); average flat {s['start_price']:.1f} lakh")
    for k, v in pct(s["example_shap"]).sort_values(key=abs, ascending=False).head(5).items():
        print(f"  {k:<22} {v:+6.1f}%")
    print(f"\nReport {REPORT_DIR / 'explain_report.md'}")
