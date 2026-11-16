"""Step 2: explore the dataset (EDA). Read-only: nothing in data/ is changed.

    python -m house_price.explore

Prints a summary and saves 8 charts plus a short report to reports/eda/.
The "problems found" list at the end is what step 3 (cleaning) will fix.
"""
from __future__ import annotations

import difflib

import matplotlib

matplotlib.use("Agg")  # save files only, no window
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy import stats

from .paths import COMBINED_FILE, PROJECT_ROOT

OUT = PROJECT_ROOT / "reports" / "eda"
MIN_LISTINGS = 30          # localities with fewer listings are left out of locality charts
PUNE_BOX = {"lat": (18.2, 18.95), "lon": (73.4, 74.3)}   # rough bounding box of Pune district's urban area

plt.rcParams.update({"figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False,
                     "axes.grid": True, "grid.alpha": 0.3, "font.size": 9})


def _save(fig, name: str) -> str:
    fig.tight_layout()
    fig.savefig(OUT / name)
    plt.close(fig)
    return name


def explore() -> dict:
    OUT.mkdir(parents=True, exist_ok=True)
    df = pd.read_csv(COMBINED_FILE, low_memory=False)
    df["rate"] = df["price_lakh"] * 1e5 / df["area_sqft"]          # INR per sqft
    ok = df[(df.area_sqft >= 250) & (df.price_lakh >= 5)]           # sensible rows for charts only
    charts, notes = [], {}

    # 1 price distribution
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.6))
    ax[0].hist(ok.price_lakh.clip(upper=500), bins=60, color="#3b6ea5")
    ax[0].set(title="Price (capped at 500 lakh)", xlabel="Price (INR lakh)", ylabel="Listings")
    ax[1].hist(np.log10(ok.price_lakh), bins=60, color="#3b6ea5")
    ax[1].set(title="log10(price): close to a bell curve", xlabel="log10(price lakh)")
    charts.append(_save(fig, "01_price_distribution.png"))
    notes["price_skew"] = round(float(stats.skew(ok.price_lakh)), 2)
    notes["log_price_skew"] = round(float(stats.skew(np.log(ok.price_lakh))), 2)

    # 2 area vs price
    fig, ax = plt.subplots(figsize=(6, 4.2))
    s = ok.sample(min(8000, len(ok)), random_state=1)
    ax.scatter(s.area_sqft, s.price_lakh, s=3, alpha=0.25, color="#3b6ea5")
    ax.set(xscale="log", yscale="log", title="Area vs price (log scales)", xlabel="Area (sqft)", ylabel="Price (INR lakh)")
    charts.append(_save(fig, "02_area_vs_price.png"))
    notes["corr_log_area_log_price"] = round(float(np.corrcoef(np.log(ok.area_sqft), np.log(ok.price_lakh))[0, 1]), 3)

    # 3 locality rates
    loc = ok.groupby(ok.locality.str.strip()).rate.agg(["median", "size"])
    loc = loc[loc["size"] >= MIN_LISTINGS].sort_values("median")
    show = pd.concat([loc.head(15), loc.tail(15)])
    fig, ax = plt.subplots(figsize=(7, 7.5))
    ax.barh(show.index, show["median"], color=["#c0504d"] * 15 + ["#4f8a4f"] * 15)
    ax.set(title=f"Median INR/sqft: 15 cheapest and 15 costliest localities ({MIN_LISTINGS}+ listings)",
           xlabel="INR per sqft")
    charts.append(_save(fig, "03_locality_rates.png"))
    groups = [g.rate.to_numpy() for _, g in ok.groupby("locality") if len(g) >= MIN_LISTINGS]
    h, p = stats.kruskal(*groups)
    notes["locality_test"] = {"kruskal_h": round(float(h)), "p_value": float(p), "localities_tested": len(groups),
                              "cheapest": f"{loc.index[0]} {loc['median'].iloc[0]:,.0f}",
                              "costliest": f"{loc.index[-1]} {loc['median'].iloc[-1]:,.0f}"}

    # 4 price by BHK
    fig, ax = plt.subplots(figsize=(6, 4))
    bhk = [ok[ok.bedrooms == b].price_lakh for b in range(1, 6)]
    ax.boxplot(bhk, tick_labels=[f"{b} BHK" for b in range(1, 6)], showfliers=False)
    ax.set(title="Price by BHK (outliers hidden)", ylabel="Price (INR lakh)")
    charts.append(_save(fig, "04_price_by_bhk.png"))
    notes["median_price_by_bhk"] = {f"{b} BHK": float(x.median()) for b, x in zip(range(1, 6), bhk)}

    # 5 floor effect, compared with the same locality
    f = ok.dropna(subset=["floor_number", "total_floors"]).copy()
    f["vs_locality"] = f.rate / f.groupby("locality").rate.transform("median")
    bands = pd.cut(f.floor_number, [0, 1, 3, 7, 12, 20, 200], labels=["0-1", "2-3", "4-7", "8-12", "13-20", "21+"])
    rel = f.groupby(bands, observed=True).vs_locality.median()
    fig, ax = plt.subplots(figsize=(6, 3.8))
    ax.bar(rel.index.astype(str), (rel - 1) * 100, color="#3b6ea5")
    ax.axhline(0, color="black", lw=0.8)
    ax.set(title="Floor effect: INR/sqft vs median of the same locality", xlabel="Floor of the flat", ylabel="%")
    charts.append(_save(fig, "05_floor_effect.png"))
    notes["floor_effect_pct"] = {k: round((v - 1) * 100, 1) for k, v in rel.items()}

    # 6 building height
    tb = pd.cut(f.total_floors, [0, 4, 7, 12, 20, 30, 200], labels=["1-4", "5-7", "8-12", "13-20", "21-30", "31+"])
    th = f.groupby(tb, observed=True).rate.median()
    fig, ax = plt.subplots(figsize=(6, 3.8))
    ax.bar(th.index.astype(str), th.values, color="#3b6ea5")
    ax.set(title="Median INR/sqft by building height", xlabel="Floors in building", ylabel="INR per sqft")
    charts.append(_save(fig, "06_building_height.png"))

    # 7 missing values
    miss = (df.isna().mean() * 100).drop("rate").sort_values()
    fig, ax = plt.subplots(figsize=(6, 4.2))
    ax.barh(miss.index, miss.values, color="#8064a2")
    ax.set(title="Missing values per column", xlabel="% of rows empty", xlim=(0, 100))
    charts.append(_save(fig, "07_missing_values.png"))
    notes["missing_pct"] = {k: round(v, 1) for k, v in miss.items() if v > 0}

    # 8 rows per source and age
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.6))
    df.source.value_counts().plot.bar(ax=ax[0], color="#3b6ea5", rot=0, title="Listings per source")
    df.age.value_counts().plot.barh(ax=ax[1], color="#3b6ea5", title="Age of construction (filled rows)")
    charts.append(_save(fig, "08_sources_and_age.png"))

    # problems to fix in step 3
    lat, lon = df.latitude, df.longitude
    names = df.locality.str.lower().str.strip().value_counts()
    common = names[names >= MIN_LISTINGS].index.tolist()
    similar = [f"{a} / {b}" for i, a in enumerate(common) for b in common[i + 1:]
               if difflib.SequenceMatcher(None, a, b).ratio() > 0.85]
    problems = {
        "area_below_250_sqft": int((df.area_sqft < 250).sum()),
        "area_above_10000_sqft": int((df.area_sqft > 10000).sum()),
        "price_below_5_lakh": int((df.price_lakh < 5).sum()),
        "price_above_2000_lakh": int((df.price_lakh > 2000).sum()),
        "rate_below_2000_or_above_40000_per_sqft": int(((df.rate < 2000) | (df.rate > 40000)).sum()),
        "bathrooms_more_than_bedrooms_plus_2": int((df.bathrooms > df.bedrooms + 2).sum()),
        "coordinates_outside_pune": int((lat.notna() & ~(lat.between(*PUNE_BOX["lat"]) & lon.between(*PUNE_BOX["lon"]))).sum()),
        "localities_with_under_10_listings": int((names < 10).sum()),
        "possible_duplicate_locality_names": similar,
    }

    summary = {"rows": len(df), "localities": int(names.size), "charts": charts, "findings": notes, "problems": problems}
    _write_report(summary)
    return summary


