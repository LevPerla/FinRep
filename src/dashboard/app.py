import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs

from dash import Dash, Input, MATCH, Output, State, ctx, dcc, html
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash.exceptions import PreventUpdate

from src import config
from src.data.get import clear_data_cache, get_transactions
from src.data.assets_editor import read_asset_snapshot, write_asset_snapshot
from src.data.crypto import read_crypto_wallets, refresh_crypto_balances, refresh_crypto_price_cache
from src.data.debts import (
    DEBT_TYPES,
    active_debt_balances,
    create_debt,
    create_debt_payment_from_cash,
    migrate_legacy_debts,
)
from src.data.importers.kaspi_pdf import parse_kaspi_upload_contents, save_kaspi_import_to_staging
from src.data.staging import (
    DRAFT_COLUMNS,
    DRAFT_STATUSES,
    append_transaction_draft,
    delete_transaction_drafts,
    export_monthly_transaction_drafts,
    merge_transaction_draft_rows,
    preview_monthly_transaction_export,
    read_monthly_transaction_csv,
    read_transaction_drafts,
)
from src.dashboard.export import export_dashboard_page
from src.dashboard.investment_data import build_investment_dashboard_data
from src.dashboard.main_data import DashboardDataset, build_main_dashboard_data
from src.dashboard.month_data import build_month_dashboard_data
from src.dashboard.planning_data import build_planning_dashboard_data, save_goal_targets
from src.dashboard.year_data import build_year_dashboard_data
from src.model.create_tables import clear_table_cache
from src import utils


DEFAULT_CURRENCY = "RUB"
DEFAULT_YEAR = datetime.now().strftime("%Y")
DEFAULT_MONTH = datetime.now().strftime("%m")
DEFAULT_FX_NETWORK_ENABLED = False
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_FOLDER = PROJECT_ROOT / "assets"
DashboardTab = tuple[str, str, str]
MAIN_DASHBOARD_TABS: list[DashboardTab] = [
    ("main", "Основной отчет", "Главная"),
    ("year", "Годовой отчет", "Год"),
    ("month", "Месячный отчет", "Месяц"),
    ("debts", "Долги", "Долги"),
    ("planning", "План и прогноз", "План"),
    ("investments", "Инвестиции", "Инвест"),
    ("input", "Ввод данных", "Ввод"),
]
MAIN_DASHBOARD_TAB_IDS = {tab_id for tab_id, _desktop_label, _mobile_label in MAIN_DASHBOARD_TABS}


def _app_index_string() -> str:
    return """
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
        <style>
            .finrep-theme-dark .nav-tabs { border-bottom-color: #646464 !important; }
            .finrep-theme-dark .nav-tabs .nav-link,
            .finrep-theme-dark .nav-tabs .nav-link:hover,
            .finrep-theme-dark .nav-tabs .nav-link:focus {
                color: #6897bb !important;
                background-color: transparent !important;
                border-color: transparent !important;
            }
            .finrep-theme-dark .nav-tabs .nav-link.active,
            .finrep-theme-dark .nav-tabs .nav-item.show .nav-link {
                color: #dcdcdc !important;
                background-color: #1e293b !important;
                border-color: #646464 #646464 #1e293b !important;
            }
            .finrep-theme-dark .Select,
            .finrep-theme-dark .Select-control,
            .finrep-theme-dark .Select-menu-outer,
            .finrep-theme-dark .Select-menu,
            .finrep-theme-dark .Select-option,
            .finrep-theme-dark .Select-value,
            .finrep-theme-dark .Select-placeholder {
                background-color: #2b2b2b !important;
                border-color: #646464 !important;
                color: #a9b7c6 !important;
            }
            .finrep-theme-dark .Select-value-label,
            .finrep-theme-dark .Select-input,
            .finrep-theme-dark .Select-input > input,
            .finrep-theme-dark .Select-arrow-zone,
            .finrep-theme-dark .Select-clear-zone {
                color: #a9b7c6 !important;
            }
            .finrep-theme-dark .Select-arrow { border-top-color: #a9b7c6 !important; }
            .finrep-theme-dark .Select-option.is-focused,
            .finrep-theme-dark .Select-option.is-selected {
                background-color: #3c3f41 !important;
                color: #dcdcdc !important;
            }
            .finrep-theme-dark .dash-dropdown,
            .finrep-theme-dark .dash-dropdown .Select-control,
            .finrep-theme-dark .dash-dropdown .Select__control {
                background-color: #2b2b2b !important;
                border-color: #646464 !important;
                color: #a9b7c6 !important;
                box-shadow: none !important;
            }
            .finrep-theme-dark .dash-dropdown .Select-control:hover,
            .finrep-theme-dark .dash-dropdown .Select__control:hover {
                border-color: #6f6f6f !important;
            }
            .finrep-theme-dark .dash-dropdown .Select-value-label,
            .finrep-theme-dark .dash-dropdown .Select-placeholder,
            .finrep-theme-dark .dash-dropdown .Select__single-value,
            .finrep-theme-dark .dash-dropdown .Select__placeholder,
            .finrep-theme-dark .dash-dropdown .Select__input-container,
            .finrep-theme-dark .dash-dropdown .Select__input,
            .finrep-theme-dark .dash-dropdown input {
                color: #a9b7c6 !important;
            }
            .finrep-theme-dark .dash-dropdown .Select-menu-outer,
            .finrep-theme-dark .dash-dropdown .Select-menu,
            .finrep-theme-dark .dash-dropdown .Select__menu,
            .finrep-theme-dark .dash-dropdown .Select__menu-list {
                background-color: #2b2b2b !important;
                border-color: #646464 !important;
                color: #a9b7c6 !important;
            }
            .finrep-theme-dark .dash-dropdown .Select-option,
            .finrep-theme-dark .dash-dropdown .Select__option {
                background-color: #2b2b2b !important;
                color: #a9b7c6 !important;
            }
            .finrep-theme-dark .dash-dropdown .Select-option.is-focused,
            .finrep-theme-dark .dash-dropdown .Select-option.is-selected,
            .finrep-theme-dark .dash-dropdown .Select__option--is-focused,
            .finrep-theme-dark .dash-dropdown .Select__option--is-selected {
                background-color: #3c3f41 !important;
                color: #dcdcdc !important;
            }
            .finrep-theme-dark .dash-dropdown .Select-arrow,
            .finrep-theme-dark .dash-dropdown .Select__dropdown-indicator,
            .finrep-theme-dark .dash-dropdown .Select__indicator,
            .finrep-theme-dark .dash-dropdown .Select__indicator svg {
                color: #a9b7c6 !important;
                fill: #a9b7c6 !important;
                border-top-color: #a9b7c6 !important;
            }
            .finrep-theme-dark .ag-theme-alpine,
            .finrep-theme-dark .ag-theme-alpine-dark {
                --ag-background-color: #2b2b2b !important;
                --ag-foreground-color: #a9b7c6 !important;
                --ag-header-background-color: #3c3f41 !important;
                --ag-header-foreground-color: #dcdcdc !important;
                --ag-data-color: #a9b7c6 !important;
                --ag-odd-row-background-color: #323232 !important;
                --ag-row-hover-color: #4b4b4b !important;
                --ag-border-color: #555555 !important;
                --ag-secondary-border-color: #555555 !important;
            }
            .finrep-theme-dark .ag-theme-alpine .ag-root-wrapper,
            .finrep-theme-dark .ag-theme-alpine-dark .ag-root-wrapper,
            .finrep-theme-dark .ag-theme-alpine .ag-header,
            .finrep-theme-dark .ag-theme-alpine-dark .ag-header,
            .finrep-theme-dark .ag-theme-alpine .ag-header-cell,
            .finrep-theme-dark .ag-theme-alpine-dark .ag-header-cell {
                background-color: #3c3f41 !important;
                color: #dcdcdc !important;
                border-color: #555555 !important;
            }
            .finrep-theme-dark .ag-theme-alpine .ag-row,
            .finrep-theme-dark .ag-theme-alpine-dark .ag-row,
            .finrep-theme-dark .ag-theme-alpine .ag-cell,
            .finrep-theme-dark .ag-theme-alpine-dark .ag-cell {
                border-color: #555555 !important;
            }

.dashboard-toolbar .dashboard-filter .Select-control,
.dashboard-toolbar .dashboard-filter .Select__control {
  min-height: 36px !important;
  height: 36px !important;
  border-radius: 6px !important;
}
.dashboard-toolbar .dashboard-filter .Select-placeholder,
.dashboard-toolbar .dashboard-filter .Select-value,
.dashboard-toolbar .dashboard-filter .Select__value-container,
.dashboard-toolbar .dashboard-filter .Select__single-value {
  line-height: 34px !important;
  min-height: 34px !important;
  padding-top: 0 !important;
  padding-bottom: 0 !important;
  font-weight: 600 !important;
}
.dashboard-toolbar .btn {
  height: 36px;
  padding-top: 0.35rem;
  padding-bottom: 0.35rem;
}
.finrep-theme-dark .dashboard-toolbar .dashboard-filter,
.finrep-theme-dark .dashboard-toolbar .dashboard-filter .Select-control,
.finrep-theme-dark .dashboard-toolbar .dashboard-filter .Select__control {
  background-color: #2b2b2b !important;
  border-color: #646464 !important;
  color: #a9b7c6 !important;
}
.finrep-theme-dark .dashboard-toolbar .dashboard-filter .Select-value-label,
.finrep-theme-dark .dashboard-toolbar .dashboard-filter .Select__single-value,
.finrep-theme-dark .dashboard-toolbar .dashboard-filter .Select-placeholder,
.finrep-theme-dark .dashboard-toolbar .dashboard-filter .Select__placeholder,
.finrep-theme-dark .dashboard-toolbar .dashboard-filter input {
  color: #a9b7c6 !important;
}

            .finrep-theme-dark .ag-theme-alpine .ag-icon,
            .finrep-theme-dark .ag-theme-alpine-dark .ag-icon {
                color: #a9b7c6 !important;
                filter: invert(1) opacity(0.8);
            }
            .finrep-theme-dark #transaction-input-date,
            .finrep-theme-dark #transaction-input-amount,
            .finrep-theme-dark #transaction-input-comment {
                background-color: #2b2b2b !important;
                border-color: #646464 !important;
                color: #dcdcdc !important;
                -webkit-text-fill-color: #dcdcdc !important;
                caret-color: #dcdcdc !important;
            }
            .finrep-theme-dark #transaction-input-date::placeholder,
            .finrep-theme-dark #transaction-input-amount::placeholder,
            .finrep-theme-dark #transaction-input-comment::placeholder {
                color: #dcdcdc !important;
                -webkit-text-fill-color: #dcdcdc !important;
                opacity: 1 !important;
            }
            .finrep-theme-dark #transaction-input-date::-webkit-input-placeholder,
            .finrep-theme-dark #transaction-input-amount::-webkit-input-placeholder,
            .finrep-theme-dark #transaction-input-comment::-webkit-input-placeholder {
                color: #dcdcdc !important;
                -webkit-text-fill-color: #dcdcdc !important;
                opacity: 1 !important;
            }
            .finrep-theme-dark #transaction-input-date::-moz-placeholder,
            .finrep-theme-dark #transaction-input-amount::-moz-placeholder,
            .finrep-theme-dark #transaction-input-comment::-moz-placeholder {
                color: #dcdcdc !important;
                opacity: 1 !important;
            }
            .finrep-theme-dark #transaction-input-date::-webkit-calendar-picker-indicator {
                filter: invert(0.85) brightness(1.4) !important;
                opacity: 1 !important;
            }
            .finrep-theme-dark #transaction-input-category .Select-control,
            .finrep-theme-dark #transaction-input-currency .Select-control,
            .finrep-theme-dark #transaction-filter-month .Select-control,
            .finrep-theme-dark #transaction-filter-category .Select-control,
            .finrep-theme-dark #transaction-filter-status .Select-control,
            .finrep-theme-dark #transaction-filter-source .Select-control,
            .finrep-theme-dark #transaction-input-category .Select__control,
            .finrep-theme-dark #transaction-input-currency .Select__control,
            .finrep-theme-dark #transaction-filter-month .Select__control,
            .finrep-theme-dark #transaction-filter-category .Select__control,
            .finrep-theme-dark #transaction-filter-status .Select__control,
            .finrep-theme-dark #transaction-filter-source .Select__control {
                background-color: #2b2b2b !important;
                border-color: #646464 !important;
                color: #dcdcdc !important;
            }
            .finrep-theme-dark #transaction-input-category .Select-placeholder,
            .finrep-theme-dark #transaction-input-currency .Select-placeholder,
            .finrep-theme-dark #transaction-filter-month .Select-placeholder,
            .finrep-theme-dark #transaction-filter-category .Select-placeholder,
            .finrep-theme-dark #transaction-filter-status .Select-placeholder,
            .finrep-theme-dark #transaction-filter-source .Select-placeholder,
            .finrep-theme-dark #transaction-input-category .Select-value-label,
            .finrep-theme-dark #transaction-input-currency .Select-value-label,
            .finrep-theme-dark #transaction-filter-month .Select-value-label,
            .finrep-theme-dark #transaction-filter-category .Select-value-label,
            .finrep-theme-dark #transaction-filter-status .Select-value-label,
            .finrep-theme-dark #transaction-filter-source .Select-value-label,
            .finrep-theme-dark #transaction-input-category .Select-input > input,
            .finrep-theme-dark #transaction-input-currency .Select-input > input,
            .finrep-theme-dark #transaction-filter-month .Select-input > input,
            .finrep-theme-dark #transaction-filter-category .Select-input > input,
            .finrep-theme-dark #transaction-filter-status .Select-input > input,
            .finrep-theme-dark #transaction-filter-source .Select-input > input,
            .finrep-theme-dark #transaction-input-category .Select__placeholder,
            .finrep-theme-dark #transaction-input-currency .Select__placeholder,
            .finrep-theme-dark #transaction-filter-month .Select__placeholder,
            .finrep-theme-dark #transaction-filter-category .Select__placeholder,
            .finrep-theme-dark #transaction-filter-status .Select__placeholder,
            .finrep-theme-dark #transaction-filter-source .Select__placeholder,
            .finrep-theme-dark #transaction-input-category .Select__single-value,
            .finrep-theme-dark #transaction-input-currency .Select__single-value,
            .finrep-theme-dark #transaction-filter-month .Select__single-value,
            .finrep-theme-dark #transaction-filter-category .Select__single-value,
            .finrep-theme-dark #transaction-filter-status .Select__single-value,
            .finrep-theme-dark #transaction-filter-source .Select__single-value,
            .finrep-theme-dark #transaction-input-category .Select__input,
            .finrep-theme-dark #transaction-input-currency .Select__input,
            .finrep-theme-dark #transaction-filter-month .Select__input,
            .finrep-theme-dark #transaction-filter-category .Select__input,
            .finrep-theme-dark #transaction-filter-status .Select__input,
            .finrep-theme-dark #transaction-filter-source .Select__input {
                color: #dcdcdc !important;
                -webkit-text-fill-color: #dcdcdc !important;
            }
            .finrep-theme-dark #transaction-input-category .Select-arrow,
            .finrep-theme-dark #transaction-input-currency .Select-arrow,
            .finrep-theme-dark #transaction-filter-month .Select-arrow,
            .finrep-theme-dark #transaction-filter-category .Select-arrow,
            .finrep-theme-dark #transaction-filter-status .Select-arrow,
            .finrep-theme-dark #transaction-filter-source .Select-arrow,
            .finrep-theme-dark #transaction-input-category .Select-clear,
            .finrep-theme-dark #transaction-input-currency .Select-clear,
            .finrep-theme-dark #transaction-filter-month .Select-clear,
            .finrep-theme-dark #transaction-filter-category .Select-clear,
            .finrep-theme-dark #transaction-filter-status .Select-clear,
            .finrep-theme-dark #transaction-filter-source .Select-clear,
            .finrep-theme-dark #transaction-input-category .Select__indicator,
            .finrep-theme-dark #transaction-input-currency .Select__indicator,
            .finrep-theme-dark #transaction-filter-month .Select__indicator,
            .finrep-theme-dark #transaction-filter-category .Select__indicator,
            .finrep-theme-dark #transaction-filter-status .Select__indicator,
            .finrep-theme-dark #transaction-filter-source .Select__indicator {
                color: #dcdcdc !important;
                fill: #dcdcdc !important;
                border-top-color: #dcdcdc !important;
            }
        </style>
    </head>
    <body>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
"""


