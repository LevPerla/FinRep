# FinRep Next Roadmap

## Context

FinRep is moving from a report generator into a personal finance workspace. The current stable core is still CSV-based: transactions and assets are the source of truth, reports are generated from `main.py`, and Dash is the interactive UI layer.

Important rule: do not delete, move, or automatically rewrite files inside `data/transactions_info` or `data/assets_info` without an explicit confirmation step and backup.

## Goal

Make FinRep easier to maintain and more complete:

- simplify manual transaction input;
- import Kaspi PDF statements into a reviewable staging area;
- convert active receivables and liabilities into the selected report currency;
- build a serious investment layer for stocks, funds, and crypto;
- deploy Dash safely on a private server through Docker and VPN/local network access.

## Phase 1: Currency-aware debts

Purpose: fix a known correctness gap before adding new workflows.

- [x] Add `currency` parameter to active receivables calculation.
- [x] Add `currency` parameter to active liabilities calculation.
- [x] Preserve raw debt grouping by `Комментарий`.
- [x] Convert both debt creation and repayment rows using their original transaction currency.
- [x] Keep backward-compatible defaults for existing report code.
- [x] Update monthly Dash data adapter to request debts in the selected report currency.
- [x] Update legacy monthly Plotly report to display debts in the selected report currency.
- [x] Add validation/smoke check for `RUB`, `KZT`, `USD`, and `EUR`.

Done when:

- [x] `Дебиторская задолженность` and `Кредиторская задолженность` change correctly when the report currency changes.
- [x] Existing `main.py` report generation still works.
- [x] Dash monthly tab still renders.

Verification note: `validate_all_data(False)`, Dash layout import, debt calls for `RUB/KZT/USD/EUR`, and `uv run python main.py` pass. `compileall` is replaced by AST syntax checks in this sandbox because local `__pycache__` writes are permission-blocked.

## Phase 2: Transaction staging model

Purpose: create a safe buffer between new input flows and protected source CSV files.

- [x] Add a new staging folder outside protected source folders, for example `data/staging/`.
- [x] Add a transaction draft CSV format:
  `date;category;currency;amount;comment;source;source_id;status`.
- [x] Support statuses: `draft`, `ready`, `exported`, `ignored`.
- [x] Add a data module that can read, validate, append, update, and list draft transactions.
- [x] Add stable generated `source_id` for manual entries.
- [x] Add duplicate detection by `source + source_id`.
- [x] Add validation for date, category, currency, amount, and status.
- [x] Ensure staging reads never mutate `data/transactions_info`.

Done when:

- [x] A draft transaction can be added and read back from staging.
- [x] Invalid draft rows produce readable validation issues.
- [x] Duplicate source rows are detected before export.

Verification note: temporary staging append/read/update, duplicate detection, `validate_all_data(False)`, staging AST syntax check, and Dash layout import pass.

## Phase 3: Dash manual transaction input

Purpose: make weekly manual entry easier than editing wide monthly CSVs.

- [x] Add a new Dash tab `Ввод данных`.
- [x] Add a compact transaction form with fields:
  date, category, currency, amount, comment.
- [x] Pre-fill date with today and currency with the selected dashboard currency.
- [x] Use current known categories from transaction CSV columns.
- [x] Save submitted entries into staging as `draft`.
- [x] Add a staging table with edit/delete/status controls.
- [x] Add filtering by month, category, status, and source.
- [x] Add XLSX/CSV export for staging review.

Done when:

- [x] A transaction can be entered from Dash and appears in the staging table.
- [x] Draft rows can be edited before export.
- [x] No direct write to `data/transactions_info` happens from the form.

Verification note: Dash input layout import, callback registration, staging add/save/delete helper flow, CSV export data path, `validate_all_data(False)`, and Dash layout import pass.

## Phase 4: Export staging into monthly CSV

Purpose: keep the existing report engine while making input easier.

- [x] Add a converter from long staging rows into the current wide monthly CSV format.
- [x] Preserve the current multi-entry cell syntax: `amount|CUR|comment#amount|CUR|comment`.
- [x] Merge draft rows with existing target month data in memory first.
- [x] Show a preview of the final monthly table before writing.
- [x] Require explicit confirmation before writing to `data/transactions_info`.
- [x] Create a timestamped backup of the old month CSV before overwrite.
- [x] Mark exported staging rows as `exported`.
- [x] Add a rollback note/path in the UI after export.

Done when:

- [x] A manually entered draft can become a normal monthly transaction visible in reports.
- [x] Existing month CSV content is preserved during merge.
- [x] Backup is created before any overwrite.

Verification note: export flow tested on `/private/tmp` with existing month CSV, multi-entry merge, backup creation, CSV write, and exported status update. `validate_all_data(False)`, Dash layout import, old report imports, and AST syntax checks pass.

## Phase 5: Kaspi PDF import

Purpose: reduce manual entry by importing bank statements into the same staging flow.

