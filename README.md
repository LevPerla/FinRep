# FinRep

FinRep is a personal finance monitoring project. It is built around manually maintained CSV files and local reports rather than a hosted product or CLI tool.

The project has two report flows:

- `main.py` is the primary, reliable Plotly HTML report generator.
- `src/dashboard/` is an experimental local Dash MVP that reuses the same data and calculation layer.

## Current Project Shape

Source data lives under `data/` and is updated manually, usually weekly.

Important data areas:

- `data/transactions_info/`: protected source transaction CSV files.
- `data/assets_info/`: protected source asset CSV files.
- `data/investments.csv`: investment transactions.
- `data/rates/fx_rates.csv`: long-format FX cache.

Do not delete, move, or auto-clean anything inside `data/transactions_info/` or `data/assets_info/`. Treat these as source-of-truth folders.

Generated outputs live under `reports/`.

## Main Plotly Flow

Run the regular reports with:

```bash
uv run python main.py
```

`main.py` currently generates:

- main report;
- yearly report;
- monthly report.

The selected defaults are configured at the top of `main.py`:

- `CURRENCY`
- `YEAR`
- `MONTH`
- `FX_NETWORK_ENABLED`
- `VALIDATE_DATA`

The preferred workflow is still function-oriented Python entry points, not a CLI rewrite.

## Currency Rates

FX logic is centralized in `src/data/get_finance.py`.

The persistent cache is:

```text
data/rates/fx_rates.csv
```

Its format is:

```text
date;currency;usd_rate;source;fetched_at
```

`usd_rate` means `1 currency -> USD`. Cross-rates are calculated through USD.
For example:

- `KZT -> RUB = KZT/USD / RUB/USD`
- `USD -> RUB = 1 / RUB/USD`
- `RUB -> USD = RUB/USD`

Provider priority is configured in `src/config.py`:

```python
FX_PROVIDER_ORDER = ["yfinance", "cbr"]
```

Normal report generation uses the cache first and fetches missing rates from providers when `FX_NETWORK_ENABLED = True`.

Manual provider comparison is available but is not part of normal report generation:

```python
from src.data.get_finance import update_fx_cache_interactive

update_fx_cache_interactive(["RUB", "KZT", "EUR", "GBP"], "2026-01-01", "2026-04-28")
```

This function fetches rates from providers, compares them, asks which source to cache, and writes only the selected provider data.

## Data Validation And Caching

CSV preflight validation lives in:

```text
src/data/validation.py
```

`main.py` can run validation before report generation with `VALIDATE_DATA = True`.

Within a single Python process, repeated CSV reads and repeated report calculations are cached:

- `src/data/get.py` caches source CSV loading.
- `src/model/create_tables.py` caches repeated report tables.
- Public cached functions return deep copies so report code does not mutate the cached DataFrame.

## Dash MVP

The Dash dashboard is developed in parallel with the existing Plotly report flow. The old `main.py` report generation remains the fallback.

Run the local Dash app:

```bash
uv run python -m src.dashboard.app
```

By default this starts Dash in debug mode with hot reload enabled, so Python code changes should restart the dev server and refresh the browser automatically. After changing the app runner itself, restart the command once.

Useful environment switches:

```bash
FINREP_DASH_DEBUG=0 uv run python -m src.dashboard.app
FINREP_DASH_HOT_RELOAD=0 uv run python -m src.dashboard.app
```

The Dash app currently supports:

- main report tab;
- yearly report tab;
- monthly report tab;
- currency, year, and month selectors;
- per-widget XLSX downloads;
- PNG/PDF export through Playwright.

Dashboard exports are saved under:

```text
reports/dashboard_exports/<currency>/
```

If Playwright Chromium is missing, install it with:

```bash
uv run playwright install chromium
```

Dash is still considered an MVP. Existing Plotly reports should remain available and working while the Dash UI evolves.

## Project Plans

Planning documents:

- `PROJECT_PLAN.md`: cleanup and reliability work for the core project.
- `DASH_MVP_PLAN.md`: Dash dashboard MVP and extension plan.

`FinRep.code-workspace` is intentionally kept tracked. It should stay portable and point to `"."`, not to an absolute local path.

## Useful Checks

Run these after meaningful changes:

```bash
uv run python -c "from src.data.validation import validate_all_data; issues = validate_all_data(False); print(f'issues={len(issues)}')"
PYTHONPYCACHEPREFIX=/private/tmp/finrep_pycache python3 -m compileall main.py src
uv run python main.py
```

For Dash import/layout smoke checks:

```bash
uv run python -c "from src.dashboard.app import create_app; app = create_app(); print(app.title)"
```
