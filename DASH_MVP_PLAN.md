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
- Use tabs for report nesting, for example:
  - Overview;
  - Cashflow;
  - Capital;
  - FX.
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

- [ ] Add dashboard dependencies to `pyproject.toml` and refresh `uv.lock`.
- [ ] Create a new `src/dashboard/` package without changing the existing `src/reports/` entry points.
- [ ] Add `src/dashboard/app.py` with a minimal Dash app and a local run command.
- [ ] Add a simple landing layout with a title, currency selector, and placeholder tabs.
- [ ] Confirm `uv run python -m src.dashboard.app` starts locally.
- [ ] Confirm `uv run python main.py` still uses the existing Plotly report flow.

### Phase 2: Shared Main-report Data Adapter

- [ ] Create a dashboard data adapter for the main report, separate from HTML rendering.
- [ ] Reuse existing model/data functions such as `get_balance_by_month` and FX helpers.
- [ ] Return raw or analysis-ready DataFrames for yearly stats, FX info, income/expense trend, delta, and capital.
- [ ] Keep formatted display values separate from numeric values needed for charts and XLSX export.
- [ ] Add a lightweight dataset registry with stable widget IDs and human-readable titles.
- [ ] Add tests or smoke checks that the adapter returns expected datasets for a supported currency.

### Phase 3: Main-report Dash UI

- [ ] Replace placeholder tabs with `Overview`, `Cashflow`, `Capital`, and `FX`.
- [ ] Render each chart as an independent `dcc.Graph(responsive=True)`.
- [ ] Move away from fixed full-report Plotly subplot heights in the Dash UI.
- [ ] Add responsive layout containers using `dash-bootstrap-components`.
- [ ] Add table views for summary and FX data using Dash AG Grid or a simple Dash table component.
- [ ] Add empty/error states for missing CSV data or unavailable FX data.
- [ ] Verify the UI remains usable on desktop and mobile-width viewports.

### Phase 4: XLSX Downloads

- [ ] Add per-widget download buttons for chart/table source data.
- [ ] Implement XLSX generation with `openpyxl` or pandas Excel writer.
- [ ] Use clear filenames that include report type, widget ID, currency, and date.
- [ ] Preserve numeric columns for Excel analysis instead of exporting only preformatted strings.
- [ ] Validate generated XLSX files can be opened and contain expected columns.

### Phase 5: PNG/PDF Page Export

- [ ] Add export buttons for PNG and PDF in the Dash top control bar.
- [ ] Implement a Playwright-based export helper that opens the current dashboard URL.
- [ ] Wait for Dash and Plotly charts to finish rendering before capture.
- [ ] Export the active tab or full rendered page to `reports/dashboard_exports/<currency>/`.
- [ ] Use filenames that include report type, active tab, currency, and timestamp.
- [ ] Verify exported PNG/PDF files are non-empty and do not cut off visible charts.

### Phase 6: Compatibility and Documentation

- [ ] Keep existing Plotly report functions available and unchanged unless a compatibility-preserving refactor is required.
- [ ] Keep `main.py` behavior intact for the current main/year/month report generation.
- [ ] Document the new Dash command in `README.md`.
- [ ] Document that Dash MVP is experimental and the current Plotly reports remain the fallback.
- [ ] Add a final compatibility smoke check for both the Dash app import and old Plotly report imports.
- [ ] Update this checklist as tasks are completed.

## Test Plan

- Verify the new Dash app imports and builds its layout without running the old report generation.
- Verify the existing `main.py` Plotly report flow still imports and remains callable.
- Add focused tests for the dashboard data adapter: expected dataset IDs, expected columns, and non-empty outputs when source data exists.
- Validate generated XLSX files with `openpyxl`.
- Use Playwright smoke checks for desktop and mobile viewport sizes to confirm charts render and tabs switch.
- Verify PNG/PDF export creates non-empty files and does not cut off the active tab content.

## Assumptions

- The old Plotly reports stay as the reliable fallback until a later migration plan says otherwise.
- The first Dash version is local-only and does not require authentication, hosting, or Telegram integration.
- The first export implementation captures the rendered page state controlled by Dash UI inputs, not manual browser-only Plotly interactions such as temporary zoom or pan.
- More advanced charting libraries can be evaluated later after the Dash shell proves useful.
