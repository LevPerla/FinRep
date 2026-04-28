# FinRep Improvement Plan

## Context

FinRep is a personal finance monitoring project. Source data is stored in CSV files under `data/` and updated weekly. Reports are generated with Plotly from functions called in `main.py`: main, yearly, and monthly reports. Currency conversion is handled by a dedicated data module that fetches FX rates and converts transactions to the selected target currency.

## Plan

- [x] Clean unused dependencies in `pyproject.toml` and refresh `uv.lock`.
- [x] Remove obsolete absolute `sys.path` modifications.
- [x] Add a CSV preflight validation step for weekly data updates.
- [x] Simplify and centralize currency-rate/conversion logic.
- [x] Cache data loading and repeated calculations within one `main.py` run.
- [ ] Extract repeated report blocks into shared helpers.
- [ ] Make report directory creation robust with `Path.mkdir(parents=True, exist_ok=True)`.
- [ ] Clean or fix permissions for stale `__pycache__` directories.
- [ ] Decide whether to commit or ignore `FinRep.code-workspace`.

## Notes

- Keep `main.py` as the preferred entry point. No CLI rewrite is needed.
- Keep the project function-oriented unless a small helper clearly reduces duplication or risk.
- Prioritize reliability around CSV parsing and currency conversion, because those are the main places where weekly manual updates can break reports.
