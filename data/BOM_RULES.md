# BOM rule files

Every engineering rule the BOQ → BOM mapping agent uses comes from the files in this folder.
Edit these files to change behaviour; no code changes are needed. Restart the backend after editing.
The loader is strict: a malformed row or an unknown work key stops start-up with the file name and row number.

| File | Controls | Columns |
|---|---|---|
| `recipes.csv` | Material consumption per unit of work | work_key, match_text, unit, requires_thickness, material_id, qty_per_unit, note |
| `material_catalog.csv` | Materials, purchase units, rates, wastage | material_id, name, category, unit, rate_inr, wastage_pct |
| `mapping_config.json` | Match thresholds, stop-words, thickness keywords, trusted dimension sources, which element types may lend their width as a thickness (`width_as_thickness_element_types`, default `wall` — a room's width is never a thickness) | — |
| `units.csv` | Which purchase units are counted in whole numbers (rounded up) | unit, countable |
| `work_rules.csv` | For work items needing a thickness: valid range, whether reviewed wall width may be used, volume allowance | work_key, thickness_min_mm, thickness_max_mm, thickness_from_dimension_width, volume_allowance_pct, note |
| `recipe_aliases.csv` | Extra phrases that identify a work item | work_key, match_text |
| `scope_rules.csv` | Companion work expected with a work item (reported as possibly missing; never auto-added) | trigger_work_key, expected_work_keys (`a\|b` = any of), quantity_factor (blank = cannot be derived), note |
| `conflict_rules.csv` | Work items that should not both appear (double counting) | work_key_a, work_key_b, message |
| `deduction_rules.csv` | Openings deducted from host work, by size band | host_work_key, opening_element_types, min_opening_area_m2, max_opening_area_m2, deduct_fraction, note |

## How the rules are applied

1. **Thickness** — for work items with `requires_thickness=true`, the agent reads `NN mm` values from the BOQ
   description. Values next to a thickness keyword (`thick`, `thk`, …) win. Only values inside the work item's
   range in `work_rules.csv` are accepted. If none or several remain, the BOM is blocked for review. The reviewed
   wall width is used only where `thickness_from_dimension_width=true` (masonry), never for plaster or mortar beds.
2. **Volume allowance** — `volume_allowance_pct` is added to area × thickness (e.g. plaster undulation).
3. **Opening deductions** — a door/window/opening is deducted from the BOQ line whose `element_id` equals the
   opening's `host_element_id`, using the first matching size band in `deduction_rules.csv`. Openings without a
   host link are reported, not guessed. Set the link with
   `PATCH /extractions/{id}/elements/{index}` → `{"host_element_id": "<wall element id>"}`.
4. **Scope** — if a trigger work item exists without any of its expected companions, a warning and a row in
   `suggestions` are returned with a quantity when it can be derived (same unit × factor). Suggestions are never
   added to the BOM lines; add the work to the BOQ if it is in scope.
5. **Conflicts** — warnings only.

Values in these files (thickness ranges, 20 % plaster allowance, deduction thresholds) are starting points.
Verify them against your project specification, IS 1200 edition and contract measurement rules.

## Adding a new material system (example: AAC block masonry)

Block masonry is intentionally **not** matched to the brick recipe. To support it:
1. Add the block, mortar/adhesive materials to `material_catalog.csv`.
2. Add `aac_masonry` rows to `recipes.csv` (unit `m2`, `requires_thickness=true`, quantities per m³).
3. Add `aac_masonry` to `work_rules.csv` (thickness range), and optionally to `scope_rules.csv` and `deduction_rules.csv`.
