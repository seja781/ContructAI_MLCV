"""Step 7: tune LightGBM with random search + five-fold grouped cross-validation.

    python -m house_price.tune

Reads   data/split/train.csv          (test.csv is still NOT used)
        data/config.json  -> "tuning" (how many trials, which values to try)
Writes  data/config.json  -> models.lightgbm_tuned   (the best settings found)
        reports/tuning/   tuning_results.csv, tuning_report.md, chart

How it works: each trial picks one random value for every setting in "search_space", trains LightGBM
with five-fold grouped cross-validation, and selects settings by MAE in INR lakh. Similar listings
stay together in one fold. The starting settings are scored too; RMSE and MAPE are also reported.
"""
from __future__ import annotations

import json
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from sklearn.model_selection import ParameterSampler

from .paths import CONFIG_FILE, PROJECT_ROOT
from .split import TRAIN_FILE
from .train import BASELINE_FEATURES, TARGET, build_models, cross_validate

REPORT_DIR = PROJECT_ROOT / "reports" / "tuning"
NAME = "LightGBM"


def load_config() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    if "tuning" not in cfg or "models" not in cfg:
        raise ValueError("data/config.json needs both 'models' and 'tuning' sections")
    return cfg


def score_settings(lgbm_settings: dict, models_cfg: dict, X, y) -> dict:
    """Five-fold grouped cross-validation of LightGBM with the given settings."""
    s = {**models_cfg, "lightgbm": lgbm_settings}
    model = {NAME: build_models(s)[NAME]}
    return cross_validate(model, X, y, s, verbose=False).iloc[0].to_dict()


def tune() -> dict:
    cfg = load_config()
    t_cfg, m_cfg = cfg["tuning"], cfg["models"]
    train = pd.read_csv(TRAIN_FILE)
    X, y = train[BASELINE_FEATURES], train[TARGET]
    print(f"Training rows: {len(train):,} | {t_cfg['n_trials']} trials x {m_cfg['cv_folds']}-fold cross-validation\n")

    start = m_cfg["lightgbm"]
    t0 = time.time()
    base = score_settings(start, m_cfg, X, y)
    print(f"  step 6 settings      MAE {base['MAE (lakh)']:7.2f} lakh  ({base['Seconds']} s)")

    trials = list(ParameterSampler(t_cfg["search_space"], n_iter=t_cfg["n_trials"], random_state=t_cfg["random_state"]))
    fixed = {k: v for k, v in start.items() if k not in t_cfg["search_space"]}     # e.g. subsample_freq
    rows = [{"trial": 0, "MAE (lakh)": base["MAE (lakh)"], "RMSE (lakh)": base["RMSE (lakh)"],
             "MAPE (%)": base["MAPE (%)"],
             "R2": base["R2"], "Within 20% (%)": base["Within 20% (%)"],
             "Seconds": base["Seconds"], **start}]
    best = rows[0]
    for i, params in enumerate(trials, start=1):
        settings = {**fixed, **params, "subsample_freq": 1}
        r = score_settings(settings, m_cfg, X, y)
        rows.append({"trial": i, "MAE (lakh)": r["MAE (lakh)"], "RMSE (lakh)": r["RMSE (lakh)"],
                     "MAPE (%)": r["MAPE (%)"],
                     "R2": r["R2"], "Within 20% (%)": r["Within 20% (%)"],
                     "Seconds": r["Seconds"], **settings})
        mark = ""
        if r["MAE (lakh)"] < best["MAE (lakh)"]:
            best, mark = rows[-1], "  <- new best"
        print(f"  trial {i:>2}/{len(trials)}          MAE {r['MAE (lakh)']:7.2f} lakh  "
              f"({r['Seconds']} s){mark}")

    results = pd.DataFrame(rows).sort_values("MAE (lakh)").reset_index(drop=True)
    best_settings = {k: (v.item() if hasattr(v, "item") else v)
                     for k, v in best.items()
                     if k not in ("trial", "MAE (lakh)", "RMSE (lakh)", "MAPE (%)", "R2",
                                  "Within 20% (%)", "Seconds")}
    _save_best(best_settings, best, base)
    summary = {"start_mae": base["MAE (lakh)"], "best_mae": best["MAE (lakh)"],
               "start_rmse": base["RMSE (lakh)"], "best_rmse": best["RMSE (lakh)"],
               "best_mape": best["MAPE (%)"], "best_trial": int(best["trial"]),
               "best_r2": best["R2"], "best_within_20": best["Within 20% (%)"], "best_settings": best_settings,
               "minutes": round((time.time() - t0) / 60, 1)}
    _report(results, summary)
    return summary


