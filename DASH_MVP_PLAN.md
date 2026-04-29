# Dash MVP Plan

## Context

FinRep is a personal finance tracker based on CSV files that are updated weekly. The current reporting layer builds Plotly HTML dashboards for three report types: main, yearly, and monthly. Currency conversion is handled by the existing finance data modules and should remain the shared source of truth.

## Goal

Build a local Dash MVP for a more flexible dashboard experience while preserving the current Plotly report generation flow.

The MVP should focus on the main report first and prove the new interaction model:

- responsive layouts for different screen sizes;
- tabbed report sections;
- PNG/PDF export of the rendered page or active tab;
- XLSX downloads for chart and table data;
- enough customization to adjust visualizations without low-level frontend work.

## Compatibility Requirement

- The Dash MVP must be implemented in parallel with the existing logic.
- Existing Plotly HTML reports must remain available and working.
- `main.py` should continue to generate the current main, yearly, and monthly Plotly reports unless a later plan explicitly changes that behavior.
- Existing functions such as `create_main_report`, `create_year_report`, and `create_month_report` should not be removed or replaced as part of this MVP.
- Shared data preparation and currency conversion functions may be reused by the Dash app, but changes to shared code must keep the old Plotly reports compatible.

## MVP Scope

- Add a new local Dash app entry point, for example `uv run python -m src.dashboard.app`.
- Start with the main report only.
- Keep Plotly as the charting library for the first version, but render charts as separate responsive `dcc.Graph` components instead of one large fixed-height subplot.
- Use one tab per report type:
  - Main report;
  - Yearly report;
  - Monthly report.
- Inside each report tab, keep visual blocks in the same order as the corresponding existing Plotly dashboard.
- Add per-widget data downloads to XLSX.
- Add page or active-tab export to PNG/PDF for future Telegram sharing.

## Implementation Plan

- Add dashboard dependencies: `dash`, `dash-bootstrap-components`, `dash-ag-grid`, `openpyxl`, and `playwright`.
- Create a dashboard data adapter that exposes main-report datasets as pandas DataFrames and Plotly figures without depending on HTML file generation.
- Build a Dash layout with a top control bar for currency, refresh, PNG export, and PDF export.
- Use responsive containers and `dcc.Graph(responsive=True)` so charts resize with the viewport instead of relying on fixed Plotly figure heights.
- Use Dash AG Grid or Dash download callbacks for table/chart data export.
- Use Playwright for full-page or active-tab PNG/PDF export after the Dash page has rendered.
- Save dashboard exports under `reports/dashboard_exports/<currency>/`.

## Step-by-step Checklist

### Phase 1: Dependencies and App Skeleton

- [x] Add dashboard dependencies to `pyproject.toml` and refresh `uv.lock`.
- [x] Create a new `src/dashboard/` package without changing the existing `src/reports/` entry points.
- [x] Add `src/dashboard/app.py` with a minimal Dash app and a local run command.
- [x] Add a simple landing layout with a title, currency selector, and placeholder tabs.
- [x] Confirm `uv run python -m src.dashboard.app` starts locally.
- [ ] Confirm `uv run python main.py` still uses the existing Plotly report flow.

### Phase 2: Shared Main-report Data Adapter

- [x] Create a dashboard data adapter for the main report, separate from HTML rendering.
- [x] Reuse existing model/data functions such as `get_balance_by_month` and FX helpers.
- [x] Return raw or analysis-ready DataFrames for yearly stats, FX info, income/expense trend, delta, and capital.
- [x] Keep formatted display values separate from numeric values needed for charts and XLSX export.
- [x] Add a lightweight dataset registry with stable widget IDs and human-readable titles.
- [x] Add tests or smoke checks that the adapter returns expected datasets for a supported currency.

### Phase 3: Main-report Dash UI

- [x] Replace section tabs with report-level tabs: main, yearly, and monthly.
- [x] Keep main-report widgets stacked in the original Plotly dashboard order.
- [x] Render each chart as an independent `dcc.Graph(responsive=True)`.
- [x] Move away from fixed full-report Plotly subplot heights in the Dash UI.
- [x] Add responsive layout containers using `dash-bootstrap-components`.
- [x] Add table views for summary and FX data using Dash AG Grid or a simple Dash table component.
- [x] Add empty/error states for missing CSV data or unavailable FX data.
- [x] Verify the UI remains usable on desktop and mobile-width viewports.

### Phase 4: XLSX Downloads

- [x] Add per-widget download buttons for chart/table source data.
- [x] Implement XLSX generation with `openpyxl` or pandas Excel writer.
- [x] Use clear filenames that include report type, widget ID, currency, and date.
- [x] Preserve numeric columns for Excel analysis instead of exporting only preformatted strings.
- [x] Validate generated XLSX files can be opened and contain expected columns.

### Phase 5: PNG/PDF Page Export

- [x] Add export buttons for PNG and PDF in the Dash top control bar.
- [x] Implement a Playwright-based export helper that opens the current dashboard URL.
- [x] Wait for Dash and Plotly charts to finish rendering before capture.
- [x] Export the active tab or full rendered page to `reports/dashboard_exports/<currency>/`.
- [x] Use filenames that include report type, active tab, currency, and timestamp.
- [x] Verify exported PNG/PDF files are non-empty and do not cut off visible charts.

### Phase 6: Compatibility and Documentation

- [x] Keep existing Plotly report functions available and unchanged unless a compatibility-preserving refactor is required.
- [x] Keep `main.py` behavior intact for the current main/year/month report generation.
- [x] Document the new Dash command in `README.md`.
- [x] Document that Dash MVP is experimental and the current Plotly reports remain the fallback.
- [x] Add a final compatibility smoke check for both the Dash app import and old Plotly report imports.
- [x] Update this checklist as tasks are completed.

