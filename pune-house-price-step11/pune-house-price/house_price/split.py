"""Step 5: split the feature table into a training set and a test set.

    python -m house_price.split

Reads   data/pune_features.csv
Writes  data/split/train.csv   (80%: the model learns from these)
        data/split/test.csv    (20%: kept aside; used ONCE at the end to measure accuracy)
        reports/split/          split_report.md + chart

The split is stratified by price: both parts get the same mix of cheap and costly flats.
It is done once and saved, so every team member trains and tests on exactly the same rows.
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split

from .features import FEATURES_FILE
from .paths import CONFIG_FILE, DATA_DIR, PROJECT_ROOT

SPLIT_DIR = DATA_DIR / "split"
TRAIN_FILE = SPLIT_DIR / "train.csv"
TEST_FILE = SPLIT_DIR / "test.csv"
REPORT_DIR = PROJECT_ROOT / "reports" / "split"


def load_settings() -> dict:
    s = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("split")
    if not s:
        raise ValueError("data/config.json has no 'split' section")
    return s


def split() -> dict:
    s = load_settings()
    df = pd.read_csv(FEATURES_FILE)
    price_band = pd.qcut(df.price_lakh, q=s["price_bins"], labels=False, duplicates="drop")
    train, test = train_test_split(df, test_size=s["test_size"], random_state=s["random_state"], stratify=price_band)

    SPLIT_DIR.mkdir(parents=True, exist_ok=True)
    train.to_csv(TRAIN_FILE, index=False)
    test.to_csv(TEST_FILE, index=False)

    def describe(d):
        return {"rows": len(d), "median_price_lakh": round(float(d.price_lakh.median()), 1),
                "mean_price_lakh": round(float(d.price_lakh.mean()), 1),
                "median_area_sqft": round(float(np.exp(d.log_area).median())),
                **{f"{b} BHK %": round(float((d.bedrooms == b).mean() * 100), 1) for b in (1, 2, 3, 4)},
                "with_floor_info %": round(float(d.floor_number.notna().mean() * 100), 1)}

    unseen = sorted(set(test.locality_group) - set(train.locality_group))
    summary = {"settings": {k: v for k, v in s.items() if not k.startswith("_")},
               "train": describe(train), "test": describe(test),
               "test_locality_groups_not_in_train": unseen}
    _report(summary, train, test)
    return summary


def _report(s: dict, train: pd.DataFrame, test: pd.DataFrame) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 3.8))
    bins = np.linspace(train.log_price.min(), train.log_price.max(), 50)
    ax.hist(train.log_price, bins=bins, density=True, alpha=0.55, label=f"train ({len(train):,})", color="#3b6ea5")
    ax.hist(test.log_price, bins=bins, density=True, alpha=0.55, label=f"test ({len(test):,})", color="#e0803a")
    ax.set(title="Same price mix in train and test", xlabel="log(price lakh)", ylabel="share of rows")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "train_test_prices.png", dpi=110)
    plt.close(fig)

    keys = list(s["train"])
    lines = ["# Step 5: train/test split", "",
             f"Settings: {s['settings']}", "",
             "| | Train | Test |", "|---|---|---|",
             *[f"| {k} | {s['train'][k]:,} | {s['test'][k]:,} |" for k in keys], "",
             f"Locality groups in test but not in train: {s['test_locality_groups_not_in_train'] or 'none'}", "",
             "![prices](train_test_prices.png)"]
    (REPORT_DIR / "split_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = split()
    print(f"{'':<22}{'train':>10}{'test':>10}")
    for k in s["train"]:
        print(f"{k:<22}{s['train'][k]:>10,}{s['test'][k]:>10,}")
    print(f"\nLocality groups in test but not in train: {s['test_locality_groups_not_in_train'] or 'none'}")
    print(f"Saved {TRAIN_FILE}\n      {TEST_FILE}\nReport {REPORT_DIR / 'split_report.md'}")