def _save_best(settings: dict, best: dict, base: dict) -> None:
    """Write the winning settings into config.json -> models.lightgbm_tuned (step 6 settings are kept)."""
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    cfg["models"]["lightgbm_tuned"] = {
        **settings,
        "_note": f"Step 7 best of {cfg['tuning']['n_trials']} trials: CV MAE {best['MAE (lakh)']} lakh, "
                 f"CV RMSE {best['RMSE (lakh)']} lakh, MAPE {best['MAPE (%)']}% "
                 f"(step 6 settings: {base['MAPE (%)']}%)"}
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _report(results: pd.DataFrame, s: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    results.to_csv(REPORT_DIR / "tuning_results.csv", index=False)

    ordered = results.sort_values("trial")
    fig, ax = plt.subplots(figsize=(8, 3.8))
    colors = ["#9e9e9e" if t == 0 else "#4f8a4f" if t == s["best_trial"] else "#3b6ea5" for t in ordered.trial]
    ax.bar(ordered.trial.astype(str), ordered["MAE (lakh)"], color=colors)
    ax.axhline(s["start_mae"], color="#9e9e9e", ls="--", lw=1,
               label=f"step 6 settings ({s['start_mae']} lakh)")
    lo = ordered["MAE (lakh)"].min()
    ax.set(ylim=(max(0, lo - (ordered["MAE (lakh)"].max() - lo) * 0.05),
                 ordered["MAE (lakh)"].max() * 1.05),
           title="Cross-validation MAE per trial (lower is better)",
           xlabel="trial (0 = step 6 settings, green = best)", ylabel="MAE (INR lakh)")
    ax.legend()
    ax.grid(alpha=0.3, axis="y")
    ax.tick_params(axis="x", labelsize=7)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "tuning_trials.png", dpi=110)
    plt.close(fig)

    top = results.head(10)
    cols = list(top.columns)
    lines = ["# Step 7: LightGBM tuning", "",
             f"Random search, {len(results) - 1} trials, each scored with five-fold grouped cross-validation on train.csv "
             f"({s['minutes']} min).", "",
             f"Settings were selected by MAE: **{s['start_mae']} lakh → {s['best_mae']} lakh** "
             f"(trial {s['best_trial']}); RMSE **{s['start_rmse']} lakh → {s['best_rmse']} lakh**, "
             f"MAPE {s['best_mape']}% "
             f"(R² {s['best_r2']}, within 20%: {s['best_within_20']}%).", "",
             "Best settings (saved to `data/config.json` → `models.lightgbm_tuned`):", "```json",
             json.dumps(s["best_settings"], indent=2), "```", "",
             "## Top 10 trials", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols),
             *["| " + " | ".join(str(v) for v in row) + " |" for row in top.itertuples(index=False)], "",
             "![trials](tuning_trials.png)"]
    (REPORT_DIR / "tuning_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = tune()
    print(f"\nStep 6 settings MAE/RMSE: {s['start_mae']}/{s['start_rmse']} lakh  ->  "
          f"best: {s['best_mae']}/{s['best_rmse']} lakh "
          f"(trial {s['best_trial']}), MAPE {s['best_mape']}%, "
          f"R2 {s['best_r2']}, within 20%: {s['best_within_20']}%")
    print("Best settings (saved to data/config.json -> models.lightgbm_tuned):")
    for k, v in s["best_settings"].items():
        print(f"  {k:<18} {v}")
    print(f"\nTook {s['minutes']} min. Report {REPORT_DIR / 'tuning_report.md'}")