def _write_report(s: dict) -> None:
    n, p = s["findings"], s["problems"]
    lt = n["locality_test"]
    lines = [
        "# Step 2: data exploration", "",
        f"Rows: {s['rows']:,}  ·  localities: {s['localities']:,}", "",
        "## Findings",
        f"1. Price is right-skewed (skew {n['price_skew']}); log(price) is nearly symmetric (skew {n['log_price_skew']}). "
        "-> the model will predict log(price).",
        f"2. Area and price are strongly related on log scales (correlation {n['corr_log_area_log_price']}).",
        f"3. Locality matters: Kruskal-Wallis p = {lt['p_value']:.1e} across {lt['localities_tested']} localities. "
        f"Cheapest {lt['cheapest']} vs costliest {lt['costliest']} INR/sqft.",
        "4. Median price by BHK: " + ", ".join(f"{k} {v:.0f} lakh" for k, v in n["median_price_by_bhk"].items()) + ".",
        "5. Floor effect vs same locality: " + ", ".join(f"floor {k}: {v:+.1f}%" for k, v in n["floor_effect_pct"].items()) + ".",
        "6. Missing values: " + ", ".join(f"{k} {v}%" for k, v in n["missing_pct"].items()) + ".", "",
        "## Problems for step 3 (cleaning)",
        *[f"- {k.replace('_', ' ')}: {v}" for k, v in p.items() if k != "possible_duplicate_locality_names"],
        "- possible duplicate locality names: " + "; ".join(p["possible_duplicate_locality_names"]), "",
        "## Charts", *[f"![{c}]({c})" for c in s["charts"]],
    ]
    (OUT / "eda_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = explore()
    print(f"Rows {s['rows']:,} | localities {s['localities']:,}\n")
    for k, v in s["findings"].items():
        print(f"{k:<26} {v}")
    print("\nProblems to fix in step 3:")
    for k, v in s["problems"].items():
        print(f"  {k:<42} {v}")
    print(f"\nCharts and report saved in {OUT}")
