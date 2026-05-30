# FinRep

FinRep is a local-first personal finance dashboard and report generator. It reads manually maintained CSV files, builds Plotly/Dash views, and writes reports to your own machine. It is not a hosted service and it does not upload your transactions.

Русская версия кратко: FinRep хранит транзакции, активы и отчеты локально. Публичный репозиторий содержит только вымышленные `sample_data/`, чтобы можно было сразу увидеть интерфейс без приватных CSV.

## Quick Start

Clone the repository and enter the project folder:

```bash
git clone https://github.com/LevPerla/FinRep.git
cd FinRep
```

Install dependencies with [uv](https://docs.astral.sh/uv/):

```bash
uv sync
```

Run the Dash dashboard with demo data:

```bash
FINREP_DATA_DIR=sample_data FINREP_REPORTS_DIR=reports uv run python -m src.dashboard.app
```

Open the local URL printed by Dash, usually:

```text
http://127.0.0.1:8050
```

Run the legacy Plotly HTML reports with demo data:

```bash
FINREP_DATA_DIR=sample_data FINREP_REPORTS_DIR=reports uv run python main.py
```

For private use, keep your real CSV files under local `data/` or point `FINREP_DATA_DIR` to another private directory. The default local paths are:

```text
data/
reports/
```

Both are git-ignored.

## Privacy Model

FinRep is designed around local files:

- source transactions live in `data/transactions_info/`;
- asset snapshots live in `data/assets_info/`;
- investment data lives in `data/investments/`;
- generated HTML/PDF/XLSX outputs live in `reports/`.

The app reads and writes those local paths only. Network access is used only when you explicitly refresh market/FX/crypto provider data. Normal dashboard tab changes use cached CSV data.

Before publishing a fork or public repository, run the checklist in `AGENTS_MD/OPEN_SOURCE_CHECKLIST.md`.

## Data Layout

The demo dataset mirrors the private layout:

```text
sample_data/
  transactions_info/2025/*.csv
  transactions_info/2026/*.csv
  assets_info/2025/*.csv
  assets_info/2026/*.csv
  investments/investments.csv
  investments/transactions.csv
  investments/instruments.csv
  investments/price_cache.csv
  rates/fx_rates.csv
  plans/goals.csv
  staging/transaction_drafts.csv
  import_rules/categories.csv
```

Transaction month files are semicolon-separated CSVs. The first column is `Дата`; every other column is a category. A cell can be:

```text
0
1250|RUB|Comment
1250|RUB|First item#850|KZT|Second item
```

Asset snapshots use:

```text
Счет;Сумма
Checking;100000|RUB
Broker cash;1200|USD
```

Legacy investment data uses:

```text
Тип_транзакции;Актив;Тикер;Количество;Дата;Цена
Покупка;Акции;DEMO;8;2026-04-04;95|USD
```

The normalized investment layer also supports:

```text
transactions.csv: date;operation;asset_type;ticker;quantity;price;currency;fee;account;comment
instruments.csv: ticker;name;asset_type;currency;provider;exchange
price_cache.csv: date;ticker;price;currency;source;fetched_at
```

## Dashboard

Run the local Dash app:

```bash
uv run python -m src.dashboard.app
```

Useful environment switches:

```bash
FINREP_DATA_DIR=sample_data uv run python -m src.dashboard.app
FINREP_DASH_DEBUG=0 FINREP_DASH_HOT_RELOAD=0 uv run python -m src.dashboard.app
```

The dashboard includes:

- `Основной отчет`: income, expenses, balance, capital, and asset reconciliation.
- `Год`: yearly category distribution and monthly trends.
- `Месяц`: selected month transactions and category totals.
- `Инвестиции`: portfolio summary, positions, allocation by asset type/currency, and crypto wallet cache status.
- `План и прогноз`: goals, capital forecast, runway, and FX stress scenarios.
- `Ввод данных`: manual transaction drafts, Kaspi PDF import preview, monthly export, and asset snapshot editing.

Exports are written under:

```text
reports/dashboard_exports/<currency>/
```

PNG/PDF dashboard export uses Playwright. If Chromium is missing:

```bash
uv run playwright install chromium
```

## Reports

`main.py` generates the legacy Plotly HTML reports:

- main report;
- yearly report;
- monthly report.

Defaults are configured at the top of `main.py`:

```python
CURRENCY = "RUB"
YEAR = "2026"
MONTH = "04"
FX_NETWORK_ENABLED = True
VALIDATE_DATA = True
```

For demo report output, use:

```bash
FINREP_DATA_DIR=sample_data FINREP_REPORTS_DIR=/tmp/finrep_reports uv run python main.py
```

For strictly offline report generation, set `FX_NETWORK_ENABLED = False` in `main.py` before running the legacy report flow.

## FX And Market Data

FX cache data lives in:

```text
data/rates/fx_rates.csv
```

The format is:

```text
date;currency;usd_rate;source;fetched_at
```

`usd_rate` means `1 currency -> USD`. Cross-rates are calculated through USD. The dashboard reads the cache first and fills weekends/known FX holidays from nearby cached values. Provider refreshes are explicit and append provider-returned rows to the same cache.

## Docker

Copy `.env.example` to `.env`, then choose either demo data or private server paths:

```bash
cp .env.example .env
docker compose up -d --build
docker compose ps
```

By default the compose file binds to `127.0.0.1`. Keep it that way unless the host is protected by a firewall, VPN, or SSH tunnel.

## Useful Checks

Validate CSV data:

```bash
FINREP_DATA_DIR=sample_data uv run python -c "from src.data.validation import validate_all_data; issues = validate_all_data(False); print(f'issues={len(issues)}')"
```

Run a syntax/import check:

```bash
PYTHONPYCACHEPREFIX=/private/tmp/finrep_pycache python3 -m compileall main.py src
```

Dash layout smoke check:

```bash
FINREP_DATA_DIR=sample_data FINREP_REPORTS_DIR=/private/tmp/finrep_reports uv run python -c "from src.dashboard.app import create_app; app = create_app(); assert app.layout is not None; print(app.title)"
```

## Notes For Contributors

Keep real financial data out of git. Do not commit `data/`, `reports/`, `.env`, bank statements, exports, generated PDFs, or `src/secrets.json`.

Русская памятка: публичные изменения должны работать на `sample_data/`. Реальные транзакции и отчеты остаются только локально.
