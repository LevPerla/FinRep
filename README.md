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
- `data/investments/investments.csv`: investment transactions.
- `data/rates/fx_rates.csv`: long-format FX cache.

Do not delete, move, or auto-clean anything inside `data/transactions_info/` or `data/assets_info/`. Treat these as source-of-truth folders.

Generated outputs live under `reports/`.

## Investment Data

The legacy investment source remains:

```text
data/investments/investments.csv
```

The normalized investment layer lives in `src/data/investments.py`. It can read the legacy CSV in memory and migrate it into three v1 tables:

```text
data/investments/transactions.csv
data/investments/instruments.csv
data/investments/price_cache.csv
```

Schemas:

```text
transactions.csv: date;operation;asset_type;ticker;quantity;price;currency;fee;account;comment
instruments.csv: ticker;name;asset_type;currency;provider;exchange
price_cache.csv: date;ticker;price;currency;source;fetched_at
```

Supported v1 asset types are `stocks`, `funds`, and `crypto`; bonds are intentionally left for a later dedicated model. The migration/export helper is explicit, so normal report runs keep using `data/investments/investments.csv` until the investment calculations phase switches over.

Investment calculations live in:

```text
src/data/investment_calculations.py
src/dashboard/investment_data.py
```

Dash has an `Инвестиции` tab with portfolio value, realized/unrealized PnL, allocation by asset type/currency, and a positions table. PnL uses FIFO. Prices are read from `data/investments/price_cache.csv`; when the cache has no row for a known ticker, FinRep seeds a reproducible baseline from the latest transaction price with `source=latest_transaction`. Provider refresh is available through `update_price_cache_from_providers(...)` and writes provider prices into the same cache before reports use them.

Crypto wallets are part of the investment layer:

```text
data/investments/crypto_wallets.csv
data/investments/crypto_balances.csv
data/investments/crypto_transactions.csv
```

Wallet config schema:

```text
account;chain;asset;address;token_contract;enabled;label
```

Use `account` for wallet names such as `ledger`, `tangem`, or `base wallet`. Supported v1 assets are `BTC`, `ETH`, `TON`, `SOL`, `LINK`, `KAS`, and `USDT`; supported chains are `bitcoin`, `ethereum`, `base`, `ton`, `solana`, and `kaspa`. For EVM tokens such as `LINK` or `USDT`, set `token_contract` unless FinRep has a default contract for that chain/asset.

Crypto balances are loaded into the same `Инвестиции` portfolio table as `asset_type=crypto`. Balance and price refreshes are explicit:

```python
from src.data.crypto import refresh_crypto_balances, refresh_crypto_price_cache

refresh_crypto_balances()
refresh_crypto_price_cache(["BTC", "ETH", "TON", "SOL", "LINK", "KAS", "USDT"])
```

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

## Planning And Forecasting

Dash includes a `План и прогноз` tab. It uses manually maintained annual goals from:

```text
data/plans/goals.csv
```

Expected columns:

```text
year;currency;target_capital;target_monthly_income;target_monthly_expense;notes
```

`target_monthly_income` and `target_monthly_expense` are average monthly goals. Goals can be entered once in one currency, for example `RUB`; when another dashboard currency is selected, money goals are converted through the FX layer.

The planning tab shows goal progress, a capital chart with facts from the start of the previous year plus a 12-month forecast, runway in months and years based on average monthly expenses, and FX stress scenarios `-20%`, `-10%`, `0%`, `+10%`, `+20%`. In FX scenarios the selected dashboard currency is what changes: for example, `RUB +20%` means RUB strengthens against the other asset currencies by 20%, so non-RUB assets become worth less in RUB.

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

Dash uses cached FX rates by default. Normal tab/filter changes read `data/rates/fx_rates.csv` and fill weekends/known FX holidays from the nearest cached value. Press `Обновить курс` in the dashboard toolbar when you want the FX layer to try configured providers for missing fetchable dates and append only provider-returned rows to the long cache.

The Dash app currently supports:

- main report tab;
- yearly report tab;
- monthly report tab;
- planning and forecast tab;
- manual transaction input tab backed by staging drafts;
- currency, year, and month selectors;
- per-widget XLSX downloads;
- PNG/PDF export through Playwright.

Manual transaction drafts live in:

```text
data/staging/transaction_drafts.csv
```

The `Ввод данных` tab writes new rows to staging drafts first. It can also import Kaspi Gold PDF statements into the same staging flow. The importer parses dates, amounts, currencies, operation text, applies merchant/category rules from `data/import_rules/categories.csv`, and shows a preview before saving. Internal account transfers such as `To Kaspi Deposit` are marked as `skip` and do not enter staging. Duplicate control happens twice: during preview and again during save, using `source="kaspi_pdf"`, a stable `source_id`, and date/currency/amount comparison against already loaded source transactions.

The export block can preview the final monthly source CSV, then write it to `data/transactions_info` only after explicit confirmation and after creating a timestamped backup of the previous month file under `data/backups/transactions_info/<year>/`. Backups are intentionally kept outside `data/transactions_info` so reports do not read them as duplicate source data. When a selected monthly transaction CSV does not exist yet, Dash creates an empty month file from the previous month columns and fills all days with zeroes.

The `Ввод данных` tab also has an `Активы` sub-tab for monthly asset snapshots. It edits the file selected by the dashboard year/month filters under `data/assets_info/<year>/<year>_<month>.csv`. If that file does not exist yet, it is created as a copy of the latest previous month snapshot so the account list does not need to be re-entered. Saving writes the edited table directly to CSV and creates a backup under `data/backups/assets_info/<year>/`.

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
