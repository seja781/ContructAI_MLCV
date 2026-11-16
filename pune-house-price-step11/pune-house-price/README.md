# Pune House Price Prediction

A separate ML project: enter **region, floors, rooms and square feet**, and the model predicts the price of a
Pune flat. It's built here on its own first, then added to ConstructAI later.

## Setup (Windows PowerShell)

```powershell
cd pune-house-price
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

## Progress

| Step | What | Command | Status |
|---|---|---|---|
| 1 | Dataset: combine 3 real Pune sources (68,978 listings) | `python -m house_price.build_dataset` | ✅ |
| 1b | Price update factor (2024 → today, NHB RESIDEX) | `python -m house_price.adjustment 80` | ✅ |
| 2 | Explore the data (EDA): 8 charts + report in reports/eda | `python -m house_price.explore` | ✅ |
| 3 | Clean: standardize, enforce fixed bounds, remove exact duplicates → data/pune_clean.csv (63,687 rows) | `python -m house_price.clean` | ✅ |
| 4 | Features: 21 model inputs → data/pune_features.csv + locality_table.csv | `python -m house_price.features` | ✅ |
| 5 | Train/test split: 80% train (50,949) / 20% test (12,738) → data/split/ | `python -m house_price.split` | ✅ |
| 6a | Rule baseline uses exact locality, BHK, property type and area-similar training listings, with locality/city fallbacks | `python -m house_price.train` | ✅ |
| 6b | Compare models using five-fold, price-stratified grouped CV; report MAE, RMSE and MAPE | `python -m house_price.train` | ✅ |
| 7 | Tune LightGBM by grouped CV on training rows; report MAE, RMSE and MAPE | `python -m house_price.tune` | ✅ |
| 8 | Compare models and the LightGBM/random-forest blend on the fixed random holdout; also report grouped CV and train/validation gaps | `python -m house_price.evaluate` | ✅ |
| 9 | Explain: shuffle test + SHAP → area most important, then region, rooms, floors | `python -m house_price.explain` | ✅ |
| 10 | Price range: low/high LightGBM (quantile) calibrated by CV → 81.5% of test prices inside, typical −19% / +24% | `python -m house_price.price_range` | ✅ |
| 11a | Train final models on all 63,687 flats → models/house_price_model.joblib + model_info.json | `python -m house_price.save_model` | ✅ |
| 11b | `predict()`: locality, bedrooms, sqft (+ floors) → today's price, range, ₹/sqft, top reasons | `python -m house_price.predictor --locality Baner --bedrooms 2 --area 1050 --floor 5 --total-floors 12` | ✅ |
| 12 | Add to ConstructAI (API + dashboard screen) | `POST /house-price/predict` | ✅ |

## Folder layout

```
pune-house-price/
  data/
    raw/                              original source files
    pune_house_prices_combined.csv    combined dataset (step 1)
    pune_clean.csv                    cleaned dataset (step 3)
    locality_aliases.csv              spelling fixes for locality names (step 3)
    locality_table.csv                locality list, group and centre coordinates (step 4)
    pune_features.csv                 model inputs (step 4)
    split/train.csv, split/test.csv   training and test rows (step 5)
    config.json                       settings, incl. price update factor
    README.md                         data dictionary and sources
  house_price/
    paths.py                          all file locations (the only file to change when merging)
    build_dataset.py                  step 1
    adjustment.py                     step 1b
    explore.py                        step 2
    clean.py                          step 3
    features.py                       step 4 (also used by the app later)
    split.py                          step 5
    train.py                          step 6
    tune.py                           step 7
    evaluate.py                       step 8
    explain.py                        step 9
    price_range.py                    step 10
    save_model.py                     step 11a
    predictor.py                      step 11b (what the app calls)
  models/                             saved model bundle (tracked with Git LFS; rebuild with save_model)
  reports/eda/                        charts + eda_report.md (step 2)
  reports/cleaning/                   cleaning_report.md (step 3)
  reports/features/                   feature_report.md + chart (step 4)
  reports/split/                      split_report.md + chart (step 5)
  reports/models/                     model_comparison.md/.csv + chart (step 6)
  reports/tuning/                     tuning_report.md, tuning_results.csv + chart (step 7)
  reports/evaluation/                 evaluation_report.md, test_predictions.csv + 4 charts (step 8)
  reports/explain/                    explain_report.md + 6 charts (step 9)
  reports/price_range/                price_range_report.md + 3 charts (step 10)
  requirements.txt
```

## Updating prices (every quarter)
Edit `price_adjustment` in `data/config.json` using the latest Pune figure from NHB RESIDEX
(https://residex.nhbonline.org.in/), then run `python -m house_price.adjustment 80` to check it.

## Accuracy and validation notes
- Cross-validation holds listings with the same locality, BHK, property type and 25 sqft area band
  together, then stratifies folds by price. This is stricter than randomly splitting similar listings.
- The fixed test split remains a random, stratified split created before grouped validation was added;
  its MAE is useful for comparison but can be optimistic when similar listings occur on both sides.
- The final blended model currently measures **₹16.65 lakh MAE, ₹35.96 lakh RMSE and 13.53% MAPE**
  on that random holdout; its grouped training-only CV is **₹18.35 lakh MAE, ₹38.89 lakh RMSE
  and 14.48% MAPE**. Use grouped CV as the more conservative generalization estimate.
- The rule-based baseline's wider area-comparison kernel reduced grouped-CV MAE from ₹24.88 lakh
  to ₹24.40 lakh, but its random-holdout MAE moved from ₹22.37 lakh to ₹23.09 lakh. This is not a
  consistent improvement across both evaluation methods.
- These absolute errors are at the 2024 source-price level. With the current Oct 2026 price factor
  of 1.086, the same holdout errors would scale to about **₹18.08 lakh MAE and ₹39.05 lakh RMSE**
  in today's-price units (MAPE is unchanged). This is an adjustment, not validation against recent sales.
- Prices are historical asking prices (2023-2024), not completed sales. The app multiplies predictions
  by the configured market adjustment factor, so today's-price errors scale by that same factor.
- Age is missing for about 41% of cleaned listings, floor details for about 32-34%, and location for
  about 1.6%. Features such as furnishing and transaction type are not used because users do not provide
  them at prediction time. Do not treat their omission, or a low single-feature correlation, as proof
  that they are intrinsically unimportant.
- Random-fold validation initially looked more optimistic because same-locality, similar-size listings
  can appear on both sides of a random split. CatBoost and XGBoost are not installed in the project
  environment; model comparisons use the existing scikit-learn and LightGBM dependencies.
- A nested grouped-CV residual-learning check worsened MAE/MAPE, so no residual layer was deployed.
