"""Step 11a: train the final models ONCE on all clean flats and save them.

    python -m house_price.save_model

Reads   data/pune_features.csv (all 63,484 flats = train + test), data/locality_table.csv
        data/config.json -> models.lightgbm_tuned, price_range, features, final_model
Writes  models/house_price_model.joblib   the 3 models + everything predict() needs
        models/model_info.json            what was trained, when, and how accurate it was

Why all flats now? Steps 6-10 already measured the accuracy honestly on test.csv. For the app we want
the model to learn from every flat we have, so the test flats are added back in. (Same idea as using
all the data after the exam is over.) The accuracy we report is still the step 8 test result.
"""
from __future__ import annotations

import json
from datetime import date

import joblib
import numpy as np
import pandas as pd

from .evaluate import FINAL, LIGHTGBM_WEIGHT, all_models
from .features import FEATURES_FILE, LOCALITY_FILE, load_settings as feature_settings
from .paths import CONFIG_FILE, MODEL_DIR, MODEL_FILE, MODEL_INFO_FILE, PROJECT_ROOT
from .price_range import calibration_shift, fit, quantile_model
from .train import FEATURES, TARGET

EVAL_FILE = PROJECT_ROOT / "reports" / "evaluation" / "test_predictions.csv"


def test_scores() -> dict:
    """Accuracy of the tuned model on the unseen test flats, from step 8's saved predictions."""
    if not EVAL_FILE.exists():
        return {"note": "run step 8 (python -m house_price.evaluate) to record test accuracy"}
    t = pd.read_csv(EVAL_FILE)
    error = t.predicted_lakh - t.price_lakh
    ape = error.abs() / t.price_lakh
    return {"test_flats": len(t), "test_mae_lakh": round(float(error.abs().mean()), 2),
            "test_rmse_lakh": round(float(np.sqrt(np.mean(error ** 2))), 2),
            "test_mape_pct": round(float(ape.mean() * 100), 2),
            "test_within_20_pct": round(float((ape <= 0.2).mean() * 100), 1),
            "test_median_error_lakh": round(float(error.abs().median()), 1)}


def save() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    m_cfg, r_cfg = cfg["models"], cfg["price_range"]
    data = pd.read_csv(FEATURES_FILE)
    X, y = data[FEATURES], data[TARGET]
    print(f"Training final models on all {len(data):,} clean flats\n")

    print("  1/3 typical price model (tuned LightGBM)")
    typical = fit(all_models(m_cfg)[FINAL], X, y)
    print(f"  2/3 price range: calibrating on all flats ({r_cfg['cv_folds']}-fold cross-validation)")
    shift, raw_cover = calibration_shift(m_cfg, r_cfg, X, y)
    print(f"  3/3 low and high price models")
    low = fit(quantile_model(m_cfg, r_cfg["low_quantile"]), X, y)
    high = fit(quantile_model(m_cfg, r_cfg["high_quantile"]), X, y)

    info = {
        "saved_on": date.today().isoformat(),
        "trained_on_flats": len(data),
        "localities_known": int(pd.read_csv(LOCALITY_FILE).shape[0]),
        "model": f"{FINAL} ({LIGHTGBM_WEIGHT:.0%} LightGBM, {1 - LIGHTGBM_WEIGHT:.0%} random forest): "
                 + ", ".join(f"{k}={v}" for k, v in m_cfg["lightgbm_tuned"].items() if not k.startswith("_")),
        "price_range": {"target_inside_pct": r_cfg["coverage"] * 100, "widen_each_side_pct": round(float((np.exp(shift) - 1) * 100), 2),
                        "inside_before_calibration_pct": round(raw_cover * 100, 1)},
        "accuracy_on_unseen_test_flats": test_scores(),
        "prices_are_from": cfg["price_adjustment"]["data_period"],
        "inputs": FEATURES,
    }
    bundle = {"typical": typical, "low": low, "high": high, "range_shift": shift, "features": FEATURES,
              "localities": pd.read_csv(LOCALITY_FILE), "feature_settings": feature_settings(), "info": info}
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, MODEL_FILE, compress=3)
    MODEL_INFO_FILE.write_text(json.dumps(info, indent=2), encoding="utf-8")
    info["file_mb"] = round(MODEL_FILE.stat().st_size / 1e6, 1)
    return info


if __name__ == "__main__":
    i = save()
    a = i["accuracy_on_unseen_test_flats"]
    print(f"\nSaved {MODEL_FILE} ({i['file_mb']} MB)\n      {MODEL_INFO_FILE}")
    print(f"Trained on {i['trained_on_flats']:,} flats, {i['localities_known']:,} localities known")
    print(f"Price range: widened {i['price_range']['widen_each_side_pct']}% each side "
          f"(before: {i['price_range']['inside_before_calibration_pct']}% inside, target 80%)")
    if "test_mape_pct" in a:
        print(f"Reported accuracy (step 8 test): MAE {a['test_mae_lakh']} lakh, "
              f"RMSE {a['test_rmse_lakh']} lakh, MAPE {a['test_mape_pct']}%, "
              f"{a['test_within_20_pct']}% within 20%")
    print("\nTry it:  python -m house_price.predictor --locality Baner --bedrooms 2 --area 1050 --floor 5 --total-floors 12")
