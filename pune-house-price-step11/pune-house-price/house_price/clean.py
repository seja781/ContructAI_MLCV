"""Step 3: clean the combined dataset using the rules in data/config.json ("cleaning").

    python -m house_price.clean

Reads   data/pune_house_prices_combined.csv
Writes  data/pune_clean.csv                     (the agreed format for the model)
        reports/cleaning/cleaning_report.md      (what was changed or removed, and why)

What it does, in order:
  1. Locality names: tidy spacing/case, expand Bk/Kh, merge names that differ only by spaces
     (Karvenagar = Karve Nagar), then apply data/locality_aliases.csv (Hinjawadi = Hinjewadi).
  2. Property type and age: text -> standard names / years.
  3. Wrong coordinates (outside Pune): emptied, row kept.
  4. Impossible values: rows removed (area, price, INR/sqft, bedrooms, bathrooms, floors).
  5. Exact duplicates after cleaning: removed.
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from .paths import COMBINED_FILE, CONFIG_FILE, DATA_DIR, PROJECT_ROOT

CLEAN_FILE = DATA_DIR / "pune_clean.csv"
REPORT_DIR = PROJECT_ROOT / "reports" / "cleaning"
CLEAN_COLUMNS = ["locality", "bedrooms", "bathrooms", "balconies", "area_sqft", "floor_number", "total_floors",
                 "age_years", "property_type", "latitude", "longitude", "price_lakh",
                 "furnished", "transaction", "source"]


def load_rules() -> dict:
    rules = json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("cleaning")
    if not rules:
        raise ValueError("data/config.json has no 'cleaning' section")
    return rules


# ---------------------------------------------------------------- 1. locality names
def _tidy(name, word_replacements: dict) -> str:
    """'  KONDHWA  BK ' -> 'kondhwa budruk' (lower case, single spaces, abbreviations expanded)."""
    words = re.sub(r"\s+", " ", str(name).strip().lower()).split(" ")
    return " ".join(word_replacements.get(w, w) for w in words)


def standardise_localities(names: pd.Series, rules: dict) -> tuple[pd.Series, list[dict]]:
    tidy = names.map(lambda n: _tidy(n, rules["word_replacements"]))

    # names that are the same without spaces ("karvenagar", "karve nagar") -> the most common spelling
    counts = tidy.value_counts()
    nospace = pd.Series(counts.index, index=counts.index).str.replace(" ", "", regex=False)
    best = {key: grp.index[0] for key, grp in counts.groupby(nospace.values, sort=False)}  # counts are sorted desc
    merged = tidy.map(lambda n: best[n.replace(" ", "")])

    aliases = pd.read_csv(DATA_DIR / rules["aliases_file"])
    alias_map = {_tidy(v, rules["word_replacements"]): s for v, s in zip(aliases.variant, aliases.standard_name)}
    final = merged.map(lambda n: alias_map.get(n, n.title()))

    changes = (pd.DataFrame({"from": names.str.strip(), "to": final})
               .loc[lambda d: d["from"].str.lower() != d["to"].str.lower()]
               .value_counts().reset_index(name="rows").to_dict("records"))
    return final, changes


# ---------------------------------------------------------------- main
def clean() -> dict:
    rules = load_rules()
    df = pd.read_csv(COMBINED_FILE, low_memory=False)
    log = {"start_rows": len(df), "removed": {}, "changed": {}}

    df["locality"], name_changes = standardise_localities(df["locality"], rules)
    log["changed"]["locality names standardised (rows)"] = int(sum(c["rows"] for c in name_changes))
    log["changed"]["localities before -> after"] = (
        f"{pd.read_csv(COMBINED_FILE, usecols=['locality']).locality.str.strip().str.lower().nunique()} -> "
        f"{df['locality'].nunique()}")

    df["property_type"] = df["property_type"].map(rules["property_types"]).fillna(df["property_type"])
    df["age_years"] = df["age"].map(rules["age_years"])

    lat_lo, lat_hi = rules["blank"]["pune_latitude"]
    lon_lo, lon_hi = rules["blank"]["pune_longitude"]
    outside = df.latitude.notna() & ~(df.latitude.between(lat_lo, lat_hi) & df.longitude.between(lon_lo, lon_hi))
    df.loc[outside, ["latitude", "longitude"]] = np.nan
    log["changed"]["coordinates outside Pune emptied (rows kept)"] = int(outside.sum())

    def remove(mask: pd.Series, reason: str):
        nonlocal df
        log["removed"][reason] = int(mask.sum())
        df = df[~mask]

    r = rules["remove"]
    df["rate_per_sqft"] = df.price_lakh * 1e5 / df.area_sqft
    remove(~df.area_sqft.between(*r["area_sqft"]), f"area outside {r['area_sqft'][0]}-{r['area_sqft'][1]} sqft")
    remove(~df.price_lakh.between(*r["price_lakh"]), f"price outside {r['price_lakh'][0]}-{r['price_lakh'][1]} lakh")
    remove(~df.rate_per_sqft.between(*r["rate_per_sqft"]),
           f"INR/sqft outside {r['rate_per_sqft'][0]:,}-{r['rate_per_sqft'][1]:,}")
    remove(~df.bedrooms.between(*r["bedrooms"]), f"bedrooms outside {r['bedrooms'][0]}-{r['bedrooms'][1]}")
    remove(df.bathrooms > df.bedrooms + r["max_extra_bathrooms"],
           f"bathrooms more than bedrooms + {r['max_extra_bathrooms']}")
    remove((df.total_floors > r["max_total_floors"]) | (df.floor_number > df.total_floors),
           f"floors impossible (building > {r['max_total_floors']} floors, or flat above top floor)")

    remove(df.duplicated(subset=CLEAN_COLUMNS[:12], keep="first"), "exact duplicates after cleaning")

    out = df[CLEAN_COLUMNS].reset_index(drop=True)
    out.to_csv(CLEAN_FILE, index=False)
    log["end_rows"] = len(out)
    log["localities"] = int(out.locality.nunique())
    log["localities_with_10_plus"] = int((out.locality.value_counts() >= 10).sum())
    _write_report(log, name_changes)
    return log


def _write_report(log: dict, name_changes: list[dict]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    removed = sum(log["removed"].values())
    lines = ["# Step 3: data cleaning", "",
             f"**{log['start_rows']:,} rows in → {log['end_rows']:,} rows out** "
             f"({removed:,} removed, {removed / log['start_rows'] * 100:.1f}%).", "",
             "## Rows removed", "| Rule | Rows |", "|---|---|",
             *[f"| {k} | {v:,} |" for k, v in log["removed"].items()], f"| **Total** | **{removed:,}** |", "",
             "## Values changed (rows kept)", "| Change | Count |", "|---|---|",
             *[f"| {k} | {v} |" for k, v in log["changed"].items()], "",
             f"Localities after cleaning: {log['localities']:,} ({log['localities_with_10_plus']} with 10+ listings).", "",
             "## Locality names merged (top 40)", "| From | To | Rows |", "|---|---|---|",
             *[f"| {c['from']} | {c['to']} | {c['rows']} |" for c in name_changes[:40]]]
    (REPORT_DIR / "cleaning_report.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    s = clean()
    print(f"Rows: {s['start_rows']:,} -> {s['end_rows']:,}\n\nRemoved:")
    for k, v in s["removed"].items():
        print(f"  {v:>6,}  {k}")
    print("\nChanged (rows kept):")
    for k, v in s["changed"].items():
        print(f"  {k}: {v}")
    print(f"\nLocalities: {s['localities']:,} ({s['localities_with_10_plus']} with 10+ listings)")
    print(f"Saved {CLEAN_FILE}\nReport {REPORT_DIR / 'cleaning_report.md'}")