## Test Plan

- Verify the new Dash app imports and builds its layout without running the old report generation.
- Verify the existing `main.py` Plotly report flow still imports and remains callable.
- Add focused tests for the dashboard data adapter: expected dataset IDs, expected columns, and non-empty outputs when source data exists.
- Validate generated XLSX files with `openpyxl`.
- Use Playwright smoke checks for desktop and mobile viewport sizes to confirm charts render and tabs switch.
- Verify PNG/PDF export creates non-empty files and does not cut off the active tab content.

## Next Extension Plan: Yearly and Monthly Reports

### Extension Goal

Add Dash versions of the existing yearly and monthly reports while preserving the current Plotly report functions as fallback.

The `Годовой отчет` and `Месячный отчет` tabs should stop being placeholders and become full report pages. Each tab should keep the visual block order from the corresponding existing Plotly dashboard, use the shared XLSX/export mechanisms, and reuse existing data/currency functions instead of duplicating CSV parsing or FX logic.

### Yearly Report Checklist

- [x] Add year selector to the top control bar or a report-specific controls row.
- [x] Create `build_year_dashboard_data(year, currency, fx_network_enabled=False)` in a dashboard data adapter.
- [x] Reuse existing data functions such as `get_balance_by_month`, `get_cost_distribution`, and FX info helpers.
- [x] Return stable datasets for quarter totals, FX rates, cost distribution, cost distribution chart, income by month, cost by month, income/cost stats, capital by month, and capital chart.
- [x] Keep raw numeric DataFrames separate from display-formatted tables for XLSX export.
- [x] Render the yearly report tab in the same order as `src/reports/year_report.py`.
- [x] Use independent responsive Plotly graphs instead of one large fixed subplot.
- [x] Use Dash AG Grid for table-heavy blocks.
- [x] Add XLSX buttons for every yearly report widget using the existing download callback pattern.
- [x] Ensure PNG/PDF export works for the yearly tab and filenames include `year`.
- [x] Add smoke checks for supported year/currency combinations.

### Monthly Report Checklist

- [x] Add month selector next to the year selector, visible or relevant for the monthly tab.
- [x] Create `build_month_dashboard_data(year, month, currency, fx_network_enabled=False)` in a dashboard data adapter.
- [x] Reuse existing data functions such as `get_month_transactions`, `get_balance_by_month`, `get_act_receivables`, `get_act_liabilities`, `get_cost_distribution`, `get_assets_by_currencies`, and FX info helpers.
- [x] Return stable datasets for transactions, FX rates, monthly summary stats, receivables, liabilities, cost distribution, cost distribution chart, and assets by currency/account.
- [x] Keep raw numeric DataFrames separate from display-formatted tables for XLSX export.
- [x] Render the monthly report tab in the same order as `src/reports/month_report.py`.
- [x] Use responsive table heights and pagination so large transaction tables remain usable on mobile and desktop.
- [x] Add XLSX buttons for every monthly report widget using the existing download callback pattern.
- [x] Ensure PNG/PDF export works for the monthly tab and filenames include `year` and `month`.
- [x] Add smoke checks for the currently selected `YEAR`/`MONTH` defaults from `main.py`.

### Shared Extension Tasks

- [ ] Refactor common dashboard UI helpers if yearly/monthly implementation duplicates main-report rendering patterns.
- [x] Extend URL state with `year` and `month` query parameters so export URLs reproduce the selected report state.
- [x] Extend export filename generation to include report type, currency, year, month when applicable.
- [x] Keep old `create_year_report` and `create_month_report` untouched unless a compatibility-preserving shared helper extraction is needed.
- [x] Add final compatibility smoke checks for Dash main/year/month tabs and old Plotly report imports.
- [x] Update README with the supported Dash report tabs and selector behavior.

## Assumptions

- The old Plotly reports stay as the reliable fallback until a later migration plan says otherwise.
- The first Dash version is local-only and does not require authentication, hosting, or Telegram integration.
- The first export implementation captures the rendered page state controlled by Dash UI inputs, not manual browser-only Plotly interactions such as temporary zoom or pan.
- More advanced charting libraries can be evaluated later after the Dash shell proves useful.

## Yearly Dashboard Polish

- [x] Add pastel conditional formatting to the Yearly Stats grid: green balance for positive values, red for negative values, green income gradient, and red expense gradient.
- [x] Disable mouse wheel zoom for the yearly Income and Expense chart because the range slider already covers zooming and wheel zoom interferes with scrolling.
- [x] Add sparse, readable value labels to yearly Delta and Capital charts so the trend is informative without label collisions.
- [x] Add conditional formatting to the yearly quarter totals grid.
- [x] Move the yearly cost distribution chart above the table, expand the table height, and add red conditional formatting to cost columns.
- [x] Format yearly monthly-table dates as `YYYY-MM-DD` and add green/red conditional formatting to income, expense, and capital columns.
- [x] Remove data zoom range sliders from the yearly income/expense and capital charts.

## Planning And Forecast V1

- [x] Add `data/plans/goals.csv` as a manual annual-goals file with monthly income/expense targets.
- [x] Add a Dash planning adapter for annual goals, 12-month capital forecast, runway in months/years, and FX stress scenarios.
- [x] Support entering goals once in one currency and converting them for the selected dashboard currency.
- [x] Show capital facts from the start of the previous year before the 12-month forecast.
- [x] Add explicit FX scenario labels where the selected dashboard currency strengthens or weakens against other currencies.
- [x] Add the `План и прогноз` Dash tab without changing the legacy Plotly report flow.
- [x] Keep planning data outside protected source folders `data/transactions_info/` and `data/assets_info/`.