def create_app() -> Dash:
    app = Dash(
        __name__,
        assets_folder=str(ASSETS_FOLDER),
        external_stylesheets=[dbc.themes.BOOTSTRAP],
        meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1, viewport-fit=cover"}],
        title="FinRep Dashboard",
        suppress_callback_exceptions=True,
    )
    app.index_string = _app_index_string()
    app.server.add_url_rule("/healthz", "healthz", _healthcheck)
    app.layout = create_layout()
    register_callbacks(app)
    return app


def _healthcheck():
    return {"status": "ok"}, 200


def _dashboard_tabs() -> dbc.Tabs:
    return dbc.Tabs(
        [
            dbc.Tab(label=desktop_label, tab_id=tab_id)
            for tab_id, desktop_label, _mobile_label in MAIN_DASHBOARD_TABS
        ],
        id="dashboard-tabs",
        active_tab="main",
        className="dashboard-tabs",
    )


def _mobile_bottom_nav() -> html.Nav:
    return html.Nav(
        dcc.RadioItems(
            id="mobile-dashboard-tabs",
            options=[
                {"label": mobile_label, "value": tab_id}
                for tab_id, _desktop_label, mobile_label in MAIN_DASHBOARD_TABS
            ],
            value="main",
            className="mobile-dashboard-tabs-control",
            labelClassName="mobile-dashboard-tab",
            inputClassName="mobile-dashboard-tab-input",
        ),
        className="mobile-bottom-tabs",
        **{"aria-label": "Основные разделы dashboard"},
    )


def create_layout():
    currency_options = [
        {"label": ticker, "value": ticker}
        for ticker in config.UNIQUE_TICKERS.keys()
    ]
    year_options = [{"label": year, "value": year} for year in reversed(utils.get_reports_years())]
    month_options = [{"label": f"{month:02d}", "value": f"{month:02d}"} for month in range(1, 13)]

    return dbc.Container(
        [
            dcc.Location(id="dashboard-location"),
            dcc.Store(id="dashboard-theme", data="dark"),
            dcc.Store(id="dashboard-refresh-token", data=0),
            dcc.Store(
                id="crypto-refresh-status",
                data={
                    "message": "Crypto refresh отправляет включенные wallet addresses в публичные blockchain API и обновляет локальный cache.",
                    "color": "secondary",
                },
            ),
            dbc.Row(
                [
                    dbc.Col(
                        html.H1("Finance Dashboard", className="h3 mb-0"),
                        xs=12,
                        md=4,
                    ),
                    dbc.Col(
                        html.Div(
                            [
                                dcc.Dropdown(
                                    id="dashboard-currency",
                                    options=currency_options,
                                    value=DEFAULT_CURRENCY,
                                    clearable=False,
                                    className="dashboard-filter",
                                    style={"width": "92px"},
                                ),
                                dcc.Dropdown(
                                    id="dashboard-year",
                                    options=year_options,
                                    value=DEFAULT_YEAR,
                                    clearable=False,
                                    className="dashboard-filter",
                                    style={"width": "104px"},
                                ),
                                dcc.Dropdown(
                                    id="dashboard-month",
                                    options=month_options,
                                    value=DEFAULT_MONTH,
                                    clearable=False,
                                    className="dashboard-filter",
                                    style={"width": "78px"},
                                ),
                                dbc.Button("Обновить", id="refresh-reports", color="secondary", outline=True),
                                dbc.Button("Обновить курс", id="refresh-fx-rates", color="warning", outline=True),
                                dbc.Button("Светлая", id="theme-toggle", color="secondary", outline=True),
                                dbc.Button("PNG", id="export-png", color="primary", outline=True),
                                dbc.Button("PDF", id="export-pdf", color="primary", outline=True),
                                dcc.Download(id="page-export-download"),
                            ],
                            className="dashboard-toolbar d-flex flex-wrap justify-content-md-end align-items-center gap-2",
                        ),
                        xs=12,
                        md=8,
                        className="mt-3 mt-md-0",
                    ),
                ],
                align="center",
                className="py-3",
            ),
            _dashboard_tabs(),
            dcc.Loading(
                html.Div(id="dashboard-content", className="py-4"),
                type="circle",
            ),
            _mobile_bottom_nav(),
        ],
        id="dashboard-shell",
        className="finrep-shell finrep-theme-dark",
        fluid=True,
        style=_theme_shell_style("dark"),
    )


