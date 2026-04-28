# FinRep Improvement Plan

## Context

FinRep is a personal finance monitoring project. Source data is stored in CSV files under `data/` and updated weekly. Reports are generated with Plotly from functions called in `main.py`: main, yearly, and monthly reports. Currency conversion is handled by a dedicated data module that fetches FX rates and converts transactions to the selected target currency.

## Plan

- [x] Clean unused dependencies in `pyproject.toml` and refresh `uv.lock`.
- [x] Remove obsolete absolute `sys.path` modifications.
- [x] Add a CSV preflight validation step for weekly data updates.
- [x] Simplify and centralize currency-rate/conversion logic.
- [x] Cache data loading and repeated calculations within one `main.py` run.
- [x] Extract repeated report blocks into shared helpers.
- [x] Make report directory creation robust with `Path.mkdir(parents=True, exist_ok=True)`.
- [x] Clean or fix permissions for stale `__pycache__` directories.
- [x] Decide whether to commit or ignore `FinRep.code-workspace`.

## Notes

- Keep `main.py` as the preferred entry point. No CLI rewrite is needed.
- Keep the project function-oriented unless a small helper clearly reduces duplication or risk.
- Prioritize reliability around CSV parsing and currency conversion, because those are the main places where weekly manual updates can break reports.
- `FinRep.code-workspace` is tracked and contains only a portable `"."` folder path, so keep it committed.
- Next plan: see `DASH_MVP_PLAN.md` for the parallel Dash dashboard MVP. Existing Plotly reports must remain available while that MVP is built.
