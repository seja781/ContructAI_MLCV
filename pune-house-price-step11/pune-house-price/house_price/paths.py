"""All file locations in one place.

Standalone project:   <project>/data/...
When merged into ConstructAI later, only DATA_DIR needs to change (or set the
HOUSE_PRICE_DATA environment variable to the data folder).
"""
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("HOUSE_PRICE_DATA", PROJECT_ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
CONFIG_FILE = DATA_DIR / "config.json"
COMBINED_FILE = DATA_DIR / "pune_house_prices_combined.csv"
MODEL_DIR = Path(os.environ.get("HOUSE_PRICE_MODELS", PROJECT_ROOT / "models"))
MODEL_FILE = MODEL_DIR / "house_price_model.joblib"
MODEL_INFO_FILE = MODEL_DIR / "model_info.json"