def register_callbacks(app: Dash) -> None:
    @app.callback(
        Output("dashboard-refresh-token", "data"),
        Input("refresh-reports", "n_clicks"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def refresh_reports(n_clicks: int | None, current_token: int | None):
        if not n_clicks:
            raise PreventUpdate
        clear_data_cache()
        clear_table_cache()
        return int(current_token or 0) + 1

    @app.callback(
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Output("crypto-refresh-status", "data"),
        Input("crypto-refresh-button", "n_clicks"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def refresh_crypto_data(n_clicks: int | None, current_token: int | None):
        if not n_clicks:
            raise PreventUpdate
        try:
            wallets = read_crypto_wallets()
            enabled_assets = sorted(
                {
                    str(row["asset"]).upper()
                    for _, row in wallets.iterrows()
                    if str(row.get("enabled", "1")).strip().lower() not in {"0", "false", "no", "off"}
                }
            )
            balances = refresh_crypto_balances()
            if enabled_assets:
                refresh_crypto_price_cache(enabled_assets)
            clear_data_cache()
            clear_table_cache()
            errors = balances.attrs.get("errors", [])
            statuses = balances.attrs.get("statuses", [])
            message = f"Crypto обновлено: {len(balances)} balance row(s), assets: {', '.join(enabled_assets) or 'нет включенных кошельков'}."
            if statuses:
                message += " Успешно: " + " | ".join(statuses[:6])
                if len(statuses) > 6:
                    message += f" | еще {len(statuses) - 6}"
            if errors:
                message += " Ошибки: " + " | ".join(errors[:8])
                if len(errors) > 8:
                    message += f" | еще {len(errors) - 8}"
            return int(current_token or 0) + 1, {"message": message, "color": "warning" if errors else "success"}
        except Exception as exc:
            return int(current_token or 0), {"message": f"Crypto refresh не удался: {exc}", "color": "danger"}

    @app.callback(
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Input("planning_goals-grid", "cellValueChanged"),
        State("planning_goals-grid", "rowData"),
        State("dashboard-year", "value"),
        State("dashboard-currency", "value"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def save_planning_goal_cell(cell_change, row_data, year, currency, current_token):
        if not _ag_grid_changed_column(cell_change, "Цель"):
            raise PreventUpdate
        save_goal_targets(year, currency, row_data or [])
        clear_data_cache()
        clear_table_cache()
        return int(current_token or 0) + 1

    @app.callback(
        Output("dashboard-theme", "data"),
        Output("dashboard-shell", "className"),
        Output("dashboard-shell", "style"),
        Output("theme-toggle", "children"),
        Input("theme-toggle", "n_clicks"),
        State("dashboard-theme", "data"),
    )
    def toggle_theme(n_clicks: int | None, current_theme: str | None):
        theme = current_theme if current_theme in {"light", "dark"} else "light"
        if n_clicks:
            theme = "dark" if theme == "light" else "light"
        label = "Светлая" if theme == "dark" else "Темная"
        shell_style = _theme_shell_style(theme)
        return theme, f"finrep-shell finrep-theme-{theme}", shell_style, label

    @app.callback(
        Output("dashboard-currency", "value"),
        Output("dashboard-year", "value"),
        Output("dashboard-month", "value"),
        Output("dashboard-tabs", "active_tab"),
        Output("mobile-dashboard-tabs", "value"),
        Input("dashboard-location", "search"),
    )
    def apply_url_state(search: str):
        params = parse_qs((search or "").lstrip("?"))
        currency = params.get("currency", [DEFAULT_CURRENCY])[0]
        year = params.get("year", [DEFAULT_YEAR])[0]
        month = params.get("month", [DEFAULT_MONTH])[0]
        tab = params.get("tab", ["main"])[0]
        if currency not in config.UNIQUE_TICKERS:
            currency = DEFAULT_CURRENCY
        available_years = set(utils.get_reports_years())
        if year not in available_years:
            year = DEFAULT_YEAR
        if month not in {f"{value:02d}" for value in range(1, 13)}:
            month = DEFAULT_MONTH
        if tab not in MAIN_DASHBOARD_TAB_IDS:
            tab = "main"
        return currency, year, month, tab, tab

    @app.callback(
        Output("dashboard-tabs", "active_tab", allow_duplicate=True),
        Input("mobile-dashboard-tabs", "value"),
        State("dashboard-tabs", "active_tab"),
        prevent_initial_call=True,
    )
    def apply_mobile_tab(active_mobile_tab: str | None, active_desktop_tab: str | None):
        if not active_mobile_tab or active_mobile_tab == active_desktop_tab:
            raise PreventUpdate
        return active_mobile_tab

    @app.callback(
        Output("mobile-dashboard-tabs", "value", allow_duplicate=True),
        Input("dashboard-tabs", "active_tab"),
        State("mobile-dashboard-tabs", "value"),
        prevent_initial_call=True,
    )
    def sync_mobile_tab(active_desktop_tab: str | None, active_mobile_tab: str | None):
        if not active_desktop_tab or active_desktop_tab == active_mobile_tab:
            raise PreventUpdate
        return active_desktop_tab

    @app.callback(
        Output("dashboard-content", "children"),
        Input("dashboard-currency", "value"),
        Input("dashboard-year", "value"),
        Input("dashboard-month", "value"),
        Input("dashboard-tabs", "active_tab"),
        Input("dashboard-theme", "data"),
        Input("dashboard-refresh-token", "data"),
        Input("refresh-fx-rates", "n_clicks"),
        State("crypto-refresh-status", "data"),
    )
    def render_dashboard_content(currency: str, year: str, month: str, active_tab: str, theme: str, refresh_token: int, fx_refresh_clicks: int | None, crypto_status: dict | None):
        fx_network_enabled = ctx.triggered_id == "refresh-fx-rates"
        if fx_network_enabled:
            clear_table_cache()

        if active_tab == "year":
            try:
                datasets = build_year_dashboard_data(
                    year,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state("Не удалось загрузить данные годового отчета.", exc)

            _apply_theme_to_datasets(datasets, theme)
            return _year_report_layout(datasets, theme)

        if active_tab == "planning":
            try:
                datasets = build_planning_dashboard_data(
                    year,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state("Не удалось загрузить данные плана и прогноза.", exc)

            _apply_theme_to_datasets(datasets, theme)
            return _planning_report_layout(datasets, theme)

        if active_tab == "month":
            try:
                datasets = build_month_dashboard_data(
                    year,
                    month,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state("Не удалось загрузить данные месячного отчета.", exc)

            _apply_theme_to_datasets(datasets, theme)
            return _month_report_layout(datasets, theme)

        if active_tab == "investments":
            try:
                datasets = build_investment_dashboard_data(
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state("Не удалось загрузить инвестиционный отчет.", exc)

            _apply_theme_to_datasets(datasets, theme)
            return _investment_report_layout(datasets, theme, crypto_status)

        if active_tab == "debts":
            return _debt_report_layout(currency, theme)

        if active_tab == "input":
            return _input_report_layout(currency, year, month, theme)

        try:
            datasets = build_main_dashboard_data(
                currency,
                fx_network_enabled=fx_network_enabled,
            )
        except Exception as exc:
            return _error_state("Не удалось загрузить данные основного отчета.", exc)

        _apply_theme_to_datasets(datasets, theme)
        return html.Div(
            [
                _grid_section(datasets["yearly_stats"], height="300px", theme=theme),
                _grid_section(datasets["fx_rates"], height="260px", theme=theme),
                _graph_section(datasets["income_expense"], theme=theme),
                _graph_section(datasets["delta"], theme=theme),
                _graph_section(datasets["capital"], height="640px", theme=theme),
                _graph_section(datasets["fx_changes"], theme=theme),
            ],
            className="d-grid gap-4",
        )

    @app.callback(
        Output({"type": "dataset-download", "dataset_id": MATCH}, "data"),
        Input({"type": "dataset-download-button", "dataset_id": MATCH}, "n_clicks"),
        State({"type": "dataset-download-button", "dataset_id": MATCH}, "id"),
        State("dashboard-currency", "value"),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("dashboard-tabs", "active_tab"),
        prevent_initial_call=True,
    )
    def download_dataset(
        n_clicks: int,
        button_id: dict,
        currency: str,
        year: str,
        month: str,
        active_tab: str,
    ):
        if not n_clicks:
            raise PreventUpdate

        dataset_id = button_id["dataset_id"]
        datasets = _datasets_for_tab(active_tab, currency, year, month)
        if dataset_id not in datasets:
            raise PreventUpdate

        dataset = datasets[dataset_id]
        filename = _download_filename(dataset, currency, active_tab, year, month)
        return dcc.send_bytes(_dataframe_to_xlsx_bytes(dataset.dataframe, dataset.title), filename)

    @app.callback(
        Output("page-export-download", "data"),
        Input("export-png", "n_clicks"),
        Input("export-pdf", "n_clicks"),
        State("dashboard-currency", "value"),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("dashboard-tabs", "active_tab"),
        State("dashboard-location", "href"),
        prevent_initial_call=True,
    )
    def export_page(
        png_clicks: int,
        pdf_clicks: int,
        currency: str,
        year: str,
        month: str,
        active_tab: str,
        href: str,
    ):
        if not png_clicks and not pdf_clicks:
            raise PreventUpdate

        export_format = "png" if ctx.triggered_id == "export-png" else "pdf"
        export_path = export_dashboard_page(
            href,
            currency,
            active_tab,
            export_format,
            year=year,
            month=month if active_tab == "month" else None,
        )
        return dcc.send_file(str(export_path))

    @app.callback(
        Output("kaspi-import-grid", "rowData"),
        Output("kaspi-import-grid", "columnDefs"),
        Output("kaspi-import-message", "children"),
        Output("kaspi-import-message", "color"),
        Input("kaspi-upload", "contents"),
        State("kaspi-upload", "filename"),
        prevent_initial_call=True,
    )
    def preview_kaspi_pdf(contents, filename):
        if not contents:
            raise PreventUpdate
        try:
            data = parse_kaspi_upload_contents(contents)
            internal_count = int(data["skip_reason"].eq("internal_transfer").sum()) if "skip_reason" in data else 0
            message = (
                f"{filename or 'PDF'}: найдено строк {len(data)}, "
                f"к импорту {int(data['import_action'].eq('import').sum())}, "
                f"skip {int(data['import_action'].eq('skip').sum())}, "
                f"внутренние переводы {internal_count}."
            )
            return _dataframe_records(data), _kaspi_import_column_defs(), message, "secondary"
        except Exception as exc:
            return [], _kaspi_import_column_defs(), str(exc), "danger"

    @app.callback(
        Output("kaspi-import-message", "children", allow_duplicate=True),
        Output("kaspi-import-message", "color", allow_duplicate=True),
        Output("transaction-drafts-grid", "rowData", allow_duplicate=True),
        Input("kaspi-save-button", "n_clicks"),
        State("kaspi-import-grid", "rowData"),
        State("transaction-filter-month", "value"),
        State("transaction-filter-category", "value"),
        State("transaction-filter-status", "value"),
        State("transaction-filter-source", "value"),
        prevent_initial_call=True,
    )
    def save_kaspi_import(n_clicks, row_data, month_filter, category_filter, status_filter, source_filter):
        if not n_clicks:
            raise PreventUpdate
        try:
            result = save_kaspi_import_to_staging(row_data or [])
            message = f"Сохранено в staging: {result['accepted_rows']}. Пропущено дублей/skip: {result['skipped_rows']}."
            color = "success"
        except Exception as exc:
            message = str(exc)
            color = "danger"
        return message, color, _transaction_draft_records(month_filter, category_filter, status_filter, source_filter)


    @app.callback(
        Output("transaction-drafts-grid", "rowData"),
        Output("transaction-input-message", "children"),
        Output("transaction-input-message", "color"),
        Output("transaction-filter-month", "value"),
        Input("transaction-add-button", "n_clicks"),
        Input("transaction-save-grid-button", "n_clicks"),
        Input("transaction-delete-button", "n_clicks"),
        Input("transaction-filter-month", "value"),
        Input("transaction-filter-category", "value"),
        Input("transaction-filter-status", "value"),
        Input("transaction-filter-source", "value"),
        State("transaction-input-date", "value"),
        State("transaction-input-category", "value"),
        State("transaction-input-currency", "value"),
        State("transaction-input-amount", "value"),
        State("transaction-input-comment", "value"),
        State("transaction-drafts-grid", "rowData"),
        State("transaction-drafts-grid", "selectedRows"),
    )
    def sync_transaction_drafts(
        add_clicks,
        save_clicks,
        delete_clicks,
        month_filter,
        category_filter,
        status_filter,
        source_filter,
        input_date,
        input_category,
        input_currency,
        input_amount,
        input_comment,
        row_data,
        selected_rows,
    ):
        trigger = ctx.triggered_id
        message = ""
        color = "secondary"

        try:
            if trigger == "transaction-add-button":
                if not input_date or not input_category or not input_currency or input_amount in {None, ""}:
                    raise ValueError("Заполни дату, категорию, валюту и сумму.")
                append_transaction_draft(
                    date=input_date,
                    category=input_category,
                    currency=input_currency,
                    amount=input_amount,
                    comment=input_comment or "",
                )
                month_filter = pd.to_datetime(input_date).strftime("%Y-%m")
                message = "Черновик добавлен."
                color = "success"
            elif trigger == "transaction-save-grid-button":
                merge_transaction_draft_rows(row_data or [])
                message = "Правки в таблице сохранены."
                color = "success"
            elif trigger == "transaction-delete-button":
                if not selected_rows:
                    raise ValueError("Выбери строки для удаления.")
                delete_transaction_drafts(selected_rows)
                message = f"Удалено строк: {len(selected_rows)}."
                color = "warning"
        except Exception as exc:
            message = str(exc)
            color = "danger"

        return _transaction_draft_records(month_filter, category_filter, status_filter, source_filter), message, color, month_filter

    @app.callback(
        Output("transaction-export-preview-grid", "rowData"),
        Output("transaction-export-preview-grid", "columnDefs"),
        Output("transaction-export-message", "children"),
        Output("transaction-export-message", "color"),
        Input("transaction-preview-export-button", "n_clicks"),
        Input("transaction-confirm-export-button", "n_clicks"),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("transaction-export-preview-grid", "rowData"),
        prevent_initial_call=True,
    )
    def preview_or_export_transaction_month(preview_clicks, export_clicks, year, month, preview_rows):
        trigger = ctx.triggered_id
        try:
            if trigger == "transaction-confirm-export-button":
                result = export_monthly_transaction_drafts(year, month, preview_rows=preview_rows or None)
                preview = read_monthly_transaction_csv(year, month)
                message = (
                    f"Экспортировано строк: {result['exported_rows']}. "
                    f"Файл: {result['target_path']}. "
                    f"Backup: {result['backup_path'] or 'не создавался'}."
                )
                return _dataframe_records(preview), _simple_column_defs(preview), message, "success"

            preview = preview_monthly_transaction_export(year, month)
            message = f"Preview построен для {year}-{str(month).zfill(2)}. Запись в source CSV еще не выполнена."
            return _dataframe_records(preview), _simple_column_defs(preview), message, "secondary"
        except Exception as exc:
            empty = pd.DataFrame()
            return [], _simple_column_defs(empty), str(exc), "danger"


    @app.callback(
        Output("transaction-drafts-download", "data"),
        Input("transaction-export-csv-button", "n_clicks"),
        State("transaction-filter-month", "value"),
        State("transaction-filter-category", "value"),
        State("transaction-filter-status", "value"),
        State("transaction-filter-source", "value"),
        prevent_initial_call=True,
    )
    def export_transaction_drafts(n_clicks, month_filter, category_filter, status_filter, source_filter):
        if not n_clicks:
            raise PreventUpdate
        data = pd.DataFrame(_transaction_draft_records(month_filter, category_filter, status_filter, source_filter))
        filename = f"transaction_drafts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return dcc.send_data_frame(data.to_csv, filename, sep=";", index=False)

    @app.callback(
        Output("active-receivable-debts-grid", "rowData"),
        Output("active-liability-debts-grid", "rowData"),
        Output("debt-transaction-drafts-grid", "rowData"),
        Output("debt-payment-id", "options"),
        Output("debt-payment-id", "value"),
        Output("debt-input-message", "children"),
        Output("debt-input-message", "color"),
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Input("debt-add-button", "n_clicks"),
        Input("debt-payment-button", "n_clicks"),
        Input("debt-migrate-button", "n_clicks"),
        Input("dashboard-currency", "value"),
        State("debt-opened-date", "value"),
        State("debt-type", "value"),
        State("debt-counterparty", "value"),
        State("debt-principal-amount", "value"),
        State("debt-principal-currency", "value"),
        State("debt-cash-amount", "value"),
        State("debt-cash-currency", "value"),
        State("debt-comment", "value"),
        State("debt-payment-id", "value"),
        State("debt-payment-date", "value"),
        State("debt-payment-amount", "value"),
        State("debt-payment-cash-currency", "value"),
        State("debt-payment-comment", "value"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def sync_debts(
        add_clicks,
        payment_clicks,
        migrate_clicks,
        currency,
        opened_date,
        debt_type,
        counterparty,
        principal_amount,
        principal_currency,
        cash_amount,
        cash_currency,
        comment,
        selected_debt_id,
        payment_date,
        payment_amount,
        payment_cash_currency,
        payment_comment,
        current_token,
    ):
        trigger = ctx.triggered_id
        message = ""
        color = "secondary"
        token = int(current_token or 0)

        try:
            if trigger == "debt-add-button":
                if not opened_date or not debt_type or not counterparty or principal_amount in {None, ""} or not principal_currency:
                    raise ValueError("Заполни дату, тип, контрагента, сумму и валюту долга.")
                result = create_debt(
                    debt_type=debt_type,
                    counterparty=counterparty,
                    opened_date=opened_date,
                    principal_amount=principal_amount,
                    principal_currency=principal_currency,
                    cash_amount=cash_amount,
                    cash_currency=cash_currency,
                    comment=comment or "",
                )
                clear_data_cache()
                clear_table_cache()
                token += 1
                message = f"Долг создан: {result['debt_id']}. Черновик транзакции добавлен."
                color = "success"
            elif trigger == "debt-payment-button":
                debt_id = str(selected_debt_id or "")
                if not debt_id or not payment_date or payment_amount in {None, ""}:
                    raise ValueError("Выбери долг в поле выбора и заполни дату/сумму погашения.")
                result = create_debt_payment_from_cash(
                    debt_id=debt_id,
                    date=payment_date,
                    cash_amount=payment_amount,
                    cash_currency=payment_cash_currency,
                    comment=payment_comment or "",
                )
                clear_data_cache()
                clear_table_cache()
                token += 1
                message = f"Погашение создано: {result['payment_id']}. Черновик транзакции добавлен во вкладку Ввод данных."
                color = "success"
            elif trigger == "debt-migrate-button":
                result = migrate_legacy_debts()
                clear_table_cache()
                token += 1
                if result.get("skipped"):
                    message = f"Миграция пропущена: {result['skipped']}."
                    color = "warning"
                else:
                    message = f"Миграция завершена: долгов {result['created_debts']}, погашений {result['created_payments']}."
                    color = "success"
        except Exception as exc:
            message = str(exc)
            color = "danger"

        options = _debt_select_options(currency)
        option_values = {option["value"] for option in options}
        selected_value = selected_debt_id if selected_debt_id in option_values else ""
        return (
            _active_debt_records(currency, "receivable"),
            _active_debt_records(currency, "liability"),
            _debt_transaction_draft_records(),
            options,
            selected_value,
            message,
            color,
            token,
        )

    @app.callback(
        Output("assets-input-grid", "rowData"),
        Output("assets-input-message", "children"),
        Output("assets-input-message", "color"),
        Input("assets-load-button", "n_clicks"),
        Input("assets-add-row-button", "n_clicks"),
        Input("assets-delete-row-button", "n_clicks"),
        Input("assets-apply-button", "n_clicks"),
        Input("dashboard-year", "value"),
        Input("dashboard-month", "value"),
        State("assets-input-grid", "rowData"),
        State("assets-input-grid", "selectedRows"),
    )
    def sync_assets_snapshot(load_clicks, add_clicks, delete_clicks, apply_clicks, year, month, row_data, selected_rows):
        trigger = ctx.triggered_id
        try:
            if trigger == "assets-add-row-button":
                rows = list(row_data or [])
                rows.append({"account": "", "amount": 0, "currency": DEFAULT_CURRENCY})
                return rows, "Добавлена пустая строка. Заполни счет, сумму и валюту, затем нажми Применить.", "secondary"

            if trigger == "assets-delete-row-button":
                rows = list(row_data or [])
                if not selected_rows:
                    raise ValueError("Выбери строки активов для удаления.")
                selected_keys = {_asset_row_key(row) for row in selected_rows}
                rows = [row for row in rows if _asset_row_key(row) not in selected_keys]
                return rows, f"Удалено строк: {len(selected_rows)}. Нажми Применить, чтобы записать изменения в CSV.", "warning"

            if trigger == "assets-apply-button":
                result = write_asset_snapshot(row_data or [], year, month)
                clear_data_cache()
                clear_table_cache()
                message = (
                    f"Активы сохранены: {result['rows']} строк. "
                    f"Файл: {result['path']}. "
                    f"Backup: {result['backup_path'] or 'не создавался'}."
                )
                return _asset_input_records(year, month), message, "success"

            read_asset_snapshot(year, month)
            path_info = f"Файл: {config.ASSETS_INFO_PATH}/{year}/{year}_{str(int(month)).zfill(2)}.csv"
            if trigger == "assets-load-button":
                message = f"Активы загружены для {year}-{str(int(month)).zfill(2)}. {path_info}"
            else:
                message = f"Активы для выбранного месяца. Если файла не было, он создан из предыдущего месяца. {path_info}"
            return _asset_input_records(year, month), message, "secondary"
        except Exception as exc:
            return row_data or [], str(exc), "danger"


def _ag_grid_changed_column(change_event, column_name: str) -> bool:
    if not change_event:
        return False
    events = change_event if isinstance(change_event, list) else [change_event]
    for event in events:
        if not isinstance(event, dict):
            continue
        changed_column = event.get("colId") or event.get("column") or event.get("field")
        if changed_column == column_name:
            return True
    return False


def _theme_shell_style(theme: str | None) -> dict:
    if theme == "dark":
        return {"backgroundColor": "#2b2b2b", "color": "#a9b7c6", "minHeight": "100vh"}
    return {"backgroundColor": "#ffffff", "color": "#212529", "minHeight": "100vh"}


def _section_style(theme: str | None) -> dict:
    if theme == "dark":
        return {"backgroundColor": "#2b2b2b", "color": "#a9b7c6"}
    return {}


def _apply_theme_to_datasets(datasets: dict[str, DashboardDataset], theme: str | None) -> None:
    if theme != "dark":
        return
    for dataset in datasets.values():
        if dataset.figure is None:
            continue
        dataset.figure.update_layout(
            paper_bgcolor="#2b2b2b",
            plot_bgcolor="#3c3f41",
            font=dict(color="#a9b7c6"),
            title=dict(font=dict(color="#f3f4f6")),
            legend=dict(font=dict(color="#a9b7c6")),
            xaxis=dict(
                color="#d1d5db",
                gridcolor="#555555",
                zerolinecolor="#646464",
            ),
            yaxis=dict(
                color="#d1d5db",
                gridcolor="#555555",
                zerolinecolor="#646464",
            ),
        )


def _placeholder_report(title: str):
    return html.Section(
        [
            html.H2(title, className="h5 mb-2"),
            html.Div("Этот отчет будет добавлен после MVP основного отчета.", className="text-muted"),
        ],
        className="py-4",
    )


def _year_report_layout(datasets: dict[str, DashboardDataset], theme: str | None):
    return html.Div(
        [
            _grid_section(datasets["year_quarter_stats"], height="260px", theme=theme),
            _grid_section(datasets["year_fx_rates"], height="260px", theme=theme),
            _graph_section(datasets["year_cost_distribution_chart"], theme=theme),
            _grid_section(datasets["year_cost_distribution"], height="620px", theme=theme),
            dbc.Row(
                [
                    dbc.Col(_grid_section(datasets["year_income_by_month"], height="560px", theme=theme), xs=12, lg=3),
                    dbc.Col(_graph_section(datasets["year_income_expense"], height="560px", theme=theme), xs=12, lg=6),
                    dbc.Col(_grid_section(datasets["year_cost_by_month"], height="560px", theme=theme), xs=12, lg=3),
                ],
                className="g-4",
            ),
            _grid_section(datasets["year_income_cost_stats"], height="360px", theme=theme),
            _grid_section(datasets["year_capital_by_month"], height="560px", theme=theme),
            _graph_section(datasets["year_capital_chart"], height="560px", theme=theme),
        ],
        className="d-grid gap-4",
    )


def _planning_report_layout(datasets: dict[str, DashboardDataset], theme: str | None):
    return html.Div(
        [
            _grid_section(datasets["planning_goals"], height="260px", theme=theme),
            dbc.Row(
                [
                    dbc.Col(_graph_section(datasets["planning_capital_forecast"], height="520px", theme=theme), xs=12, lg=8),
                    dbc.Col(_runway_section(datasets["planning_runway"], theme=theme), xs=12, lg=4),
                ],
                className="g-4",
            ),
            _grid_section(datasets["planning_fx_scenarios"], height="320px", theme=theme),
            _graph_section(datasets["planning_fx_scenarios"], height="360px", theme=theme),
        ],
        className="d-grid gap-4",
    )


def _runway_section(dataset: DashboardDataset, theme: str | None = None):
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    if data.empty:
        return _empty_section(dataset)

    row = data.iloc[0]
    card_style = {
        "backgroundColor": "#3c3f41",
        "border": "1px solid #555555",
        "color": "#a9b7c6",
        "borderRadius": "8px",
        "padding": "16px",
    } if theme == "dark" else {
        "backgroundColor": "#ffffff",
        "border": "1px solid #dee2e6",
        "borderRadius": "8px",
        "padding": "16px",
    }
    label_style = {"fontSize": "0.82rem", "opacity": 0.75, "marginBottom": "6px"}
    value_style = {"fontSize": "1.55rem", "fontWeight": 700, "lineHeight": 1.15}

    def card(label: str, value: str):
        return html.Div(
            [
                html.Div(label, style=label_style),
                html.Div(value, style=value_style),
            ],
            style=card_style,
        )

    return html.Section(
        [
            _section_header(dataset),
            html.Div(
                [
                    card("Runway в месяцах", str(row.get("Runway, мес.", "не рассчитано"))),
                    card("Runway в годах", str(row.get("Runway, лет", "не рассчитано"))),
                    card("Капитал", str(row.get("Капитал", "не задано"))),
                    card("Средний расход/мес", str(row.get("Средний расход", "не задано"))),
                    html.Div(str(row.get("Статус", "")), className="small", style={"opacity": 0.7}),
                ],
                className="d-grid gap-3",
            ),
        ],
        style=_section_style(theme),
    )


def _month_report_layout(datasets: dict[str, DashboardDataset], theme: str | None):
    return html.Div(
        [
            _grid_section(datasets["month_transactions"], height="1450px", theme=theme),
            _grid_section(datasets["month_fx_rates"], height="260px", theme=theme),
            _grid_section(datasets["month_summary"], height="180px", theme=theme),
            _graph_section(datasets["month_cost_distribution_chart"], theme=theme),
            _grid_section(datasets["month_cost_distribution"], height="520px", theme=theme),
            _grid_section(datasets["month_assets"], height="1120px", theme=theme),
        ],
        className="d-grid gap-4",
    )


def _investment_report_layout(datasets: dict[str, DashboardDataset], theme: str | None, crypto_status: dict | None = None):
    crypto_status = crypto_status or {}
    return html.Div(
        [
            html.Section(
                [
                    html.Div(
                        [
                            html.H2("Инвестиции", className="h5 mb-0"),
                            dbc.Button("Обновить crypto", id="crypto-refresh-button", color="warning", outline=True, size="sm"),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dbc.Alert(
                        id="crypto-refresh-message",
                        children=crypto_status.get("message", "Crypto refresh отправляет включенные wallet addresses в публичные blockchain API и обновляет локальный cache."),
                        color=crypto_status.get("color", "secondary"),
                        is_open=True,
                        className="mb-0 py-2",
                    ),
                ],
                style=_section_style(theme),
            ),
            _grid_section(datasets["crypto_wallets"], height="320px", theme=theme),
            _grid_section(datasets["investment_summary"], height="220px", theme=theme),
            dbc.Row(
                [
                    dbc.Col(_graph_section(datasets["investment_allocation_type"], height="420px", theme=theme), xs=12, lg=6),
                    dbc.Col(_graph_section(datasets["investment_allocation_currency"], height="420px", theme=theme), xs=12, lg=6),
                ],
                className="g-4",
            ),
            dbc.Row(
                [
                    dbc.Col(_grid_section(datasets["investment_allocation_type"], height="280px", theme=theme), xs=12, lg=6),
                    dbc.Col(_grid_section(datasets["investment_allocation_currency"], height="280px", theme=theme), xs=12, lg=6),
                ],
                className="g-4",
            ),
            _grid_section(datasets["investment_positions"], height="560px", theme=theme),
        ],
        className="d-grid gap-4",
    )


def _debt_report_layout(currency: str, theme: str | None):
    return _debt_input_layout(currency, theme, include_create=True)


def _input_report_layout(currency: str, year: str, month: str, theme: str | None):
    return dbc.Tabs(
        [
            dbc.Tab(_transaction_input_layout(currency, year, month, theme), label="Транзакции", tab_id="input-transactions"),
            dbc.Tab(_assets_input_layout(year, month, theme), label="Активы", tab_id="input-assets"),
        ],
        id="input-inner-tabs",
        active_tab="input-transactions",
        className="mb-3",
    )


def _ag_grid_class_name(theme: str | None) -> str:
    theme_class = "ag-theme-alpine-dark" if theme == "dark" else "ag-theme-alpine"
    return f"{theme_class} finrep-ag-grid"


def _ag_grid_style(height: str) -> dict:
    return {"height": height, "width": "100%"}


def _ag_grid_default_col_def(**overrides) -> dict:
    defaults = {"sortable": True, "filter": True, "resizable": True, "minWidth": 112}
    defaults.update(overrides)
    return defaults


def _ag_grid_scroll(grid):
    return html.Div(grid, className="finrep-grid-scroll")


def _transaction_input_layout(currency: str, year: str, month: str, theme: str | None):
    category_options = _transaction_category_options()
    currency_options = [{"label": ticker, "value": ticker} for ticker in config.UNIQUE_TICKERS]
    month_value = f"{year}-{str(month).zfill(2)}"

    return html.Div(
        [
            html.Section(
                [
                    html.H2("Ручной ввод транзакции", className="h5 mb-3"),
                    dbc.Row(
                        [
                            dbc.Col(dbc.Input(id="transaction-input-date", type="date", value=datetime.now().date().isoformat(), className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dcc.Dropdown(id="transaction-input-category", options=category_options, value=category_options[0]["value"] if category_options else None, clearable=False, className="dash-dropdown"), xs=12, md=2),
                            dbc.Col(dcc.Dropdown(id="transaction-input-currency", options=currency_options, value=currency, clearable=False, className="dash-dropdown"), xs=12, md=2),
                            dbc.Col(dbc.Input(id="transaction-input-amount", type="number", placeholder="Сумма", step="any", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Input(id="transaction-input-comment", type="text", placeholder="Комментарий", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=3),
                            dbc.Col(dbc.Button("Добавить", id="transaction-add-button", color="primary", className="w-100"), xs=12, md=1),
                        ],
                        className="g-2",
                    ),
                    dbc.Alert(id="transaction-input-message", children="", color="secondary", is_open=True, className="mt-3 mb-0 py-2"),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.H2("Импорт Kaspi PDF", className="h5 mb-0"),
                            dbc.Button("Сохранить импорт в staging", id="kaspi-save-button", color="primary", outline=True, size="sm"),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dcc.Upload(
                        id="kaspi-upload",
                        children=html.Div(
                            [
                                html.Div("Перетащи Kaspi PDF сюда", className="fw-semibold"),
                                html.Div("или нажми для выбора файла", className="small opacity-75"),
                            ],
                            className="kaspi-upload-content",
                        ),
                        multiple=False,
                        accept=".pdf,application/pdf",
                        className="kaspi-upload-zone",
                        style={
                            "border": "1px dashed #646464",
                            "borderRadius": "8px",
                            "padding": "28px",
                            "textAlign": "center",
                            "cursor": "pointer",
                            "minHeight": "112px",
                            "display": "flex",
                            "alignItems": "center",
                            "justifyContent": "center",
                            **_section_style(theme),
                        },
                    ),
                    dbc.Alert(id="kaspi-import-message", children="PDF preview появится здесь. Дубли из staging/source CSV будут помечены как skip.", color="secondary", is_open=True, className="my-3 py-2"),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="kaspi-import-grid",
                            rowData=[],
                            columnDefs=_kaspi_import_column_defs(),
                            defaultColDef=_ag_grid_default_col_def(editable=False),
                            dashGridOptions={"pagination": False, "suppressFieldDotNotation": True, "stopEditingWhenCellsLoseFocus": True},
                            className=_ag_grid_class_name(theme),
                            style=_ag_grid_style("420px"),
                        )
                    ),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.H2("Черновики транзакций", className="h5 mb-0"),
                            html.Div(
                                [
                                    dbc.Button("Сохранить правки", id="transaction-save-grid-button", color="primary", outline=True, size="sm"),
                                    dbc.Button("Удалить выбранные", id="transaction-delete-button", color="danger", outline=True, size="sm"),
                                    dbc.Button("CSV", id="transaction-export-csv-button", color="secondary", outline=True, size="sm"),
                                    dcc.Download(id="transaction-drafts-download"),
                                ],
                                className="d-flex flex-wrap gap-2",
                            ),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dbc.Row(
                        [
                            dbc.Col(dbc.Select(id="transaction-filter-month", options=_native_select_options(_transaction_month_options(month_value), "Месяц", include_empty=False), value=month_value, className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=3),
                            dbc.Col(dbc.Select(id="transaction-filter-category", options=_native_select_options(category_options, "Все категории"), value="__all__", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=3),
                            dbc.Col(dbc.Select(id="transaction-filter-status", options=_native_select_options(_draft_status_options(), "Все статусы"), value="__all__", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=3),
                            dbc.Col(dbc.Select(id="transaction-filter-source", options=_native_select_options(_draft_source_options(), "Все источники"), value="__all__", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=3),
                        ],
                        className="g-2 mb-3",
                    ),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="transaction-drafts-grid",
                            rowData=_transaction_draft_records(month_value, None, None, None),
                            columnDefs=_transaction_draft_column_defs(category_options, list(config.UNIQUE_TICKERS)),
                            defaultColDef=_ag_grid_default_col_def(editable=True),
                            dashGridOptions={"pagination": False, "suppressFieldDotNotation": True, "rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True},
                            className=_ag_grid_class_name(theme),
                            style=_ag_grid_style("640px"),
                        )
                    ),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.Div(
                        [
                            html.H2("Экспорт в месячный CSV", className="h5 mb-0"),
                            html.Div(
                                [
                                    dbc.Button("Preview", id="transaction-preview-export-button", color="secondary", outline=True, size="sm"),
                                    dbc.Button("Подтвердить экспорт", id="transaction-confirm-export-button", color="danger", outline=True, size="sm"),
                                ],
                                className="d-flex flex-wrap gap-2",
                            ),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dbc.Alert(id="transaction-export-message", children="Preview покажет итоговый месячный CSV. Запись произойдет только после подтверждения.", color="secondary", is_open=True, className="mb-3 py-2"),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="transaction-export-preview-grid",
                            rowData=[],
                            columnDefs=[],
                            defaultColDef=_ag_grid_default_col_def(editable=True),
                            dashGridOptions={"pagination": False, "suppressFieldDotNotation": True, "stopEditingWhenCellsLoseFocus": True, "undoRedoCellEditing": True},
                            className=_ag_grid_class_name(theme),
                            style=_ag_grid_style("520px"),
                        )
                    ),
                ],
                style=_section_style(theme),
            ),
        ],
        className="d-grid gap-4 pt-3",
    )


def _debt_input_layout(currency: str, theme: str | None, include_create: bool = True):
    currency_options = _native_select_options([{"label": ticker, "value": ticker} for ticker in config.UNIQUE_TICKERS], "Валюта", include_empty=False)
    debt_type_options = [
        {"label": "Мне должны", "value": "receivable"},
        {"label": "Я должен", "value": "liability"},
    ]
    sections = []

    if include_create:
        sections.append(
            html.Section(
                [
                    html.H2("Новый долг", className="h5 mb-3"),
                    dbc.Row(
                        [
                            dbc.Col(dbc.Input(id="debt-opened-date", type="date", value=datetime.now().date().isoformat(), className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Select(id="debt-type", options=_native_select_options(debt_type_options, "Тип", include_empty=False), value="receivable", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Input(id="debt-counterparty", type="text", placeholder="Контрагент", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Input(id="debt-principal-amount", type="number", placeholder="Сумма долга", step="any", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Select(id="debt-principal-currency", options=currency_options, value=currency, className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=1),
                            dbc.Col(dbc.Input(id="debt-cash-amount", type="number", placeholder="Сумма проводки", step="any", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Select(id="debt-cash-currency", options=currency_options, value=currency, className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=1),
                        ],
                        className="g-2",
                    ),
                    dbc.Row(
                        [
                            dbc.Col(dbc.Input(id="debt-comment", type="text", placeholder="Комментарий", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=10),
                            dbc.Col(dbc.Button("Добавить", id="debt-add-button", color="primary", className="w-100"), xs=12, md=2),
                        ],
                        className="g-2 mt-2",
                    ),
                ],
                style=_section_style(theme),
            )
        )

    sections.extend(
        [
            html.Section(
                [
                    html.Div(
                        [
                            html.H2("Активные долги", className="h5 mb-0"),
                            dbc.Button("Миграция legacy", id="debt-migrate-button", color="secondary", outline=True, size="sm"),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dbc.Alert(id="debt-input-message", children="", color="secondary", is_open=True, className="mb-3 py-2"),
                    _active_debts_grid(currency, theme, "receivable"),
                    html.Div(className="my-4"),
                    _active_debts_grid(currency, theme, "liability"),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.H2("Погашение выбранного долга", className="h5 mb-3"),
                    dbc.Row(
                        [
                            dbc.Col(dbc.Select(id="debt-payment-id", options=_debt_select_options(currency), value="", className="finrep-native-input", style=_form_control_style(theme)), xs=12, lg=5),
                            dbc.Col(dbc.Input(id="debt-payment-date", type="date", value=datetime.now().date().isoformat(), className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Input(id="debt-payment-amount", type="number", placeholder="Сумма платежа", step="any", className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Select(id="debt-payment-cash-currency", options=currency_options, value=currency, className="finrep-native-input", style=_form_control_style(theme)), xs=12, md=2),
                            dbc.Col(dbc.Button("Погасить", id="debt-payment-button", color="primary", outline=True, className="w-100"), xs=12, md=1),
                        ],
                        className="g-2",
                    ),
                    dbc.Input(id="debt-payment-comment", type="text", placeholder="Комментарий к погашению", className="finrep-native-input mt-2", style=_form_control_style(theme)),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.H2("Черновики транзакций по долгам", className="h5 mb-3"),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="debt-transaction-drafts-grid",
                            rowData=_debt_transaction_draft_records(),
                            columnDefs=_debt_transaction_draft_column_defs(),
                            defaultColDef=_ag_grid_default_col_def(),
                            dashGridOptions={"pagination": False, "suppressFieldDotNotation": True},
                            className=_ag_grid_class_name(theme),
                            style=_ag_grid_style("260px"),
                        )
                    ),
                ],
                style=_section_style(theme),
            ),
        ]
    )

    return html.Div(
        sections,
        className="d-grid gap-4 pt-3",
    )


def _assets_input_layout(year: str, month: str, theme: str | None):
    return html.Div(
        [
            html.Section(
                [
                    html.Div(
                        [
                            html.H2("Активы", className="h5 mb-0"),
                            html.Div(
                                [
                                    dbc.Button("Загрузить/создать", id="assets-load-button", color="secondary", outline=True, size="sm"),
                                    dbc.Button("Добавить строку", id="assets-add-row-button", color="secondary", outline=True, size="sm"),
                                    dbc.Button("Удалить выбранные", id="assets-delete-row-button", color="danger", outline=True, size="sm"),
                                    dbc.Button("Применить", id="assets-apply-button", color="primary", outline=True, size="sm"),
                                ],
                                className="d-flex flex-wrap gap-2",
                            ),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dbc.Alert(
                        id="assets-input-message",
                        children=f"Редактируется snapshot активов за {year}-{str(int(month)).zfill(2)}. Если файла нет, он будет создан копией предыдущего месяца.",
                        color="secondary",
                        is_open=True,
                        className="mb-3 py-2",
                    ),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="assets-input-grid",
                            rowData=_asset_input_records(year, month),
                            columnDefs=_asset_input_column_defs(),
                            defaultColDef=_ag_grid_default_col_def(editable=True),
                            dashGridOptions={"pagination": False, "suppressFieldDotNotation": True, "rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True, "undoRedoCellEditing": True},
                            className=_ag_grid_class_name(theme),
                            style=_ag_grid_style("920px"),
                        )
                    ),
                ],
                style=_section_style(theme),
            ),
        ],
        className="d-grid gap-4 pt-3",
    )

def _dataframe_records(data: pd.DataFrame) -> list[dict]:
    if data.empty:
        return []
    return data.fillna("0").to_dict("records")


def _simple_column_defs(data: pd.DataFrame) -> list[dict]:
    if data.empty:
        return []
    return [{"field": column, "minWidth": 120, "flex": 1 if column != "Дата" else 0} for column in data.columns]


def _form_control_style(theme: str | None) -> dict:
    if theme == "dark":
        return {
            "backgroundColor": "#2b2b2b",
            "borderColor": "#646464",
            "color": "#dcdcdc",
            "WebkitTextFillColor": "#dcdcdc",
            "caretColor": "#dcdcdc",
            "height": "38px",
            "fontWeight": 600,
        }
    return {
        "backgroundColor": "#ffffff",
        "borderColor": "#ced4da",
        "color": "#212529",
        "WebkitTextFillColor": "#212529",
        "caretColor": "#212529",
        "height": "38px",
    }


def _native_select_options(options: list[dict], placeholder: str, include_empty: bool = True) -> list[dict]:
    normalized = [{"label": str(option.get("label", "")), "value": str(option.get("value", ""))} for option in options]
    if include_empty:
        return [{"label": placeholder, "value": "__all__"}, *normalized]
    return normalized


def _transaction_category_options() -> list[dict]:
    try:
        categories = sorted(str(value) for value in get_transactions()["Категория"].dropna().unique())
    except Exception:
        categories = sorted(config.NOT_COST_COLS)
    return [{"label": category, "value": category} for category in categories]


def _draft_status_options() -> list[dict]:
    return [{"label": status, "value": status} for status in sorted(DRAFT_STATUSES)]


def _draft_source_options() -> list[dict]:
    data = read_transaction_drafts()
    sources = sorted(source for source in data["source"].dropna().unique() if str(source))
    return [{"label": source, "value": source} for source in sources]


def _transaction_month_options(selected_month: str | None = None) -> list[dict]:
    data = read_transaction_drafts()
    months = set(pd.to_datetime(data["date"], errors="coerce").dropna().dt.strftime("%Y-%m").unique())
    if selected_month:
        months.add(str(selected_month))
    return [{"label": month, "value": month} for month in sorted(months, reverse=True)]


def _transaction_draft_records(month_filter, category_filter, status_filter, source_filter) -> list[dict]:
    data = read_transaction_drafts()
    category_filter = None if category_filter in {None, "", "__all__"} else category_filter
    status_filter = None if status_filter in {None, "", "__all__"} else status_filter
    source_filter = None if source_filter in {None, "", "__all__"} else source_filter
    if month_filter:
        months = pd.to_datetime(data["date"], errors="coerce").dt.strftime("%Y-%m")
        data = data[months == str(month_filter)]
    if category_filter:
        data = data[data["category"] == str(category_filter)]
    if status_filter:
        data = data[data["status"] == str(status_filter)]
    if source_filter:
        data = data[data["source"] == str(source_filter)]
    return data.sort_values(["date", "category", "comment"], kind="mergesort").to_dict("records")


def _asset_input_records(year: str, month: str) -> list[dict]:
    data = read_asset_snapshot(year, month).copy(deep=True)
    if data.empty:
        return []
    data["amount_sort"] = pd.to_numeric(data["amount"], errors="coerce").fillna(0)
    data = data.sort_values("amount_sort", ascending=False, kind="mergesort")
    data["amount"] = data["amount"].map(_format_input_amount)
    return _dataframe_records(data)


def _active_debts_grid(currency: str, theme: str | None, debt_type: str):
    title = "Дебиторская задолженность" if debt_type == "receivable" else "Кредиторская задолженность"
    grid_id = f"active-{debt_type}-debts-grid"
    return html.Div(
        [
            html.H3(title, className="h6 mb-2"),
            _ag_grid_scroll(
                dag.AgGrid(
                    id=grid_id,
                    rowData=_active_debt_records(currency, debt_type),
                    columnDefs=_active_debt_column_defs(currency),
                    defaultColDef=_ag_grid_default_col_def(),
                    dashGridOptions={"pagination": False, "suppressFieldDotNotation": True},
                    className=_ag_grid_class_name(theme),
                    style=_ag_grid_style("320px"),
                )
            ),
        ]
    )


def _active_debt_records(currency: str, debt_type: str | None = None) -> list[dict]:
    frames = []
    debt_types = [debt_type] if debt_type else sorted(DEBT_TYPES)
    for current_type in debt_types:
        frame = active_debt_balances(current_type, currency).copy(deep=True)
        if frame.empty:
            continue
        frame["Тип"] = frame["type"].map({"receivable": "Мне должны", "liability": "Я должен"})
        frames.append(frame)
    if not frames:
        return []

    data = pd.concat(frames, ignore_index=True)
    converted_column = f"outstanding_{currency}"
    display = pd.DataFrame(
        {
            "debt_id": data["debt_id"],
            "Тип": data["Тип"],
            "Контрагент": data["counterparty"],
            "Дата": data["opened_date"],
            "Валюта долга": data["principal_currency"],
            "Сумма долга": data["principal_amount"].map(_format_input_amount),
            "Погашено": data["paid_amount"].map(_format_input_amount),
            "Остаток": data["outstanding_amount"].map(_format_input_amount),
            f"Остаток {currency}": data[converted_column].map(_format_input_amount) if converted_column in data else data["outstanding_amount"].map(_format_input_amount),
            "Комментарий": data["comment"],
        }
    )
    return display.sort_values(["Тип", "Контрагент", "Дата"], kind="mergesort").to_dict("records")


def _active_debt_column_defs(currency: str) -> list[dict]:
    return [
        {"field": "debt_id", "headerName": "ID", "width": 170},
        {"field": "Тип", "width": 130},
        {"field": "Контрагент", "flex": 1, "minWidth": 180},
        {"field": "Дата", "width": 120},
        {"field": "Валюта долга", "width": 120},
        {"field": "Сумма долга", "width": 140},
        {"field": "Погашено", "width": 130},
        {"field": "Остаток", "width": 130},
        {"field": f"Остаток {currency}", "width": 150},
        {"field": "Комментарий", "flex": 1, "minWidth": 220},
    ]


def _debt_select_options(currency: str) -> list[dict]:
    options = [{"label": "Выбери долг для погашения", "value": ""}]
    frames = []
    for debt_type in sorted(DEBT_TYPES):
        frame = active_debt_balances(debt_type, currency).copy(deep=True)
        if frame.empty:
            continue
        frame["Тип"] = frame["type"].map({"receivable": "Мне должны", "liability": "Я должен"})
        frames.append(frame)
    if not frames:
        return options

    data = pd.concat(frames, ignore_index=True).sort_values(["type", "counterparty", "opened_date"], kind="mergesort")
    for _, row in data.iterrows():
        label = (
            f"{row['Тип']} | {row['counterparty']} | "
            f"{_format_input_amount(row['outstanding_amount'])} {row['principal_currency']} | {row['debt_id']}"
        )
        options.append({"label": label, "value": str(row["debt_id"])})
    return options


def _debt_transaction_draft_records() -> list[dict]:
    data = read_transaction_drafts()
    data = data[data["source"].eq("debt")].copy(deep=True)
    if data.empty:
        return []
    return data.sort_values(["date", "category", "comment"], ascending=[False, True, True], kind="mergesort").to_dict("records")


def _debt_transaction_draft_column_defs() -> list[dict]:
    return [
        {"field": "date", "headerName": "Дата", "width": 120},
        {"field": "category", "headerName": "Категория", "width": 180},
        {"field": "amount", "headerName": "Сумма", "width": 120},
        {"field": "currency", "headerName": "Валюта", "width": 100},
        {"field": "comment", "headerName": "Комментарий", "flex": 1, "minWidth": 240},
        {"field": "status", "headerName": "Статус", "width": 120},
        {"field": "source_id", "headerName": "ID", "flex": 1, "minWidth": 220},
    ]


def _asset_input_column_defs() -> list[dict]:
    currencies = list(config.UNIQUE_TICKERS)
    return [
        {"field": "account", "headerName": "Счет", "editable": True, "flex": 1, "minWidth": 260},
        {"field": "amount", "headerName": "Сумма", "editable": True, "width": 170},
        {"field": "currency", "headerName": "Валюта", "editable": True, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": currencies}, "width": 120},
        {"field": "amount_sort", "hide": True, "sort": "desc", "sortIndex": 0},
    ]


def _asset_row_key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("account", "")), str(row.get("amount", "")), str(row.get("currency", "")))


def _format_input_amount(value) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "0"
    if float(numeric).is_integer():
        return f"{int(numeric):,}".replace(",", " ")
    return f"{float(numeric):,.2f}".replace(",", " ").rstrip("0").rstrip(".")


def _kaspi_import_column_defs() -> list[dict]:
    categories = [option["value"] for option in _transaction_category_options()]
    category_class_rules = {
        "kaspi-category-income": "params.value == 'Доход'",
        "kaspi-category-saving": "params.value == 'Сбережения' || params.value == 'Инвестиции'",
        "kaspi-category-internal": "params.value == 'Внутренний перевод'",
        "kaspi-category-food": "params.value == 'Пища'",
        "kaspi-category-transport": "params.value == 'Транспорт'",
        "kaspi-category-communication": "params.value == 'Связь'",
        "kaspi-category-other": "params.value == 'Прочее'",
    }
    return [
        {"field": "category", "headerName": "Категория", "editable": True, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": categories}, "width": 190, "sort": "asc", "cellClassRules": category_class_rules},
        {"field": "date", "headerName": "Дата", "width": 120, "sort": "asc", "sortIndex": 1},
        {"field": "amount", "headerName": "Сумма", "width": 120},
        {"field": "currency", "headerName": "Валюта", "width": 100},
        {"field": "comment", "headerName": "Комментарий", "editable": True, "flex": 1, "minWidth": 220},
        {"field": "import_action", "headerName": "Действие", "editable": True, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": ["import", "skip"]}, "width": 120},
        {"field": "skip_reason", "headerName": "Причина skip", "width": 170},
        {"field": "duplicate_in_source", "headerName": "Дубль в CSV", "width": 130},
        {"field": "duplicate_in_staging", "headerName": "Дубль в staging", "width": 150},
        {"field": "details", "headerName": "Детали PDF", "flex": 1, "minWidth": 240},
        {"field": "source_id", "headerName": "ID", "hide": True},
        {"field": "source", "headerName": "Источник", "hide": True},
        {"field": "status", "headerName": "Статус", "hide": True},
    ]


def _transaction_draft_column_defs(category_options: list[dict], currencies: list[str]) -> list[dict]:
    categories = [option["value"] for option in category_options]
    return [
        {"field": "date", "headerName": "Дата", "width": 130},
        {"field": "category", "headerName": "Категория", "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": categories}, "width": 180},
        {"field": "currency", "headerName": "Валюта", "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": currencies}, "width": 110},
        {"field": "amount", "headerName": "Сумма", "width": 130},
        {"field": "comment", "headerName": "Комментарий", "flex": 1, "minWidth": 220},
        {"field": "status", "headerName": "Статус", "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": sorted(DRAFT_STATUSES)}, "width": 130},
        {"field": "source", "headerName": "Источник", "editable": False, "width": 130},
        {"field": "source_id", "headerName": "ID", "editable": False, "width": 220},
    ]


def _error_state(message: str, exc: Exception):
    return dbc.Alert(
        [
            html.Div(message, className="fw-semibold"),
            html.Div(str(exc), className="small mt-1"),
        ],
        color="danger",
    )


def _graph_section(dataset: DashboardDataset, height: str = "520px", theme: str | None = None):
    if dataset.dataframe.empty:
        return _empty_section(dataset)

    graph_config = {"displaylogo": False, "responsive": True, "scrollZoom": False}
    if dataset.graph_config:
        graph_config.update(dataset.graph_config)

    return html.Section(
        [
            _section_header(dataset),
            dcc.Graph(
                id=f"{dataset.id}-graph",
                figure=dataset.figure,
                responsive=True,
                style={"height": height},
                config=graph_config,
            ),
        ],
        style=_section_style(theme),
    )


def _grid_section(dataset: DashboardDataset, height: str = "360px", theme: str | None = None):
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    if data.empty:
        return _empty_section(dataset)

    return html.Section(
        [
            _section_header(dataset),
            _ag_grid_scroll(
                dag.AgGrid(
                    id=f"{dataset.id}-grid",
                    rowData=_grid_row_data(dataset, data),
                    columnDefs=_grid_column_defs(dataset, data, theme),
                    defaultColDef=_ag_grid_default_col_def(),
                    columnSize="autoSize" if dataset.id in {"fx_rates", "year_fx_rates", "month_fx_rates"} else "sizeToFit",
                    dashGridOptions={
                        "pagination": False,
                        "suppressFieldDotNotation": True,
                    },
                    className=_ag_grid_class_name(theme),
                    style=_ag_grid_style(height),
                )
            ),
        ],
        style=_section_style(theme),
    )


def _grid_row_data(dataset: DashboardDataset, data: pd.DataFrame) -> list[dict]:
    display_data = _fill_zero_display_cells(data) if dataset.id in {"month_transactions", "month_summary"} else data
    records = display_data.to_dict("records")
    raw = dataset.dataframe.reset_index(drop=True)

    if dataset.id == "yearly_stats":
        income_max = _positive_max(raw[raw["Год"].astype(str) != "Всего"], "Доход")
        expense_max = _positive_max(raw[raw["Год"].astype(str) != "Всего"], "Расход")

        for index, record in enumerate(records):
            if index >= len(raw):
                continue
            row = raw.iloc[index]
            is_total = str(row.get("Год")) == "Всего"
            record["__is_total"] = is_total
            record["__income_level"] = 0 if is_total else _gradient_level(row.get("Доход"), income_max)
            record["__expense_level"] = 0 if is_total else _gradient_level(row.get("Расход"), expense_max)
            record["__balance_sign"] = "total" if is_total else _value_sign(row.get("Сальдо"))
            record["__income_pct_sign"] = "total" if is_total else _percentage_sign(row.get("Процент дохода"))
        return records

    if dataset.id == "year_quarter_stats":
        total_mask = raw["Квартал"].astype(str) == "Всего"
        _add_level_metadata(records, raw[~total_mask].reset_index(drop=True), {"Общий доход": "__quarter_income_level"})
        _add_level_metadata(records, raw[~total_mask].reset_index(drop=True), {"Общий расход": "__quarter_expense_level"})
        for index, record in enumerate(records):
            if index >= len(raw):
                continue
            row = raw.iloc[index]
            is_total = str(row.get("Квартал")) == "Всего"
            record["__is_total"] = is_total
            if is_total:
                record["__quarter_income_level"] = 0
                record["__quarter_expense_level"] = 0
                record["__quarter_balance_sign"] = "total"
            else:
                record["__quarter_balance_sign"] = _value_sign(row.get("Сальдо"))
        return records

    if dataset.id == "year_income_cost_stats":
        _add_level_metadata(records, raw, {"Доход": "__stats_income_level", "Расход": "__stats_expense_level"})
        return records

    if dataset.id in {"year_cost_distribution", "month_cost_distribution"}:
        _add_level_metadata(
            records,
            raw,
            {
                "Суммарно": "__cost_sum_level",
                "Среднее": "__cost_avg_level",
                "Процент": "__cost_pct_level",
            },
        )
        return records

    if dataset.id == "month_transactions":
        _add_level_metadata(records, raw, {"Доход": "__month_income_level", "Сбережения": "__month_savings_level"})
        expense_columns = [column for column in raw.columns if column not in ["Дата", *config.NOT_COST_COLS]]
        _add_level_metadata(records, raw, {column: f"__month_expense_{index}_level" for index, column in enumerate(expense_columns)})
        return records

    if dataset.id == "month_summary":
        _add_level_metadata(records, raw, {"Доход": "__month_summary_income_level", "Сбережения": "__month_summary_savings_level"})
        _add_level_metadata(records, raw, {"Расход": "__month_summary_expense_level"})
        _add_sign_metadata(
            records,
            raw,
            {
                "Валютная переоценка": "__month_summary_fx_sign",
                "Расхождение с активами": "__month_summary_asset_gap_sign",
            },
        )
        return records

    if dataset.id == "month_assets":
        _add_asset_metadata(records, raw)
        return records

    if dataset.id == "planning_goals":
        for index, record in enumerate(records):
            if index >= len(raw):
                continue
            row = raw.iloc[index]
            record["__planning_delta_sign"] = _value_sign(row.get("Отклонение"))
            record["__planning_progress_level"] = _gradient_level(row.get("Прогресс (%)"), 100)
        return records

    if dataset.id == "planning_fx_scenarios":
        _add_level_metadata(records, raw, {"Капитал": "__planning_fx_capital_level"})
        for index, record in enumerate(records):
            if index < len(raw):
                record["__planning_fx_delta_sign"] = _value_sign(raw.iloc[index].get("Изменение капитала"))
        return records

    if dataset.id == "investment_summary":
        for index, record in enumerate(records):
            if index < len(raw):
                record["__investment_summary_sign"] = _value_sign(raw.iloc[index].get("value"))
        return records

    if dataset.id == "investment_positions":
        _add_level_metadata(records, raw, {"market_value": "__investment_value_level", "allocation": "__investment_allocation_level"})
        for index, record in enumerate(records):
            if index < len(raw):
                row = raw.iloc[index]
                record["__investment_unrealized_sign"] = _value_sign(row.get("unrealized_pnl"))
                record["__investment_realized_sign"] = _value_sign(row.get("realized_pnl"))
                record["__investment_total_sign"] = _value_sign(row.get("total_pnl"))
        return records

    if dataset.id in {"investment_allocation_type", "investment_allocation_currency"}:
        _add_level_metadata(records, raw, {"market_value": "__investment_alloc_value_level", "allocation": "__investment_alloc_pct_level"})
        return records

    simple_level_tables = {
        "year_income_by_month": {"Доход": ("__monthly_income_level", "green")},
        "year_cost_by_month": {"Расход": ("__monthly_cost_level", "red")},
        "year_capital_by_month": {
            "Капитал": ("__monthly_capital_level", "green"),
            "Капитал по активам": ("__monthly_asset_capital_level", "blue"),
        },
    }
    if dataset.id in simple_level_tables:
        _add_level_metadata(
            records,
            raw,
            {column: field for column, (field, _palette) in simple_level_tables[dataset.id].items()},
        )
        if dataset.id == "year_capital_by_month":
            _add_sign_metadata(
                records,
                raw,
                {
                    "Валютная переоценка": "__monthly_fx_revaluation_sign",
                    "Расхождение с активами": "__monthly_asset_gap_sign",
                },
            )
    return records


def _grid_column_defs(dataset: DashboardDataset, data: pd.DataFrame, theme: str | None = None) -> list[dict]:
    if dataset.id == "yearly_stats":
        column_defs = []
        for column in data.columns:
            column_def = {"field": column, "cellStyle": _total_row_style(theme)}
            if column == "Доход":
                column_def["cellStyle"] = _merge_total_style(_level_style("__income_level", "green", theme), theme)
            elif column == "Расход":
                column_def["cellStyle"] = _merge_total_style(_level_style("__expense_level", "red", theme), theme)
            elif column == "Сальдо":
                column_def["cellStyle"] = _merge_total_style(_sign_style("__balance_sign", theme), theme)
            elif column == "Процент дохода":
                column_def["cellStyle"] = _merge_total_style(_sign_style("__income_pct_sign", theme), theme)
            column_defs.append(column_def)
        return column_defs

    if dataset.id in {"fx_rates", "year_fx_rates", "month_fx_rates"}:
        widths = {
            "Валюта": {"width": 88, "maxWidth": 100},
            "Курс": {"width": 112, "maxWidth": 128},
            "Обратный курс": {"width": 138, "maxWidth": 158},
            "Источник": {"width": 620, "minWidth": 520},
            "Изменение (%)": {"width": 142, "maxWidth": 160},
        }
        return [
            {
                "field": column,
                "cellStyle": _plain_cell_style(theme),
                **widths.get(column, {"flex": 1}),
            }
            for column in data.columns
        ]

    style_by_dataset = {
        "year_quarter_stats": {
            "Квартал": _total_row_style(theme),
            "Общий доход": _merge_total_style(_level_style("__quarter_income_level", "green", theme), theme),
            "Общий расход": _merge_total_style(_level_style("__quarter_expense_level", "red", theme), theme),
            "Сальдо": _merge_total_style(_sign_style("__quarter_balance_sign", theme), theme),
        },
        "year_income_cost_stats": {
            "Доход": _level_style("__stats_income_level", "green", theme),
            "Расход": _level_style("__stats_expense_level", "red", theme),
        },
        "year_cost_distribution": {
            "Суммарно": _level_style("__cost_sum_level", "red", theme),
            "Среднее": _level_style("__cost_avg_level", "red", theme),
            "Процент": _level_style("__cost_pct_level", "red", theme),
        },
        "month_cost_distribution": {
            "Суммарно": _level_style("__cost_sum_level", "red", theme),
            "Среднее": _level_style("__cost_avg_level", "red", theme),
            "Процент": _level_style("__cost_pct_level", "red", theme),
        },
        "month_transactions": _month_transaction_column_styles(data, theme),
        "month_summary": {
            "Доход": _level_style("__month_summary_income_level", "green", theme),
            "Сбережения": _level_style("__month_summary_savings_level", "green", theme),
            "Расход": _level_style("__month_summary_expense_level", "red", theme),
            "Валютная переоценка": _sign_style("__month_summary_fx_sign", theme),
            "Расхождение с активами": _sign_style("__month_summary_asset_gap_sign", theme),
        },
        "month_assets": _month_asset_column_styles(data, theme),
        "year_income_by_month": {"Доход": _level_style("__monthly_income_level", "green", theme)},
        "year_cost_by_month": {"Расход": _level_style("__monthly_cost_level", "red", theme)},
        "year_capital_by_month": {
            "Капитал": _level_style("__monthly_capital_level", "green", theme),
            "Капитал по активам": _level_style("__monthly_asset_capital_level", "blue", theme),
            "Валютная переоценка": _sign_style("__monthly_fx_revaluation_sign", theme),
            "Расхождение с активами": _sign_style("__monthly_asset_gap_sign", theme),
        },
        "planning_goals": {
            "Цель": _editable_cell_style(theme),
            "Отклонение": _sign_style("__planning_delta_sign", theme),
            "Прогресс (%)": _level_style("__planning_progress_level", "green", theme),
        },
        "planning_fx_scenarios": {
            "Капитал": _level_style("__planning_fx_capital_level", "blue", theme),
            "Изменение капитала": _sign_style("__planning_fx_delta_sign", theme),
        },
        "investment_summary": {
            "Значение": _sign_style("__investment_summary_sign", theme),
        },
        "investment_positions": {
            "Стоимость": _level_style("__investment_value_level", "green", theme),
            "Нереализованный PnL": _sign_style("__investment_unrealized_sign", theme),
            "Реализованный PnL": _sign_style("__investment_realized_sign", theme),
            "Итого PnL": _sign_style("__investment_total_sign", theme),
            "Доля (%)": _level_style("__investment_allocation_level", "blue", theme),
        },
        "investment_allocation_type": {
            "Стоимость": _level_style("__investment_alloc_value_level", "green", theme),
            "Доля (%)": _level_style("__investment_alloc_pct_level", "blue", theme),
        },
        "investment_allocation_currency": {
            "Стоимость": _level_style("__investment_alloc_value_level", "green", theme),
            "Доля (%)": _level_style("__investment_alloc_pct_level", "blue", theme),
        },
    }
    column_styles = style_by_dataset.get(dataset.id, {})
    column_defs = []
    for column in data.columns:
        column_def = {"field": column, "cellStyle": column_styles.get(column, _plain_cell_style(theme))}
        if dataset.id == "planning_goals" and column == "Цель":
            column_def.update({"editable": True, "singleClickEdit": True})
        column_defs.append(column_def)
    return column_defs


def _fill_zero_display_cells(data: pd.DataFrame) -> pd.DataFrame:
    display = data.copy(deep=True)
    for column in display.columns:
        if column == "Дата":
            continue
        display[column] = display[column].replace("", "0").fillna("0")
    return display


def _plain_cell_style(theme: str | None = None) -> dict:
    return {"backgroundColor": "#2b2b2b", "color": "#a9b7c6"} if theme == "dark" else {}


def _editable_cell_style(theme: str | None = None) -> dict:
    if theme == "dark":
        return {
            "backgroundColor": "#333b45",
            "color": "#dcdcdc",
            "border": "1px solid #6897bb",
            "fontWeight": "600",
        }
    return {"backgroundColor": "#eef6ff", "border": "1px solid #9ec5fe", "fontWeight": "600"}


def _add_level_metadata(records: list[dict], raw: pd.DataFrame, field_map: dict[str, str]) -> None:
    max_by_column = {column: _positive_max(raw, column) for column in field_map}
    for index, record in enumerate(records):
        if index >= len(raw):
            continue
        row = raw.iloc[index]
        for column, field in field_map.items():
            record[field] = _gradient_level(row.get(column), max_by_column[column])


def _add_sign_metadata(records: list[dict], raw: pd.DataFrame, field_map: dict[str, str]) -> None:
    for index, record in enumerate(records):
        if index >= len(raw):
            continue
        row = raw.iloc[index]
        for column, field in field_map.items():
            record[field] = _value_sign(row.get(column))


def _add_asset_metadata(records: list[dict], raw: pd.DataFrame) -> None:
    value_columns = [column for column in raw.columns if column != "Счет"]
    max_by_column = {column: _positive_max_from_any(raw[column]) for column in value_columns}
    for index, record in enumerate(records):
        if index >= len(raw):
            continue
        row = raw.iloc[index]
        account = str(row.get("Счет", ""))
        record["__is_asset_total"] = account in {"Всего", "Всего в валюте"}
        record["__is_asset_currency_total"] = account == "Всего в валюте,%"
        row_max = _positive_max_from_any(row[value_columns]) if record["__is_asset_currency_total"] else 0.0
        for column in value_columns:
            if record["__is_asset_total"]:
                record[f"__asset_{column}_level"] = 0
                record[f"__asset_blue_{column}_level"] = 0
            else:
                record[f"__asset_{column}_level"] = _gradient_level(_to_number(row.get(column)), max_by_column[column])
                record[f"__asset_blue_{column}_level"] = _gradient_level(_to_number(row.get(column)), row_max)


def _month_transaction_column_styles(data: pd.DataFrame, theme: str | None = None) -> dict:
    styles = {
        "Доход": _level_style("__month_income_level", "green", theme),
        "Сбережения": _level_style("__month_savings_level", "green", theme),
    }
    expense_columns = [column for column in data.columns if column not in ["Дата", *config.NOT_COST_COLS]]
    styles.update(
        {column: _level_style(f"__month_expense_{index}_level", "red", theme) for index, column in enumerate(expense_columns)}
    )
    return styles


def _month_asset_column_styles(data: pd.DataFrame, theme: str | None = None) -> dict:
    return {
        column: _merge_asset_total_style(
            _merge_row_level_style(
                _level_style(f"__asset_blue_{column}_level", "blue", theme),
                _level_style(f"__asset_{column}_level", "green", theme),
            ),
            theme,
        )
        for column in data.columns
        if column != "Счет"
    } | {"Счет": _merge_asset_currency_total_style(_asset_total_row_style(theme), theme)}


def _asset_total_row_style(theme: str | None = None) -> dict:
    total_style = _total_style(theme)
    return {
        "styleConditions": [
            {
                "condition": "params.data.__is_asset_total",
                "style": {
                    **total_style,
                },
            }
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _merge_asset_total_style(style: dict, theme: str | None = None) -> dict:
    total_condition = _asset_total_row_style(theme)["styleConditions"][0]
    return {
        "styleConditions": [total_condition] + style.get("styleConditions", []),
        "defaultStyle": style.get("defaultStyle", {}),
    }


def _asset_currency_total_style(theme: str | None = None) -> dict:
    if theme == "dark":
        base_style = {"backgroundColor": "#3c3f41", "color": "#a9b7c6", "fontWeight": "600"}
    else:
        base_style = {"backgroundColor": "#a9b7c6", "color": "#1e3a8a", "fontWeight": "600"}
    return {
        "styleConditions": [
            {
                "condition": "params.data.__is_asset_currency_total",
                "style": base_style,
            }
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _merge_asset_currency_total_style(style: dict, theme: str | None = None) -> dict:
    currency_condition = _asset_currency_total_style(theme)["styleConditions"][0]
    return {
        "styleConditions": style.get("styleConditions", []) + [currency_condition],
        "defaultStyle": style.get("defaultStyle", _plain_cell_style(theme)),
    }


def _merge_row_level_style(primary: dict, secondary: dict) -> dict:
    return {
        "styleConditions": primary.get("styleConditions", []) + secondary.get("styleConditions", []),
        "defaultStyle": primary.get("defaultStyle", secondary.get("defaultStyle", {})),
    }


def _positive_max_from_any(values: pd.Series) -> float:
    numeric = values.map(_to_number).abs().dropna()
    return float(numeric.max()) if not numeric.empty else 0.0


def _to_number(value) -> float:
    if pd.isna(value):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value)
    for symbol in ["₽", "$", "€", "₸", "£", "%", " "]:
        cleaned = cleaned.replace(symbol, "")
    cleaned = cleaned.replace(",", ".")
    return pd.to_numeric(cleaned, errors="coerce")


def _positive_max(data: pd.DataFrame, column: str) -> float:
    if column not in data.columns or data.empty:
        return 0.0
    values = pd.to_numeric(data[column], errors="coerce").abs().dropna()
    return float(values.max()) if not values.empty else 0.0


def _gradient_level(value, max_value: float) -> int:
    if max_value <= 0:
        return 0
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric) or numeric <= 0:
        return 0
    ratio = float(numeric) / max_value
    if ratio >= 0.75:
        return 3
    if ratio >= 0.45:
        return 2
    return 1


def _value_sign(value) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "zero"
    if numeric > 0:
        return "positive"
    if numeric < 0:
        return "negative"
    return "zero"


def _percentage_sign(value) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        return "zero"
    if numeric >= 100:
        return "positive"
    return "negative"


def _sign_style(field: str, theme: str | None = None) -> dict:
    if theme == "dark":
        positive_style = {"backgroundColor": "#31452f", "color": "#b6d7a8"}
        negative_style = {"backgroundColor": "#4a2f2f", "color": "#d99694"}
    else:
        positive_style = {"backgroundColor": "#dff3e3", "color": "#1f5130"}
        negative_style = {"backgroundColor": "#f8dddd", "color": "#6b2626"}
    return {
        "styleConditions": [
            {
                "condition": f"params.data.{field} == 'positive'",
                "style": positive_style,
            },
            {
                "condition": f"params.data.{field} == 'negative'",
                "style": negative_style,
            },
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _total_style(theme: str | None = None) -> dict:
    if theme == "dark":
        return {"backgroundColor": "#555555", "color": "#dcdcdc", "fontWeight": "600"}
    return {"backgroundColor": "rgba(108, 117, 125, 0.16)", "color": "#2f3742", "fontWeight": "600"}


def _total_row_style(theme: str | None = None) -> dict:
    total_style = _total_style(theme)
    return {
        "styleConditions": [
            {
                "condition": "params.data.__is_total",
                "style": {
                    **total_style,
                },
            }
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _merge_total_style(style: dict, theme: str | None = None) -> dict:
    total_condition = _total_row_style(theme)["styleConditions"][0]
    return {
        "styleConditions": [total_condition] + style.get("styleConditions", []),
        "defaultStyle": style.get("defaultStyle", {}),
    }


def _level_style(field: str, palette: str, theme: str | None = None) -> dict:
    if theme == "dark" and palette == "green":
        colors = {1: "#2f3d2f", 2: "#3d5a3a", 3: "#4f714b"}
        text_color = "#b6d7a8"
    elif theme == "dark" and palette == "blue":
        colors = {1: "#2f3d4a", 2: "#38546b", 3: "#4a6f8a"}
        text_color = "#a9b7c6"
    elif theme == "dark":
        colors = {1: "#3f2d2d", 2: "#5a3838", 3: "#704444"}
        text_color = "#d99694"
    elif palette == "green":
        colors = {1: "#edf8ef", 2: "#d7efd9", 3: "#bde5c0"}
        text_color = "#214d2c"
    elif palette == "blue":
        colors = {1: "#eff6ff", 2: "#a9b7c6", 3: "#a9b7c6"}
        text_color = "#1e3a8a"
    else:
        colors = {1: "#fff0ed", 2: "#f9d8d2", 3: "#f1c0b8"}
        text_color = "#63312c"

    return {
        "styleConditions": [
            {
                "condition": f"params.data.{field} == 3",
                "style": {"backgroundColor": colors[3], "color": text_color},
            },
            {
                "condition": f"params.data.{field} == 2",
                "style": {"backgroundColor": colors[2], "color": text_color},
            },
            {
                "condition": f"params.data.{field} == 1",
                "style": {"backgroundColor": colors[1], "color": text_color},
            },
        ],
        "defaultStyle": _plain_cell_style(theme),
    }


def _empty_section(dataset: DashboardDataset):
    return html.Section(
        [
            _section_header(dataset),
            dbc.Alert("Нет данных для отображения.", color="warning", className="mb-0"),
        ]
    )


def _section_header(dataset: DashboardDataset):
    return html.Div(
        [
            html.H2(dataset.title, className="h5 mb-0"),
            html.Div(
                [
                    dbc.Button(
                        "XLSX",
                        id={"type": "dataset-download-button", "dataset_id": dataset.id},
                        color="secondary",
                        outline=True,
                        size="sm",
                    ),
                    dcc.Download(id={"type": "dataset-download", "dataset_id": dataset.id}),
                ],
                className="d-flex gap-2",
            ),
        ],
        className="d-flex justify-content-between align-items-center mb-3",
    )


def _datasets_for_tab(
    active_tab: str,
    currency: str,
    year: str,
    month: str,
) -> dict[str, DashboardDataset]:
    if active_tab == "year":
        return build_year_dashboard_data(
            year,
            currency,
            fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
        )
    if active_tab == "planning":
        return build_planning_dashboard_data(
            year,
            currency,
            fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
        )
    if active_tab == "investments":
        return build_investment_dashboard_data(
            currency,
            fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
        )
    if active_tab == "month":
        return build_month_dashboard_data(
            year,
            month,
            currency,
            fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
        )
    return build_main_dashboard_data(
        currency,
        fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
    )


def _download_filename(
    dataset: DashboardDataset,
    currency: str,
    active_tab: str,
    year: str,
    month: str,
) -> str:
    timestamp = datetime.now().strftime("%Y%m%d")
    if active_tab == "year":
        return f"year_report_{year}_{dataset.id}_{currency}_{timestamp}.xlsx"
    if active_tab == "month":
        return f"month_report_{year}_{month}_{dataset.id}_{currency}_{timestamp}.xlsx"
    if active_tab == "planning":
        return f"planning_{year}_{dataset.id}_{currency}_{timestamp}.xlsx"
    if active_tab == "investments":
        return f"investments_{dataset.id}_{currency}_{timestamp}.xlsx"
    return f"main_report_{dataset.id}_{currency}_{timestamp}.xlsx"


def _dataframe_to_xlsx_bytes(data: pd.DataFrame, sheet_name: str) -> bytes:
    output = BytesIO()
    safe_sheet_name = sheet_name[:31] or "data"
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        data.to_excel(writer, sheet_name=safe_sheet_name, index=False)
    return output.getvalue()


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("FINREP_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("FINREP_DASH_PORT", "8050"))
    debug = os.environ.get("FINREP_DASH_DEBUG", "1") == "1"
    hot_reload = os.environ.get("FINREP_DASH_HOT_RELOAD", "1") == "1"
    app.run(
        host=host,
        port=port,
        debug=debug,
        dev_tools_hot_reload=debug and hot_reload,
        dev_tools_hot_reload_interval=500,
        dev_tools_hot_reload_watch_interval=500,
    )
