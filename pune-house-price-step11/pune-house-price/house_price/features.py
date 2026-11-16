"""Step 4: turn cleaned listings into model inputs (features).

    python -m house_price.features

Reads   data/pune_clean.csv
Writes  data/locality_table.csv     one row per locality: listings, group, centre coordinates
        data/pune_features.csv      cleaned rows + feature columns (what step 5 trains on)
        reports/features/           feature_report.md + correlation chart

The same make_features() is used later for a user's input in the app, so training and
prediction always build inputs the same way.

Note: no feature here is calculated from price. Price-based inputs (e.g. a locality's average
price level) must be learned from the training rows only, inside step 5/6, otherwise the test
score would be falsely good (data leakage).
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from .clean import CLEAN_FILE
from .paths import CONFIG_FILE, DATA_DIR, PROJECT_ROOT

LOCALITY_FILE = DATA_DIR / "locality_table.csv"
FEATURES_FILE = DATA_DIR / "pune_features.csv"
REPORT_DIR = PROJECT_ROOT / "reports" / "features"

# what the model will use (step 5 imports these lists)
NUMERIC_FEATURES = ["log_area", "bedrooms", "bathrooms", "balconies", "sqft_per_bedroom", "bath_per_bedroom",
                    "floor_number", "total_floors", "floor_ratio", "is_ground_floor", "is_top_floor", "is_high_rise",
                    "age_years", "dist_city_centre_km", "dist_hinjewadi_km", "dist_kharadi_km", "dist_airport_km",
                    "lat", "lon"]
CATEGORICAL_FEATURES = ["locality_group", "property_type"]
TARGET = "log_price"


def load_settings() -> dict:
    s = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("features")
    if not s:
        raise ValueError("data/config.json has no 'features' section")
    return s


def km_between(lat1, lon1, lat2, lon2):
    """Straight-line distance in km (haversine)."""
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * np.arcsin(np.sqrt(a))


def build_locality_table(clean: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """Per locality: number of listings, the group used by the model, and its centre (median coordinates)."""
    t = clean.groupby("locality").agg(listings=("locality", "size"),
                                      centre_lat=("latitude", "median"), centre_lon=("longitude", "median"))
    t["locality_group"] = np.where(t.listings >= settings["min_listings_per_locality"], t.index,
                                   settings["other_locality"])
    return t.reset_index().sort_values("listings", ascending=False)


def make_features(df: pd.DataFrame, localities: pd.DataFrame, settings: dict) -> pd.DataFrame:
    """Add feature columns. Works for many listings (training) or one user input (app)."""
    out = df.copy()
    loc = localities.set_index("locality")

    # location: own coordinates if present, else the locality centre
    out["lat"] = out.get("latitude", pd.Series(np.nan, index=out.index)).fillna(out.locality.map(loc.centre_lat))
    out["lon"] = out.get("longitude", pd.Series(np.nan, index=out.index)).fillna(out.locality.map(loc.centre_lon))
    for key, mark in settings["landmarks"].items():
        name = key.removesuffix("_it_park")          # hinjewadi_it_park -> dist_hinjewadi_km
        out[f"dist_{name}_km"] = km_between(out.lat, out.lon, mark["lat"], mark["lon"]).round(2)
    out["locality_group"] = out.locality.map(loc.locality_group).fillna(settings["other_locality"])

    # size and rooms
    out["log_area"] = np.log(out.area_sqft)
    out["sqft_per_bedroom"] = (out.area_sqft / out.bedrooms).round(1)
    out["bath_per_bedroom"] = (out.bathrooms / out.bedrooms).round(2)

    # floors (empty stays empty; the model handles missing values)
    out["floor_ratio"] = (out.floor_number / out.total_floors).round(3)
    out["is_ground_floor"] = np.where(out.floor_number.isna(), np.nan, (out.floor_number <= 1).astype(float))
    out["is_top_floor"] = np.where(out.floor_number.isna() | out.total_floors.isna(), np.nan,
                                   (out.floor_number == out.total_floors).astype(float))
    out["is_high_rise"] = np.where(out.total_floors.isna(), np.nan,
                                   (out.total_floors >= settings["high_rise_floors"]).astype(float))

    if "price_lakh" in out:
        out[TARGET] = np.log(out.price_lakh)
    return out


def build() -> dict:
    settings = load_settings()
    clean = pd.read_csv(CLEAN_FILE)
    localities = build_locality_table(clean, settings)
    localities.to_csv(LOCALITY_FILE, index=False)

    feats = make_features(clean, localities, settings)
    cols = ["locality", *CATEGORICAL_FEATURES, *NUMERIC_FEATURES, "price_lakh", TARGET]
    feats[cols].to_csv(FEATURES_FILE, index=False)

    corr = (feats[NUMERIC_FEATURES].corrwith(feats[TARGET]).round(3).sort_values(key=abs, ascending=False))
    filled = (feats[NUMERIC_FEATURES].notna().mean() * 100).round(1)
    summary = {
        "rows": len(feats), "features": len(NUMERIC_FEATURES) + len(CATEGORICAL_FEATURES),
        "locality_groups": int(feats.locality_group.nunique()),
        "rows_in_other": int((feats.locality_group == settings["other_locality"]).sum()),
        "rows_with_location": round(float(feats.lat.notna().mean() * 100), 1),
        "correlation_with_log_price": corr.to_dict(), "filled_pct": filled.to_dict(),
    }
    _report(summary, corr)
    return summary


def _report(s: dict, corr: pd.Series) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(6.5, 5.5))
    c = corr.sort_values()
    ax.barh(c.index, c.values, color=np.where(c.values >= 0, "#4f8a4f", "#c0504d"))
    ax.axvline(0, color="black", lw=0.8)
    ax.set(title="Correlation of each feature with log(price)", xlabel="correlation", xlim=(-1, 1))
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(REPORT_DIR / "feature_correlation.png", dpi=110)
    plt.close(fig)
    lines = ["# Step 4: features", "",
             f"{s['rows']:,} rows · {s['features']} features · {s['locality_groups']} locality groups "
             f"({s['rows_in_other']:,} rows in 'Other') · location known for {s['rows_with_location']}% of rows", "",
             "| Feature | Correlation with log(price) | Filled % |", "|---|---|---|",
             *[f"| {k} | {v:+.3f} | {s['filled_pct'][k]} |" for k, v in s["correlation_with_log_price"].items()], "",
             "Categorical: locality_group, property_type (encoded inside the model pipeline in step 6).", "",
             "![correlation](feature_correlation.png)"]
    (REPORT_DIR / "feature_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = build()
    print(f"Rows {s['rows']:,} | features {s['features']} | locality groups {s['locality_groups']} "
          f"({s['rows_in_other']:,} rows in 'Other') | location known {s['rows_with_location']}%\n")
    print(f"{'feature':<22}{'corr with log(price)':>22}{'filled %':>10}")
    for k, v in s["correlation_with_log_price"].items():
        print(f"{k:<22}{v:>+22.3f}{s['filled_pct'][k]:>10}")
    print(f"\nSaved {FEATURES_FILE}\n      {LOCALITY_FILE}\nReport {REPORT_DIR / 'feature_report.md'}")
