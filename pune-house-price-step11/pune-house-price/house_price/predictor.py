"""Step 11b: predict the price of a Pune flat from a few inputs. This is what the app calls.

From the terminal:
    python -m house_price.predictor --locality Baner --bedrooms 2 --area 1050 --floor 5 --total-floors 12
    python -m house_price.predictor --list-localities kothrud

From Python (e.g. the ConstructAI API):
    from house_price.predictor import predict
    result = predict(locality="Baner", bedrooms=2, area_sqft=1050, floor_number=5, total_floors=12)

Needs models/house_price_model.joblib (run python -m house_price.save_model once).
Prices come out in INR lakh, adjusted from the data year to today with data/config.json -> price_adjustment.
"""
from __future__ import annotations

import argparse
import difflib
import json
from functools import lru_cache

import joblib
import numpy as np
import pandas as pd

from .adjustment import load_adjustment
from .clean import load_rules, standardise_localities
from .features import make_features
from .paths import CONFIG_FILE, MODEL_FILE


class PredictionError(ValueError):
    """Raised for inputs the model cannot price (message is safe to show to the user)."""


@lru_cache(maxsize=1)
def _bundle() -> dict:
    if not MODEL_FILE.exists():
        raise FileNotFoundError(f"{MODEL_FILE} not found - run: python -m house_price.save_model")
    return joblib.load(MODEL_FILE)


def _final_cfg() -> dict:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))["final_model"]


def localities(search: str = "") -> list[str]:
    """Locality names the model knows (most listings first), optionally filtered."""
    t = _bundle()["localities"]
    names = t.locality.tolist()
    return [n for n in names if search.lower() in n.lower()] if search else names


def resolve_locality(name: str) -> tuple[str, list[str]]:
    """Map what the user typed to a known locality (same spelling rules as cleaning). Returns (name, notes)."""
    if not str(name).strip():
        raise PredictionError("Please enter a locality")
    std = standardise_localities(pd.Series([name]), load_rules())[0].iloc[0]
    known = _bundle()["localities"]
    by_key = {n.lower().replace(" ", ""): n for n in known.locality}
    key = std.lower().replace(" ", "")
    if key in by_key:
        found = by_key[key]
        groups = dict(zip(known.locality, known.locality_group))
        if groups[found] != found:
            return found, [f"Few listings in {found}; priced with the general 'Other' locality level plus its location"]
        return found, []
    close = difflib.get_close_matches(key, list(by_key), n=3, cutoff=0.75)
    hint = f" Did you mean: {', '.join(by_key[c] for c in close)}?" if close else ""
    raise PredictionError(f"Unknown locality '{name}'.{hint}")


def predict(locality: str, bedrooms: int, area_sqft: float, total_floors: int | None = None,
            floor_number: int | None = None, bathrooms: int | None = None, balconies: int | None = None,
            age_years: float | None = None, property_type: str | None = None) -> dict:
    """Price of one flat. Required: locality, bedrooms, area_sqft. Everything else is optional."""
    b, cfg = _bundle(), _final_cfg()
    lim, notes = cfg["input_limits"], []

    def check(name, value):
        lo, hi = lim[name]
        if value is not None and not lo <= value <= hi:
            raise PredictionError(f"{name.replace('_', ' ')} must be between {lo} and {hi} (got {value})")

    check("area_sqft", area_sqft), check("bedrooms", bedrooms), check("total_floors", total_floors)
    if floor_number is not None and total_floors is not None and floor_number > total_floors:
        raise PredictionError("Floor number cannot be higher than the total floors of the building")
    if bathrooms is None:
        bathrooms = bedrooms
        notes.append(f"Bathrooms not given; assumed {bathrooms} (same as bedrooms)")
    check("bathrooms", bathrooms)
    if total_floors is None or floor_number is None:
        notes.append("Floor details not given; priced without floor information (less precise)")

    loc, loc_notes = resolve_locality(locality)
    notes += loc_notes
    row = pd.DataFrame([{
        "locality": loc, "bedrooms": float(bedrooms), "bathrooms": float(bathrooms),
        "balconies": np.nan if balconies is None else float(balconies), "area_sqft": float(area_sqft),
        "floor_number": np.nan if floor_number is None else float(floor_number),
        "total_floors": np.nan if total_floors is None else float(total_floors),
        "age_years": np.nan if age_years is None else float(age_years),
        "property_type": property_type or cfg["defaults"]["property_type"],
        "latitude": np.nan, "longitude": np.nan}])
    X = make_features(row, b["localities"], b["feature_settings"])[b["features"]]

    mid = float(np.exp(b["typical"].predict(X))[0])
    lo = min(float(np.exp(b["low"].predict(X) - b["range_shift"])[0]), mid)
    hi = max(float(np.exp(b["high"].predict(X) + b["range_shift"])[0]), mid)
    adj = load_adjustment()
    f = adj["factor"]
    return {
        "locality": loc,
        "price_lakh": round(mid * f, 1), "low_lakh": round(lo * f, 1), "high_lakh": round(hi * f, 1),
        "rate_per_sqft": int(round(mid * f * 1e5 / area_sqft, -1)),
        "price_period": adj["target_period"],
        "base_price_lakh": round(mid, 1), "base_low_lakh": round(lo, 1), "base_high_lakh": round(hi, 1),
        "base_period": adj["data_period"], "adjustment_factor": f,
        "range_meaning": f"About {round(b['info']['price_range']['target_inside_pct'])} in 100 similar flats are priced inside this range",
        "top_reasons": _reasons(b["typical"], X, cfg["top_reasons"]),
        "notes": notes,
        "model_accuracy": b["info"]["accuracy_on_unseen_test_flats"],
    }


