import json
import logging
import os
from decimal import Decimal
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from dash import ALL, Dash, Input, MATCH, Output, State, ctx, dcc, html, no_update
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash.exceptions import PreventUpdate
from flask import request

from src import config
from src.data.get import clear_data_cache, get_transactions
from src.data.assets_editor import (
    asset_snapshot_path,
    previous_asset_snapshot_path,
    read_asset_snapshot,
    write_asset_snapshot,
)
from src.data.crypto import read_crypto_wallets, refresh_crypto_balances, refresh_crypto_price_cache
from src.data.debts import (
    DEBT_TYPES,
    active_debt_balances,
    create_debt,
    create_debt_payment_from_cash,
    migrate_legacy_debts,
)
from src.data.importers.bank_pdf import (
    BANK_PDF_UPLOAD_LIMIT_LABEL,
    MAX_BANK_PDF_REQUEST_BYTES,
    BankPdfError,
    parse_bank_upload_contents,
)
from src.data.importers.common import save_import_to_staging
from src.data.money import format_money_amount
from src.data.staging import (
    append_transaction_draft_rows,
    export_monthly_transaction_drafts,
    prepare_monthly_transaction_export,
    read_monthly_transaction_csv,
    read_transaction_drafts,
)
from src.dashboard.export import ExportBusyError, export_dashboard_page
from src.dashboard.auth import configure_auth
from src.dashboard.investment_data import build_investment_dashboard_data
from src.dashboard.i18n import (
    DEFAULT_LOCALE,
    localize_report_datasets,
    localize_export_dataframe,
    normalize_locale,
    report_column_label,
    report_text,
    tr,
)
from src.dashboard.main_data import DashboardDataset, build_main_dashboard_data, clear_main_dashboard_cache
from src.dashboard.month_data import build_month_dashboard_data, get_day_transaction_details
from src.dashboard.planning_data import build_planning_dashboard_data, save_goal_targets
from src.dashboard.report_tables import (
    _grid_column_defs,
    _grid_row_data,
    _level_palette,
    _report_cell_class,
    _report_cell_style,
    _report_row_class,
    _report_table_style_maps,
)
from src.dashboard.year_data import build_year_dashboard_data
from src.model.create_tables import clear_table_cache, get_balance_by_month
from src import utils


DEFAULT_CURRENCY = "RUB"
DEFAULT_YEAR = datetime.now().strftime("%Y")
DEFAULT_MONTH = datetime.now().strftime("%m")
DEFAULT_FX_NETWORK_ENABLED = False
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_FOLDER = PROJECT_ROOT / "assets"
logger = logging.getLogger(__name__)
DashboardTab = tuple[str, str, str]
MAIN_DASHBOARD_TABS: list[DashboardTab] = [
    ("main", "Основной отчет", "Главная"),
    ("year", "Годовой отчет", "Год"),
    ("month", "Месячный отчет", "Месяц"),
    ("planning", "План и прогноз", "План"),
    ("input", "Ввод данных", "Ввод"),
    ("debts", "Долги · Beta", "Долги β"),
    ("investments", "Инвестиции · Beta", "Инвест β"),
]
MAIN_DASHBOARD_TAB_IDS = {tab_id for tab_id, _desktop_label, _mobile_label in MAIN_DASHBOARD_TABS}
MOBILE_PRIMARY_TABS: list[DashboardTab] = [
    ("main", "Основной отчет", "Основной отчет"),
    ("year", "Годовой отчет", "Годовой отчет"),
    ("month", "Месячный отчет", "Месячный отчет"),
    ("planning", "План и прогноз", "План"),
]
MOBILE_SECONDARY_TABS: list[DashboardTab] = [
    ("input", "Ввод данных", "Ввод данных"),
    ("debts", "Долги · Beta", "Долги · Beta"),
    ("investments", "Инвестиции · Beta", "Инвестиции · Beta"),
]
MOBILE_PRIMARY_TAB_IDS = {tab_id for tab_id, _desktop_label, _mobile_label in MOBILE_PRIMARY_TABS}
MOBILE_SECONDARY_TAB_IDS = {tab_id for tab_id, _desktop_label, _mobile_label in MOBILE_SECONDARY_TABS}
MOBILE_TAB_ICONS = {
    "main": "⌂",
    "year": "Y",
    "month": "M",
    "debts": "₽",
    "planning": "↗",
    "investments": "%",
    "input": "+",
    "more": "•••",
}


def _i18n_text(key: str, *, initial_key: str | None = None, **kwargs):
    return html.Span(
        tr(initial_key or key, DEFAULT_LOCALE),
        id={"type": "i18n-text", "key": key},
        **kwargs,
    )


def _localized_text_values(component_ids: list[dict], locale: str | None, theme: str | None) -> list[str]:
    normalized_theme = theme if theme in {"light", "dark"} else "dark"
    values = []
    for component_id in component_ids:
        key = component_id["key"]
        if key == "dashboard.theme_toggle":
            key = "dashboard.theme_light" if normalized_theme == "dark" else "dashboard.theme_dark"
        values.append(tr(key, locale))
    return values


def _resolve_locale_update(triggered_id, selected_locale: str | None, stored_locale: str | None) -> str:
    if triggered_id == "dashboard-locale-select" and selected_locale in {"ru", "en"}:
        return normalize_locale(selected_locale)
    return normalize_locale(stored_locale)


