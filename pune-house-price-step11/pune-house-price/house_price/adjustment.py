"""Bring model prices (2024 listings) up to today's level using the factor in data/config.json.

    python -m house_price.adjustment            # show the current factor
    python -m house_price.adjustment 80         # convert 80 lakh (2024) to today's price
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .paths import CONFIG_FILE as CONFIG


def load_adjustment(config_file: Path = CONFIG) -> dict:
    adj = json.loads(Path(config_file).read_text(encoding="utf-8")).get("price_adjustment")
    if not adj:
        raise ValueError("config.json has no 'price_adjustment' section")
    factor = float(adj["factor"])
    if not 0.5 <= factor <= 2.0:
        raise ValueError(f"price_adjustment.factor looks wrong: {factor} (expected between 0.5 and 2.0)")
    return {**adj, "factor": factor}


def adjust(price_lakh: float, config_file: Path = CONFIG) -> dict:
    """Return the base (data-period) price, the adjusted price and how it was adjusted."""
    adj = load_adjustment(config_file)
    return {
        "base_price_lakh": round(price_lakh, 2),
        "base_period": adj["data_period"],
        "price_lakh": round(price_lakh * adj["factor"], 2),
        "price_period": adj["target_period"],
        "adjustment_pct": round((adj["factor"] - 1) * 100, 1),
        "basis": adj["basis"],
    }


if __name__ == "__main__":
    a = load_adjustment()
    print(f"Factor {a['factor']}  ({a['data_period']} -> {a['target_period']}, updated {a.get('updated_on', '?')})")
    print(f"Basis: {a['basis']}")
    if len(sys.argv) > 1:
        r = adjust(float(sys.argv[1]))
        print(f"{r['base_price_lakh']} lakh ({r['base_period']})  ->  {r['price_lakh']} lakh ({r['price_period']}), "
              f"+{r['adjustment_pct']}%")