READABLE = {"log_area": "Area", "sqft_per_bedroom": "Space per bedroom", "bedrooms": "Bedrooms", "bathrooms": "Bathrooms",
            "balconies": "Balconies", "bath_per_bedroom": "Bathrooms per bedroom", "floor_number": "Floor",
            "total_floors": "Building height", "floor_ratio": "Floor position in building",
            "is_ground_floor": "Ground floor", "is_top_floor": "Top floor", "is_high_rise": "High-rise building",
            "age_years": "Building age", "dist_city_centre_km": "Distance to city centre",
            "dist_hinjewadi_km": "Distance to Hinjewadi IT park", "dist_kharadi_km": "Distance to Kharadi IT park",
            "dist_airport_km": "Distance to airport", "lat": "Location (north-south)", "lon": "Location (east-west)",
            "locality_group": "Locality", "property_type": "Property type"}


def _reasons(model, X: pd.DataFrame, n: int) -> list[dict]:
    """Explain the LightGBM component; empty inputs are not listed as reasons."""
    component = getattr(model, "lightgbm_", model)
    prep, booster = component.named_steps["prep"], component.named_steps["model"].booster_
    contrib = booster.predict(prep.transform(X), pred_contrib=True)[0][:-1]
    names = [c.split("__", 1)[-1] for c in prep.get_feature_names_out()]
    given = [(k, v) for k, v in zip(names, contrib) if not pd.isna(X.iloc[0][k])]
    top = sorted(given, key=lambda t: -abs(t[1]))[:n]
    return [{"input": READABLE.get(k, k), "effect_pct": round(float((np.exp(v) - 1) * 100), 1)} for k, v in top]


def _cli():
    p = argparse.ArgumentParser(description="Predict a Pune flat price")
    p.add_argument("--locality")
    p.add_argument("--bedrooms", type=int)
    p.add_argument("--area", type=float, help="built-up area in sqft")
    p.add_argument("--floor", type=int, help="floor of the flat")
    p.add_argument("--total-floors", type=int, help="floors in the building")
    p.add_argument("--bathrooms", type=int)
    p.add_argument("--balconies", type=int)
    p.add_argument("--age", type=float, help="building age in years")
    p.add_argument("--list-localities", nargs="?", const="", metavar="SEARCH", help="show known localities")
    a = p.parse_args()
    if a.list_localities is not None:
        names = localities(a.list_localities)
        print(f"{len(names)} localities" + (f" matching '{a.list_localities}'" if a.list_localities else "") + ":")
        print(", ".join(names[:200]))
        return
    if not (a.locality and a.bedrooms and a.area):
        p.error("--locality, --bedrooms and --area are required")
    try:
        r = predict(a.locality, a.bedrooms, a.area, a.total_floors, a.floor, a.bathrooms, a.balconies, a.age)
    except PredictionError as e:
        print(f"Cannot predict: {e}")
        return
    print(f"\n{r['locality']} · {a.bedrooms} BHK · {a.area:.0f} sqft"
          + (f" · floor {a.floor} of {a.total_floors}" if a.floor is not None and a.total_floors else ""))
    print(f"  Estimated price ({r['price_period']}):  ₹{r['price_lakh']} lakh")
    print(f"  Likely range:                 ₹{r['low_lakh']} - {r['high_lakh']} lakh   ({r['range_meaning'].lower()})")
    print(f"  Rate:                         ₹{r['rate_per_sqft']:,} per sqft")
    print(f"  At {r['base_period']} prices:              ₹{r['base_price_lakh']} lakh  (× {r['adjustment_factor']} to today)")
    print("  Main reasons vs an average flat: "
          + ", ".join(f"{t['input']} {t['effect_pct']:+.0f}%" for t in r["top_reasons"]))
    for n in r["notes"]:
        print(f"  Note: {n}")


if __name__ == "__main__":
    _cli()