- [x] Add `pdfplumber` dependency.
- [x] Add `data/import_rules/categories.csv` for merchant/category mapping.
- [x] Add `src/data/importers/` package with a Kaspi PDF adapter.
- [x] Parse statement date, operation text, amount, currency, and transaction identifier/hash.
- [x] Normalize expenses and income into the staging long format.
- [x] Auto-assign category when a rule matches.
- [x] Mark unknown categories for manual review.
- [x] Add PDF upload to the Dash `Ввод данных` tab.
- [x] Show import preview before saving into staging.
- [x] Deduplicate repeated imports using `source="kaspi_pdf"` and `source_id`.

Done when:

- [x] A Kaspi PDF can be uploaded and previewed.
- [x] Accepted rows land in staging.
- [x] Re-uploading the same PDF does not create duplicate rows.
- [x] Unknown rows remain editable before export.

Verification note: `data/bank_data/Kaspi/gold_statement.pdf` parses into 121 rows; 103 rows are import candidates and 18 are skipped as existing source duplicates. Duplicate save protection was tested with a temp staging file: identical operations inside one preview get stable distinct IDs by occurrence order; accidental duplicate IDs in the same save are skipped instead of raising raw validation errors; repeated save skips already staged rows. `validate_all_data(False)`, Dash layout import, and compileall pass.

## Phase 6: Investment data model

Purpose: replace the old partial investment attempt with a durable model.

- [ ] Define transaction schema for investments:
  date, operation, asset_type, ticker, quantity, price, currency, fee, account, comment.
- [ ] Define instrument registry:
  ticker, name, asset_type, currency, provider, exchange.
- [ ] Define price cache:
  date, ticker, price, currency, source, fetched_at.
- [ ] Keep a reader for current `data/investments.csv`.
- [ ] Add migration/export helper from the old investments CSV into the new model.
- [ ] Support asset types in v1: stocks, funds, crypto.
- [ ] Leave bonds for a later dedicated phase.

Done when:

- [ ] Current `data/investments.csv` can be loaded through the new investment layer.
- [ ] New schema can represent existing buys and sells without data loss.
- [ ] Investment validation reports missing tickers, bad dates, bad quantities, and unsupported currencies.

## Phase 7: Investment calculations and reports

Purpose: make investments useful in analysis, not just stored.

- [ ] Calculate current positions by ticker.
- [ ] Calculate average cost.
- [ ] Calculate realized PnL using FIFO.
- [ ] Calculate unrealized PnL from latest cached prices.
- [ ] Calculate current market value in selected report currency.
- [ ] Add price providers for stocks/funds and crypto with provider fallback.
- [ ] Cache prices before using them in reports.
- [ ] Add an `Инвестиции` Dash tab.
- [ ] Show portfolio value, PnL, allocation by asset type, allocation by currency, and positions table.
- [ ] Add investment value into capital as a separate account/line.

Done when:

- [ ] Portfolio value can be shown in `RUB`, `KZT`, `USD`, and `EUR`.
- [ ] PnL numbers are reproducible from cached prices.
- [ ] Total capital includes investments without double-counting cash assets.

## Phase 8: Private Docker deployment

Purpose: run Dash on a private server without exposing sensitive data publicly.

- [ ] Add Dockerfile for the Dash app.
- [ ] Add docker-compose example.
- [ ] Run Dash with `FINREP_DASH_DEBUG=0`.
- [ ] Mount `data/` as a server volume.
- [ ] Mount `reports/` as a server volume.
- [ ] Do not copy sensitive CSV data into the Docker image.
- [ ] Bind service only to VPN/local network interface.
- [ ] Add healthcheck endpoint or command.
- [ ] Add `.env.example` for deployment settings.
- [ ] Document server setup in README or a dedicated deployment doc.

Done when:

- [ ] Container starts and serves Dash.
- [ ] App reads data from mounted volume.
- [ ] App is not reachable from public internet.
- [ ] Restarting the container does not lose data.

## Cross-cutting checks

Run after each major phase:

```bash
uv run python -c "from src.data.validation import validate_all_data; issues=validate_all_data(False); print(f'issues={len(issues)}')"
PYTHONPYCACHEPREFIX=/private/tmp/finrep_pycache python3 -m compileall main.py src
uv run python -c "from src.dashboard.app import create_app; app=create_app(); assert app.layout is not None; print('dash app ok')"
```

For phases that touch legacy reports:

```bash
uv run python main.py
```

## Suggested order

- [ ] Phase 1: Currency-aware debts.
- [ ] Phase 2: Transaction staging model.
- [ ] Phase 3: Dash manual transaction input.
- [ ] Phase 4: Export staging into monthly CSV.
- [ ] Phase 5: Kaspi PDF import.
- [ ] Phase 6: Investment data model.
- [ ] Phase 7: Investment calculations and reports.
- [ ] Phase 8: Private Docker deployment.

