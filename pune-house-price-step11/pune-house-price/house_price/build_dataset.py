"""Step 1: combine the raw Pune listing files into one dataset with a common set of columns.

    python -m house_price.build_dataset

Reads   data/raw/*.csv
Writes  data/pune_house_prices_combined.csv

Only renaming, unit conversion (price to INR lakh) and de-duplication happen here.
Cleaning (outliers, locality spellings, wrong coordinates) is step 3.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .paths import COMBINED_FILE as OUT, RAW_DIR as RAW

COLUMNS = ["source", "locality", "latitude", "longitude", "property_type", "bedrooms", "bathrooms", "balconies",
           "area_sqft", "carpet_area_sqft", "floor_number", "total_floors", "age", "furnished", "transaction",
           "price_lakh"]


def _num(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", ""), errors="coerce")


def _magicbricks(path: Path, source: str) -> pd.DataFrame:
    d = pd.read_csv(path, low_memory=False)
    return pd.DataFrame({
        "source": source,
        "locality": d["localityName"].str.split(",").str[0].str.strip(),
        "latitude": _num(d["latitude"]), "longitude": _num(d["longitude"]),
        "property_type": d["propertyType"],
        "bedrooms": _num(d["bedrooms"]), "bathrooms": _num(d["bathrooms"]), "balconies": _num(d["numberOfBalconied"]),
        "area_sqft": _num(d["coveredArea"]), "carpet_area_sqft": _num(d["carpetArea"]),
        "floor_number": _num(d["floorNumber"]), "total_floors": _num(d["totalFloorNumber"]),
        "age": d["ageofcons"], "furnished": d["furnished"], "transaction": d["transactionType"],
        "price_lakh": (_num(d["price"]) / 1e5).round(2),
        "url": d["url"],
    })


def _apartments_25k(path: Path, source: str) -> pd.DataFrame:
    d = pd.read_csv(path)
    return pd.DataFrame({
        "source": source, "locality": d["site_location"].str.strip(), "property_type": "Apartment",
        "bedrooms": d["size"].str.extract(r"(\d+)")[0].astype(float),
        "bathrooms": _num(d["bath"]), "balconies": _num(d["balcony"]),
        "area_sqft": _num(d["total_sqft"]), "price_lakh": _num(d["price"]).round(2),
    })


def _match_key(d: pd.DataFrame) -> pd.Series:
    """Same locality + BHK + area + price = the same flat listed in two sources."""
    return (d["locality"].str.lower().str.replace(r"[^a-z]", "", regex=True) + "|"
            + d["bedrooms"].astype(int).astype(str) + "|" + d["area_sqft"].round().astype(int).astype(str) + "|"
            + d["price_lakh"].round(1).astype(str))


def build(raw_dir: Path = RAW, out_file: Path = OUT) -> dict:
    required = ["locality", "bedrooms", "area_sqft", "price_lakh"]
    mb = pd.concat([_magicbricks(raw_dir / "magicbricks_pune_a.csv", "magicbricks_a"),
                    _magicbricks(raw_dir / "magicbricks_pune_b.csv", "magicbricks_b")], ignore_index=True)
    report = {"magicbricks_raw_rows": len(mb)}
    mb = mb.drop_duplicates("url").drop(columns="url").dropna(subset=required)
    report["magicbricks_unique_rows"] = len(mb)

    apt = _apartments_25k(raw_dir / "pune_apartments_25k.csv", "pune_apartments_25k").dropna(subset=required)
    report["apartments_25k_rows"] = len(apt)
    repeated = _match_key(apt).isin(set(_match_key(mb)))
    report["apartments_25k_already_in_magicbricks"] = int(repeated.sum())

    combined = pd.concat([mb, apt[~repeated]], ignore_index=True)[COLUMNS]
    combined.to_csv(out_file, index=False)
    report["combined_rows"] = len(combined)
    report["localities"] = int(combined["locality"].str.lower().nunique())
    return report


if __name__ == "__main__":
    for key, value in build().items():
        print(f"{key:<40} {value}")