def _app_index_string() -> str:
    return """
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>{%title%}</title>
        {%favicon%}
        {%css%}
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
    app.server.config["MAX_CONTENT_LENGTH"] = MAX_BANK_PDF_REQUEST_BYTES
    configure_auth(app.server)
    app.index_string = _app_index_string()
    app.server.add_url_rule("/healthz", "healthz", _healthcheck)
    app.layout = create_layout
    app.validation_layout = _callback_validation_layout(create_layout())
    register_callbacks(app)
    return app


def _healthcheck():
    return {"status": "ok"}, 200


def _default_dashboard_period() -> tuple[str, str]:
    if not config.is_test_mode():
        return DEFAULT_YEAR, DEFAULT_MONTH

    periods: list[tuple[int, int]] = []
    for csv_path in config.active_data_path("transactions_info").glob("*/*.csv"):
        parts = csv_path.stem.rstrip("_").split("_")
        if len(parts) != 2:
            continue
        try:
            year, month = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if 1 <= month <= 12:
            periods.append((year, month))
    if not periods:
        return DEFAULT_YEAR, DEFAULT_MONTH
    year, month = max(periods)
    return str(year), f"{month:02d}"


def _dashboard_tabs() -> dbc.Tabs:
    return dbc.Tabs(
        [
            dbc.Tab(
                label=desktop_label,
                tab_id=tab_id,
                id={"type": "i18n-tab-label", "key": f"nav.{tab_id}.desktop"},
            )
            for tab_id, desktop_label, _mobile_label in MAIN_DASHBOARD_TABS
        ],
        id="dashboard-tabs",
        active_tab="main",
        className="dashboard-tabs",
    )


def _mobile_bottom_nav() -> html.Nav:
    return html.Nav(
        html.Div(
            [
                html.Button(
                    [
                        html.Span(MOBILE_TAB_ICONS[tab_id], className="mobile-dashboard-tab-icon", **{"aria-hidden": "true"}),
                        _i18n_text(f"nav.{tab_id}.mobile", className="mobile-dashboard-tab-label"),
                    ],
                    id=f"mobile-tab-{tab_id}",
                    type="button",
                    className="mobile-dashboard-tab",
                    **{"aria-pressed": "true" if tab_id == "main" else "false"},
                )
                for tab_id, _desktop_label, mobile_label in MOBILE_PRIMARY_TABS
            ]
            + [
                html.Button(
                    [
                        html.Span(MOBILE_TAB_ICONS["more"], className="mobile-dashboard-tab-icon", **{"aria-hidden": "true"}),
                        _i18n_text("nav.more.mobile", className="mobile-dashboard-tab-label"),
                    ],
                    id="mobile-tab-more",
                    type="button",
                    className="mobile-dashboard-tab",
                    **{"aria-haspopup": "dialog", "aria-controls": "mobile-more-menu", "aria-pressed": "false"},
                )
            ],
            className="mobile-dashboard-tabs-control",
        ),
        id="mobile-bottom-nav",
        className="mobile-bottom-tabs",
        **{"aria-label": tr("nav.primary_label")},
    )


def _mobile_more_menu(theme: str = "dark") -> dbc.Offcanvas:
    return dbc.Offcanvas(
        [
            html.Div(
                [
                    dbc.Button(
                        [
                            html.Span(MOBILE_TAB_ICONS[tab_id], className="mobile-more-item-icon", **{"aria-hidden": "true"}),
                            _i18n_text(f"nav.{tab_id}.mobile"),
                        ],
                        id=f"mobile-more-{tab_id}",
                        color="secondary",
                        outline=True,
                        className="mobile-more-item",
                    )
                    for tab_id, _desktop_label, mobile_label in MOBILE_SECONDARY_TABS
                ],
                className="mobile-more-list",
            ),
            dbc.Button(_i18n_text("action.close.mobile_more"), id="mobile-more-close", color="secondary", className="mt-3 w-100"),
        ],
        id="mobile-more-menu",
        title=_i18n_text("nav.more_title"),
        placement="bottom",
        is_open=False,
        scrollable=False,
        backdrop=True,
        className=f"finrep-mobile-more finrep-mobile-more-{theme}",
    )


def create_layout():
    test_mode = config.is_test_mode()
    default_year, default_month = _default_dashboard_period()
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
            dcc.Store(id="dashboard-locale", data=DEFAULT_LOCALE, storage_type="local"),
            dcc.Store(id="dashboard-refresh-token", data=0),
            dcc.Store(id="transaction-save-result", storage_type="session"),
            dcc.Store(
                id="transaction-add-request-id",
                data=uuid4().hex,
                storage_type="session",
            ),
            dcc.Store(
                id="debt-create-request-id",
                data=uuid4().hex,
                storage_type="session",
            ),
            dcc.Store(
                id="debt-payment-request-id",
                data=uuid4().hex,
                storage_type="session",
            ),
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
                        html.H1(_i18n_text("dashboard.title"), className="h3 mb-0"),
                        xs=12,
                        md=3,
                    ),
                    dbc.Col(
                        html.Div(
                            [
                                html.Details(
                                    [
                                        html.Summary(
                                            [
                                                html.Span("⚙", className="dashboard-settings-icon", **{"aria-hidden": "true"}),
                                                _i18n_text("dashboard.settings"),
                                            ],
                                            className="dashboard-settings-summary",
                                        ),
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
                                                    value=default_year,
                                                    clearable=False,
                                                    className="dashboard-filter",
                                                    style={"width": "104px"},
                                                ),
                                                dcc.Dropdown(
                                                    id="dashboard-month",
                                                    options=month_options,
                                                    value=default_month,
                                                    clearable=False,
                                                    className="dashboard-filter",
                                                    style={"width": "78px"},
                                                ),
                                                dbc.Button(_i18n_text("dashboard.refresh"), id="refresh-reports", color="secondary", outline=True),
                                                dbc.Button(_i18n_text("dashboard.refresh_fx"), id="refresh-fx-rates", color="warning", outline=True, disabled=test_mode),
                                                dbc.Button(
                                                    _i18n_text("dashboard.theme_toggle", initial_key="dashboard.theme_light"),
                                                    id="theme-toggle",
                                                    color="secondary",
                                                    outline=True,
                                                ),
                                                dbc.Button("PNG", id="export-png", color="primary", outline=True, disabled=test_mode),
                                                dbc.Button("PDF", id="export-pdf", color="primary", outline=True, disabled=test_mode),
                                                html.Div(
                                                    [
                                                        dbc.RadioItems(
                                                            id="dashboard-locale-select",
                                                            options=[
                                                                {"label": "RU", "value": "ru"},
                                                                {"label": "EN", "value": "en"},
                                                            ],
                                                            value=None,
                                                            inline=True,
                                                            className="finrep-locale-options",
                                                            inputClassName="btn-check",
                                                            labelClassName="finrep-locale-option",
                                                            labelCheckedClassName="is-active",
                                                        ),
                                                    ],
                                                    id="dashboard-locale-control",
                                                    role="group",
                                                    className="finrep-locale-control",
                                                    **{"aria-label": tr("dashboard.locale_label")},
                                                ),
                                                html.Form(
                                                    dbc.Button(
                                                        _i18n_text("dashboard.logout"),
                                                        id="dashboard-logout-button",
                                                        type="submit",
                                                        color="secondary",
                                                        outline=True,
                                                    ),
                                                    id="dashboard-logout-form",
                                                    action="/logout",
                                                    method="post",
                                                ),
                                                dcc.Download(id="page-export-download"),
                                            ],
                                            className="dashboard-toolbar d-flex flex-wrap justify-content-end align-items-center gap-2",
                                        ),
                                    ],
                                    id="dashboard-settings",
                                    className="dashboard-settings",
                                    open=True,
                                ),
                                dbc.Badge(
                                    _i18n_text("dashboard.mode_test" if test_mode else "dashboard.mode_live"),
                                    id="dashboard-mode-badge",
                                    color="warning" if test_mode else "success",
                                    className="px-2 py-2",
                                ),
                            ],
                            className="dashboard-header-actions",
                        ),
                        xs=12,
                        md=9,
                        className="mt-3 mt-md-0",
                    ),
                ],
                align="center",
                className="py-3",
            ),
            dbc.Alert(
                id="page-export-message",
                children=_i18n_text("dashboard.export_live_only") if test_mode else "",
                color="warning" if test_mode else "secondary",
                is_open=test_mode,
                className="py-2 mb-3",
            ),
            _dashboard_tabs(),
            dcc.Loading(
                html.Div(id="dashboard-content", className="py-4"),
                type="circle",
            ),
            dbc.Modal(
                [
                    dbc.ModalHeader(dbc.ModalTitle(id="month-transaction-modal-title"), close_button=False),
                    dbc.ModalBody(id="month-transaction-modal-body"),
                    dbc.ModalFooter(dbc.Button(_i18n_text("action.close.month_modal"), id="month-transaction-modal-close", color="secondary")),
                ],
                id="month-transaction-modal",
                className="finrep-transaction-modal finrep-modal-dark",
                is_open=False,
                centered=True,
                scrollable=True,
                size="lg",
            ),
            _mobile_bottom_nav(),
            _mobile_more_menu(),
        ],
        id="dashboard-shell",
        className="finrep-shell finrep-theme-dark",
        fluid=True,
        style=_theme_shell_style("dark"),
    )


def register_callbacks(app: Dash) -> None:
    @app.callback(
        Output("dashboard-locale", "data"),
        Output("dashboard-locale-select", "value"),
        Input("dashboard-locale", "modified_timestamp"),
        Input("dashboard-locale-select", "value"),
        State("dashboard-locale", "data"),
    )
    def sync_dashboard_locale(_modified_timestamp, selected_locale, stored_locale):
        locale = _resolve_locale_update(ctx.triggered_id, selected_locale, stored_locale)
        stored_update = no_update if stored_locale == locale else locale
        return stored_update, locale

    @app.callback(
        Output({"type": "i18n-text", "key": ALL}, "children"),
        Output({"type": "i18n-tab-label", "key": ALL}, "label"),
        Output("mobile-bottom-nav", "aria-label"),
        Output("dashboard-locale-control", "aria-label"),
        Input("dashboard-locale", "data"),
        Input("dashboard-theme", "data"),
        State({"type": "i18n-text", "key": ALL}, "id"),
        State({"type": "i18n-tab-label", "key": ALL}, "id"),
    )
    def localize_dashboard_chrome(locale, theme, component_ids, tab_ids):
        return (
            _localized_text_values(component_ids, locale, theme),
            _localized_text_values(tab_ids, locale, theme),
            tr("nav.primary_label", locale),
            tr("dashboard.locale_label", locale),
        )

    app.clientside_callback(
        """
        function(locale) {
            const normalized = locale === "en" ? "en" : "ru";
            document.documentElement.lang = normalized;
            document.title = normalized === "en" ? "Finance" : "Финансы";
            return normalized;
        }
        """,
        Output("dashboard-shell", "lang"),
        Input("dashboard-locale", "data"),
    )

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
        clear_main_dashboard_cache()
        return int(current_token or 0) + 1

    @app.callback(
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Output("crypto-refresh-status", "data"),
        Input("crypto-refresh-button", "n_clicks", allow_optional=True),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def refresh_crypto_data(n_clicks: int | None, current_token: int | None):
        if not n_clicks:
            raise PreventUpdate
        try:
            config.require_writable_mode()
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
            clear_main_dashboard_cache()
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
        Input("planning_goals-grid", "cellValueChanged", allow_optional=True),
        State("planning_goals-grid", "rowData", allow_optional=True),
        State("dashboard-year", "value"),
        State("dashboard-currency", "value"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def save_planning_goal_cell(cell_change, row_data, year, currency, current_token):
        if not _ag_grid_changed_column(cell_change, "Цель"):
            raise PreventUpdate
        config.require_writable_mode()
        save_goal_targets(year, currency, row_data or [])
        clear_data_cache()
        clear_table_cache()
        clear_main_dashboard_cache()
        return int(current_token or 0) + 1

    @app.callback(
        Output("dashboard-theme", "data"),
        Output("dashboard-shell", "className"),
        Output("dashboard-shell", "style"),
        Output("month-transaction-modal", "className"),
        Output("mobile-more-menu", "className"),
        Input("theme-toggle", "n_clicks"),
        State("dashboard-theme", "data"),
    )
    def toggle_theme(n_clicks: int | None, current_theme: str | None):
        theme = current_theme if current_theme in {"light", "dark"} else "light"
        if n_clicks:
            theme = "dark" if theme == "light" else "light"
        shell_style = _theme_shell_style(theme)
        return (
            theme,
            f"finrep-shell finrep-theme-{theme}",
            shell_style,
            _transaction_modal_class(theme),
            f"finrep-mobile-more finrep-mobile-more-{theme}",
        )

    @app.callback(
        Output("dashboard-currency", "value"),
        Output("dashboard-year", "value"),
        Output("dashboard-month", "value"),
        Output("dashboard-tabs", "active_tab"),
        Input("dashboard-location", "search"),
    )
    def apply_url_state(search: str):
        default_year, default_month = _default_dashboard_period()
        params = parse_qs((search or "").lstrip("?"))
        currency = params.get("currency", [DEFAULT_CURRENCY])[0]
        year = params.get("year", [default_year])[0]
        month = params.get("month", [default_month])[0]
        tab = params.get("tab", ["main"])[0]
        if currency not in config.UNIQUE_TICKERS:
            currency = DEFAULT_CURRENCY
        available_years = set(utils.get_reports_years())
        if year not in available_years:
            year = default_year
        if month not in {f"{value:02d}" for value in range(1, 13)}:
            month = default_month
        if tab not in MAIN_DASHBOARD_TAB_IDS:
            tab = "main"
        return currency, year, month, tab

    @app.callback(
        Output("dashboard-tabs", "active_tab", allow_duplicate=True),
        Output("mobile-more-menu", "is_open"),
        Input("mobile-tab-main", "n_clicks"),
        Input("mobile-tab-year", "n_clicks"),
        Input("mobile-tab-month", "n_clicks"),
        Input("mobile-tab-planning", "n_clicks"),
        Input("mobile-tab-more", "n_clicks"),
        Input("mobile-more-input", "n_clicks"),
        Input("mobile-more-debts", "n_clicks"),
        Input("mobile-more-investments", "n_clicks"),
        Input("mobile-more-close", "n_clicks"),
        State("dashboard-tabs", "active_tab"),
        State("mobile-more-menu", "is_open"),
        prevent_initial_call=True,
    )
    def apply_mobile_tab(
        _main_clicks,
        _year_clicks,
        _month_clicks,
        _planning_clicks,
        _more_clicks,
        _input_clicks,
        _debts_clicks,
        _investments_clicks,
        _close_clicks,
        active_desktop_tab: str | None,
        more_is_open: bool,
    ):
        triggered_id = ctx.triggered_id
        if triggered_id == "mobile-tab-more":
            return no_update, not more_is_open
        if triggered_id == "mobile-more-close":
            return no_update, False

        target_by_button = {
            **{f"mobile-tab-{tab_id}": tab_id for tab_id in MOBILE_PRIMARY_TAB_IDS},
            **{f"mobile-more-{tab_id}": tab_id for tab_id in MOBILE_SECONDARY_TAB_IDS},
        }
        target = target_by_button.get(triggered_id)
        if not target:
            raise PreventUpdate
        return no_update if target == active_desktop_tab else target, False

    @app.callback(
        Output("mobile-tab-main", "className"),
        Output("mobile-tab-year", "className"),
        Output("mobile-tab-month", "className"),
        Output("mobile-tab-planning", "className"),
        Output("mobile-tab-more", "className"),
        Output("mobile-tab-main", "aria-pressed"),
        Output("mobile-tab-year", "aria-pressed"),
        Output("mobile-tab-month", "aria-pressed"),
        Output("mobile-tab-planning", "aria-pressed"),
        Output("mobile-tab-more", "aria-pressed"),
        Input("dashboard-tabs", "active_tab"),
    )
    def sync_mobile_tab(active_desktop_tab: str | None):
        active_button = (
            active_desktop_tab
            if active_desktop_tab in MOBILE_PRIMARY_TAB_IDS
            else "more" if active_desktop_tab in MOBILE_SECONDARY_TAB_IDS else "main"
        )
        button_ids = [tab_id for tab_id, _desktop_label, _mobile_label in MOBILE_PRIMARY_TABS] + ["more"]
        classes = [
            "mobile-dashboard-tab is-active" if tab_id == active_button else "mobile-dashboard-tab"
            for tab_id in button_ids
        ]
        pressed = ["true" if tab_id == active_button else "false" for tab_id in button_ids]
        return tuple(classes + pressed)

    @app.callback(
        Output("dashboard-content", "children"),
        Input("dashboard-currency", "value"),
        Input("dashboard-year", "value"),
        Input("dashboard-month", "value"),
        Input("dashboard-tabs", "active_tab"),
        Input("dashboard-theme", "data"),
        Input("dashboard-locale", "data"),
        Input("dashboard-refresh-token", "data"),
        Input("refresh-fx-rates", "n_clicks"),
        Input("transaction-save-result", "data"),
        State("crypto-refresh-status", "data"),
    )
    def render_dashboard_content(currency: str, year: str, month: str, active_tab: str, theme: str, locale: str, refresh_token: int, fx_refresh_clicks: int | None, transaction_save_result: dict | None, crypto_status: dict | None):
        fx_network_enabled = ctx.triggered_id == "refresh-fx-rates" and not config.is_test_mode()
        if fx_network_enabled:
            clear_table_cache()
            clear_main_dashboard_cache()

        if active_tab == "year":
            try:
                datasets = build_year_dashboard_data(
                    year,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state(str(report_text("Не удалось загрузить данные годового отчета.", locale)), exc, locale=locale)

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return _year_report_layout(datasets, theme, locale=locale)

        if active_tab == "planning":
            try:
                datasets = build_planning_dashboard_data(
                    year,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state(str(report_text("Не удалось загрузить данные плана и прогноза.", locale)), exc, locale=locale)

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return _planning_report_layout(datasets, theme, read_only=config.is_test_mode(), locale=locale)

        if active_tab == "month":
            try:
                datasets = build_month_dashboard_data(
                    year,
                    month,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state(str(report_text("Не удалось загрузить данные месячного отчета.", locale)), exc, locale=locale)

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return _month_report_layout(datasets, theme, locale=locale)

        if active_tab == "investments":
            try:
                datasets = build_investment_dashboard_data(
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return _error_state("Не удалось загрузить инвестиционный отчет.", exc)

            _apply_theme_to_datasets(datasets, theme)
            return _investment_report_layout(datasets, theme, crypto_status, read_only=config.is_test_mode())

        if active_tab == "debts":
            return _debt_report_layout(currency, theme, read_only=config.is_test_mode())

        if active_tab == "input":
            return _input_report_layout(
                currency,
                year,
                month,
                theme,
                read_only=config.is_test_mode(),
                transaction_save_result=transaction_save_result,
                locale=locale,
            )

        try:
            datasets = build_main_dashboard_data(
                currency,
                fx_network_enabled=fx_network_enabled,
                year=year,
                month=month,
            )
        except Exception as exc:
            return _error_state(str(report_text("Не удалось загрузить данные основного отчета.", locale)), exc, locale=locale)

        datasets = localize_report_datasets(datasets, locale)
        _apply_theme_to_datasets(datasets, theme)
        return _main_report_layout(
            datasets,
            theme=theme,
            currency=currency,
            year=year,
            month=month,
            locale=locale,
        )

    @app.callback(
        Output("month-transaction-modal", "is_open"),
        Output("month-transaction-modal-title", "children"),
        Output("month-transaction-modal-body", "children"),
        Input({"type": "month-transaction-day", "date": ALL}, "n_clicks"),
        Input("month-transaction-modal-close", "n_clicks"),
        State("dashboard-currency", "value"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def toggle_month_transaction_modal(day_clicks, close_clicks, currency: str, locale: str):
        if ctx.triggered_id == "month-transaction-modal-close":
            return False, "", []
        triggered_value = ctx.triggered[0].get("value") if ctx.triggered else None
        date = _clicked_transaction_date(ctx.triggered_id, triggered_value)
        if date is None:
            raise PreventUpdate

        details = get_day_transaction_details(date, currency)
        title_date = pd.to_datetime(date, errors="coerce")
        title = (
            f"{report_text('Транзакции', locale)} — {title_date.strftime('%d.%m.%Y')}"
            if not pd.isna(title_date)
            else str(report_text("Транзакции за день", locale))
        )
        return True, title, _month_transaction_modal_body(details, currency, locale=locale)

    @app.callback(
        Output({"type": "dataset-download", "dataset_id": MATCH}, "data"),
        Input({"type": "dataset-download-button", "dataset_id": MATCH}, "n_clicks"),
        State({"type": "dataset-download-button", "dataset_id": MATCH}, "id"),
        State("dashboard-currency", "value"),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("dashboard-tabs", "active_tab"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def download_dataset(
        n_clicks: int,
        button_id: dict,
        currency: str,
        year: str,
        month: str,
        active_tab: str,
        locale: str,
    ):
        if not n_clicks:
            raise PreventUpdate

        dataset_id = button_id["dataset_id"]
        datasets = _datasets_for_tab(active_tab, currency, year, month)
        if dataset_id not in datasets:
            raise PreventUpdate

        dataset = datasets[dataset_id]
        export_data = localize_export_dataframe(dataset.dataframe, locale)
        export_title = str(report_text(dataset.title, locale))
        filename = _download_filename(dataset, currency, active_tab, year, month)
        return dcc.send_bytes(_dataframe_to_xlsx_bytes(export_data, export_title), filename)

    @app.callback(
        Output("page-export-download", "data"),
        Output("page-export-message", "children"),
        Output("page-export-message", "color"),
        Output("page-export-message", "is_open"),
        Input("export-png", "n_clicks"),
        Input("export-pdf", "n_clicks"),
        State("dashboard-currency", "value"),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("dashboard-tabs", "active_tab"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def export_page(
        png_clicks: int,
        pdf_clicks: int,
        currency: str,
        year: str,
        month: str,
        active_tab: str,
        locale: str,
    ):
        if not png_clicks and not pdf_clicks:
            raise PreventUpdate

        if config.is_test_mode():
            return no_update, tr("dashboard.export_live_only", locale), "warning", True

        export_format = "png" if ctx.triggered_id == "export-png" else "pdf"
        try:
            export_path = export_dashboard_page(
                currency,
                active_tab,
                export_format,
                year=year,
                month=month if active_tab == "month" else None,
                session_cookie=request.cookies.get(app.server.config.get("SESSION_COOKIE_NAME", "session")),
                session_cookie_name=app.server.config.get("SESSION_COOKIE_NAME", "session"),
                locale=locale,
            )
        except ExportBusyError as exc:
            message = "An export is already running. Try again after it finishes." if normalize_locale(locale) == "en" else str(exc)
            return no_update, message, "warning", True
        message = "Export ready." if normalize_locale(locale) == "en" else "Экспорт готов."
        return dcc.send_file(str(export_path)), message, "success", True

    @app.callback(
        Output("kaspi-import-grid", "rowData"),
        Output("kaspi-import-grid", "columnDefs"),
        Output("kaspi-import-message", "children"),
        Output("kaspi-import-message", "color"),
        Output("transaction-import-period", "options"),
        Output("transaction-import-period", "value"),
        Input("kaspi-upload", "contents", allow_optional=True),
        State("kaspi-upload", "filename", allow_optional=True),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def preview_kaspi_pdf(contents, filename, locale):
        if not contents:
            raise PreventUpdate
        display_filename = _safe_upload_filename(filename)
        try:
            data = parse_bank_upload_contents(contents)
            internal_count = int(data["skip_reason"].eq("internal_transfer").sum()) if "skip_reason" in data else 0
            if normalize_locale(locale) == "en":
                message = (
                    f"{display_filename}: {len(data)} rows found, "
                    f"{int(data['import_action'].eq('import').sum())} to import, "
                    f"{int(data['import_action'].eq('skip').sum())} skipped, "
                    f"{int(data['import_action'].eq('review').sum())} require review, "
                    f"{internal_count} internal transfers."
                )
            else:
                message = (
                    f"{display_filename}: найдено строк {len(data)}, "
                    f"к импорту {int(data['import_action'].eq('import').sum())}, "
                    f"skip {int(data['import_action'].eq('skip').sum())}, "
                    f"требуют решения {int(data['import_action'].eq('review').sum())}, "
                    f"внутренние переводы {internal_count}."
                )
            period_options, period_value = _import_period_selection(data)
            if len(period_options) > 1:
                message += " The statement contains multiple months — select a period before Preview." if normalize_locale(locale) == "en" else " Выписка содержит несколько месяцев — выбери период перед Preview."
            return (
                _dataframe_records(data),
                _localized_input_column_defs(_kaspi_import_column_defs(), locale),
                message,
                "secondary",
                period_options,
                period_value,
            )
        except BankPdfError as exc:
            logger.warning(
                "Bank PDF upload rejected: filename=%r error=%s",
                display_filename,
                type(exc).__name__,
                exc_info=True,
            )
            return [], _localized_input_column_defs(_kaspi_import_column_defs(), locale), f"{display_filename}: {report_text(str(exc), locale)}", "danger", [], None
        except Exception:
            logger.exception("Unexpected bank PDF import failure: filename=%r", display_filename)
            return (
                [],
                _localized_input_column_defs(_kaspi_import_column_defs(), locale),
                (f"{display_filename}: import failed due to an internal error." if normalize_locale(locale) == "en" else f"{display_filename}: импорт не выполнен из-за внутренней ошибки."),
                "danger",
                [],
                None,
            )

    @app.callback(
        Output("transaction-input-message", "children"),
        Output("transaction-input-message", "color"),
        Output("transaction-input-amount", "value"),
        Output("transaction-input-comment", "value"),
        Output("transaction-add-request-id", "data"),
        Input("transaction-add-button", "n_clicks", allow_optional=True),
        State("transaction-input-date", "value", allow_optional=True),
        State("transaction-input-category", "value", allow_optional=True),
        State("transaction-input-currency", "value", allow_optional=True),
        State("transaction-input-amount", "value", allow_optional=True),
        State("transaction-input-comment", "value", allow_optional=True),
        State("transaction-add-request-id", "data"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def add_manual_transaction(
        add_clicks,
        input_date,
        input_category,
        input_currency,
        input_amount,
        input_comment,
        add_request_id,
        locale,
    ):
        if not add_clicks:
            raise PreventUpdate
        next_add_request_id = add_request_id or uuid4().hex
        try:
            config.require_writable_mode()
            if not input_date or not input_category or not input_currency or input_amount in {None, ""}:
                raise ValueError("Заполни дату, категорию, валюту и сумму.")
            result = append_transaction_draft_rows(
                pd.DataFrame(
                    [
                        {
                            "date": input_date,
                            "category": input_category,
                            "currency": input_currency,
                            "amount": input_amount,
                            "comment": input_comment or "",
                            "source": "manual",
                            "source_id": f"manual:{next_add_request_id}",
                            "status": "draft",
                        }
                    ]
                )
            )
            message = (
                "Черновик добавлен."
                if result["accepted_rows"]
                else "Черновик уже был добавлен; повтор не создан."
            )
            return report_text(message, locale), "success", None, "", uuid4().hex
        except Exception as exc:
            return report_text(str(exc), locale), "danger", no_update, no_update, next_add_request_id

    @app.callback(
        Output("transaction-export-preview-grid", "rowData"),
        Output("transaction-export-preview-grid", "columnDefs"),
        Output("transaction-export-message", "children"),
        Output("transaction-export-message", "color"),
        Output("transaction-export-preview-state", "data"),
        Output("transaction-save-result", "data"),
        Input("transaction-preview-export-button", "n_clicks", allow_optional=True),
        Input("transaction-confirm-export-button", "n_clicks", allow_optional=True),
        State("dashboard-currency", "value"),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("kaspi-import-grid", "rowData", allow_optional=True),
        State("transaction-import-period", "value", allow_optional=True),
        State("transaction-export-preview-grid", "rowData", allow_optional=True),
        State("transaction-export-preview-state", "data", allow_optional=True),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def preview_or_export_transaction_month(
        preview_clicks,
        export_clicks,
        currency,
        year,
        month,
        import_rows,
        import_period,
        preview_rows,
        preview_state,
        locale,
    ):
        trigger = ctx.triggered_id
        try:
            if trigger == "transaction-confirm-export-button":
                config.require_writable_mode()
                if not preview_rows or not preview_state:
                    raise ValueError("Сначала нажми Preview, затем подтверди экспорт.")
                year = str(preview_state.get("year", ""))
                month = str(preview_state.get("month", ""))
                result = export_monthly_transaction_drafts(
                    year,
                    month,
                    preview_rows=preview_rows,
                    preview_state=preview_state,
                )
                preview = read_monthly_transaction_csv(year, month)
                save_result = _transaction_save_result(
                    year,
                    month,
                    currency,
                    result["exported_rows"],
                    preview_state.get("import_summary"),
                )
                return (
                    _dataframe_records(preview),
                    _localized_input_column_defs(_simple_column_defs(preview), locale),
                    report_text(f"Месяц {year}-{str(month).zfill(2)} сохранён.", locale),
                    "success",
                    None,
                    save_result,
                )

            import_result = None
            if import_rows:
                periods = _import_periods(import_rows)
                if len(periods) == 1:
                    import_period = periods[0]
                if len(periods) > 1 and not import_period:
                    raise ValueError("Выписка содержит несколько месяцев: выбери период перед Preview.")
                if import_period not in periods:
                    raise ValueError("Выбранный период не соответствует строкам текущей выписки.")
                year, month = import_period.split("-", 1)
                config.require_writable_mode()
                import_result = save_import_to_staging(import_rows)

            preview, preview_state = prepare_monthly_transaction_export(year, month)
            if import_result is not None:
                preview_state["import_summary"] = _transaction_import_summary(
                    import_rows, import_result
                )
            message = str(report_text(f"Preview построен для {year}-{str(month).zfill(2)}.", locale))
            if import_result is not None:
                message += (f" Accepted from statement: {import_result['accepted_rows']}; skipped: {import_result['skipped_rows']}." if normalize_locale(locale) == "en" else f" Принято из выписки: {import_result['accepted_rows']}; пропущено: {import_result['skipped_rows']}.")
                if import_result.get("replaced_pending_rows"):
                    message += (f" Pending replaced: {import_result['replaced_pending_rows']}." if normalize_locale(locale) == "en" else f" Заменено pending: {import_result['replaced_pending_rows']}.")
            message += " The monthly CSV has not changed yet." if normalize_locale(locale) == "en" else " Месячный CSV ещё не изменён."
            return (
                _dataframe_records(preview),
                _localized_input_column_defs(_simple_column_defs(preview), locale),
                message,
                "secondary",
                preview_state,
                no_update,
            )
        except Exception as exc:
            if trigger == "transaction-confirm-export-button" and preview_rows:
                failed_preview = pd.DataFrame(preview_rows)
                return (
                    preview_rows,
                    _localized_input_column_defs(_simple_column_defs(failed_preview), locale),
                    report_text(str(exc), locale),
                    "danger",
                    preview_state,
                    no_update,
                )
            empty = pd.DataFrame()
            return [], _localized_input_column_defs(_simple_column_defs(empty), locale), report_text(str(exc), locale), "danger", preview_state, no_update

    @app.callback(
        Output("active-receivable-debts-grid", "rowData"),
        Output("active-liability-debts-grid", "rowData"),
        Output("debt-transaction-drafts-grid", "rowData"),
        Output("debt-payment-id", "options"),
        Output("debt-payment-id", "value"),
        Output("debt-input-message", "children"),
        Output("debt-input-message", "color"),
        Output("debt-create-request-id", "data"),
        Output("debt-payment-request-id", "data"),
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Input("debt-add-button", "n_clicks", allow_optional=True),
        Input("debt-payment-button", "n_clicks", allow_optional=True),
        Input("debt-migrate-button", "n_clicks", allow_optional=True),
        State("dashboard-currency", "value"),
        State("debt-opened-date", "value", allow_optional=True),
        State("debt-type", "value", allow_optional=True),
        State("debt-counterparty", "value", allow_optional=True),
        State("debt-principal-amount", "value", allow_optional=True),
        State("debt-principal-currency", "value", allow_optional=True),
        State("debt-cash-amount", "value", allow_optional=True),
        State("debt-cash-currency", "value", allow_optional=True),
        State("debt-comment", "value", allow_optional=True),
        State("debt-payment-id", "value", allow_optional=True),
        State("debt-payment-date", "value", allow_optional=True),
        State("debt-payment-amount", "value", allow_optional=True),
        State("debt-payment-cash-currency", "value", allow_optional=True),
        State("debt-payment-comment", "value", allow_optional=True),
        State("debt-create-request-id", "data"),
        State("debt-payment-request-id", "data"),
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
        create_request_id,
        payment_request_id,
        current_token,
    ):
        trigger = ctx.triggered_id
        message = ""
        color = "secondary"
        token = int(current_token or 0)
        next_create_request_id = create_request_id or uuid4().hex
        next_payment_request_id = payment_request_id or uuid4().hex

        try:
            if trigger in {"debt-add-button", "debt-payment-button", "debt-migrate-button"}:
                config.require_writable_mode()
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
                    operation_id=next_create_request_id,
                )
                next_create_request_id = uuid4().hex
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
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
                    operation_id=next_payment_request_id,
                )
                next_payment_request_id = uuid4().hex
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
                token += 1
                message = f"Погашение создано: {result['payment_id']}. Черновик транзакции добавлен во вкладку Ввод данных."
                color = "success"
            elif trigger == "debt-migrate-button":
                result = migrate_legacy_debts()
                clear_table_cache()
                clear_main_dashboard_cache()
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
            next_create_request_id,
            next_payment_request_id,
            token,
        )

    @app.callback(
        Output("assets-input-grid", "rowData"),
        Output("assets-input-message", "children"),
        Output("assets-input-message", "color"),
        Input("assets-load-button", "n_clicks", allow_optional=True),
        Input("assets-add-row-button", "n_clicks", allow_optional=True),
        Input("assets-delete-row-button", "n_clicks", allow_optional=True),
        Input("assets-apply-button", "n_clicks", allow_optional=True),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("assets-input-grid", "rowData", allow_optional=True),
        State("assets-input-grid", "selectedRows", allow_optional=True),
        State("dashboard-locale", "data"),
    )
    def sync_assets_snapshot(load_clicks, add_clicks, delete_clicks, apply_clicks, year, month, row_data, selected_rows, locale):
        trigger = ctx.triggered_id
        try:
            if trigger in {"assets-add-row-button", "assets-delete-row-button", "assets-apply-button"}:
                config.require_writable_mode()
            if trigger == "assets-add-row-button":
                rows = list(row_data or [])
                rows.append({"account": "", "amount": 0, "currency": DEFAULT_CURRENCY})
                message = "An empty row was added. Enter the account, amount, and currency, then select Apply." if normalize_locale(locale) == "en" else "Добавлена пустая строка. Заполни счет, сумму и валюту, затем нажми Применить."
                return rows, message, "secondary"

            if trigger == "assets-delete-row-button":
                rows = list(row_data or [])
                if not selected_rows:
                    raise ValueError("Выбери строки активов для удаления.")
                selected_keys = {_asset_row_key(row) for row in selected_rows}
                rows = [row for row in rows if _asset_row_key(row) not in selected_keys]
                message = (f"Rows deleted: {len(selected_rows)}. Select Apply to write the changes to CSV." if normalize_locale(locale) == "en" else f"Удалено строк: {len(selected_rows)}. Нажми Применить, чтобы записать изменения в CSV.")
                return rows, message, "warning"

            if trigger == "assets-apply-button":
                result = write_asset_snapshot(row_data or [], year, month)
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
                message = ((f"Assets saved: {result['rows']} rows. File: {result['path']}. Backup: {result['backup_path'] or 'not created'}." ) if normalize_locale(locale) == "en" else (f"Активы сохранены: {result['rows']} строк. Файл: {result['path']}. Backup: {result['backup_path'] or 'не создавался'}."))
                return _asset_input_records(year, month), message, "success"

            message, color = _asset_input_status(year, month, locale)
            return _asset_input_records(year, month), message, color
        except Exception as exc:
            return row_data or [], report_text(str(exc), locale), "danger"


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


def _main_report_layout(
    datasets: dict[str, DashboardDataset],
    theme: str,
    currency: str,
    year: str,
    month: str,
    locale: str = DEFAULT_LOCALE,
):
    if datasets["cockpit_metrics"].dataframe.empty:
        return _main_first_run_state(currency, year, month, locale=locale)

    sections = [
        _cockpit_section(datasets["cockpit_metrics"], theme=theme, locale=locale),
        _grid_section(datasets["yearly_stats"], height="300px", theme=theme, locale=locale),
        _grid_section(datasets["fx_rates"], height="260px", theme=theme, locale=locale),
        _graph_section(datasets["income_expense"], theme=theme, locale=locale),
        _graph_section(datasets["delta"], theme=theme, locale=locale),
        _graph_section(datasets["savings_rate"], theme=theme, locale=locale),
        _graph_section(datasets["capital"], height="640px", theme=theme, locale=locale),
        _graph_section(datasets["fx_revaluation"], height="420px", theme=theme, locale=locale),
        _graph_section(datasets["asset_currency_allocation"], height="520px", theme=theme, locale=locale),
        _graph_section(datasets["fx_changes"], theme=theme, locale=locale),
        _grid_section(datasets["top_purchases"], height="680px", theme=theme, locale=locale),
    ]
    metrics = datasets["cockpit_metrics"].dataframe
    if metrics.attrs.get("selected_period_available") is False:
        sections.insert(0, _main_missing_month_notice(str(metrics.attrs["selected_period"]), locale=locale))
    return html.Div(sections, className="d-grid gap-4")


def _main_missing_month_notice(period: str, locale: str = DEFAULT_LOCALE):
    return dbc.Alert(
        [
            html.Div((f"No data for {period}" if normalize_locale(locale) == "en" else f"Нет данных за {period}"), className="fw-semibold"),
            html.Div(
                report_text("Показатели выбранного месяца недоступны. История и показатели с указанной последней датой остаются видимыми.", locale),
                className="small mt-1",
            ),
        ],
        id="main-missing-month-notice",
        color="warning",
        className="mb-0",
    )


def _main_first_run_state(currency: str, year: str, month: str, locale: str = DEFAULT_LOCALE):
    input_href = "?" + urlencode(
        {"currency": currency, "year": year, "month": month, "tab": "input"}
    )
    return html.Section(
        [
            html.Div(report_text("Первый запуск", locale), className="finrep-first-run-kicker"),
            html.H2(report_text("Добавьте первые операции", locale), className="h3 mb-2"),
            html.P(
                report_text("После сохранения месяца здесь появятся баланс, динамика расходов и показатели для сверки.", locale),
                className="finrep-first-run-intro",
            ),
            html.Ol(
                [
                    html.Li(report_text("Откройте раздел «Ввод данных».", locale)),
                    html.Li(report_text("Загрузите банковскую выписку или добавьте операцию вручную.", locale)),
                    html.Li(report_text("Проверьте Preview и нажмите «Сохранить месяц».", locale)),
                ],
                className="finrep-first-run-steps",
            ),
            dcc.Link(
                dbc.Button(
                    report_text("Перейти к вводу данных", locale),
                    color="primary",
                    className="finrep-first-run-action",
                ),
                id="main-first-run-input-link",
                href=input_href,
            ),
            html.Div(
                report_text("На телефоне: Ещё → Ввод данных.", locale),
                id="main-first-run-mobile-hint",
                className="finrep-first-run-mobile-hint",
            ),
        ],
        id="main-first-run",
        className="finrep-first-run",
    )


def _cockpit_section(dataset: DashboardDataset, theme: str | None = None, locale: str = DEFAULT_LOCALE):
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    if data.empty:
        return _empty_section(dataset, locale=locale)

    rows_by_metric = {
        str(row.get("ID") or row.get("Показатель", "")): row
        for _, row in data.iterrows()
    }
    primary_metrics = [
        metric
        for metric in ("capital", "monthly_income", "monthly_expense", "monthly_cash_flow")
        if metric in rows_by_metric
    ]
    reconciliation_metrics = [
        metric for metric in ("asset_gap", "monthly_fx_revaluation") if metric in rows_by_metric
    ]
    grouped_metrics = set(primary_metrics + reconciliation_metrics)
    stability_metrics = [
        metric
        for metric in ("savings_rate", "runway")
        if metric in rows_by_metric and metric not in grouped_metrics
    ]
    grouped_metrics.update(stability_metrics)
    stability_metrics.extend(metric for metric in rows_by_metric if metric not in grouped_metrics)

    return html.Section(
        [
            _section_header(dataset),
            html.Div(
                [_cockpit_card(rows_by_metric[metric]) for metric in primary_metrics],
                id="main-metrics-primary",
                className="finrep-cockpit-grid finrep-main-metrics-primary",
            ),
            html.Div(
                [
                    _cockpit_metric_group(
                        "main-metrics-reconciliation",
                        str(report_text("Сверка", locale)),
                        reconciliation_metrics,
                        rows_by_metric,
                    ),
                    _cockpit_metric_group(
                        "main-metrics-stability",
                        str(report_text("Устойчивость", locale)),
                        stability_metrics,
                        rows_by_metric,
                    ),
                ],
                className="finrep-main-metrics-supporting",
            ),
        ],
        style=_section_style(theme),
    )


def _cockpit_metric_group(group_id: str, title: str, metrics: list[str], rows_by_metric: dict[str, pd.Series]):
    return html.Div(
        [
            html.H3(title, className="finrep-cockpit-group-title"),
            html.Div(
                [_cockpit_card(rows_by_metric[metric], compact=True) for metric in metrics],
                className="finrep-cockpit-grid finrep-cockpit-grid-compact",
            ),
        ],
        id=group_id,
        className="finrep-main-metric-group",
    )


def _cockpit_card(row, compact: bool = False):
    compact_class = " finrep-cockpit-card-compact" if compact else ""
    return html.Div(
        [
            html.Div(str(row.get("Показатель", "")), className="finrep-cockpit-label"),
            html.Div(str(row.get("Значение", "")), className="finrep-cockpit-value"),
            html.Div(str(row.get("Статус", "")), className="finrep-cockpit-status"),
            html.Div(str(row.get("Детали", "")), className="finrep-cockpit-detail"),
        ],
        className=f"finrep-cockpit-card finrep-cockpit-{_cockpit_status_class(row.get('Статус ID', row.get('Статус', '')))}{compact_class}",
    )


def _month_summary_section(dataset: DashboardDataset, theme: str | None = None, locale: str = DEFAULT_LOCALE):
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    if data.empty:
        return _empty_section(dataset, locale=locale)

    groups = [
        ("cash-flow", str(report_text("Денежный поток", locale)), ("Доход", "Расход", "Сбережения", "Дельта", "Баланс")),
        (
            "debts",
            str(report_text("Задолженности", locale)),
            (
                "Дебиторская задолженность",
                "Погашение деб. зад.",
                "Кредиторская задолженность",
                "Погашение кред. зад.",
            ),
        ),
        (
            "capital",
            str(report_text("Капитал и активы", locale)),
            ("Капитал", "Капитал по активам", "Инвестиции", "Расхождение с активами", "Валютная переоценка"),
        ),
    ]
    rows_by_metric = {
        str(dataset.dataframe.iloc[index].get("Показатель", "")): row
        for index, (_, row) in enumerate(data.iterrows())
    }
    grouped_metrics = {metric for _group_id, _title, metrics in groups for metric in metrics}
    other_metrics = [metric for metric in rows_by_metric if metric not in grouped_metrics]
    if other_metrics:
        groups.append(("other", str(report_text("Прочие показатели", locale)), tuple(other_metrics)))

    return html.Section(
        [
            _section_header(dataset),
            html.Div(
                [
                    html.Div(
                        [
                            html.H3(title, className="finrep-cockpit-group-title"),
                            html.Div(
                                [_cockpit_card(rows_by_metric[metric]) for metric in metrics if metric in rows_by_metric],
                                className="finrep-cockpit-grid",
                            ),
                        ],
                        id=f"month-summary-{group_id}",
                        className="finrep-cockpit-group",
                    )
                    for group_id, title, metrics in groups
                    if any(metric in rows_by_metric for metric in metrics)
                ],
                id="month-summary-groups",
                className="finrep-cockpit-groups",
            ),
        ],
        style=_section_style(theme),
    )


def _cockpit_status_class(status) -> str:
    status = str(status).strip().lower()
    if status in {"assets", "cash-flow", "positive", "strong", "ok"}:
        return "ok"
    if status in {"negative", "watch", "thin", "review"}:
        return "warn"
    return "neutral"


def _year_report_layout(datasets: dict[str, DashboardDataset], theme: str | None, locale: str = DEFAULT_LOCALE):
    if "year_empty" in datasets:
        year = str(datasets["year_empty"].dataframe.iloc[0]["Год"])
        return html.Section(
            [
                html.Div(report_text("Год без операций", locale), className="finrep-first-run-kicker"),
                html.H2(
                    f"No data for {year}" if normalize_locale(locale) == "en" else f"Нет данных за {year} год",
                    id="year-empty-title",
                    className="h3 mb-2",
                ),
                html.P(
                    report_text("Выберите другой год или добавьте и сохраните операции за этот период.", locale),
                    className="finrep-first-run-intro mb-0",
                ),
            ],
            id="year-empty-state",
            className="finrep-first-run",
        )

    return html.Div(
        [
            _grid_section(datasets["year_quarter_stats"], height="260px", theme=theme, locale=locale),
            _grid_section(datasets["year_fx_rates"], height="260px", theme=theme, locale=locale),
            _graph_section(datasets["year_cost_distribution_chart"], theme=theme, locale=locale),
            _grid_section(datasets["year_cost_distribution"], height="620px", theme=theme, locale=locale),
            _grid_section(datasets["year_top_purchases"], height="680px", theme=theme, locale=locale),
            dbc.Row(
                [
                    dbc.Col(_grid_section(datasets["year_income_by_month"], height="560px", theme=theme, locale=locale), xs=12, lg=3),
                    dbc.Col(_graph_section(datasets["year_income_expense"], height="560px", theme=theme, locale=locale), xs=12, lg=6),
                    dbc.Col(_grid_section(datasets["year_cost_by_month"], height="560px", theme=theme, locale=locale), xs=12, lg=3),
                ],
                className="g-4",
            ),
            _grid_section(datasets["year_income_cost_stats"], height="360px", theme=theme, locale=locale),
            _grid_section(datasets["year_capital_by_month"], height="560px", theme=theme, locale=locale),
            _graph_section(datasets["year_capital_chart"], height="560px", theme=theme, locale=locale),
            _graph_section(datasets["year_fx_revaluation"], height="420px", theme=theme, locale=locale),
        ],
        className="d-grid gap-4",
    )


def _planning_report_layout(datasets: dict[str, DashboardDataset], theme: str | None, read_only: bool = False, locale: str = DEFAULT_LOCALE):
    return html.Div(
        [
            _grid_section(datasets["planning_goals"], height="260px", theme=theme, read_only=read_only, locale=locale),
            dbc.Row(
                [
                    dbc.Col(_graph_section(datasets["planning_capital_forecast"], height="520px", theme=theme, locale=locale), xs=12, lg=8),
                    dbc.Col(_runway_section(datasets["planning_runway"], theme=theme, locale=locale), xs=12, lg=4),
                ],
                className="g-4",
            ),
            _grid_section(datasets["planning_fx_scenarios"], height="320px", theme=theme, locale=locale),
            _graph_section(datasets["planning_fx_scenarios"], height="360px", theme=theme, locale=locale),
        ],
        className="d-grid gap-4",
    )


def _runway_section(dataset: DashboardDataset, theme: str | None = None, locale: str = DEFAULT_LOCALE):
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    if data.empty:
        return _empty_section(dataset, locale=locale)

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
                    card(str(report_text("Финансовый запас по денежному потоку, месяцев", locale)), str(row.get("Runway, мес.", report_text("не рассчитано", locale)))),
                    card(str(report_text("Финансовый запас по денежному потоку, лет", locale)), str(row.get("Runway, лет", report_text("не рассчитано", locale)))),
                    card(str(report_text("Капитал по денежному потоку", locale)), str(row.get("Капитал по cash-flow", report_text("не задано", locale)))),
                    card(str(report_text("Средний расход/мес", locale)), str(row.get("Средний расход", report_text("не задано", locale)))),
                ],
                className="d-grid gap-3",
            ),
        ],
        style=_section_style(theme),
    )


def _month_report_layout(datasets: dict[str, DashboardDataset], theme: str | None, locale: str = DEFAULT_LOCALE):
    if "month_empty" in datasets:
        return _month_empty_state(datasets["month_empty"], locale=locale)

    return html.Div(
        [
            _month_summary_section(datasets["month_summary"], theme=theme, locale=locale),
            _grid_section(datasets["month_transactions"], height="1450px", theme=theme, locale=locale),
            _grid_section(datasets["month_fx_rates"], height="260px", theme=theme, locale=locale),
            _graph_section(datasets["month_cost_distribution_chart"], theme=theme, locale=locale),
            _grid_section(datasets["month_cost_distribution"], height="520px", theme=theme, locale=locale),
            _grid_section(datasets["month_assets"], height="1120px", theme=theme, locale=locale),
        ],
        className="d-grid gap-4",
    )


def _month_empty_state(dataset: DashboardDataset, locale: str = DEFAULT_LOCALE):
    row = dataset.dataframe.iloc[0]
    year = str(row["Год"])
    month = str(row["Месяц"]).zfill(2)
    currency = str(row["Валюта"])
    input_href = "?" + urlencode(
        {"currency": currency, "year": year, "month": month, "tab": "input"}
    )
    return html.Section(
        [
            html.Div(report_text("Месяц не сохранён", locale), className="finrep-first-run-kicker"),
            html.H2(f"No data for {year}-{month}" if normalize_locale(locale) == "en" else f"Нет данных за {year}-{month}", className="h3 mb-2"),
            html.P(
                report_text("Выбранный месяц ещё не создан. Добавьте или импортируйте операции, проверьте Preview и сохраните месяц.", locale),
                className="finrep-first-run-intro",
            ),
            dcc.Link(
                dbc.Button(
                    report_text("Перейти к вводу данных", locale),
                    color="primary",
                    className="finrep-first-run-action",
                ),
                id="month-empty-input-link",
                href=input_href,
            ),
        ],
        id="month-empty-state",
        className="finrep-first-run",
    )


def _fx_dense_table_section(dataset: DashboardDataset, theme: str | None, locale: str = DEFAULT_LOCALE):
    rows = _fx_display_rows(dataset)
    if not rows:
        return _empty_section(dataset, locale=locale)

    return html.Section(
        [
            _section_header(dataset),
            html.Div(_fx_dense_table(rows, locale=locale), className="finrep-table-scroll"),
        ],
        style=_section_style(theme),
    )


def _fx_display_rows(dataset: DashboardDataset) -> list[dict]:
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    return data.fillna("").to_dict("records")


def _fx_change_class(value) -> str:
    text = str(value).strip()
    if text.startswith("-"):
        return "is-negative"
    if text.startswith("+") and text not in {"+0%", "+0.0%", "+0.00%"}:
        return "is-positive"
    return "is-flat"


def _fx_dense_table(rows: list[dict], locale: str = DEFAULT_LOCALE):
    return html.Table(
        [
            html.Thead(html.Tr([html.Th(report_column_label(label, locale)) for label in ["Валюта", "Курс", "Обратный", "Изм.", "Источник"]])),
            html.Tbody(
                [
                    html.Tr(
                        [
                            html.Td(row["Валюта"], className="fx-code"),
                            html.Td(row["Курс"], className="fx-number"),
                            html.Td(row["Обратный курс"], className="fx-number"),
                            html.Td(row["Изменение (%)"], className=f"fx-change {_fx_change_class(row['Изменение (%)'])}"),
                            html.Td(row["Источник"], className="fx-source"),
                        ]
                    )
                    for row in rows
                ]
            ),
        ],
        className="finrep-fx-table fx-table-dense",
    )


def _investment_report_layout(datasets: dict[str, DashboardDataset], theme: str | None, crypto_status: dict | None = None, read_only: bool = False):
    crypto_status = crypto_status or {}
    return html.Div(
        [
            html.Section(
                [
                    html.Div(
                        [
                            html.H2("Инвестиции", className="h5 mb-0"),
                            dbc.Button("Обновить crypto", id="crypto-refresh-button", color="warning", outline=True, size="sm", disabled=read_only),
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


def _debt_report_layout(currency: str, theme: str | None, read_only: bool = False):
    return _debt_input_layout(currency, theme, include_create=True, read_only=read_only)


def _input_report_layout(
    currency: str,
    year: str,
    month: str,
    theme: str | None,
    load_asset_records: bool = True,
    read_only: bool = False,
    transaction_save_result: dict | None = None,
    locale: str = DEFAULT_LOCALE,
):
    return dbc.Tabs(
        [
            dbc.Tab(
                _transaction_input_layout(
                    currency,
                    year,
                    month,
                    theme,
                    read_only=read_only,
                    transaction_save_result=transaction_save_result,
                    locale=locale,
                ),
                label=report_text("Транзакции", locale),
                tab_id="input-transactions",
            ),
            dbc.Tab(_assets_input_layout(year, month, theme, load_records=load_asset_records, read_only=read_only, locale=locale), label=report_text("Активы", locale), tab_id="input-assets"),
        ],
        id="input-inner-tabs",
        active_tab="input-transactions",
        className="mb-3",
    )


def _callback_validation_layout(layout):
    return html.Div(
        [
            layout,
            _debt_report_layout(DEFAULT_CURRENCY, "dark"),
            _input_report_layout(DEFAULT_CURRENCY, DEFAULT_YEAR, DEFAULT_MONTH, "dark", load_asset_records=False),
            dbc.Button("Обновить crypto", id="crypto-refresh-button"),
            dag.AgGrid(id="planning_goals-grid"),
        ]
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


def _ag_grid_limited_scroll(grid, max_height: str):
    return html.Div(grid, className="finrep-grid-scroll is-limited", style={"maxHeight": max_height})


def _transaction_input_layout(
    currency: str,
    year: str,
    month: str,
    theme: str | None,
    read_only: bool = False,
    transaction_save_result: dict | None = None,
    locale: str = DEFAULT_LOCALE,
):
    category_options = _transaction_category_options()
    currency_options = [{"label": ticker, "value": ticker} for ticker in config.UNIQUE_TICKERS]
    month_value = f"{year}-{str(month).zfill(2)}"
    upload_limit_label = BANK_PDF_UPLOAD_LIMIT_LABEL
    if normalize_locale(locale) == "en":
        upload_limit_label = upload_limit_label.replace(" и ", " and ").replace(" страниц", " pages")

    return html.Div(
        [
            html.Section(
                [
                    html.H2(report_text("Ручной ввод транзакции", locale), className="h5 mb-3"),
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Дата", locale), html_for="transaction-input-date", className="small mb-1"),
                                    dbc.Input(id="transaction-input-date", type="date", value=datetime.now().date().isoformat(), className="finrep-native-input", style=_form_control_style(theme)),
                                ],
                                xs=12,
                                md=2,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Категория", locale), id="transaction-input-category-label", className="small mb-1"),
                                    dcc.Dropdown(id="transaction-input-category", options=category_options, value=category_options[0]["value"] if category_options else None, clearable=False, className="dash-dropdown"),
                                ],
                                xs=12,
                                md=2,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Валюта", locale), id="transaction-input-currency-label", className="small mb-1"),
                                    dcc.Dropdown(id="transaction-input-currency", options=currency_options, value=currency, clearable=False, className="dash-dropdown"),
                                ],
                                xs=12,
                                md=2,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Сумма", locale), html_for="transaction-input-amount", className="small mb-1"),
                                    dbc.Input(id="transaction-input-amount", type="number", step="any", className="finrep-native-input", style=_form_control_style(theme)),
                                ],
                                xs=12,
                                md=2,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Комментарий", locale), html_for="transaction-input-comment", className="small mb-1"),
                                    dbc.Input(id="transaction-input-comment", type="text", className="finrep-native-input", style=_form_control_style(theme)),
                                ],
                                xs=12,
                                md=3,
                            ),
                            dbc.Col(dbc.Button(report_text("Добавить", locale), id="transaction-add-button", color="primary", className="w-100", disabled=read_only), xs=12, md=1, className="d-flex align-items-end"),
                        ],
                        className="g-2",
                    ),
                    dbc.Alert(id="transaction-input-message", children="", color="secondary", is_open=True, className="mt-3 mb-0 py-2"),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.H2(report_text("Импорт банковского PDF", locale), className="h5 mb-3"),
                    dcc.Upload(
                        id="kaspi-upload",
                        children=html.Div(
                            [
                                html.Div(report_text("Перетащи Kaspi, BCC или Ozon PDF сюда", locale), className="fw-semibold"),
                                html.Div(report_text("или нажми для выбора файла", locale), className="small opacity-75"),
                                html.Div((f"up to {upload_limit_label}" if normalize_locale(locale) == "en" else f"до {upload_limit_label}"), className="small opacity-75"),
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
                    dbc.Alert(id="kaspi-import-message", children=report_text("Операции из PDF появятся здесь. Дубли среди черновиков и сохранённых операций будут пропущены.", locale), color="secondary", is_open=True, className="my-3 py-2"),
                    html.Div(
                        report_text("Категории: клик — одна ячейка, Shift+клик — диапазон, Ctrl/Cmd+клик — несколько; Ctrl/Cmd+C и Ctrl/Cmd+V — копировать и вставить.", locale),
                        className="small opacity-75 mb-2",
                    ),
                    html.Div(
                        dag.AgGrid(
                            id="kaspi-import-grid",
                            rowData=[],
                            columnDefs=_localized_input_column_defs(_kaspi_import_column_defs(), locale),
                            defaultColDef=_ag_grid_default_col_def(editable=False),
                            dashGridOptions={"pagination": False, "suppressFieldDotNotation": True, "stopEditingWhenCellsLoseFocus": True},
                            eventListeners={
                                "cellClicked": ["finrepCategoryCellClicked(params)"],
                                "cellKeyDown": ["finrepCategoryClipboard(params)"],
                                "rowDataUpdated": ["finrepCategorySelectionReset(params)"],
                            },
                            className=f"{_ag_grid_class_name(theme)} finrep-import-grid",
                            style=_ag_grid_style("420px"),
                        ),
                        className="finrep-import-grid-shell",
                    ),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    dcc.Store(id="transaction-export-preview-state"),
                    html.Div(
                        id="transaction-save-result-panel",
                        children=_transaction_save_result_panel(transaction_save_result, locale),
                    ),
                    html.Div(
                        [
                            html.H2(report_text("Проверка и сохранение месяца", locale), className="h5 mb-0"),
                            html.Div(
                                [
                                    dbc.Button("Preview", id="transaction-preview-export-button", color="secondary", outline=True, size="sm"),
                                    dbc.Button(report_text("Сохранить месяц", locale), id="transaction-confirm-export-button", color="primary", outline=False, size="sm", disabled=read_only),
                                ],
                                className="d-flex flex-wrap gap-2",
                            ),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Период Preview", locale), html_for="transaction-import-period", className="small mb-1"),
                                    dcc.Dropdown(
                                        id="transaction-import-period",
                                        options=[{"label": month_value, "value": month_value}],
                                        value=month_value,
                                        clearable=False,
                                        placeholder=report_text("Выбери месяц выписки", locale),
                                        className="dash-dropdown",
                                    ),
                                ],
                                xs=12,
                                md=4,
                            ),
                        ],
                        className="g-2 mb-3",
                    ),
                    dbc.Alert(id="transaction-export-message", children=report_text("Проверь импорт выше и нажми Preview. Без загруженной выписки используется выбранный период отчёта. Данные месяца изменятся только после нажатия «Сохранить месяц».", locale), color="secondary", is_open=True, className="mb-3 py-2"),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="transaction-export-preview-grid",
                            rowData=[],
                            columnDefs=[],
                            defaultColDef=_ag_grid_default_col_def(editable=not read_only),
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


def _debt_input_layout(currency: str, theme: str | None, include_create: bool = True, read_only: bool = False):
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
                            dbc.Col(dbc.Button("Добавить", id="debt-add-button", color="primary", className="w-100", disabled=read_only), xs=12, md=2),
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
                            dbc.Button("Миграция legacy", id="debt-migrate-button", color="secondary", outline=True, size="sm", disabled=read_only),
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
                            dbc.Col(dbc.Button("Погасить", id="debt-payment-button", color="primary", outline=True, className="w-100", disabled=read_only), xs=12, md=1),
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


def _assets_input_layout(year: str, month: str, theme: str | None, load_records: bool = True, read_only: bool = False, locale: str = DEFAULT_LOCALE):
    records = _asset_input_records(year, month) if load_records else []
    message, message_color = _asset_input_status(year, month, locale)
    return html.Div(
        [
            html.Section(
                [
                    html.Div(
                        [
                            html.H2(report_text("Активы", locale), className="h5 mb-0"),
                            html.Div(
                                [
                                    dbc.Button(report_text("Загрузить", locale), id="assets-load-button", color="secondary", outline=True, size="sm"),
                                    dbc.Button(report_text("Добавить строку", locale), id="assets-add-row-button", color="secondary", outline=True, size="sm", disabled=read_only),
                                    dbc.Button(report_text("Удалить выбранные", locale), id="assets-delete-row-button", color="danger", outline=True, size="sm", disabled=read_only),
                                    dbc.Button(report_text("Применить", locale), id="assets-apply-button", color="primary", outline=True, size="sm", disabled=read_only),
                                ],
                                className="d-flex flex-wrap gap-2",
                            ),
                        ],
                        className="d-flex justify-content-between align-items-center mb-3",
                    ),
                    dbc.Alert(
                        id="assets-input-message",
                        children=message,
                        color=message_color,
                        is_open=True,
                        className="mb-3 py-2",
                    ),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="assets-input-grid",
                            rowData=records,
                            columnDefs=_localized_input_column_defs(_asset_input_column_defs(), locale),
                            defaultColDef=_ag_grid_default_col_def(editable=not read_only),
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
    return [
        {
            "field": column,
            "minWidth": 120,
            "flex": 1 if column != "Дата" else 0,
            "editable": column != "Дата",
        }
        for column in data.columns
    ]


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


def _safe_upload_filename(filename: str | None) -> str:
    value = str(filename or "PDF").replace("\\", "/").rsplit("/", 1)[-1]
    return " ".join(value.split()) or "PDF"


def _transaction_category_options() -> list[dict]:
    try:
        categories = sorted(str(value) for value in get_transactions()["Категория"].dropna().unique())
    except Exception:
        categories = sorted(config.NOT_COST_COLS)
    return [{"label": category, "value": category} for category in categories]


def _import_periods(rows) -> list[str]:
    data = rows if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows or [])
    if data.empty:
        return []
    if "date" not in data.columns:
        raise ValueError("В импорте отсутствует дата операции.")
    dates = pd.to_datetime(data["date"], errors="coerce")
    if dates.isna().any():
        raise ValueError("В импорте есть строка с некорректной датой.")
    return sorted(dates.dt.strftime("%Y-%m").unique())


def _import_period_selection(rows) -> tuple[list[dict], str | None]:
    periods = _import_periods(rows)
    options = [{"label": period, "value": period} for period in periods]
    return options, periods[0] if len(periods) == 1 else None


def _transaction_import_summary(rows: list[dict], result: dict) -> dict:
    reason_labels = {
        "internal_transfer": "внутренние переводы",
        "duplicate_in_staging": "уже добавлены ранее",
        "possible_duplicate": "возможные дубли сохранённых операций",
        "possible_pending_match": "неоднозначные pending-операции",
        "manual_skip": "исключены вручную",
        "already_processed": "уже обработаны",
    }
    reason_counts: dict[str, int] = {}
    for row in rows or []:
        action = str(row.get("import_action", "")).lower()
        skip_reason = str(row.get("skip_reason", ""))
        if skip_reason == "internal_transfer":
            reason = "internal_transfer"
        elif _is_truthy(row.get("duplicate_in_staging")):
            reason = "duplicate_in_staging"
        elif action == "skip":
            reason = skip_reason if skip_reason in reason_labels else "manual_skip"
        else:
            continue
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    skipped_rows = int(result.get("skipped_rows", 0))
    unclassified = skipped_rows - sum(reason_counts.values())
    if unclassified > 0:
        reason_counts["already_processed"] = unclassified
    return {
        "accepted_rows": int(result.get("accepted_rows", 0)),
        "skipped_rows": skipped_rows,
        "skip_reasons": [
            {"label": reason_labels[reason], "count": count}
            for reason, count in reason_counts.items()
        ],
    }


def _is_truthy(value) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _transaction_save_result(
    year: str,
    month: str,
    currency: str,
    exported_rows: int,
    import_summary: dict | None,
) -> dict:
    result = {
        "data_mode": config.get_data_mode(),
        "year": str(int(year)).zfill(4),
        "month": str(int(month)).zfill(2),
        "currency": str(currency).upper(),
        "exported_rows": int(exported_rows),
        "import_summary": import_summary,
    }
    try:
        monthly = get_balance_by_month(result["currency"]).loc[
            f"{result['year']}-{result['month']}"
        ]
        row = monthly.iloc[0] if isinstance(monthly, pd.DataFrame) else monthly
        result["metrics"] = {
            name: _format_saved_month_amount(row.get(name, 0), result["currency"])
            for name in ("Доход", "Расход", "Сбережения", "Баланс")
        }
    except Exception:
        logger.exception(
            "Month was saved but result metrics could not be built: period=%s-%s currency=%s",
            result["year"],
            result["month"],
            result["currency"],
        )
        result["metrics_unavailable"] = True
    return result


def _format_saved_month_amount(value, currency: str) -> str:
    amount = Decimal(format_money_amount(value))
    return f"{amount:,.2f}".replace(",", " ") + config.UNIQUE_TICKERS[currency]


def _transaction_save_result_panel(result: dict | None, locale: str = DEFAULT_LOCALE):
    if not result or result.get("data_mode") != config.get_data_mode():
        return []
    period = f"{result['year']}-{result['month']}"
    metrics = result.get("metrics") or {}
    import_summary = result.get("import_summary") or {}
    skip_reasons = import_summary.get("skip_reasons") or []
    children = [
        html.Div((f"Month {period} saved" if normalize_locale(locale) == "en" else f"Месяц {period} сохранён"), className="fw-semibold mb-1"),
        html.Div(
            (f"Transactions saved: {int(result.get('exported_rows', 0))}." if normalize_locale(locale) == "en" else f"Проведено операций: {int(result.get('exported_rows', 0))}."),
            className="mb-2",
        ),
    ]
    if metrics:
        children.append(
            dbc.Row(
                [
                    dbc.Col(
                        html.Div(
                            [
                                html.Div(report_text(name, locale), className="small opacity-75"),
                                html.Div(value, className="fw-semibold"),
                            ],
                            className="border rounded px-3 py-2 h-100",
                        ),
                        xs=6,
                        md=3,
                    )
                    for name, value in metrics.items()
                ],
                className="g-2 mb-2",
            )
        )
    elif result.get("metrics_unavailable"):
        children.append(
            html.Div(
                report_text("Месяц сохранён, но итоговые показатели сейчас недоступны. Открой сверку, чтобы повторить расчёт.", locale),
                className="small mb-2",
            )
        )
    if import_summary:
        children.append(
            html.Div(
                ((f"Current statement: {int(import_summary.get('accepted_rows', 0))} accepted, {int(import_summary.get('skipped_rows', 0))} skipped.") if normalize_locale(locale) == "en" else (f"Текущая выписка: принято {int(import_summary.get('accepted_rows', 0))}, пропущено {int(import_summary.get('skipped_rows', 0))}.")),
                className="small",
            )
        )
    if skip_reasons:
        children.append(
            html.Div(
                ("Skip reasons: " if normalize_locale(locale) == "en" else "Причины пропуска: ")
                + "; ".join(f"{report_text(item['label'], locale)} — {item['count']}" for item in skip_reasons)
                + ".",
                className="small mb-2",
            )
        )
    children.append(
        dbc.Button(
            report_text("Перейти к сверке", locale),
            color="success",
            size="sm",
            href=(
                f"/?tab=month&year={result['year']}&month={result['month']}"
                f"&currency={result['currency']}"
            ),
        )
    )
    return dbc.Alert(children, color="success", className="mb-3")


def _asset_input_records(year: str, month: str) -> list[dict]:
    data = read_asset_snapshot(year, month).copy(deep=True)
    if data.empty:
        return []
    data["amount_sort"] = data["amount"]
    data = data.sort_values("amount_sort", ascending=False, kind="mergesort")
    data["amount_sort"] = range(len(data), 0, -1)
    data["amount"] = data["amount"].map(_format_input_amount)
    return _dataframe_records(data)


def _asset_input_status(year: str, month: str, locale: str = DEFAULT_LOCALE) -> tuple[str, str]:
    period = f"{int(year):04d}-{int(month):02d}"
    target = asset_snapshot_path(year, month)
    if target.exists():
        message = f"Saved asset snapshot for {period} loaded. File: {target}" if normalize_locale(locale) == "en" else f"Загружен сохранённый снимок активов за {period}. Файл: {target}"
        return message, "secondary"

    template = previous_asset_snapshot_path(year, month)
    if template is not None:
        template_period = template.stem.replace("_", "-")
        message = (f"An unsaved copy of the {template_period} snapshot is shown. Review the values and select Apply to create the {period} snapshot." if normalize_locale(locale) == "en" else f"Показана несохранённая копия снимка за {template_period}. Проверь значения и нажми Применить, чтобы создать снимок за {period}.")
        return message, "warning"

    message = (f"There is no asset snapshot for {period} yet. Add rows and select Apply to create it." if normalize_locale(locale) == "en" else f"Снимка активов за {period} ещё нет. Добавь строки и нажми Применить, чтобы создать его.")
    return message, "warning"


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


def _localized_input_column_defs(column_defs: list[dict], locale: str | None) -> list[dict]:
    """Translate grid headers without changing field names or editor values."""
    localized = []
    for column in column_defs:
        copy = column.copy()
        label = copy.get("headerName") or copy.get("field")
        if label:
            copy["headerName"] = report_column_label(str(label), locale)
        localized.append(copy)
    return localized


def _asset_row_key(row: dict) -> tuple[str, str, str]:
    return (str(row.get("account", "")), str(row.get("amount", "")), str(row.get("currency", "")))


def _format_input_amount(value) -> str:
    text = format_money_amount(value)
    sign = "-" if text.startswith("-") else ""
    unsigned = text.removeprefix("-")
    integer, separator, fraction = unsigned.partition(".")
    grouped_integer = f"{int(integer):,}".replace(",", " ")
    return f"{sign}{grouped_integer}{separator}{fraction}"


def _kaspi_import_column_defs() -> list[dict]:
    categories = [option["value"] for option in _transaction_category_options()]
    category_class_rules = {
        "finrep-category-selected": (
            "params.api.__finrepCategorySelection && "
            "params.api.__finrepCategorySelection.has(params.node.id)"
        ),
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
        {"field": "import_action", "headerName": "Действие", "editable": True, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": ["import", "skip"]}, "width": 120, "cellClassRules": {"text-warning": "params.value == 'review'"}},
        {"field": "currency", "headerName": "Валюта", "width": 100},
        {"field": "direction", "headerName": "Направление", "hide": True},
        {"field": "bank_status", "headerName": "Статус банка", "width": 130},
        {"field": "comment", "headerName": "Комментарий", "editable": True, "flex": 1, "minWidth": 220},
        {"field": "skip_reason", "headerName": "Причина skip", "width": 170},
        {"field": "duplicate_in_source", "headerName": "Дубль в CSV", "width": 130},
        {"field": "duplicate_in_staging", "headerName": "Дубль в staging", "width": 150},
        {"field": "details", "headerName": "Детали PDF", "flex": 1, "minWidth": 240},
        {"field": "source_id", "headerName": "ID", "hide": True},
        {"field": "source", "headerName": "Источник", "hide": True},
        {"field": "status", "headerName": "Статус", "hide": True},
        {"field": "bank_reference", "headerName": "Reference", "hide": True},
        {"field": "bank_account_id", "headerName": "Счёт банка", "hide": True},
        {"field": "replaces_source_id", "headerName": "Заменяет pending", "hide": True},
        {"field": "possible_pending_match", "headerName": "Несколько pending", "hide": True},
        {"field": "staging_revision", "headerName": "Ревизия staging", "hide": True},
    ]


def _error_state(message: str, exc: Exception, locale: str = DEFAULT_LOCALE):
    return dbc.Alert(
        [
            html.Div(message, className="fw-semibold"),
            html.Div(str(report_text(str(exc), locale)), className="small mt-1"),
        ],
        color="danger",
    )


def _graph_section(dataset: DashboardDataset, height: str = "520px", theme: str | None = None, locale: str = DEFAULT_LOCALE):
    if dataset.dataframe.empty:
        return _empty_section(dataset, locale=locale)

    graph_config = {"displaylogo": False, "responsive": True, "scrollZoom": False}
    if dataset.graph_config:
        graph_config.update(dataset.graph_config)

    return html.Section(
        [
            _section_header(dataset),
            html.Div(
                dcc.Graph(
                    id=f"{dataset.id}-graph",
                    figure=dataset.figure,
                    responsive=True,
                    className="finrep-graph",
                    style={"height": height, "width": "100%"},
                    config=graph_config,
                ),
                className="finrep-chart-scroll",
            ),
        ],
        className="finrep-chart-section",
        style=_section_style(theme),
    )


REPORT_SCROLL_TABLE_IDS = {
    "yearly_stats",
    "year_quarter_stats",
    "year_cost_distribution",
    "top_purchases",
    "year_top_purchases",
    "year_income_by_month",
    "year_cost_by_month",
    "year_income_cost_stats",
    "year_capital_by_month",
    "month_transactions",
    "month_cost_distribution",
    "month_assets",
    "planning_fx_scenarios",
}


def _localized_column_defs(
    dataset: DashboardDataset,
    data: pd.DataFrame,
    theme: str | None,
    *,
    read_only: bool,
    locale: str,
) -> list[dict]:
    column_defs = _grid_column_defs(dataset, data, theme, read_only=read_only)
    if normalize_locale(locale) != "en":
        return column_defs
    localized = []
    for column_def in column_defs:
        definition = {
            **column_def,
            "headerName": report_column_label(str(column_def["field"]), locale),
        }
        if dataset.id == "planning_goals" and column_def["field"] == "Показатель":
            labels = {
                label: report_text(label, "en")
                for label in ("Капитал", "Средний доход/мес", "Средний расход/мес")
            }
            definition["valueFormatter"] = {
                "function": f"({json.dumps(labels, ensure_ascii=False)})[params.value] || params.value"
            }
        localized.append(definition)
    return localized


def _grid_section(dataset: DashboardDataset, height: str = "360px", theme: str | None = None, read_only: bool = False, locale: str = DEFAULT_LOCALE):
    if dataset.id in {"fx_rates", "year_fx_rates", "month_fx_rates"}:
        return _fx_dense_table_section(dataset, theme, locale=locale)

    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    if data.empty:
        return _empty_section(dataset, locale=locale)

    if dataset.id == "planning_goals":
        return _limited_ag_grid_section(dataset, data, height, theme, read_only=read_only, locale=locale)

    if dataset.id in REPORT_SCROLL_TABLE_IDS:
        return _report_table_section(dataset, data, height, theme, locale=locale)

    return html.Section(
        [
            _section_header(dataset),
            _ag_grid_scroll(
                dag.AgGrid(
                    id=f"{dataset.id}-grid",
                    rowData=_grid_row_data(dataset, data),
                    columnDefs=_localized_column_defs(dataset, data, theme, read_only=read_only, locale=locale),
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


def _limited_ag_grid_section(dataset: DashboardDataset, data: pd.DataFrame, max_height: str, theme: str | None = None, read_only: bool = False, locale: str = DEFAULT_LOCALE):
    return html.Section(
        [
            _section_header(dataset),
            _ag_grid_limited_scroll(
                dag.AgGrid(
                    id=f"{dataset.id}-grid",
                    rowData=_grid_row_data(dataset, data),
                    columnDefs=_localized_column_defs(dataset, data, theme, read_only=read_only, locale=locale),
                    defaultColDef=_ag_grid_default_col_def(),
                    columnSize="sizeToFit",
                    dashGridOptions={
                        "domLayout": "autoHeight",
                        "pagination": False,
                        "suppressFieldDotNotation": True,
                    },
                    className=_ag_grid_class_name(theme),
                    style={"width": "100%"},
                ),
                max_height,
            ),
        ],
        style=_section_style(theme),
    )


def _report_table_section(dataset: DashboardDataset, data: pd.DataFrame, max_height: str, theme: str | None = None, locale: str = DEFAULT_LOCALE):
    rows = _grid_row_data(dataset, data)
    columns = list(data.columns)
    style_maps = _report_table_style_maps(dataset, data)

    return html.Section(
        [
            _section_header(dataset),
            html.Div(
                html.Table(
                    [
                        html.Thead(html.Tr([html.Th(report_column_label(column, locale)) for column in columns])),
                        html.Tbody(
                            [
                                html.Tr(
                                    [
                                        html.Td(
                                            row.get(column, ""),
                                            className=_report_cell_class(column, row),
                                            style=_report_cell_style(column, row, style_maps, theme),
                                        )
                                        for column in columns
                                    ],
                                    **_report_row_props(dataset, row, locale=locale),
                                )
                                for row in rows
                            ]
                        ),
                    ],
                    className="finrep-report-table",
                ),
                className="finrep-report-table-cap",
                style={"maxHeight": max_height},
            ),
        ],
        style=_section_style(theme),
    )


def _report_row_props(dataset: DashboardDataset, row: dict, locale: str = DEFAULT_LOCALE) -> dict:
    classes = [_report_row_class(row)]
    props = {}
    if dataset.id == "month_transactions" and row.get("Дата"):
        classes.append("finrep-report-row-clickable")
        props.update(
            {
                "id": {"type": "month-transaction-day", "date": str(row["Дата"])},
                "n_clicks": 0,
                "title": report_text("Открыть детализацию транзакций за день", locale),
                "role": "button",
                "tabIndex": 0,
            }
        )
    props["className"] = " ".join(value for value in classes if value)
    return props


def _month_transaction_modal_body(details: pd.DataFrame, currency: str, locale: str = DEFAULT_LOCALE):
    if details.empty:
        return dbc.Alert(report_text("В этот день нет ненулевых транзакций.", locale), color="secondary", className="mb-0")

    rows = []
    for _, row in details.iterrows():
        original_currency = str(row.get("Валюта", ""))
        rows.append(
            html.Tr(
                [
                    html.Td(str(row.get("Категория", ""))),
                    html.Td(_format_modal_money(row.get("Исходная сумма"), original_currency)),
                    html.Td(original_currency),
                    html.Td(_format_modal_money(row.get("В валюте отчета"), currency)),
                    html.Td(str(row.get("Комментарий") or "—")),
                ]
            )
        )

    return html.Div(
        html.Table(
            [
                html.Thead(
                    html.Tr(
                        [
                            html.Th(report_column_label("Категория", locale)),
                            html.Th(report_column_label("Исходная сумма", locale)),
                            html.Th(report_column_label("Валюта", locale)),
                            html.Th(report_column_label(f"В валюте отчета ({str(currency).upper()})", locale)),
                            html.Th(report_column_label("Комментарий", locale)),
                        ]
                    )
                ),
                html.Tbody(rows),
            ],
            className="finrep-report-table finrep-transaction-detail-table",
        ),
        className="finrep-report-table-cap",
    )


def _clicked_transaction_date(triggered_id, triggered_value) -> str | None:
    if not isinstance(triggered_id, dict) or triggered_id.get("type") != "month-transaction-day":
        return None
    click_values = triggered_value if isinstance(triggered_value, list) else [triggered_value]
    if not any(pd.to_numeric(value, errors="coerce") > 0 for value in click_values):
        return None
    date = str(triggered_id.get("date", "")).strip()
    return date or None


def _transaction_modal_class(theme: str | None) -> str:
    normalized_theme = theme if theme in {"light", "dark"} else "light"
    return f"finrep-transaction-modal finrep-modal-{normalized_theme}"


def _format_modal_money(value, currency: str) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return "—"
    return f"{number:,.2f}".replace(",", " ") + f" {str(currency).upper()}"


def _empty_section(dataset: DashboardDataset, locale: str = DEFAULT_LOCALE):
    return html.Section(
        [
            _section_header(dataset),
            dbc.Alert(report_text("Нет данных для отображения.", locale), color="warning", className="mb-0"),
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
                        className="finrep-section-action",
                    ),
                    dcc.Download(id={"type": "dataset-download", "dataset_id": dataset.id}),
                ],
                className="d-flex gap-2",
            ),
        ],
        className="finrep-section-header",
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
        year=year,
        month=month,
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
        # Dataset text is untrusted; openpyxl otherwise serializes leading "=" as a formula.
        for row in writer.sheets[safe_sheet_name].iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    return output.getvalue()


app = create_app()


if __name__ == "__main__":
    host = os.environ.get("FINREP_DASH_HOST", "127.0.0.1")
    port = int(os.environ.get("PORT") or os.environ.get("FINREP_DASH_PORT", "8050"))
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
