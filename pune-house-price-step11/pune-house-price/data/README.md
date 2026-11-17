# Pune house price dataset

`pune_house_prices_combined.csv`: **68,978 real Pune apartment listings** from 3 sources, de-duplicated.
Rebuild it from the raw files with:

```
python -m house_price.build_dataset
```

The raw listing files, row-level cleaned/features/split files, and per-listing prediction exports are not
distributed in this repository. The cited source terms limit the listing data to academic, non-commercial
use. Obtain and review the source data and applicable terms before running the dataset-building or model
training steps. The trained prediction bundle and aggregate evaluation reports are included.

## Files

| File | Rows | What it is |
|---|---|---|
| `raw/magicbricks_pune_a.csv` | 33,502 | MagicBricks Pune sale listings (scrape 1) |
| `raw/magicbricks_pune_b.csv` | 29,701 | MagicBricks Pune sale listings (scrape 2; 14,638 overlap with scrape 1) |
| `raw/pune_apartments_25k.csv` | 25,215 | Pune apartment listings (area, BHK, bath, balcony, locality, price) |
| `pune_house_prices_combined.csv` | 68,978 | All three combined, common columns, duplicates removed |

## Columns

| Column | Meaning | Filled |
|---|---|---|
| source | Which raw file the row came from | 100% |
| locality | Area / region in Pune (e.g. Baner, Kharadi) | 100% |
| latitude, longitude | Location of the listing | 65% |
| property_type | Multistorey Apartment, Apartment, Builder Floor Apartment, Penthouse | 100% |
| bedrooms | Number of bedrooms (BHK) | 100% |
| bathrooms | Number of bathrooms | 100% |
| balconies | Number of balconies | 67% |
| area_sqft | Built-up / covered area, sqft | 100% |
| carpet_area_sqft | Carpet area, sqft | 51% |
| floor_number | Floor the flat is on | 63% |
| total_floors | Number of floors in the building | 65% |
| age | Age of construction (text band, e.g. "5 to 10 years") | 56% |
| furnished | Unfurnished / Semi-Furnished / Furnished | 64% |
| transaction | Resale / New Property | 65% |
| **price_lakh** | **Asking price in INR lakh (target to predict)** | 100% |

Empty cells mean the source did not provide that field; the model handles them.

## How the combined file was made
1. MagicBricks scrapes 1 and 2 joined; same listing URL kept once (63,203 → 44,860).
2. Rows without locality, bedrooms, area or price removed.
3. The 25k apartment file added, minus 1,097 rows already present (same locality, BHK, area and price).

Step 3 removes rows outside fixed physical and broad price-per-square-foot bounds and drops exact
duplicates. It does not filter prices against a locality median calculated from the whole dataset:
that would use held-out target values and can discard genuine premium or underpriced listings.

## Coverage
- 1,346 localities; 196 have 30+ listings (91% of rows), 93 have 100+ listings (83% of rows).
- BHK: 1 = 11,943 · 2 = 30,031 · 3 = 19,722 · 4 = 6,321 · 5+ ≈ 950.
- Locality strongly affects price (Kruskal-Wallis p ≈ 0): e.g. median ₹/sqft ~3,300 in Chakan vs ~16,000 in Model Colony.

## Limitations
- Apartments only; no bungalows / independent houses.
- Asking prices from listings, not registered sale prices.
- Listings collected around 2023–2024; recent prices may be higher.

## Sources (credit these in the project report)
- MagicBricks listings collected by the open-source project
  https://github.com/joshiaditya0511/pune-house-price-prediction (`Stationary/Data` and `Stationary/Iteration_3/Data`).
  Only the columns used here were kept.
- Pune apartment listings from https://github.com/saumitra-cell/Pune_house_price (`data/pune_house_data.csv`).
- Original listing data © MagicBricks and the respective sites; used here for academic, non-commercial purposes.

Not used: the widely shared `Pune_House_Data.csv` (13,320 rows). Its locality column is assigned in rotation
(every locality has 139 rows) and has no relationship with price, so it would teach the model nothing about region.
