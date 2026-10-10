import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlencode
from uuid import uuid4

from dash import ALL, Dash, Input, MATCH, Output, State, ctx, dcc, html, no_update
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash.exceptions import PreventUpdate
from flask import request, session

from src import config
from src.data.get import clear_data_cache, get_transactions
from src.data.get_finance import FX_MAX_AGE_DAYS, fx_network_mode, get_usd_rates
from src.data.inflation import refresh_official_cpi
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
    MAX_BANK_PDF_BYTES,
    MAX_BANK_PDF_BATCH_BYTES,
    MAX_BANK_PDF_BATCH_FILES,
    MAX_BANK_PDF_REQUEST_BYTES,
    BankPdfError,
    parse_bank_upload_contents,
    validate_bank_upload_batch,
)
from src.data.importers.common import (
    DEBT_CATEGORY_ACTIONS,
    DEBT_GRID_CATEGORIES,
    INTERNAL_TRANSFER_CATEGORY,
    new_manual_grid_row,
    parse_manual_grid_rows,
    save_import_to_staging,
    save_input_grid_to_transactions,
)
from src.data.money import format_money_amount
from src.data.staging import (
    append_transaction_draft_rows,
    export_monthly_transaction_drafts,
    prepare_monthly_transaction_export,
    publish_transaction_draft_rows,
    read_monthly_transaction_csv,
    read_transaction_drafts,
    read_transaction_drafts_snapshot,
)
from src.data.sqlite_bootstrap import ensure_default_live_database
from src.data.sqlite_store import (
    confirm_debt_payment_plan, cpi_observations, create_debt_payment_plan,
    fx_rates, list_debt_payment_plans,
)
from src.dashboard.expense_data import build_expense_dashboard_data
from src.dashboard.income_data import build_income_dashboard_data
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
from src.dashboard.statistics_data import build_statistics_dashboard_data
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
UNCLASSIFIED_ASSET_TYPE_VALUE = "__unclassified__"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ASSETS_FOLDER = PROJECT_ROOT / "assets"
logger = logging.getLogger(__name__)
DashboardTab = tuple[str, str, str]
_REFERENCE_REFRESH_LOCK = Lock()
_REFERENCE_TASK_LOCK = Lock()
_REFERENCE_REFRESH_POOL = ThreadPoolExecutor(max_workers=1)
_REFERENCE_REFRESH_TASK = None


def _reference_refresh_due(rows: list[dict], now: datetime | None = None) -> bool:
    timestamps = []
    for row in rows:
        try:
            value = datetime.fromisoformat(str(row.get("fetched_at", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        timestamps.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
    current = now or datetime.now(timezone.utc)
    current = current if current.tzinfo else current.replace(tzinfo=timezone.utc)
    return not timestamps or max(timestamps) < current - timedelta(days=FX_MAX_AGE_DAYS)


def _refresh_stale_reference_data(now: datetime | None = None, *, force_fx: bool = False,
                                  include_cpi: bool = True):
    if config.is_test_mode() or not config.use_sqlite_storage():
        return no_update, no_update
    with _REFERENCE_REFRESH_LOCK:
        database = config.active_database_path()
        current = now or datetime.now(timezone.utc)
        fx_result = cpi_result = no_update
        if force_fx or _reference_refresh_due(fx_rates(database), current):
            try:
                end = pd.Timestamp(current.date())
                with fx_network_mode(True):
                    get_usd_rates(
                        list(config.UNIQUE_TICKERS),
                        end - timedelta(days=FX_MAX_AGE_DAYS), end)
                status = "error" if _reference_refresh_due(
                    fx_rates(database), current) else "done"
                fx_result = {"request": "auto", "status": status}
            except Exception:
                logger.exception("Automatic FX refresh failed; cached rates retained")
                fx_result = {"request": "auto", "status": "error"}
        if include_cpi and _reference_refresh_due(cpi_observations(database), current):
            try:
                cpi_result = refresh_official_cpi(database)
                if cpi_result.get("status") != "done":
                    logger.warning("Automatic CPI refresh incomplete; cached observations retained")
            except Exception:
                logger.exception("Automatic CPI refresh failed; cached observations retained")
                cpi_result = {
                    "status": "error",
                    "results": [{"currency": "—", "status": "error",
                                 "message": "Automatic refresh failed."}],
                }
        return fx_result, cpi_result


def _start_reference_refresh(*, force_fx: bool = False, include_cpi: bool = True) -> bool:
    if config.is_test_mode() or not config.use_sqlite_storage():
        return False
    global _REFERENCE_REFRESH_TASK
    with _REFERENCE_TASK_LOCK:
        if _REFERENCE_REFRESH_TASK is None:
            _REFERENCE_REFRESH_TASK = _REFERENCE_REFRESH_POOL.submit(
                _refresh_stale_reference_data, force_fx=force_fx, include_cpi=include_cpi)
    return True


def _take_reference_refresh_result():
    global _REFERENCE_REFRESH_TASK
    with _REFERENCE_TASK_LOCK:
        task = _REFERENCE_REFRESH_TASK
        if task is None:
            return True, {"status": "unavailable"}, no_update
        if not task.done():
            return False, no_update, no_update
        _REFERENCE_REFRESH_TASK = None
    try:
        fx_result, cpi_result = task.result()
    except Exception:
        logger.exception("Reference refresh failed")
        return True, {"status": "error"}, no_update
    return True, fx_result, cpi_result


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
    live_enabled = app.server.config["FINREP_LIVE_AUTH_ENABLED"]
    if live_enabled:
        ensure_default_live_database()
    app.index_string = _app_index_string()
    app.server.add_url_rule("/healthz", "healthz", _healthcheck)
    app.layout = create_layout
    if live_enabled:
        app.validation_layout = _callback_validation_layout(create_layout())
    else:
        with app.server.test_request_context("/"):
            session["authenticated"] = True
            session["data_mode"] = "test"
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


def _cpi_period_options(currency: str) -> list[dict]:
    if not config.use_sqlite_storage():
        return []
    try:
        from src.data.sqlite_store import cpi_observations

        periods = [
            row["period"]
            for row in cpi_observations(config.active_database_path(), currency=currency)
        ]
    except (OSError, ValueError, PermissionError):
        return []
    return [{"label": period, "value": period} for period in reversed(periods)]


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
            dcc.Store(id="dashboard-document-locale", data=DEFAULT_LOCALE),
            dcc.Store(id="dashboard-refresh-token", data=0),
            dcc.Store(id="fx-refresh-result"),
            dcc.Store(id="cpi-refresh-result"),
            dcc.Interval(id="reference-refresh-poll", interval=1_000, disabled=True),
            dcc.Store(id="cpi-base-period"),
            dcc.Store(id="transaction-save-result", storage_type="memory"),
            dcc.Store(id="bank-statement-balances", storage_type="memory"),
            dcc.Store(id="asset-registry-refresh", storage_type="memory"),
            dcc.Store(
                id="transaction-add-request-id",
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
                                                dbc.Button(_i18n_text("dashboard.refresh_cpi"), id="refresh-cpi", color="warning", outline=True, disabled=test_mode),
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
            html.Div(id="fx-refresh-status", role="status", **{"aria-live": "polite"}),
            dcc.Interval(id="fx-status-hide-timer", interval=5_000, max_intervals=1, disabled=True),
            html.Div(id="cpi-refresh-status", role="status", **{"aria-live": "polite"}),
            dcc.Interval(id="cpi-status-hide-timer", interval=5_000, max_intervals=1, disabled=True),
            _dashboard_tabs(),
            html.Div(
                dbc.Tabs([
                    dbc.Tab(label="Обзор", tab_id="overview", id={"type": "i18n-tab-label", "key": "report.main.overview"}),
                    dbc.Tab(label="Расходы", tab_id="expenses", id={"type": "i18n-tab-label", "key": "report.main.expenses"}),
                    dbc.Tab(label="Доходы", tab_id="income", id={"type": "i18n-tab-label", "key": "report.main.income"}),
                    dbc.Tab(label="Статистика", tab_id="statistics", id={"type": "i18n-tab-label", "key": "report.main.statistics"}),
                ], id="main-report-tabs", active_tab="overview"),
                id="main-report-navigation", className="mt-3",
            ),
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
    app.clientside_callback(
        """function(clicks, locale) {
            const unchanged = window.dash_clientside.no_update;
            if (!clicks) return [unchanged, unchanged, unchanged, unchanged, unchanged];
            const en = locale === "en";
            return [en ? "Checking exchange rates…" : "Обновляем курсы…",
                    "finrep-fx-status is-loading", true, true, 0];
        }""",
        Output("fx-refresh-status", "children"),
        Output("fx-refresh-status", "className"),
        Output("refresh-fx-rates", "disabled"),
        Output("fx-status-hide-timer", "disabled"),
        Output("fx-status-hide-timer", "n_intervals"),
        Input("refresh-fx-rates", "n_clicks"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """function(clicks, locale) {
            const unchanged = window.dash_clientside.no_update;
            if (!clicks) return [unchanged, unchanged, unchanged, unchanged, unchanged];
            const en = locale === "en";
            return [en ? "Loading official inflation data…" : "Загружаем официальную инфляцию…",
                    "finrep-fx-status is-loading", true, true, 0];
        }""",
        Output("cpi-refresh-status", "children"),
        Output("cpi-refresh-status", "className"),
        Output("refresh-cpi", "disabled"),
        Output("cpi-status-hide-timer", "disabled"),
        Output("cpi-status-hide-timer", "n_intervals"),
        Input("refresh-cpi", "n_clicks"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """function(result, locale) {
            if (!result) return [window.dash_clientside.no_update,
                                 window.dash_clientside.no_update,
                                 window.dash_clientside.no_update,
                                 window.dash_clientside.no_update,
                                 window.dash_clientside.no_update];
            const en = locale === "en";
            const successful = (result.results || []).filter(item => item.status === "updated").length;
            const failedItems = (result.results || []).filter(item => item.status === "error");
            const failed = failedItems.length;
            let message;
            if (en) message = `Inflation data updated: ${successful}; errors: ${failed}.`;
            else message = `Инфляция обновлена: ${successful}; ошибок: ${failed}.`;
            if (failedItems.length) {
                message += " " + failedItems.map(item => `${item.currency}: ${item.message}`).join("; ");
            }
            return [message, "finrep-fx-status is-" + result.status, false, false, 0];
        }""",
        Output("cpi-refresh-status", "children", allow_duplicate=True),
        Output("cpi-refresh-status", "className", allow_duplicate=True),
        Output("refresh-cpi", "disabled", allow_duplicate=True),
        Output("cpi-status-hide-timer", "disabled", allow_duplicate=True),
        Output("cpi-status-hide-timer", "n_intervals", allow_duplicate=True),
        Input("cpi-refresh-result", "data"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("cpi-refresh-result", "data"),
        Input("refresh-cpi", "n_clicks"),
        prevent_initial_call=True,
    )
    def refresh_cpi(clicks):
        if not clicks or config.is_test_mode():
            raise PreventUpdate
        return refresh_official_cpi(config.active_database_path())

    @app.callback(
        Output("cpi-base-period", "data"),
        Input("cpi-base-period-chart", "value"),
        prevent_initial_call=True,
    )
    def store_cpi_base_period(value):
        if not value:
            raise PreventUpdate
        return value

    app.clientside_callback(
        """function(result, locale) {
            if (!result) return [window.dash_clientside.no_update,
                                 window.dash_clientside.no_update,
                                 window.dash_clientside.no_update,
                                 window.dash_clientside.no_update,
                                 window.dash_clientside.no_update];
            const en = locale === "en";
            const messages = en ? {
                done: "Exchange-rate check completed.",
                error: "Could not refresh exchange rates. See the report error.",
                unavailable: "No exchange rates are loaded in this section."
            } : {
                done: "Проверка курсов завершена.",
                error: "Не удалось обновить курсы. Подробнее — в сообщении отчёта.",
                unavailable: "В этом разделе курсы не загружаются."
            };
            return [messages[result.status], "finrep-fx-status is-" + result.status, false, false, 0];
        }""",
        Output("fx-refresh-status", "children", allow_duplicate=True),
        Output("fx-refresh-status", "className", allow_duplicate=True),
        Output("refresh-fx-rates", "disabled", allow_duplicate=True),
        Output("fx-status-hide-timer", "disabled", allow_duplicate=True),
        Output("fx-status-hide-timer", "n_intervals", allow_duplicate=True),
        Input("fx-refresh-result", "data"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """function(n) {
            if (!n) return [window.dash_clientside.no_update,
                            window.dash_clientside.no_update,
                            window.dash_clientside.no_update];
            return ["", "", true];
        }""",
        Output("fx-refresh-status", "children", allow_duplicate=True),
        Output("fx-refresh-status", "className", allow_duplicate=True),
        Output("fx-status-hide-timer", "disabled", allow_duplicate=True),
        Input("fx-status-hide-timer", "n_intervals"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """function(n) {
            if (!n) return [window.dash_clientside.no_update,
                            window.dash_clientside.no_update,
                            window.dash_clientside.no_update];
            return ["", "", true];
        }""",
        Output("cpi-refresh-status", "children", allow_duplicate=True),
        Output("cpi-refresh-status", "className", allow_duplicate=True),
        Output("cpi-status-hide-timer", "disabled", allow_duplicate=True),
        Input("cpi-status-hide-timer", "n_intervals"),
        prevent_initial_call=True,
    )

    app.clientside_callback(
        """function(click) {
            const point = click?.points?.[0];
            return point?.customdata || "";
        }""",
        Output("expenses-top-comment", "children"),
        Input("expenses_total-graph", "clickData", allow_optional=True),
    )

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
        Output("dashboard-document-locale", "data"),
        Input("dashboard-locale", "data"),
    )

    @app.callback(
        Output("dashboard-refresh-token", "data"),
        Output("reference-refresh-poll", "disabled"),
        Input("refresh-reports", "n_clicks"),
        Input("refresh-fx-rates", "n_clicks"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def refresh_reports(n_clicks: int | None, fx_clicks: int | None,
                        current_token: int | None):
        if not n_clicks and not fx_clicks:
            raise PreventUpdate
        fx_only = ctx.triggered_id == "refresh-fx-rates"
        started = _start_reference_refresh(force_fx=fx_only, include_cpi=not fx_only)
        clear_data_cache()
        clear_table_cache()
        clear_main_dashboard_cache()
        return int(current_token or 0) + 1, not started

    @app.callback(
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Output("fx-refresh-result", "data", allow_duplicate=True),
        Output("cpi-refresh-result", "data", allow_duplicate=True),
        Output("reference-refresh-poll", "disabled", allow_duplicate=True),
        Input("reference-refresh-poll", "n_intervals"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def finish_reference_refresh(_intervals: int, current_token: int | None):
        done, fx_result, cpi_result = _take_reference_refresh_result()
        if not done:
            raise PreventUpdate
        clear_data_cache()
        clear_table_cache()
        clear_main_dashboard_cache()
        return int(current_token or 0) + 1, fx_result, cpi_result, True

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
        State("dashboard-year", "value"),
        State("dashboard-currency", "value"),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def save_planning_goal_cell(cell_change, year, currency, current_token):
        if not _ag_grid_changed_column(cell_change, "Цель"):
            raise PreventUpdate
        events = cell_change if isinstance(cell_change, list) else [cell_change]
        changed_rows = [
            event.get("data")
            for event in events
            if isinstance(event, dict)
            and (event.get("colId") or event.get("column") or event.get("field")) == "Цель"
            and isinstance(event.get("data"), dict)
        ]
        if not changed_rows:
            raise PreventUpdate
        config.require_writable_mode()
        save_goal_targets(year, currency, changed_rows)
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
        Output("main-report-tabs", "active_tab"),
        Input("dashboard-location", "search"),
    )
    def apply_url_state(search: str):
        default_year, default_month = _default_dashboard_period()
        params = parse_qs((search or "").lstrip("?"))
        currency = params.get("currency", [DEFAULT_CURRENCY])[0]
        year = params.get("year", [default_year])[0]
        month = params.get("month", [default_month])[0]
        tab = params.get("tab", ["main"])[0]
        section = params.get("section", ["overview"])[0]
        if tab == "expenses":  # Keep links from the first release working.
            tab, section = "main", "expenses"
        if section not in {"overview", "expenses", "income", "statistics"}:
            section = "overview"
        if currency not in config.UNIQUE_TICKERS:
            currency = DEFAULT_CURRENCY
        available_years = set(utils.get_reports_years())
        if year not in available_years:
            year = default_year
        if month not in {f"{value:02d}" for value in range(1, 13)}:
            month = default_month
        if tab not in MAIN_DASHBOARD_TAB_IDS:
            tab = "main"
        return currency, year, month, tab, section

    @app.callback(
        Output("main-report-navigation", "style"),
        Input("dashboard-tabs", "active_tab"),
    )
    def show_main_report_navigation(active_tab: str):
        return {} if active_tab == "main" else {"display": "none"}

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
        Output("fx-refresh-result", "data"),
        Input("dashboard-currency", "value"),
        Input("dashboard-year", "value"),
        Input("dashboard-month", "value"),
        Input("cpi-base-period", "data"),
        Input("dashboard-tabs", "active_tab"),
        Input("main-report-tabs", "active_tab"),
        Input("dashboard-theme", "data"),
        Input("dashboard-locale", "data"),
        Input("dashboard-refresh-token", "data"),
        Input("cpi-refresh-result", "data"),
        Input("transaction-save-result", "data"),
        State("crypto-refresh-status", "data"),
    )
    def render_dashboard_content(currency: str, year: str, month: str,
                                 cpi_base_period: str | None, active_tab: str,
                                 main_section: str, theme: str, locale: str,
                                 refresh_token: int,
                                 _cpi_refresh_result: dict | None,
                                 transaction_save_result: dict | None,
                                 crypto_status: dict | None):
        if ctx.triggered_id == "transaction-save-result":
            raise PreventUpdate
        fx_network_enabled = False

        def finish(content):
            return content, no_update

        if active_tab == "main" and main_section == "expenses":
            try:
                datasets = build_expense_dashboard_data(
                    currency, fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return finish(_error_state(str(report_text("Не удалось загрузить аналитику расходов.", locale)), exc, locale=locale))

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return finish(_expense_report_layout(datasets, theme, locale=locale))

        if active_tab == "main" and main_section == "income":
            try:
                datasets = build_income_dashboard_data(
                    currency, fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return finish(_error_state(str(report_text("Не удалось загрузить аналитику доходов.", locale)), exc, locale=locale))

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return finish(_income_report_layout(datasets, theme, locale=locale))

        if active_tab == "main" and main_section == "statistics":
            if not config.use_sqlite_storage():
                return finish(dbc.Alert(
                    report_text("Статистика данных доступна в режиме SQLite.", locale),
                    color="secondary",
                ))
            try:
                datasets = build_statistics_dashboard_data()
            except Exception as exc:
                return finish(_error_state(
                    str(report_text("Не удалось загрузить статистику данных.", locale)),
                    exc,
                    locale=locale,
                ))
            datasets = localize_report_datasets(datasets, locale)
            return finish(_statistics_report_layout(
                datasets["data_statistics"], theme=theme, locale=locale,
            ))

        if active_tab == "year":
            try:
                datasets = build_year_dashboard_data(
                    year,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return finish(_error_state(str(report_text("Не удалось загрузить данные годового отчета.", locale)), exc, locale=locale))

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return finish(_year_report_layout(datasets, theme, locale=locale))

        if active_tab == "planning":
            try:
                datasets = build_planning_dashboard_data(
                    year,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return finish(_error_state(str(report_text("Не удалось загрузить данные плана и прогноза.", locale)), exc, locale=locale))

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return finish(_planning_report_layout(datasets, theme, read_only=config.is_test_mode(), locale=locale))

        if active_tab == "month":
            try:
                datasets = build_month_dashboard_data(
                    year,
                    month,
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return finish(_error_state(str(report_text("Не удалось загрузить данные месячного отчета.", locale)), exc, locale=locale))

            datasets = localize_report_datasets(datasets, locale)
            _apply_theme_to_datasets(datasets, theme)
            return finish(_month_report_layout(datasets, theme, locale=locale))

        if active_tab == "investments":
            try:
                datasets = build_investment_dashboard_data(
                    currency,
                    fx_network_enabled=fx_network_enabled,
                )
            except Exception as exc:
                return finish(_error_state("Не удалось загрузить инвестиционный отчет.", exc))

            _apply_theme_to_datasets(datasets, theme)
            return finish(_investment_report_layout(datasets, theme, crypto_status, read_only=config.is_test_mode()))

        if active_tab == "debts":
            return finish(_debt_report_layout(currency, theme, read_only=config.is_test_mode()))

        if active_tab == "input":
            return finish(_input_report_layout(
                currency,
                year,
                month,
                theme,
                read_only=config.is_test_mode(),
                transaction_save_result=transaction_save_result,
                locale=locale,
            ))

        try:
            datasets = build_main_dashboard_data(
                currency,
                fx_network_enabled=fx_network_enabled,
                year=year,
                month=month,
                cpi_base_period=cpi_base_period,
            )
        except Exception as exc:
            return finish(_error_state(str(report_text("Не удалось загрузить данные основного отчета.", locale)), exc, locale=locale))

        datasets = localize_report_datasets(datasets, locale)
        _apply_theme_to_datasets(datasets, theme)
        return finish(_main_report_layout(
            datasets,
            theme=theme,
            currency=currency,
            year=year,
            month=month,
            locale=locale,
        ))

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
        State("main-report-tabs", "active_tab"),
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
        main_section: str,
        locale: str,
    ):
        if not n_clicks:
            raise PreventUpdate

        if active_tab == "main" and main_section in {"expenses", "income", "statistics"}:
            active_tab = main_section
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
        State("main-report-tabs", "active_tab"),
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
        main_section: str,
        locale: str,
    ):
        if not png_clicks and not pdf_clicks:
            raise PreventUpdate

        if config.is_test_mode():
            return no_update, tr("dashboard.export_live_only", locale), "warning", True

        if active_tab == "main" and main_section in {"expenses", "income", "statistics"}:
            active_tab = main_section
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

    app.clientside_callback(
        """function(contents, filenames, locale) {
            if (!contents) return window.dash_clientside.no_update;
            const names = Array.isArray(filenames) ? filenames : [filenames || "PDF"];
            const en = locale === "en";
            return names.map((name, index) =>
                `${name} — ${index === 0 ? (en ? "parsing" : "разбирается") :
                    (en ? "queued" : "в очереди")}`
            ).join("\\n");
        }""",
        Output("bank-upload-status", "children"),
        Input("kaspi-upload", "contents", allow_optional=True),
        State("kaspi-upload", "filename", allow_optional=True),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("kaspi-import-grid", "rowData"),
        Output("kaspi-import-grid", "columnDefs"),
        Output("kaspi-import-message", "children"),
        Output("kaspi-import-message", "color"),
        Output("transaction-import-period", "options"),
        Output("transaction-import-period", "value"),
        Output("bank-upload-status", "children", allow_duplicate=True),
        Output("bank-statement-balances", "data"),
        Input("kaspi-upload", "contents", allow_optional=True),
        State("kaspi-upload", "filename", allow_optional=True),
        State("dashboard-locale", "data"),
        State("kaspi-import-grid", "rowData", allow_optional=True),
        prevent_initial_call=True,
    )
    def preview_kaspi_pdf(contents, filename, locale, current_rows):
        if not contents:
            raise PreventUpdate
        contents_list = list(contents) if isinstance(contents, list) else [contents]
        filenames = list(filename) if isinstance(filename, list) else [filename]
        filenames.extend([None] * (len(contents_list) - len(filenames)))
        display_filenames = [
            _safe_upload_filename(value) for value in filenames[:len(contents_list)]
        ]
        english = normalize_locale(locale) == "en"
        try:
            validate_bank_upload_batch(contents_list)
        except BankPdfError as exc:
            message = report_text(str(exc), locale)
            status = [
                _bank_upload_status_row(name, locale, error=message)
                for name in display_filenames
            ]
            return (
                no_update, no_update, message, "danger", no_update, no_update,
                status, [],
            )

        frames = []
        balances = []
        statuses = []
        error_messages = []
        errors = 0
        for index, (item, display_filename) in enumerate(
            zip(contents_list, display_filenames)
        ):
            try:
                data = parse_bank_upload_contents(item).copy(deep=True)
                data["source_file"] = display_filename
                frames.append(data)
                balance = data.attrs.get("statement_balance")
                if balance:
                    balances.append({
                        **balance,
                        "source_file": display_filename,
                        "source_index": index,
                    })
                imported = int(data["import_action"].eq("import").sum())
                skipped = int(data["import_action"].eq("skip").sum())
                review = int(data["import_action"].eq("review").sum())
                statuses.append(_bank_upload_status_row(
                    display_filename, locale,
                    rows=len(data), imported=imported, skipped=skipped, review=review,
                ))
            except BankPdfError as exc:
                errors += 1
                error_message = f"{display_filename}: {report_text(str(exc), locale)}"
                error_messages.append(error_message)
                logger.warning(
                    "Bank PDF upload rejected: filename=%r error=%s",
                    display_filename,
                    type(exc).__name__,
                    exc_info=True,
                )
                statuses.append(_bank_upload_status_row(
                    display_filename, locale, error=report_text(str(exc), locale),
                ))
            except Exception:
                errors += 1
                error_message = (
                    f"{display_filename}: import failed due to an internal error."
                    if english else
                    f"{display_filename}: импорт не выполнен из-за внутренней ошибки."
                )
                error_messages.append(error_message)
                logger.exception(
                    "Unexpected bank PDF import failure: filename=%r",
                    display_filename,
                )
                statuses.append(_bank_upload_status_row(
                    display_filename, locale,
                    error=(
                        "Import failed due to an internal error." if english
                        else "Импорт не выполнен из-за внутренней ошибки."
                    ),
                ))

        status = statuses
        if not frames:
            message = error_messages[0] if len(error_messages) == 1 else (
                f"Files ready: 0; errors: {errors}."
                if english else f"Готовых файлов: 0; ошибок: {errors}."
            )
            return (
                no_update, no_update, message, "danger", no_update, no_update,
                status, [],
            )

        data = _mark_cross_file_duplicates(pd.concat(frames, ignore_index=True))
        period_options, period_value = _import_period_selection(data)
        if len(period_options) > 1 and not config.use_sqlite_storage():
            period_value = None
        message = (
            f"Files ready: {len(frames)}; errors: {errors}; rows: {len(data)}."
            if english else
            f"Готовых файлов: {len(frames)}; ошибок: {errors}; строк: {len(data)}."
        )
        if len(period_options) > 1 and not config.use_sqlite_storage():
            message += (
                " Select a period before Preview."
                if english else " Выбери период перед Preview."
            )
        return (
            _merge_input_grid_rows(current_rows, _dataframe_records(data)),
            _localized_input_column_defs(_kaspi_import_column_defs(locale), locale),
            f"{message} {'; '.join(error_messages)}" if errors else message,
            "danger" if errors else "secondary",
            period_options,
            period_value,
            status,
            balances,
        )

    @app.callback(
        Output("bank-statement-balance-panel", "style"),
        Output("bank-statement-balance-table", "children"),
        Output("bank-statement-period-message", "children"),
        Output("bank-statement-period-message", "is_open"),
        Output("bank-statement-balance-apply", "disabled"),
        Input("bank-statement-balances", "data"),
        Input("dashboard-locale", "data"),
        Input("dashboard-year", "value"),
        Input("dashboard-month", "value"),
        Input("asset-registry-refresh", "data"),
        State({"type": "bank-balance-asset", "index": ALL}, "value"),
        State({"type": "bank-balance-asset", "index": ALL}, "id"),
    )
    def show_statement_balances(balances, locale, year, month, registry_refresh,
                                selected_values, selected_ids):
        del registry_refresh
        if not balances or not config.use_sqlite_storage():
            return {"display": "none"}, "", "", False, True
        options = _active_asset_options()
        selected = {}
        if ctx.triggered_id != "bank-statement-balances":
            selected = {
                item["index"]: value
                for item, value in zip(selected_ids or [], selected_values or [])
            }
        valid_accounts = {option["value"] for option in options}
        selected_period = f"{int(year):04d}-{int(month):02d}"
        english = normalize_locale(locale) == "en"
        other_periods = sorted({
            str(balance["as_of_date"])[:7] for balance in balances
            if str(balance["as_of_date"])[:7] != selected_period
        })
        period_message = (
            f"Balances for {', '.join(other_periods)} will not be applied. "
            "Switch the dashboard month to apply them."
            if english else
            f"Остатки за {', '.join(other_periods)} не будут применены. "
            "Для них переключи месяц dashboard."
        ) if other_periods else ""
        body = []
        for index, balance in enumerate(balances):
            account = str(balance.get("account_id", ""))
            same_period = str(balance["as_of_date"])[:7] == selected_period
            body.append(html.Tr([
                html.Td(balance["source_file"], className="finrep-balance-file"),
                html.Td(f"…{account[-4:]}" if account else "—"),
                html.Td(f"{_format_input_amount(balance['balance'])} {balance['currency']}"),
                html.Td(balance["as_of_date"]),
                html.Td(dcc.Dropdown(
                    id={"type": "bank-balance-asset", "index": index},
                    options=options,
                    value=selected.get(index) if selected.get(index) in valid_accounts else None,
                    placeholder="Choose asset" if english else "Выбери актив",
                    disabled=not same_period or config.is_test_mode(),
                    className="dash-dropdown",
                ), className="finrep-balance-asset"),
                html.Td(
                    ("Current month" if english else "Текущий месяц") if same_period
                    else ("Other month" if english else "Другой месяц"),
                    className="finrep-balance-period-ok" if same_period else "finrep-balance-period-other",
                ),
            ]))
        table = html.Table([
            html.Thead(html.Tr([
                html.Th("Statement" if english else "Выписка"),
                html.Th("Bank account" if english else "Счёт банка"),
                html.Th("Balance" if english else "Остаток"),
                html.Th("Date" if english else "Дата"),
                html.Th("Asset" if english else "Актив"),
                html.Th("Period" if english else "Период"),
            ])),
            html.Tbody(body),
        ], className="finrep-balance-table")
        return (
            {"display": "block"},
            table,
            period_message,
            bool(other_periods),
            bool(config.is_test_mode() or not options or not any(
                str(balance["as_of_date"])[:7] == selected_period for balance in balances
            )),
        )

    @app.callback(
        Output("asset-registry-refresh", "data"),
        Output("bank-create-asset-message", "children"),
        Output("bank-create-asset-message", "color"),
        Output("bank-create-asset-message", "is_open"),
        Output("bank-new-asset-name", "value"),
        Output("assets-registry-account", "options"),
        Output("asset-classification-grid", "rowData", allow_duplicate=True),
        Input("bank-create-asset-button", "n_clicks"),
        State("bank-new-asset-name", "value"),
        State("bank-new-asset-type", "value"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def create_statement_asset(clicks, account_name, asset_type_id, locale):
        if not clicks:
            raise PreventUpdate
        try:
            config.require_writable_mode()
            english = normalize_locale(locale) == "en"
            if not config.use_sqlite_storage():
                raise ValueError(
                    "Account creation is only available with SQLite."
                    if english else "Создание счёта доступно только в SQLite."
                )
            name = str(account_name or "").strip()
            if not name:
                raise ValueError(
                    "Enter a name for the new asset." if english
                    else "Введи название нового актива."
                )
            if not asset_type_id:
                raise ValueError("Выбери тип актива." if not english else "Choose an asset type.")
            from src.data.sqlite_store import add_asset_account, asset_accounts

            existing = next((
                row for row in asset_accounts(config.active_database_path())
                if row["name"].casefold() == name.casefold()
            ), None)
            if existing:
                raise ValueError(
                    ("This account is already in the registry. Select it from the list."
                     if english else "Счёт уже есть в реестре. Выбери его из списка.")
                    if existing["active"] else
                    ("This account is archived. Restore it under Asset settings."
                     if english else "Счёт в архиве. Восстанови его во вкладке «Настройки активов».")
                )
            account_id = uuid4().hex
            add_asset_account(
                config.active_database_path(), account_id, name,
                asset_type_id=asset_type_id,
            )
            return (
                account_id,
                (f"Asset ‘{name}’ created, awaiting its first valuation. Select it in the balance row."
                 if english else f"Актив «{name}» создан и ожидает первой оценки. Выбери его в строке остатка."),
                "success", True, "", _active_asset_options(),
                _asset_classification_rows(locale),
            )
        except Exception as exc:
            return no_update, report_text(str(exc), locale), "danger", True, no_update, no_update, no_update

    @app.callback(
        Output("bank-statement-balance-comparison", "children"),
        Output("assets-input-grid", "rowData", allow_duplicate=True),
        Output("asset-statement-highlight-message", "children"),
        Output("asset-statement-highlight-message", "color"),
        Output("asset-statement-highlight-message", "is_open"),
        Output("bank-statement-balance-message", "children", allow_duplicate=True),
        Output("bank-statement-balance-message", "color", allow_duplicate=True),
        Output("bank-statement-balance-message", "is_open", allow_duplicate=True),
        Input({"type": "bank-balance-asset", "index": ALL}, "value"),
        Input("bank-statement-balances", "data"),
        Input("dashboard-year", "value"),
        Input("dashboard-month", "value"),
        State("dashboard-locale", "data"),
        State("assets-input-grid", "rowData", allow_optional=True),
        prevent_initial_call=True,
    )
    def compare_statement_balance(account_ids, balances, year, month, locale, current_rows):
        rows, message, color, is_open = _statement_asset_previews(
            current_rows or [],
            balances or [],
            account_ids or [],
            f"{int(year):04d}-{int(month):02d}",
            locale,
        )
        return message, rows, message, color, is_open, "", "secondary", False

    @app.callback(
        Output("bank-statement-balance-message", "children"),
        Output("bank-statement-balance-message", "color"),
        Output("bank-statement-balance-message", "is_open"),
        Output("assets-input-grid", "rowData", allow_duplicate=True),
        Output("asset-statement-highlight-message", "children", allow_duplicate=True),
        Output("asset-statement-highlight-message", "color", allow_duplicate=True),
        Output("asset-statement-highlight-message", "is_open", allow_duplicate=True),
        Output("bank-statement-balance-comparison", "children", allow_duplicate=True),
        Input("bank-statement-balance-apply", "n_clicks"),
        State("bank-statement-balances", "data"),
        State({"type": "bank-balance-asset", "index": ALL}, "value"),
        State({"type": "bank-balance-asset", "index": ALL}, "id"),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def apply_statement_balance(clicks, balances, selected_values, selected_ids,
                                year, month, locale):
        if not clicks:
            raise PreventUpdate
        try:
            config.require_writable_mode()
            if not config.use_sqlite_storage():
                raise ValueError(
                    "Statement balances can only be saved in SQLite."
                    if normalize_locale(locale) == "en"
                    else "Остаток из выписки можно сохранить только в SQLite."
                )
            selected_period = f"{int(year):04d}-{int(month):02d}"
            selections = {
                item["index"]: value
                for item, value in zip(selected_ids or [], selected_values or [])
            }
            account_ids = [selections.get(index) for index in range(len(balances or []))]
            selected = [
                (balance, account_ids[index])
                for index, balance in enumerate(balances or [])
                if str(balance["as_of_date"])[:7] == selected_period and account_ids[index]
            ]
            if not selected:
                raise ValueError(
                    f"Choose an asset for at least one balance in {selected_period}."
                    if normalize_locale(locale) == "en"
                    else f"Выбери актив хотя бы для одного остатка за {selected_period}."
                )
            targets = [(account_id, balance["currency"]) for balance, account_id in selected]
            if len(targets) != len(set(targets)):
                raise ValueError(
                    "The same asset and currency were selected for multiple balances."
                    if normalize_locale(locale) == "en" else
                    "Один актив и валюта выбраны для нескольких остатков."
                )
            from src.data.sqlite_store import upsert_asset_snapshot_batch

            results = upsert_asset_snapshot_batch(config.active_database_path(), [
                {
                    "account_id": account_id,
                    "period": selected_period,
                    "amount": balance["balance"],
                    "currency": balance["currency"],
                }
                for balance, account_id in selected
            ], reason="statement closing balance")
            clear_data_cache()
            clear_table_cache()
            clear_main_dashboard_cache()
            inserted = sum(result["action"] == "inserted" for result in results)
            updated = sum(result["action"] == "updated" for result in results)
            unchanged = sum(result["action"] == "unchanged" for result in results)
            message = (
                f"Balances for {selected_period}: created {inserted}, updated {updated}, "
                f"already matched {unchanged}."
                if normalize_locale(locale) == "en" else
                f"Остатки за {selected_period}: создано {inserted}, обновлено {updated}, "
                f"уже совпадало {unchanged}."
            )
            rows, highlight, highlight_color, highlight_open = _statement_asset_previews(
                _asset_input_records(year, month, locale),
                balances or [],
                account_ids,
                selected_period,
                locale,
            )
            return (
                message,
                "success",
                True,
                rows,
                highlight,
                highlight_color,
                highlight_open,
                highlight,
            )
        except Exception as exc:
            return (
                report_text(str(exc), locale),
                "danger",
                True,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
            )

    @app.callback(
        Output("kaspi-import-grid", "rowData", allow_duplicate=True),
        Output("kaspi-import-grid", "columnDefs", allow_duplicate=True),
        Output("kaspi-import-message", "children", allow_duplicate=True),
        Output("kaspi-import-message", "color", allow_duplicate=True),
        Input("transaction-save-import-button", "n_clicks", allow_optional=True),
        State("kaspi-import-grid", "rowData", allow_optional=True),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def save_reviewed_import(save_clicks, import_rows, locale):
        if not save_clicks:
            raise PreventUpdate
        try:
            config.require_writable_mode()
            result = save_input_grid_to_transactions(import_rows or [])
            if result["published_rows"]:
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
            remaining_rows = result["remaining_rows"]
            if normalize_locale(locale) == "en":
                message = (
                    f"Saved transactions: {result['published_rows']}; "
                    f"already saved: {result['already_published_rows']}; "
                    f"pending left in staging: {result['pending_rows']}; "
                    f"need attention: {result['invalid_rows']}."
                )
            else:
                message = (
                    f"Сохранено транзакций: {result['published_rows']}; "
                    f"уже были сохранены: {result['already_published_rows']}; "
                    f"pending осталось в staging: {result['pending_rows']}; "
                    f"требуют внимания: {result['invalid_rows']}."
                )
            return remaining_rows, _kaspi_import_column_defs(locale), message, "warning" if result["invalid_rows"] else "success"
        except Exception as exc:
            return no_update, no_update, report_text(str(exc), locale), "danger"

    @app.callback(
        Output("kaspi-import-grid", "rowData", allow_duplicate=True),
        Output("kaspi-import-grid", "selectedRows"),
        Output("kaspi-import-message", "children", allow_duplicate=True),
        Output("kaspi-import-message", "color", allow_duplicate=True),
        Output("transaction-paste-modal", "is_open"),
        Output("transaction-paste-text", "value"),
        Input("transaction-grid-add-button", "n_clicks", allow_optional=True),
        Input("transaction-grid-copy-button", "n_clicks", allow_optional=True),
        Input("transaction-grid-delete-button", "n_clicks", allow_optional=True),
        Input("transaction-grid-paste-button", "n_clicks", allow_optional=True),
        Input("transaction-paste-apply-button", "n_clicks", allow_optional=True),
        Input("transaction-paste-cancel-button", "n_clicks", allow_optional=True),
        State("kaspi-import-grid", "rowData", allow_optional=True),
        State("kaspi-import-grid", "selectedRows", allow_optional=True),
        State("transaction-paste-text", "value", allow_optional=True),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def edit_transaction_input_grid(
        add_clicks,
        copy_clicks,
        delete_clicks,
        paste_clicks,
        paste_apply_clicks,
        paste_cancel_clicks,
        rows,
        selected_rows,
        paste_text,
        locale,
    ):
        del add_clicks, copy_clicks, delete_clicks, paste_clicks, paste_apply_clicks, paste_cancel_clicks
        trigger = ctx.triggered_id
        if trigger == "transaction-grid-paste-button":
            return no_update, no_update, no_update, no_update, True, no_update
        if trigger == "transaction-paste-cancel-button":
            return no_update, no_update, no_update, no_update, False, ""
        if trigger == "transaction-grid-add-button":
            updated = [*(rows or []), new_manual_grid_row()]
            message = (
                "Empty row added. Fill in the date, signed amount, currency, and category."
                if normalize_locale(locale) == "en"
                else "Добавлена пустая строка. Заполни дату, сумму со знаком, валюту и категорию."
            )
            return updated, [], message, "secondary", False, no_update
        if trigger == "transaction-grid-copy-button":
            if not selected_rows:
                message = (
                    "Select at least one row to copy."
                    if normalize_locale(locale) == "en" else "Выбери хотя бы одну строку для копирования."
                )
                return no_update, no_update, message, "warning", False, no_update
            copies = [new_manual_grid_row(row) for row in selected_rows]
            updated = [*(rows or []), *copies]
            message = (
                f"Copies with new identities added: {len(copies)}."
                if normalize_locale(locale) == "en"
                else f"Добавлено копий с новыми идентификаторами: {len(copies)}."
            )
            return updated, [], message, "secondary", False, no_update
        if trigger == "transaction-grid-delete-button":
            if not selected_rows:
                message = (
                    "Select at least one row to delete."
                    if normalize_locale(locale) == "en" else "Выбери хотя бы одну строку для удаления."
                )
                return no_update, no_update, message, "warning", False, no_update
            selected_keys = {
                (str(row.get("source", "")), str(row.get("source_id", "")))
                for row in selected_rows
            }
            updated = [
                row for row in (rows or [])
                if (str(row.get("source", "")), str(row.get("source_id", "")))
                not in selected_keys
            ]
            removed_count = len(rows or []) - len(updated)
            if not removed_count:
                message = (
                    "The selected rows are no longer in the table."
                    if normalize_locale(locale) == "en"
                    else "Выбранных строк уже нет в таблице."
                )
                return no_update, [], message, "warning", False, no_update
            message = (
                f"Unsaved rows deleted: {removed_count}."
                if normalize_locale(locale) == "en"
                else f"Удалено несохранённых строк: {removed_count}."
            )
            return updated, [], message, "secondary", False, no_update
        if trigger == "transaction-paste-apply-button":
            try:
                pasted_rows = parse_manual_grid_rows(paste_text or "")
            except ValueError as exc:
                return no_update, no_update, report_text(str(exc), locale), "danger", True, no_update
            updated = [*(rows or []), *pasted_rows]
            message = (
                f"Added rows: {len(pasted_rows)}. Review them before saving."
                if normalize_locale(locale) == "en"
                else f"Добавлено строк: {len(pasted_rows)}. Проверь их перед сохранением."
            )
            return updated, [], message, "secondary", False, ""
        raise PreventUpdate

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
            draft_row = {
                "date": input_date,
                "category": input_category,
                "currency": input_currency,
                "amount": input_amount,
                "comment": input_comment or "",
                "source": "manual",
                "source_id": f"manual:{next_add_request_id}",
                "status": "draft",
            }
            result = append_transaction_draft_rows(pd.DataFrame([draft_row]))
            if config.use_sqlite_storage():
                published = publish_transaction_draft_rows([draft_row])
                message = (
                    "Транзакция сохранена."
                    if published["published_rows"]
                    else "Транзакция уже была сохранена."
                )
            else:
                message = (
                    "Черновик добавлен."
                    if result["accepted_rows"]
                    else "Черновик уже был добавлен; повтор не создан."
                )
            return report_text(message, locale), "success", None, "", uuid4().hex
        except Exception as exc:
            return report_text(str(exc), locale), "danger", no_update, no_update, next_add_request_id

    @app.callback(
        Output("category-create-income-class", "disabled"),
        Output("category-create-income-class", "value"),
        Input("category-create-direction", "value"),
    )
    def sync_category_income_class(direction):
        return (True, None) if direction == "expense" else (False, "active")

    @app.callback(
        Output("category-rename-name", "value"),
        Input("category-registry-grid", "selectedRows"),
        prevent_initial_call=True,
    )
    def select_category_for_rename(selected_rows):
        if not selected_rows:
            return ""
        return str(selected_rows[0].get("Категория", ""))

    @app.callback(
        Output("category-registry-message", "children"),
        Output("category-registry-message", "color"),
        Output("category-registry-grid", "rowData"),
        Output("category-registry-grid", "selectedRows"),
        Output("category-create-name", "value"),
        Output("transaction-input-category", "options"),
        Output("transaction-input-category", "value"),
        Output("kaspi-import-grid", "columnDefs", allow_duplicate=True),
        Input("category-create-button", "n_clicks"),
        Input("category-rename-button", "n_clicks"),
        Input("category-toggle-button", "n_clicks"),
        State("category-create-direction", "value"),
        State("category-create-name", "value"),
        State("category-create-income-class", "value"),
        State("category-registry-grid", "selectedRows"),
        State("category-rename-name", "value"),
        State("transaction-input-category", "value"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def manage_categories(
        create_clicks,
        rename_clicks,
        toggle_clicks,
        direction,
        create_name,
        income_class,
        selected_rows,
        rename_name,
        current_transaction_category,
        locale,
    ):
        del create_clicks, rename_clicks, toggle_clicks
        trigger = ctx.triggered_id
        if trigger not in {
            "category-create-button", "category-rename-button", "category-toggle-button"
        }:
            raise PreventUpdate
        selected = selected_rows[0] if selected_rows else None
        try:
            config.require_writable_mode()
            if not config.use_sqlite_storage():
                raise ValueError("Управление категориями доступно в режиме SQLite.")
            from src.data.sqlite_store import (
                create_category,
                rename_category,
                set_category_active,
            )

            preferred_category = current_transaction_category
            cleared_create_name = no_update
            if trigger == "category-create-button":
                create_category(
                    config.active_database_path(),
                    str(create_name or ""),
                    direction=str(direction or ""),
                    income_class=(str(income_class) if direction == "income" else None),
                )
                message = "Категория добавлена."
                cleared_create_name = ""
            elif trigger == "category-rename-button":
                if selected is None:
                    raise ValueError("Выбери категорию в таблице.")
                rename_category(
                    config.active_database_path(),
                    str(selected.get("id", "")),
                    str(rename_name or ""),
                )
                message = "Категория переименована; исторические назначения сохранены."
            else:
                if selected is None:
                    raise ValueError("Выбери категорию в таблице.")
                activate = not bool(selected.get("active"))
                set_category_active(
                    config.active_database_path(),
                    str(selected.get("id", "")),
                    activate,
                )
                message = "Категория активирована." if activate else "Категория деактивирована."

            clear_data_cache()
            options = _transaction_category_options(locale)
            option_values = {option["value"] for option in options}
            selected_value = (
                preferred_category if preferred_category in option_values
                else (options[0]["value"] if options else None)
            )
            return (
                report_text(message, locale),
                "success",
                _category_registry_rows(locale),
                [],
                cleared_create_name,
                options,
                selected_value,
                _localized_input_column_defs(_kaspi_import_column_defs(locale), locale),
            )
        except Exception as exc:
            return (
                report_text(str(exc), locale),
                "danger",
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
            )

    @app.callback(
        Output("transaction-export-preview-grid", "rowData"),
        Output("transaction-export-preview-grid", "columnDefs"),
        Output("transaction-export-message", "children"),
        Output("transaction-export-message", "color"),
        Output("transaction-export-preview-state", "data"),
        Output("transaction-save-result", "data"),
        Output("kaspi-import-grid", "rowData", allow_duplicate=True),
        Output("transaction-import-period", "options", allow_duplicate=True),
        Output("transaction-import-period", "value", allow_duplicate=True),
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
                saved_period = f"{year}-{str(month).zfill(2)}"
                remaining_import_rows = [
                    dict(row) for row in (import_rows or [])
                    if pd.to_datetime(row.get("date"), errors="coerce").strftime("%Y-%m")
                    != saved_period
                ]
                _, staging_revision = read_transaction_drafts_snapshot()
                for row in remaining_import_rows:
                    row["staging_revision"] = staging_revision
                period_options, period_value = _import_period_selection(remaining_import_rows)
                return (
                    _dataframe_records(preview),
                    _localized_input_column_defs(_simple_column_defs(preview), locale),
                    report_text(f"Месяц {year}-{str(month).zfill(2)} сохранён.", locale),
                    "success",
                    None,
                    save_result,
                    remaining_import_rows,
                    period_options,
                    period_value,
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
                selected_import_rows = [
                    row for row in import_rows
                    if pd.to_datetime(row.get("date"), errors="coerce").strftime("%Y-%m")
                    == import_period
                ]
                import_result = save_import_to_staging(selected_import_rows)

            preview, preview_state = prepare_monthly_transaction_export(year, month)
            if import_result is not None:
                preview_state["import_summary"] = _transaction_import_summary(
                    selected_import_rows, import_result
                )
            message = str(report_text(f"Preview построен для {year}-{str(month).zfill(2)}.", locale))
            if import_result is not None:
                message += (f" Accepted from statement: {import_result['accepted_rows']}; skipped: {import_result['skipped_rows']}." if normalize_locale(locale) == "en" else f" Принято из выписки: {import_result['accepted_rows']}; пропущено: {import_result['skipped_rows']}.")
                if import_result.get("replaced_pending_rows"):
                    message += (f" Pending replaced: {import_result['replaced_pending_rows']}." if normalize_locale(locale) == "en" else f" Заменено pending: {import_result['replaced_pending_rows']}.")
            if config.use_sqlite_storage():
                message += (" The database has not changed yet."
                            if normalize_locale(locale) == "en"
                            else " База данных ещё не изменена.")
            else:
                message += (" The monthly CSV has not changed yet."
                            if normalize_locale(locale) == "en"
                            else " Месячный CSV ещё не изменён.")
            refreshed_import_rows = no_update
            period_options = no_update
            period_value = no_update
            if import_rows:
                _, staging_revision = read_transaction_drafts_snapshot()
                refreshed_import_rows = [dict(row) for row in import_rows]
                for row in refreshed_import_rows:
                    row["staging_revision"] = staging_revision
                period_options, _automatic_period = _import_period_selection(refreshed_import_rows)
                period_value = import_period
            return (
                _dataframe_records(preview),
                _localized_input_column_defs(_simple_column_defs(preview), locale),
                message,
                "secondary",
                preview_state,
                no_update,
                refreshed_import_rows,
                period_options,
                period_value,
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
                    no_update,
                    no_update,
                    no_update,
                )
            empty = pd.DataFrame()
            return (
                [],
                _localized_input_column_defs(_simple_column_defs(empty), locale),
                report_text(str(exc), locale),
                "danger",
                preview_state,
                no_update,
                no_update,
                no_update,
                no_update,
            )

    @app.callback(
        Output("transaction-save-result-panel", "children"),
        Input("transaction-save-result", "data"),
        State("dashboard-locale", "data"),
        prevent_initial_call=True,
    )
    def render_transaction_save_result(result, locale):
        return _transaction_save_result_panel(result, locale)

    @app.callback(
        Output("debt-new-grid", "rowData", allow_duplicate=True),
        Output("debt-new-grid", "selectedRows"),
        Input("debt-grid-add-button", "n_clicks", allow_optional=True),
        Input("debt-grid-copy-button", "n_clicks", allow_optional=True),
        Input("debt-grid-delete-button", "n_clicks", allow_optional=True),
        State("debt-new-grid", "rowData", allow_optional=True),
        State("debt-new-grid", "selectedRows", allow_optional=True),
        State("dashboard-currency", "value"),
        prevent_initial_call=True,
    )
    def edit_new_debts(_add, _copy, _delete, rows, selected, currency):
        rows = list(rows or [])
        selected = selected or []
        if ctx.triggered_id == "debt-grid-add-button":
            rows.append(_new_debt_grid_row(currency))
        elif ctx.triggered_id == "debt-grid-copy-button":
            rows.extend(_new_debt_grid_row(currency, row) for row in selected)
        elif ctx.triggered_id == "debt-grid-delete-button":
            selected_ids = {row.get("operation_id") for row in selected}
            rows = [row for row in rows if row.get("operation_id") not in selected_ids]
        else:
            raise PreventUpdate
        return rows, []

    @app.callback(
        Output("debt-payment-grid", "rowData", allow_duplicate=True),
        Output("debt-payment-grid", "selectedRows"),
        Input("debt-payment-add-row", "n_clicks", allow_optional=True),
        Input("debt-payment-copy-row", "n_clicks", allow_optional=True),
        Input("debt-payment-delete-row", "n_clicks", allow_optional=True),
        State("debt-payment-grid", "rowData", allow_optional=True),
        State("debt-payment-grid", "selectedRows", allow_optional=True),
        State("dashboard-currency", "value"),
        prevent_initial_call=True,
    )
    def edit_debt_payments(_add, _copy, _delete, rows, selected, currency):
        rows = list(rows or [])
        selected = selected or []
        if ctx.triggered_id == "debt-payment-add-row":
            rows.append(_new_debt_payment_row(currency))
        elif ctx.triggered_id == "debt-payment-copy-row":
            rows.extend(_new_debt_payment_row(currency, row) for row in selected)
        elif ctx.triggered_id == "debt-payment-delete-row":
            selected_ids = {row.get("operation_id") for row in selected}
            rows = [row for row in rows if row.get("operation_id") not in selected_ids]
        else:
            raise PreventUpdate
        return rows, []

    @app.callback(
        Output("debt-plan-new-grid", "rowData", allow_duplicate=True),
        Output("debt-plan-new-grid", "selectedRows"),
        Input("debt-plan-add-row", "n_clicks", allow_optional=True),
        Input("debt-plan-copy-row", "n_clicks", allow_optional=True),
        Input("debt-plan-delete-row", "n_clicks", allow_optional=True),
        State("debt-plan-new-grid", "rowData", allow_optional=True),
        State("debt-plan-new-grid", "selectedRows", allow_optional=True),
        State("dashboard-currency", "value"),
        prevent_initial_call=True,
    )
    def edit_debt_plans(_add, _copy, _delete, rows, selected, currency):
        rows = list(rows or [])
        selected = selected or []
        if ctx.triggered_id == "debt-plan-add-row":
            rows.append(_new_debt_plan_row(currency))
        elif ctx.triggered_id == "debt-plan-copy-row":
            rows.extend(_new_debt_plan_row(currency, row) for row in selected)
        elif ctx.triggered_id == "debt-plan-delete-row":
            selected_ids = {row.get("operation_id") for row in selected}
            rows = [row for row in rows if row.get("operation_id") not in selected_ids]
        else:
            raise PreventUpdate
        return rows, []

    @app.callback(
        Output("active-receivable-debts-grid", "rowData"),
        Output("active-liability-debts-grid", "rowData"),
        Output("debt-transaction-drafts-grid", "rowData"),
        Output("debt-payment-grid", "columnDefs"),
        Output("debt-plan-new-grid", "columnDefs"),
        Output("debt-input-message", "children"),
        Output("debt-input-message", "color"),
        Output("debt-new-grid", "rowData", allow_duplicate=True),
        Output("debt-payment-grid", "rowData", allow_duplicate=True),
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Input("debt-add-button", "n_clicks", allow_optional=True),
        Input("debt-payment-button", "n_clicks", allow_optional=True),
        Input("debt-migrate-button", "n_clicks", allow_optional=True),
        State("dashboard-currency", "value"),
        State("debt-new-grid", "rowData", allow_optional=True),
        State("debt-payment-grid", "rowData", allow_optional=True),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def sync_debts(add_clicks, payment_clicks, migrate_clicks, currency,
                   new_rows, payment_rows, current_token):
        trigger = ctx.triggered_id
        if not {"debt-add-button": add_clicks, "debt-payment-button": payment_clicks,
                "debt-migrate-button": migrate_clicks}.get(trigger):
            raise PreventUpdate
        message = ""
        color = "secondary"
        token = int(current_token or 0)
        remaining_rows = no_update
        remaining_payments = no_update

        try:
            if trigger in {"debt-add-button", "debt-payment-button", "debt-migrate-button"}:
                config.require_writable_mode()
            if trigger == "debt-add-button":
                if not new_rows:
                    raise ValueError("Добавь хотя бы одну строку долга.")
                remaining_rows = []
                saved_count = 0
                for source_row in new_rows:
                    row = dict(source_row)
                    try:
                        debt_type = {"Мне должны": "receivable", "Я должен": "liability"}.get(row.get("type"), row.get("type"))
                        if not row.get("opened_date") or not row.get("counterparty") or not row.get("principal_currency") or row.get("principal_amount") in {None, ""}:
                            raise ValueError("Заполни дату, тип, контрагента, сумму и валюту долга.")
                        create_debt(
                            debt_type=debt_type, counterparty=row["counterparty"],
                            opened_date=row["opened_date"],
                            principal_amount=row["principal_amount"],
                            principal_currency=row["principal_currency"],
                            comment=row.get("comment", ""),
                            operation_id=row["operation_id"],
                        )
                        saved_count += 1
                    except Exception as exc:
                        row["validation_error"] = str(exc)
                        remaining_rows.append(row)
                if saved_count:
                    clear_data_cache()
                    clear_table_cache()
                    clear_main_dashboard_cache()
                message = f"Сохранено долгов: {saved_count}; требуют внимания: {len(remaining_rows)}."
                color = "warning" if remaining_rows else "success"
            elif trigger == "debt-payment-button":
                if not payment_rows:
                    raise ValueError("Добавь хотя бы одну строку погашения.")
                remaining_payments = []
                saved_count = 0
                for source_row in payment_rows:
                    row = dict(source_row)
                    try:
                        if not row.get("date") or row.get("amount") in {None, ""} or not row.get("cash_currency"):
                            raise ValueError("Заполни дату, сумму и валюту платежа.")
                        create_debt_payment_from_cash(
                            debt_id=_debt_id_from_entry(row.get("debt", "")),
                            date=row["date"], cash_amount=row["amount"],
                            cash_currency=row["cash_currency"], comment=row.get("comment", ""),
                            operation_id=row["operation_id"],
                        )
                        saved_count += 1
                    except Exception as exc:
                        row["validation_error"] = str(exc)
                        remaining_payments.append(row)
                if saved_count:
                    clear_data_cache()
                    clear_table_cache()
                    clear_main_dashboard_cache()
                    token += 1
                message = f"Сохранено погашений: {saved_count}; требуют внимания: {len(remaining_payments)}."
                color = "warning" if remaining_payments else "success"
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

        return (
            _active_debt_records(currency, "receivable"),
            _active_debt_records(currency, "liability"),
            _debt_transaction_draft_records(),
            _debt_payment_column_defs(currency),
            _debt_plan_input_column_defs(currency),
            message,
            color,
            remaining_rows,
            remaining_payments,
            no_update if trigger == "debt-add-button" else token,
        )

    @app.callback(
        Output("debt-plan-message", "children"),
        Output("debt-plan-message", "color"),
        Output("debt-plan-new-grid", "rowData", allow_duplicate=True),
        Output("debt-plans-grid", "rowData", allow_duplicate=True),
        Output("dashboard-refresh-token", "data", allow_duplicate=True),
        Input("debt-plan-add-button", "n_clicks", allow_optional=True),
        Input("debt-plan-confirm-button", "n_clicks", allow_optional=True),
        State("debt-plan-new-grid", "rowData", allow_optional=True),
        State("debt-plans-grid", "rowData", allow_optional=True),
        State("debt-plans-grid", "selectedRows", allow_optional=True),
        State("dashboard-refresh-token", "data"),
        prevent_initial_call=True,
    )
    def save_debt_plan(_add_clicks, _confirm_clicks, new_rows, plan_rows,
                       selected, refresh_token):
        if not {"debt-plan-add-button": _add_clicks,
                "debt-plan-confirm-button": _confirm_clicks}.get(ctx.triggered_id):
            raise PreventUpdate
        try:
            config.require_writable_mode()
            if not config.use_sqlite_storage():
                raise ValueError("Планы платежей доступны в режиме SQLite.")
            if ctx.triggered_id == "debt-plan-add-button":
                if not new_rows:
                    raise ValueError("Добавь хотя бы одну строку плана.")
                remaining = []
                saved_count = 0
                for source_row in new_rows:
                    row = dict(source_row)
                    try:
                        if not row.get("due_on") or row.get("amount") in {None, ""}:
                            raise ValueError("Заполни дату и сумму планового платежа.")
                        create_debt_payment_plan(
                            config.active_database_path(),
                            debt_id=_debt_id_from_entry(row.get("debt", "")),
                            due_on=row["due_on"], amount=row["amount"],
                            comment=row.get("comment", ""), operation_key=row["operation_id"],
                        )
                        saved_count += 1
                    except Exception as exc:
                        row["validation_error"] = str(exc)
                        remaining.append(row)
                message = f"Сохранено планов: {saved_count}; требуют внимания: {len(remaining)}. Остаток долга не изменился."
                return (
                    message, "warning" if remaining else "success", remaining, no_update,
                    int(refresh_token or 0) + 1 if saved_count else no_update,
                )
            elif ctx.triggered_id == "debt-plan-confirm-button":
                selected_id = (selected or [{}])[0].get("id")
                plan_rows = [dict(row) for row in plan_rows or []]
                plan = next((row for row in plan_rows if row.get("id") == selected_id), None)
                if not plan:
                    raise ValueError("Выбери план в таблице.")
                if plan.get("confirmed_payment_id"):
                    raise ValueError("Этот план уже подтверждён.")
                if not plan.get("actual_date"):
                    raise ValueError("Укажи дату факта в выбранной строке.")
                try:
                    result = confirm_debt_payment_plan(
                        config.active_database_path(), plan_id=selected_id,
                        occurred_on=plan["actual_date"],
                    )
                except Exception as exc:
                    plan["validation_error"] = str(exc)
                    return str(exc), "danger", no_update, plan_rows, no_update
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
                message = f"Платёж подтверждён: {result['payment_id']}. Создан денежный черновик."
                return message, "success", no_update, no_update, int(refresh_token or 0) + 1
            else:
                raise PreventUpdate
        except PreventUpdate:
            raise
        except Exception as exc:
            return str(exc), "danger", no_update, no_update, no_update

    @app.callback(
        Output("debt-plans-grid", "rowData"),
        Input("dashboard-refresh-token", "data"),
        State("debt-plans-grid", "rowData", allow_optional=True),
        prevent_initial_call=True,
    )
    def refresh_debt_plans(_token, previous_rows):
        previous = {row["id"]: row for row in previous_rows or []}
        rows = _debt_plan_records()
        for row in rows:
            if not row["confirmed_payment_id"] and row["id"] in previous:
                row["actual_date"] = previous[row["id"]].get("actual_date", row["actual_date"])
                row["validation_error"] = previous[row["id"]].get("validation_error", "")
        return rows

    @app.callback(
        Output("assets-input-grid", "rowData"),
        Output("assets-input-message", "children"),
        Output("assets-input-message", "color"),
        Output("asset-classification-grid", "rowData", allow_duplicate=True),
        Input("assets-reset-confirm", "submit_n_clicks", allow_optional=True),
        Input("assets-add-row-button", "n_clicks", allow_optional=True),
        Input("assets-copy-previous-button", "n_clicks", allow_optional=True),
        Input("assets-add-from-registry-button", "n_clicks", allow_optional=True),
        Input("assets-delete-row-button", "n_clicks", allow_optional=True),
        Input("assets-apply-button", "n_clicks", allow_optional=True),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("assets-input-grid", "rowData", allow_optional=True),
        State("assets-input-grid", "selectedRows", allow_optional=True),
        State("assets-registry-account", "value", allow_optional=True),
        State("assets-registry-currency", "value", allow_optional=True),
        State("dashboard-locale", "data"),
        State("bank-statement-balances", "data"),
        State({"type": "bank-balance-asset", "index": ALL}, "value"),
        prevent_initial_call=True,
    )
    def sync_assets_snapshot(
        load_clicks,
        add_clicks,
        copy_previous_clicks,
        add_from_registry_clicks,
        delete_clicks,
        apply_clicks,
        year,
        month,
        row_data,
        selected_rows,
        registry_account_id,
        registry_currency,
        locale,
        statement_balances,
        statement_account_ids,
    ):
        trigger = ctx.triggered_id
        period = f"{int(year):04d}-{int(month):02d}"

        def with_statement_preview(rows):
            return _statement_asset_previews(
                rows,
                statement_balances or [],
                statement_account_ids or [],
                period,
                locale,
            )[0]

        try:
            if trigger in {"assets-add-row-button", "assets-copy-previous-button", "assets-add-from-registry-button", "assets-delete-row-button", "assets-apply-button"}:
                config.require_writable_mode()
            if trigger == "assets-add-row-button":
                rows = list(row_data or [])
                rows.append({"account": "", "asset_type_id": "", "amount": 0, "currency": DEFAULT_CURRENCY})
                message = "An empty row was added. Enter its name, type, amount and currency, then select Apply." if normalize_locale(locale) == "en" else "Добавлена пустая строка. Заполни название, тип, сумму и валюту, затем нажми «Применить»."
                return with_statement_preview(rows), message, "secondary", no_update

            if trigger == "assets-copy-previous-button":
                previous = pd.Period(period, freq="M") - 1
                previous_period = str(previous)
                source_rows = (
                    _asset_input_records(str(previous.year), f"{previous.month:02d}", locale)
                    if asset_snapshot_path(str(previous.year), f"{previous.month:02d}").exists()
                    else []
                )
                if not source_rows:
                    message = (
                        f"No saved assets found for {previous_period}."
                        if normalize_locale(locale) == "en" else
                        f"За {previous_period} нет сохранённых активов для добавления."
                    )
                    return row_data or [], message, "warning", no_update
                rows = list(row_data or [])
                existing_ids = {
                    (row.get("account_id"), str(row.get("currency", "")).upper())
                    for row in rows if row.get("account_id")
                }
                existing_names = {
                    (str(row.get("account", "")).casefold(), str(row.get("currency", "")).upper())
                    for row in rows
                }
                added = 0
                for source in source_rows:
                    currency_code = source["currency"].upper()
                    if ((source.get("account_id"), currency_code) in existing_ids
                            or (source["account"].casefold(), currency_code) in existing_names):
                        continue
                    rows.append({key: value for key, value in source.items() if key != "amount_sort"})
                    existing_ids.add((source.get("account_id"), currency_code))
                    existing_names.add((source["account"].casefold(), currency_code))
                    added += 1
                message = (
                    f"Added {added} assets from {previous_period}. Review values and select Apply."
                    if normalize_locale(locale) == "en" else
                    f"Добавлено активов из {previous_period}: {added}. Проверь суммы и нажми «Применить»."
                )
                return with_statement_preview(rows), message, "secondary", no_update

            if trigger == "assets-add-from-registry-button":
                english = normalize_locale(locale) == "en"
                if not config.use_sqlite_storage():
                    raise ValueError(
                        "Adding from the registry is only available with SQLite."
                        if english else "Добавление из реестра доступно только в SQLite."
                    )
                from src.data.sqlite_store import asset_accounts

                account = next((
                    row for row in asset_accounts(config.active_database_path())
                    if row["id"] == registry_account_id and row["active"]
                ), None)
                if account is None or registry_currency not in config.UNIQUE_TICKERS:
                    raise ValueError(
                        "Choose a registry asset and currency." if english
                        else "Выбери актив из реестра и валюту."
                    )
                rows = list(row_data or [])
                if any(
                    row.get("currency") == registry_currency
                    and (row.get("account_id") == registry_account_id
                         or str(row.get("account", "")).casefold() == account["name"].casefold())
                    for row in rows
                ):
                    raise ValueError(
                        "This asset and currency are already in the monthly table."
                        if english else "Этот актив и валюта уже есть в месячной таблице."
                    )
                rows.append({
                    "account_id": registry_account_id,
                    "account": account["name"],
                    "asset_type_id": account["asset_type_id"] or UNCLASSIFIED_ASSET_TYPE_VALUE,
                    "asset_type": (
                        account["asset_type_name_en"] if normalize_locale(locale) == "en"
                        else account["asset_type_name_ru"]
                    ) or report_text("Не классифицировано", locale),
                    "amount": "",
                    "currency": registry_currency,
                })
                message = (
                    "Asset added as a draft. Enter its balance and select Apply."
                    if normalize_locale(locale) == "en" else
                    "Актив добавлен как черновик. Введи остаток и нажми «Применить»."
                )
                return with_statement_preview(rows), message, "secondary", no_update

            if trigger == "assets-delete-row-button":
                if not selected_rows:
                    raise ValueError("Выбери счета активов для архивации.")
                if not config.use_sqlite_storage():
                    raise ValueError("Архивация активов доступна в режиме SQLite.")
                from src.data.sqlite_store import archive_asset_accounts

                result = archive_asset_accounts(
                    config.active_database_path(),
                    [row.get("account", "") for row in selected_rows],
                    period=f"{int(year):04d}-{int(month):02d}",
                )
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
                message = (
                    f"Archived accounts: {result['archived']}. They remain in {result['period']} history and are excluded from later snapshots."
                    if normalize_locale(locale) == "en"
                    else f"Отправлено в архив счетов: {result['archived']}. Они остаются в истории за {result['period']} и исключаются из следующих снимков."
                )
                return (
                    with_statement_preview(_asset_input_records(year, month, locale)),
                    message,
                    "success",
                    _asset_classification_rows(locale),
                )

            if trigger == "assets-apply-button":
                result = write_asset_snapshot(row_data or [], year, month)
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
                message = ((f"Assets saved: {result['rows']} rows. File: {result['path']}. Backup: {result['backup_path'] or 'not created'}." ) if normalize_locale(locale) == "en" else (f"Активы сохранены: {result['rows']} строк. Файл: {result['path']}. Backup: {result['backup_path'] or 'не создавался'}."))
                return with_statement_preview(_asset_input_records(year, month, locale)), message, "success", no_update

            message, color = _asset_input_status(year, month, locale)
            return with_statement_preview(_asset_input_records(year, month, locale)), message, color, no_update
        except Exception as exc:
            return row_data or [], report_text(str(exc), locale), "danger", no_update

    @app.callback(
        Output("asset-classification-message", "children"),
        Output("asset-classification-message", "color"),
        Output("asset-classification-grid", "rowData"),
        Output("assets-input-grid", "rowData", allow_duplicate=True),
        Input("asset-classification-save-button", "n_clicks", allow_optional=True),
        Input("asset-account-restore-button", "n_clicks", allow_optional=True),
        State("asset-classification-grid", "rowData", allow_optional=True),
        State("asset-classification-grid", "selectedRows", allow_optional=True),
        State("dashboard-year", "value"),
        State("dashboard-month", "value"),
        State("dashboard-locale", "data"),
        State("bank-statement-balances", "data"),
        State({"type": "bank-balance-asset", "index": ALL}, "value"),
        prevent_initial_call=True,
    )
    def save_asset_classification(save_clicks, restore_clicks, rows, selected_rows,
                                  year, month, locale, statement_balances,
                                  statement_account_ids):
        trigger = ctx.triggered_id
        if trigger not in {
            "asset-classification-save-button", "asset-account-restore-button"
        }:
            raise PreventUpdate
        try:
            config.require_writable_mode()
            if not config.use_sqlite_storage():
                raise ValueError("Классификация активов доступна в режиме SQLite.")
            if trigger == "asset-account-restore-button":
                archived = [
                    row for row in (selected_rows or []) if not row.get("active", True)
                ]
                if not archived:
                    raise ValueError("Выбери архивные счета для возврата.")
                from src.data.sqlite_store import restore_asset_accounts_to_snapshot

                result = restore_asset_accounts_to_snapshot(
                    config.active_database_path(),
                    [row.get("account_id", "") for row in archived],
                    period=f"{int(year):04d}-{int(month):02d}",
                )
                clear_data_cache()
                clear_table_cache()
                clear_main_dashboard_cache()
                refreshed_rows = _asset_classification_rows(locale)
                message = (
                    f"Restored accounts: {result['reopened']}; values copied to {result['period']}: {result['inserted']}."
                    if normalize_locale(locale) == "en"
                    else f"Возвращено счетов: {result['reopened']}; оценок скопировано в {result['period']}: {result['inserted']}."
                )
                return (
                    message,
                    "success",
                    refreshed_rows,
                    _statement_asset_previews(
                        _asset_input_records(year, month, locale),
                        statement_balances or [],
                        statement_account_ids or [],
                        f"{int(year):04d}-{int(month):02d}",
                        locale,
                    )[0],
                )

            from src.data.sqlite_store import set_asset_account_classifications

            result = set_asset_account_classifications(
                config.active_database_path(),
                [
                    {
                        "account_id": row.get("account_id"),
                        "asset_type_id": (
                            None if row.get("asset_type_id") == UNCLASSIFIED_ASSET_TYPE_VALUE
                            else row.get("asset_type_id") or None
                        ),
                        "include_in_capital": row.get("Включать в капитал"),
                        "active": row.get("active"),
                        "closed_period": row.get("closed_period") or None,
                    }
                    for row in (rows or [])
                ],
                reason="asset classification updated from dashboard",
            )
            clear_data_cache()
            clear_table_cache()
            clear_main_dashboard_cache()
            refreshed_rows = _asset_classification_rows(locale)
            status, color = _asset_classification_status(refreshed_rows, locale)
            saved = (
                f"Updated accounts: {result['updated']}. "
                if normalize_locale(locale) == "en"
                else f"Обновлено счетов: {result['updated']}. "
            )
            return saved + status, color, refreshed_rows, no_update
        except Exception as exc:
            return report_text(str(exc), locale), "danger", no_update, no_update


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
        _grid_section(
            datasets["yearly_stats"], height="none", theme=theme, locale=locale),
        _grid_section(datasets["fx_rates"], height="260px", theme=theme, locale=locale),
        _graph_section(datasets["income_expense"], theme=theme, locale=locale),
        _graph_section(datasets["delta"], theme=theme, locale=locale),
        _graph_section(datasets["savings_rate"], theme=theme, locale=locale),
        _capital_section(
            datasets["capital"], currency=currency,
            height="640px", theme=theme, locale=locale),
        _graph_section(datasets["inflation_rate"], height="520px", theme=theme, locale=locale),
        _capital_attribution_section(
            datasets["capital_attribution"],
            height="520px", theme=theme, locale=locale),
        _graph_section(datasets["fx_revaluation"], height="420px", theme=theme, locale=locale),
        _graph_section(datasets["fx_changes"], theme=theme, locale=locale),
        _graph_section(datasets["asset_currency_allocation"], height="520px", theme=theme, locale=locale),
        _graph_section(datasets["asset_liquidity_allocation"], height="520px", theme=theme, locale=locale),
    ]
    metrics = datasets["cockpit_metrics"].dataframe
    notices = []
    if metrics.attrs.get("selected_period_available") is False:
        notices.append(_main_missing_month_notice(
            str(metrics.attrs["selected_period"]), locale=locale))
    freshness = metrics.attrs.get("asset_freshness")
    if freshness and freshness.get("has_warning"):
        notices.append(_main_asset_freshness_notice(freshness, locale=locale))
    inflation = datasets["real_asset_capital"].dataframe
    if inflation.attrs.get("status") in {"missing", "partial", "stale"}:
        notices.append(_main_inflation_notice(inflation, locale=locale))
    return html.Div([*notices, *sections], className="d-grid gap-4")


def _statistics_report_layout(
    dataset: DashboardDataset,
    theme: str | None,
    locale: str = DEFAULT_LOCALE,
):
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    if data.empty:
        return _empty_section(dataset, locale=locale)

    groups = []
    for index, (section, rows) in enumerate(data.groupby("Раздел", sort=False)):
        cards = []
        for _, row in rows.iterrows():
            detail = str(row.get("Детали", ""))
            cards.append(html.Div(
                [
                    html.Div(str(row["Показатель"]), className="finrep-cockpit-label"),
                    html.Div(str(row["Значение"]), className="finrep-cockpit-value"),
                    html.Div(detail, className="finrep-cockpit-detail") if detail else None,
                ],
                className="finrep-cockpit-card finrep-cockpit-neutral",
            ))
        groups.append(html.Div(
            [
                html.H3(str(section), className="finrep-cockpit-group-title"),
                html.Div(
                    cards,
                    className="finrep-cockpit-grid finrep-mobile-metric-grid",
                ),
            ],
            id=f"data-statistics-group-{index}",
            className="finrep-cockpit-group",
        ))

    return html.Section(
        [_section_header(dataset), html.Div(groups, className="finrep-cockpit-groups")],
        id="data-statistics-section",
        style=_section_style(theme),
    )


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


def _main_asset_freshness_notice(freshness: dict, locale: str = DEFAULT_LOCALE):
    stale_names = ", ".join(freshness.get("stale_accounts", []))
    missing_names = ", ".join(freshness.get("missing_accounts", []))
    carried_names = ", ".join(freshness.get("carried_accounts", []))
    if normalize_locale(locale) == "en":
        parts = []
        if stale_names:
            parts.append(f"Stale valuations: {stale_names}.")
        if missing_names:
            parts.append(f"Unknown valuation date: {missing_names}.")
        if carried_names:
            parts.append(f"Carried forward: {carried_names}.")
        title = "Provisional asset total" if carried_names else "Asset valuations need attention"
        detail = " ".join(parts) + " Values remain included in capital."
    else:
        parts = []
        if stale_names:
            parts.append(f"Устаревшие оценки: {stale_names}.")
        if missing_names:
            parts.append(f"Дата оценки неизвестна: {missing_names}.")
        if carried_names:
            parts.append(f"Перенесённые остатки: {carried_names}.")
        title = "Предварительный итог активов" if carried_names else "Оценки активов требуют внимания"
        detail = " ".join(parts) + " Значения продолжают учитываться в капитале."
    return dbc.Alert(
        [
            html.Div(title, className="fw-semibold"),
            html.Div(detail, className="small mt-1"),
        ],
        id="main-asset-freshness-notice",
        color="warning",
        className="mb-0",
    )


def _main_inflation_notice(data: pd.DataFrame, locale: str = DEFAULT_LOCALE):
    missing = data.attrs.get("missing_periods", [])
    stale = data.attrs.get("stale", False)
    latest = data.attrs.get("latest_cpi_period", "")
    currency = data.attrs.get("currency", "")
    if normalize_locale(locale) == "en":
        title = f"Official inflation data for {currency} is incomplete"
        detail = (
            f"Missing months: {', '.join(missing)}. " if missing else ""
        ) + (f"Latest official month: {latest}. " if stale else "") \
          + ("Dependent real values are left empty. " if not stale or missing
             else "Existing real values remain visible. ") \
          + "Use ‘Refresh inflation’ to check the official source."
    else:
        title = f"Официальные данные инфляции для {currency} неполные"
        detail = (
            f"Нет месяцев: {', '.join(missing)}. " if missing else ""
        ) + (f"Последний официальный месяц: {latest}. " if stale else "") \
          + ("Зависимые реальные значения оставлены пустыми. " if not stale or missing
             else "Доступные реальные значения остаются видимыми. ") \
          + "Проверьте источник кнопкой «Обновить инфляцию»."
    return dbc.Alert(
        [html.Div(title, className="fw-semibold"), html.Div(detail, className="small mt-1")],
        id="main-inflation-notice", color="warning", className="mb-0",
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
                    html.Li(
                        report_text(
                            (
                                "Проверьте строки и нажмите «Сохранить транзакции»."
                                if config.use_sqlite_storage()
                                else "Проверьте Preview и нажмите «Сохранить месяц»."
                            ),
                            locale,
                        )
                    ),
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
    if status in {"negative", "watch", "thin", "review", "stale", "provisional"}:
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
                    card(str(report_text("Финансовый запас по активам, месяцев", locale)), str(row.get("Финансовый запас, мес.", report_text("не рассчитано", locale)))),
                    card(str(report_text("Финансовый запас по активам, лет", locale)), str(row.get("Финансовый запас, лет", report_text("не рассчитано", locale)))),
                    card(str(report_text("Капитал по активам", locale)), str(row.get("Капитал по активам", report_text("не задано", locale)))),
                    card(str(report_text("Средний расход/мес", locale)), str(row.get("Средний расход", report_text("не задано", locale)))),
                    card(
                        str(report_text("Прогресс от цели", locale)),
                        str(row.get("Прогресс от цели (%)", report_text("не рассчитано", locale))),
                    ),
                ],
                className="d-grid gap-3",
            ),
            dbc.Alert(
                str(row.get("Детали")),
                color="warning",
                className="mt-3 mb-0 py-2",
            ) if row.get("Детали") else None,
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
                report_text(
                    (
                        "За выбранный месяц ещё нет операций. Добавьте или импортируйте строки, проверьте их и сохраните транзакции."
                        if config.use_sqlite_storage()
                        else "Выбранный месяц ещё не создан. Добавьте или импортируйте операции, проверьте Preview и сохраните месяц."
                    ),
                    locale,
                ),
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
                html.Div(
                    [
                        _transaction_input_layout(
                            currency,
                            year,
                            month,
                            theme,
                            read_only=read_only,
                            transaction_save_result=transaction_save_result,
                            locale=locale,
                        ),
                        _asset_snapshot_input_layout(
                            year,
                            month,
                            theme,
                            load_records=load_asset_records,
                            read_only=read_only,
                            locale=locale,
                            currency=currency,
                        ),
                    ],
                    className="d-grid gap-4",
                ),
                label=report_text("Операции и остатки", locale),
                tab_id="input-transactions",
            ),
            dbc.Tab(
                _asset_settings_layout(
                    theme,
                    load_records=load_asset_records,
                    read_only=read_only,
                    locale=locale,
                ),
                label=report_text("Настройки активов", locale),
                tab_id="input-assets",
            ),
            dbc.Tab(
                _category_input_layout(theme, read_only=read_only, locale=locale),
                label=report_text("Категории", locale),
                tab_id="input-categories",
            ),
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
    category_options = _transaction_category_options(locale)
    currency_options = [{"label": ticker, "value": ticker} for ticker in config.UNIQUE_TICKERS]
    month_value = f"{year}-{str(month).zfill(2)}"
    sqlite_storage = config.use_sqlite_storage()
    upload_limit_label = BANK_PDF_UPLOAD_LIMIT_LABEL
    batch_limit_mib = MAX_BANK_PDF_BATCH_BYTES // (1024 * 1024)
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
                style={
                    **_section_style(theme),
                    **({"display": "none"} if sqlite_storage else {}),
                },
            ),
            html.Section(
                [
                    html.H2(
                        report_text(
                            "Новые транзакции" if sqlite_storage else "Импорт банковского PDF",
                            locale,
                        ),
                        className="h5 mb-2",
                    ),
                    *(
                        [html.P(
                            report_text(
                                "Добавляй строки вручную, копируй существующие, вставляй таблицу или загружай банковский PDF.",
                                locale,
                            ),
                            className="small opacity-75 mb-3",
                        )]
                        if sqlite_storage else []
                    ),
                    dcc.Upload(
                        id="kaspi-upload",
                        children=html.Div(
                            [
                                html.Div(report_text("Перетащи Kaspi, BCC или Ozon PDF сюда", locale), className="fw-semibold"),
                                html.Div(report_text("или нажми для выбора файла", locale), className="small opacity-75"),
                                html.Div(
                                    (
                                        f"up to {upload_limit_label} each; "
                                        f"{MAX_BANK_PDF_BATCH_FILES} files, {batch_limit_mib} MiB total"
                                        if normalize_locale(locale) == "en" else
                                        f"до {upload_limit_label} каждый; "
                                        f"{MAX_BANK_PDF_BATCH_FILES} файлов, {batch_limit_mib} MiB суммарно"
                                    ),
                                    className="small opacity-75",
                                ),
                            ],
                            className="kaspi-upload-content",
                        ),
                        multiple=True,
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
                    html.Div(
                        id="bank-upload-client-error",
                        role="alert",
                        className="alert alert-danger mt-2",
                        style={"display": "none"},
                        **{
                            "data-max-files": MAX_BANK_PDF_BATCH_FILES,
                            "data-max-file-bytes": MAX_BANK_PDF_BYTES,
                            "data-max-total-bytes": MAX_BANK_PDF_BATCH_BYTES,
                        },
                    ),
                    dcc.Loading(
                        html.Div(
                            id="bank-upload-status",
                            className="finrep-upload-status mt-2",
                            role="status",
                        ),
                        type="circle",
                    ),
                    dbc.Alert(
                        id="kaspi-import-message",
                        children=report_text(
                            (
                                "Добавь, вставь или загрузи операции. Готовые строки будут сохранены в SQLite; ошибки останутся в таблице."
                                if sqlite_storage else
                                "Операции из PDF появятся здесь. Дубли среди черновиков и сохранённых операций будут пропущены."
                            ),
                            locale,
                        ),
                        color="secondary",
                        is_open=True,
                        className="my-3 py-2",
                    ),
                    html.Div(
                        [
                            html.H3(
                                "Statement balances" if normalize_locale(locale) == "en" else "Остатки из выписок",
                                className="h6 mb-2",
                            ),
                            html.P(
                                "Choose an asset in each row, then apply the selected balances for the dashboard month."
                                if normalize_locale(locale) == "en" else
                                "Выбери актив в каждой строке, затем примени выбранные остатки за месяц dashboard.",
                                className="small opacity-75 mb-2",
                            ),
                            dbc.Alert(
                                id="bank-statement-period-message",
                                children="",
                                color="warning",
                                is_open=False,
                                className="mb-2 py-2",
                            ),
                            html.Div(
                                id="bank-statement-balance-table",
                                className="finrep-balance-table-shell",
                            ),
                            html.Div(
                                [
                                    html.Div(
                                        [
                                            dbc.Label(
                                                "New asset name" if normalize_locale(locale) == "en"
                                                else "Новый счёт актива",
                                                html_for="bank-new-asset-name",
                                                className="small mb-1",
                                            ),
                                            dbc.Input(
                                                id="bank-new-asset-name",
                                                placeholder="Short unique name (without type or currency)" if normalize_locale(locale) == "en"
                                                else "Короткое уникальное имя без типа и валюты",
                                                disabled=read_only or not config.use_sqlite_storage(),
                                            ),
                                        ],
                                        className="finrep-balance-create-input",
                                    ),
                                    html.Div(
                                        [
                                            dbc.Label(
                                                "Asset type" if normalize_locale(locale) == "en" else "Тип актива",
                                                html_for="bank-new-asset-type", className="small mb-1",
                                            ),
                                            dcc.Dropdown(
                                                id="bank-new-asset-type",
                                                options=_new_asset_type_options(locale),
                                                placeholder="Choose type" if normalize_locale(locale) == "en" else "Выбери тип",
                                                disabled=read_only or not config.use_sqlite_storage(),
                                                className="dash-dropdown",
                                            ),
                                        ],
                                        className="finrep-balance-create-input",
                                    ),
                                    dbc.Button(
                                        "Create asset" if normalize_locale(locale) == "en"
                                        else "Создать актив",
                                        id="bank-create-asset-button",
                                        color="secondary",
                                        outline=True,
                                        disabled=read_only or not config.use_sqlite_storage(),
                                    ),
                                ],
                                className="finrep-balance-create d-flex flex-wrap align-items-end gap-2 mt-3",
                            ),
                            dbc.Alert(
                                id="bank-create-asset-message",
                                children="",
                                color="secondary",
                                is_open=False,
                                className="mt-2 mb-0 py-2",
                            ),
                            html.Div(
                                [
                                    dbc.Button(
                                        "Apply selected balances" if normalize_locale(locale) == "en"
                                        else "Применить выбранные остатки",
                                        id="bank-statement-balance-apply",
                                        color="primary",
                                        disabled=read_only,
                                    ),
                                    html.A(
                                        report_text("Показать в таблице активов", locale),
                                        href="#assets-snapshot-section",
                                        className="small",
                                    ),
                                ],
                                className="d-flex flex-wrap align-items-center gap-3 mt-2",
                            ),
                            html.Div(
                                id="bank-statement-balance-comparison",
                                className="small mt-2",
                            ),
                            dbc.Alert(
                                id="bank-statement-balance-message",
                                children="",
                                color="secondary",
                                is_open=False,
                                className="mt-2 mb-0 py-2",
                            ),
                        ],
                        id="bank-statement-balance-panel",
                        style={"display": "none"},
                        className="mb-3 p-3 border rounded",
                    ),
                    html.Div(
                        [
                            *(
                                [html.Div(
                                    [
                                        dbc.Button(report_text("Добавить строку", locale), id="transaction-grid-add-button", color="secondary", outline=True, size="sm", disabled=read_only),
                                        dbc.Button(report_text("Копировать строку", locale), id="transaction-grid-copy-button", color="secondary", outline=True, size="sm", disabled=read_only),
                                        dbc.Button(report_text("Удалить строку", locale), id="transaction-grid-delete-button", color="danger", outline=True, size="sm", disabled=read_only),
                                        dbc.Button(report_text("Вставить строки", locale), id="transaction-grid-paste-button", color="secondary", outline=True, size="sm", disabled=read_only),
                                    ],
                                    className="d-flex flex-wrap gap-2",
                                )]
                                if sqlite_storage else []
                            ),
                            *(
                                [dbc.Button(
                                    report_text("Сохранить транзакции", locale),
                                    id="transaction-save-import-button",
                                    color="primary",
                                    className="ms-auto flex-shrink-0",
                                    disabled=read_only,
                                )]
                                if sqlite_storage else []
                            ),
                        ],
                        className="d-flex flex-wrap align-items-center justify-content-between gap-2 mb-2",
                    ),
                    html.Div(
                        report_text("Категории и действия: клик — одна ячейка, Shift+клик — диапазон, Ctrl/Cmd+клик — несколько; Ctrl/Cmd+C и Ctrl/Cmd+V — копировать и вставить.", locale),
                        className="small opacity-75 mb-2",
                    ),
                    html.Div(
                        "Для возникновения долга заполни «Новый контрагент»; для погашения выбери «Погашаемый долг». Долговые операции не считаются доходом или расходом.",
                        className="small opacity-75 mb-2",
                    ),
                    html.Div(
                        dag.AgGrid(
                            id="kaspi-import-grid",
                            rowData=[],
                            selectedRows=[],
                            columnDefs=_localized_input_column_defs(_kaspi_import_column_defs(locale), locale),
                            defaultColDef=_ag_grid_default_col_def(editable=False),
                            dashGridOptions={
                                "pagination": False,
                                "suppressFieldDotNotation": True,
                                "stopEditingWhenCellsLoseFocus": True,
                                **({"rowSelection": "multiple"} if sqlite_storage else {}),
                            },
                            eventListeners={
                                "cellClicked": ["finrepInputCellClicked(params)"],
                                "cellValueChanged": [
                                    "finrepInputCellChanged(params)",
                                    "finrepCategoryCellChanged(params)",
                                ],
                                "rowDataUpdated": ["finrepInputSelectionReset(params)"],
                            },
                            className=f"{_ag_grid_class_name(theme)} finrep-import-grid",
                            style=_ag_grid_style("420px"),
                        ),
                        className="finrep-import-grid-shell",
                    ),
                    dbc.Modal(
                        [
                            dbc.ModalHeader(dbc.ModalTitle(report_text("Вставить транзакции", locale))),
                            dbc.ModalBody(
                                [
                                    html.P(
                                        report_text(
                                            "Вставь строки из Excel в формате: Дата, Сумма, Валюта, Категория, Комментарий. Заголовок необязателен.",
                                            locale,
                                        ),
                                        className="small",
                                    ),
                                    dcc.Textarea(
                                        id="transaction-paste-text",
                                        value="",
                                        className="form-control finrep-native-input",
                                        style={"width": "100%", "minHeight": "220px", **_form_control_style(theme)},
                                    ),
                                ]
                            ),
                            dbc.ModalFooter(
                                [
                                    dbc.Button(report_text("Отмена", locale), id="transaction-paste-cancel-button", color="secondary", outline=True),
                                    dbc.Button(report_text("Добавить строки", locale), id="transaction-paste-apply-button", color="primary"),
                                ],
                                className="gap-2",
                            ),
                        ],
                        id="transaction-paste-modal",
                        is_open=False,
                        centered=True,
                        size="lg",
                    ),
                    *(
                        [
                            dcc.Dropdown(
                                id="transaction-import-period",
                                options=[],
                                value=None,
                                style={"display": "none"},
                            ),
                        ]
                        if sqlite_storage else []
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
            ) if not sqlite_storage else None,
        ],
        className="d-grid gap-4 pt-3",
    )


def _category_registry_rows(locale: str = DEFAULT_LOCALE) -> list[dict]:
    if not config.use_sqlite_storage():
        return []
    from src.data.sqlite_store import categories

    direction_labels = {
        "income": report_text("Доход", locale),
        "expense": report_text("Расход", locale),
    }
    class_labels = {
        "active": report_text("Активный", locale),
        "passive": report_text("Пассивный", locale),
        None: "",
    }
    return [
        {
            "id": row["id"],
            "Категория": row["name_ru"],
            "Направление": direction_labels[row["direction"]],
            "Класс дохода": class_labels.get(row["income_class"], row["income_class"] or ""),
            "Статус": report_text("Активна" if row["active"] else "Неактивна", locale),
            "Операций": row["transaction_count"],
            "Открытых черновиков": row["open_draft_count"],
            "active": bool(row["active"]),
        }
        for row in categories(config.active_database_path())
    ]


def _category_registry_column_defs(locale: str = DEFAULT_LOCALE) -> list[dict]:
    return _localized_input_column_defs(
        [
            {"field": "id", "hide": True},
            {"field": "Категория", "headerName": "Категория", "flex": 1, "minWidth": 190},
            {"field": "Направление", "headerName": "Направление", "width": 140},
            {"field": "Класс дохода", "headerName": "Класс дохода", "width": 150},
            {"field": "Статус", "headerName": "Статус", "width": 120},
            {"field": "Операций", "headerName": "Операций", "width": 120},
            {"field": "Открытых черновиков", "headerName": "Открытых черновиков", "width": 170},
            {"field": "active", "hide": True},
        ],
        locale,
    )


def _category_input_layout(
    theme: str | None,
    *,
    read_only: bool = False,
    locale: str = DEFAULT_LOCALE,
):
    if not config.use_sqlite_storage():
        return dbc.Alert(
            report_text("Управление категориями доступно в режиме SQLite.", locale),
            color="secondary",
            className="mt-3",
        )
    return html.Div(
        [
            html.Section(
                [
                    html.H2(report_text("Новая категория", locale), className="h5 mb-3"),
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Направление", locale), className="small mb-1"),
                                    dcc.Dropdown(
                                        id="category-create-direction",
                                        options=[
                                            {"label": report_text("Доход", locale), "value": "income"},
                                            {"label": report_text("Расход", locale), "value": "expense"},
                                        ],
                                        value="income",
                                        clearable=False,
                                        className="dash-dropdown",
                                    ),
                                ],
                                xs=12,
                                md=3,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Название", locale), html_for="category-create-name", className="small mb-1"),
                                    dbc.Input(id="category-create-name", type="text", className="finrep-native-input", style=_form_control_style(theme)),
                                ],
                                xs=12,
                                md=4,
                            ),
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Класс дохода", locale), className="small mb-1"),
                                    dcc.Dropdown(
                                        id="category-create-income-class",
                                        options=[
                                            {"label": report_text("Активный", locale), "value": "active"},
                                            {"label": report_text("Пассивный", locale), "value": "passive"},
                                        ],
                                        value="active",
                                        clearable=False,
                                        className="dash-dropdown",
                                    ),
                                ],
                                xs=12,
                                md=3,
                            ),
                            dbc.Col(
                                dbc.Button(
                                    report_text("Добавить", locale),
                                    id="category-create-button",
                                    color="primary",
                                    className="w-100",
                                    disabled=read_only,
                                ),
                                xs=12,
                                md=2,
                                className="d-flex align-items-end",
                            ),
                        ],
                        className="g-2",
                    ),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.H2(report_text("Справочник категорий", locale), className="h5 mb-2"),
                    html.P(
                        report_text("Выбери строку, чтобы переименовать категорию или изменить её доступность для новых операций. История сохраняет тот же ID.", locale),
                        className="small opacity-75",
                    ),
                    dbc.Alert(
                        id="category-registry-message",
                        children=report_text("Изменения категорий применяются к новым операциям; суммы истории не переписываются.", locale),
                        color="secondary",
                        is_open=True,
                        className="mb-3 py-2",
                    ),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="category-registry-grid",
                            rowData=_category_registry_rows(locale),
                            columnDefs=_category_registry_column_defs(locale),
                            defaultColDef=_ag_grid_default_col_def(editable=False),
                            dashGridOptions={
                                "pagination": False,
                                "rowSelection": "single",
                                "suppressFieldDotNotation": True,
                            },
                            className=_ag_grid_class_name(theme),
                            style=_ag_grid_style("430px"),
                        )
                    ),
                    dbc.Row(
                        [
                            dbc.Col(
                                [
                                    dbc.Label(report_text("Новое название выбранной категории", locale), html_for="category-rename-name", className="small mb-1"),
                                    dbc.Input(id="category-rename-name", type="text", className="finrep-native-input", style=_form_control_style(theme)),
                                ],
                                xs=12,
                                md=6,
                            ),
                            dbc.Col(
                                dbc.Button(
                                    report_text("Переименовать", locale),
                                    id="category-rename-button",
                                    color="secondary",
                                    outline=True,
                                    className="w-100",
                                    disabled=read_only,
                                ),
                                xs=12,
                                md=3,
                                className="d-flex align-items-end",
                            ),
                            dbc.Col(
                                dbc.Button(
                                    report_text("Активировать / деактивировать", locale),
                                    id="category-toggle-button",
                                    color="warning",
                                    outline=True,
                                    className="w-100",
                                    disabled=read_only,
                                ),
                                xs=12,
                                md=3,
                                className="d-flex align-items-end",
                            ),
                        ],
                        className="g-2 mt-2",
                    ),
                ],
                style=_section_style(theme),
            ),
        ],
        className="d-grid gap-4 pt-3",
    )


def _new_debt_grid_row(currency: str, source: dict | None = None) -> dict:
    row = {
        "operation_id": uuid4().hex,
        "opened_date": datetime.now().date().isoformat(),
        "type": "Мне должны",
        "counterparty": "",
        "principal_amount": "",
        "principal_currency": currency,
        "comment": "",
        "validation_error": "",
    }
    if source:
        row.update({key: source.get(key, "") for key in row if key not in {"operation_id", "validation_error"}})
    return row


def _new_debt_payment_row(currency: str, source: dict | None = None) -> dict:
    row = {
        "operation_id": uuid4().hex,
        "debt": "",
        "date": datetime.now().date().isoformat(),
        "amount": "",
        "cash_currency": currency,
        "comment": "",
        "validation_error": "",
    }
    if source:
        row.update({key: source.get(key, "") for key in row if key not in {"operation_id", "validation_error"}})
    return row


def _new_debt_plan_row(currency: str, source: dict | None = None) -> dict:
    row = {
        "operation_id": uuid4().hex,
        "debt": "",
        "due_on": datetime.now().date().isoformat(),
        "amount": "",
        "comment": "",
        "validation_error": "",
    }
    if source:
        row.update({key: source.get(key, "") for key in row if key not in {"operation_id", "validation_error"}})
    return row


def _debt_entry_column(currency: str, read_only: bool) -> dict:
    return {
        "field": "debt", "headerName": "Долг", "editable": not read_only,
        "minWidth": 300, "flex": 2, "cellEditor": "agSelectCellEditor",
        "cellEditorParams": {"values": [option["label"] for option in _debt_select_options(currency) if option["value"]]},
    }


def _debt_id_from_entry(label: str) -> str:
    if not label or " | " not in label:
        raise ValueError("Выбери долг в строке таблицы.")
    return label.rsplit(" | ", 1)[-1]


def _debt_payment_column_defs(currency: str, read_only: bool = False) -> list[dict]:
    return [
        _debt_entry_column(currency, read_only),
        {"field": "date", "headerName": "Дата", "editable": not read_only, "width": 130},
        {"field": "amount", "headerName": "Сумма платежа", "editable": not read_only, "width": 150},
        {"field": "cash_currency", "headerName": "Валюта платежа", "editable": not read_only,
         "width": 150, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": list(config.UNIQUE_TICKERS)}},
        {"field": "comment", "headerName": "Комментарий", "editable": not read_only, "minWidth": 180, "flex": 1},
        {"field": "validation_error", "headerName": "Проверка", "minWidth": 220, "flex": 1,
         "cellClassRules": {"text-danger": "Boolean(params.value)"}},
        {"field": "operation_id", "hide": True},
    ]


def _debt_plan_input_column_defs(currency: str, read_only: bool = False) -> list[dict]:
    return [
        _debt_entry_column(currency, read_only),
        {"field": "due_on", "headerName": "Плановая дата", "editable": not read_only, "width": 150},
        {"field": "amount", "headerName": "Плановая сумма", "editable": not read_only, "width": 160},
        {"field": "comment", "headerName": "Комментарий", "editable": not read_only, "minWidth": 180, "flex": 1},
        {"field": "validation_error", "headerName": "Проверка", "minWidth": 220, "flex": 1,
         "cellClassRules": {"text-danger": "Boolean(params.value)"}},
        {"field": "operation_id", "hide": True},
    ]


def _debt_input_layout(currency: str, theme: str | None, include_create: bool = True, read_only: bool = False):
    sections = []
    plan_read_only = read_only or not config.use_sqlite_storage()

    if include_create:
        sections.append(
            html.Section(
                [
                    html.H2("Новые долги", className="h5 mb-2"),
                    html.P("Добавь строки и сохрани их после проверки. Каждый долг создаст черновик денежной операции.", className="small opacity-75"),
                    html.Div([
                        dbc.Button("Добавить строку", id="debt-grid-add-button", color="secondary", outline=True, size="sm", disabled=read_only),
                        dbc.Button("Копировать строку", id="debt-grid-copy-button", color="secondary", outline=True, size="sm", disabled=read_only),
                        dbc.Button("Удалить строку", id="debt-grid-delete-button", color="danger", outline=True, size="sm", disabled=read_only),
                        dbc.Button("Сохранить долги", id="debt-add-button", color="primary", size="sm", disabled=read_only),
                    ], className="d-flex flex-wrap gap-2 mb-2"),
                    _ag_grid_scroll(dag.AgGrid(
                        id="debt-new-grid", rowData=[], selectedRows=[],
                        columnDefs=[
                            {"field": "opened_date", "headerName": "Дата", "editable": not read_only, "width": 130},
                            {"field": "type", "headerName": "Тип", "editable": not read_only, "width": 145,
                             "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": ["Мне должны", "Я должен"]}},
                            {"field": "counterparty", "headerName": "Контрагент", "editable": not read_only, "minWidth": 180, "flex": 1},
                            {"field": "principal_amount", "headerName": "Сумма", "editable": not read_only, "width": 140},
                            {"field": "principal_currency", "headerName": "Валюта", "editable": not read_only, "width": 110,
                             "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": list(config.UNIQUE_TICKERS)}},
                            {"field": "comment", "headerName": "Комментарий", "editable": not read_only, "minWidth": 180, "flex": 1},
                            {"field": "validation_error", "headerName": "Проверка", "minWidth": 220, "flex": 1,
                             "cellClassRules": {"text-danger": "Boolean(params.value)"}},
                            {"field": "operation_id", "hide": True},
                        ],
                        defaultColDef=_ag_grid_default_col_def(editable=False),
                        dashGridOptions={"pagination": False, "rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True},
                        className=_ag_grid_class_name(theme), style=_ag_grid_style("260px"),
                    )),
                    dbc.Alert(id="debt-input-message", children="", color="secondary", is_open=True, className="mt-2 py-2"),
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
                    _active_debts_grid(currency, theme, "receivable"),
                    html.Div(className="my-4"),
                    _active_debts_grid(currency, theme, "liability"),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.H2("Погашения долгов", className="h5 mb-2"),
                    html.P("Выбери долг в ячейке и сохрани заполненные строки. Для каждого погашения появится денежный черновик.", className="small opacity-75"),
                    html.Div([
                        dbc.Button("Добавить строку", id="debt-payment-add-row", color="secondary", outline=True, size="sm", disabled=read_only),
                        dbc.Button("Копировать строку", id="debt-payment-copy-row", color="secondary", outline=True, size="sm", disabled=read_only),
                        dbc.Button("Удалить строку", id="debt-payment-delete-row", color="danger", outline=True, size="sm", disabled=read_only),
                        dbc.Button("Сохранить погашения", id="debt-payment-button", color="primary", size="sm", disabled=read_only),
                    ], className="d-flex flex-wrap gap-2 mb-2"),
                    _ag_grid_scroll(dag.AgGrid(
                        id="debt-payment-grid", rowData=[], selectedRows=[],
                        columnDefs=_debt_payment_column_defs(currency, read_only),
                        defaultColDef=_ag_grid_default_col_def(editable=False),
                        dashGridOptions={"pagination": False, "rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True},
                        className=_ag_grid_class_name(theme), style=_ag_grid_style("220px"),
                    )),
                ],
                style=_section_style(theme),
            ),
            html.Section(
                [
                    html.H2("Будущие платежи", className="h5 mb-2"),
                    html.P("План не меняет остаток и денежный поток. Факт появляется только после подтверждения.", className="text-muted"),
                    dbc.Alert(id="debt-plan-message", children="", color="secondary", className="py-2"),
                    html.Div([
                        dbc.Button("Добавить строку", id="debt-plan-add-row", color="secondary", outline=True, size="sm", disabled=plan_read_only),
                        dbc.Button("Копировать строку", id="debt-plan-copy-row", color="secondary", outline=True, size="sm", disabled=plan_read_only),
                        dbc.Button("Удалить строку", id="debt-plan-delete-row", color="danger", outline=True, size="sm", disabled=plan_read_only),
                        dbc.Button("Сохранить планы", id="debt-plan-add-button", color="primary", size="sm", disabled=plan_read_only),
                    ], className="d-flex flex-wrap gap-2 mb-2"),
                    _ag_grid_scroll(dag.AgGrid(
                        id="debt-plan-new-grid", rowData=[], selectedRows=[],
                        columnDefs=_debt_plan_input_column_defs(currency, plan_read_only),
                        defaultColDef=_ag_grid_default_col_def(editable=False),
                        dashGridOptions={"pagination": False, "rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True},
                        className=_ag_grid_class_name(theme), style=_ag_grid_style("220px"),
                    )),
                    html.H3("Сохранённые планы", className="h6 mt-3"),
                    html.P("Выбери план, при необходимости измени дату факта в его строке и подтверди.", className="small opacity-75"),
                    _ag_grid_scroll(dag.AgGrid(
                        id="debt-plans-grid", rowData=_debt_plan_records(), selectedRows=[],
                        columnDefs=[
                            {"field": "due_on", "headerName": "Плановая дата", "width": 150},
                            {"field": "counterparty", "headerName": "Контрагент", "flex": 1, "minWidth": 180},
                            {"field": "amount", "headerName": "Сумма", "width": 130},
                            {"field": "currency", "headerName": "Валюта", "width": 100},
                            {"field": "status", "headerName": "Статус", "width": 150},
                            {"field": "actual_date", "headerName": "Дата факта",
                             "editable": {"function": "params.data.status == 'План'"} if not plan_read_only else False,
                             "width": 150},
                            {"field": "comment", "headerName": "Комментарий", "flex": 1, "minWidth": 180},
                            {"field": "validation_error", "headerName": "Проверка", "minWidth": 220,
                             "cellClassRules": {"text-danger": "Boolean(params.value)"}},
                            {"field": "id", "hide": True},
                        ],
                        defaultColDef=_ag_grid_default_col_def(),
                        dashGridOptions={"pagination": False, "rowSelection": "single", "stopEditingWhenCellsLoseFocus": True,
                                         "getRowId": {"function": "params.data.id"}},
                        className=_ag_grid_class_name(theme), style=_ag_grid_style("240px"),
                    )),
                    dbc.Button("Подтвердить выбранный план", id="debt-plan-confirm-button", color="primary",
                               className="mt-2", disabled=plan_read_only),
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


def _asset_snapshot_input_layout(
    year: str,
    month: str,
    theme: str | None,
    load_records: bool = True,
    read_only: bool = False,
    locale: str = DEFAULT_LOCALE,
    currency: str = DEFAULT_CURRENCY,
):
    records = _asset_input_records(year, month, locale) if load_records else []
    message, message_color = _asset_input_status(year, month, locale)
    period = f"{int(year):04d}-{int(month):02d}"
    title = f"Assets for {period}" if normalize_locale(locale) == "en" else f"Активы за {period}"
    return html.Section(
        [
            html.Div(
                [
                    html.H2(title, className="h5 mb-0"),
                    html.Div(
                        [
                            dcc.ConfirmDialogProvider(
                                dbc.Button(
                                    "Reset edits" if normalize_locale(locale) == "en" else "Сбросить правки",
                                    color="secondary", outline=True, size="sm",
                                ),
                                id="assets-reset-confirm",
                                message=(
                                    "Discard unsaved changes and reload assets for this month?"
                                    if normalize_locale(locale) == "en" else
                                    "Несохранённые изменения будут потеряны. Загрузить активы за этот месяц заново?"
                                ),
                            ),
                            dbc.Button(report_text("Добавить строку", locale), id="assets-add-row-button", color="secondary", outline=True, size="sm", disabled=read_only),
                            dbc.Button(
                                "Add from previous month" if normalize_locale(locale) == "en"
                                else "Добавить из прошлого месяца",
                                id="assets-copy-previous-button",
                                color="secondary",
                                outline=True,
                                size="sm",
                                disabled=read_only,
                            ) if not config.use_sqlite_storage() else None,
                            dbc.Button(report_text("Отправить в архив", locale), id="assets-delete-row-button", color="warning", outline=True, size="sm", disabled=read_only),
                            dbc.Button(report_text("Применить", locale), id="assets-apply-button", color="primary", outline=True, size="sm", disabled=read_only),
                        ],
                        className="d-flex flex-wrap gap-2 finrep-assets-actions",
                    ),
                ],
                className="d-flex justify-content-between align-items-center mb-3 finrep-assets-header",
            ),
            html.Div(
                [
                    html.Div(
                        [
                            dbc.Label(
                                "Asset from registry" if normalize_locale(locale) == "en"
                                else "Актив из реестра",
                                html_for="assets-registry-account",
                                className="small mb-1",
                            ),
                            dcc.Dropdown(
                                id="assets-registry-account",
                                options=_active_asset_options() if load_records else [],
                                placeholder="Choose asset" if normalize_locale(locale) == "en"
                                else "Выбери актив",
                                disabled=read_only or not config.use_sqlite_storage(),
                                className="dash-dropdown",
                            ),
                        ],
                        className="finrep-registry-account",
                    ),
                    html.Div(
                        [
                            dbc.Label(
                                report_text("Валюта", locale),
                                html_for="assets-registry-currency",
                                className="small mb-1",
                            ),
                            dcc.Dropdown(
                                id="assets-registry-currency",
                                options=[{"label": code, "value": code} for code in config.UNIQUE_TICKERS],
                                value=currency,
                                clearable=False,
                                disabled=read_only or not config.use_sqlite_storage(),
                                className="dash-dropdown",
                            ),
                        ],
                        className="finrep-registry-currency",
                    ),
                    dbc.Button(
                        "Add to month" if normalize_locale(locale) == "en"
                        else "Добавить в месяц",
                        id="assets-add-from-registry-button",
                        color="secondary",
                        outline=True,
                        size="sm",
                        disabled=read_only or not config.use_sqlite_storage(),
                    ),
                ],
                className="finrep-registry-picker d-flex flex-wrap align-items-end gap-2 mb-3",
            ),
            dbc.Alert(
                id="assets-input-message",
                children=message,
                color=message_color,
                is_open=True,
                className="mb-3 py-2",
            ),
            dbc.Alert(
                id="asset-statement-highlight-message",
                children="",
                color="secondary",
                is_open=False,
                className="mb-3 py-2",
            ),
            _ag_grid_scroll(
                dag.AgGrid(
                    id="assets-input-grid",
                    rowData=records,
                    columnDefs=_localized_input_column_defs(_asset_input_column_defs(locale), locale),
                    defaultColDef=_ag_grid_default_col_def(editable=not read_only),
                    dashGridOptions={"pagination": False, "suppressFieldDotNotation": True, "rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True, "undoRedoCellEditing": True},
                    className=_ag_grid_class_name(theme),
                    style=_ag_grid_style(_asset_grid_height(len(records), maximum=920)),
                )
            ),
        ],
        id="assets-snapshot-section",
        style=_section_style(theme),
    )


def _asset_settings_layout(
    theme: str | None,
    load_records: bool = True,
    read_only: bool = False,
    locale: str = DEFAULT_LOCALE,
):
    classification_rows = _asset_classification_rows(locale) if load_records else []
    classification_message, classification_color = _asset_classification_status(
        classification_rows, locale)
    classification_read_only = read_only or not config.use_sqlite_storage()
    return html.Div(
        [
            html.Section(
                [
                    html.Div(
                        [
                            html.Div(
                                [
                                    html.H2(
                                        report_text("Классификация активов", locale),
                                        className="h5 mb-1",
                                    ),
                                    html.P(
                                        report_text(
                                            "Для брокерского счёта снимок содержит только свободные деньги; бумаги и криптоактивы учитываются отдельно.",
                                            locale,
                                        ),
                                        className="small mb-0",
                                        style={"color": "var(--finrep-muted)"},
                                    ),
                                    html.P(
                                        report_text(
                                            "Архивные счета сохраняются в истории и не проверяются на актуальность. Выбери архивный счёт в таблице, чтобы вернуть его в текущий снимок.",
                                            locale,
                                        ),
                                        className="small mb-0",
                                        style={"color": "var(--finrep-muted)"},
                                    ),
                                    html.P(
                                        "Новый актив требует типа. Ликвидность определяется типом. Перед архивацией сохрани нулевой остаток за месяц закрытия во всех валютах; после этого он не переносится в следующие месяцы."
                                        if normalize_locale(locale) != "en" else
                                        "New assets need a type; liquidity follows the type. Save zero balances in every currency for the closing month before archiving.",
                                        className="small mb-0",
                                        style={"color": "var(--finrep-muted)"},
                                    ),
                                ]
                            ),
                            html.Div(
                                [
                                    dbc.Button(
                                        report_text("Вернуть из архива в текущий снимок", locale),
                                        id="asset-account-restore-button",
                                        color="secondary",
                                        outline=True,
                                        size="sm",
                                        disabled=classification_read_only,
                                    ),
                                    dbc.Button(
                                        report_text("Сохранить классификацию", locale),
                                        id="asset-classification-save-button",
                                        color="primary",
                                        size="sm",
                                        disabled=classification_read_only,
                                    ),
                                ],
                                className="d-flex flex-wrap gap-2",
                            ),
                        ],
                        className="d-flex flex-wrap justify-content-between align-items-start gap-3 mb-3 finrep-asset-classification-header",
                    ),
                    dbc.Alert(
                        id="asset-classification-message",
                        children=classification_message,
                        color=classification_color,
                        is_open=True,
                        className="mb-3 py-2",
                    ),
                    _ag_grid_scroll(
                        dag.AgGrid(
                            id="asset-classification-grid",
                            rowData=classification_rows,
                            columnDefs=_asset_classification_column_defs(
                                locale, editable=not classification_read_only),
                            defaultColDef=_ag_grid_default_col_def(),
                            dashGridOptions={
                                "pagination": False,
                                "suppressFieldDotNotation": True,
                                "rowSelection": "multiple",
                                "stopEditingWhenCellsLoseFocus": True,
                                "undoRedoCellEditing": True,
                            },
                            className=_ag_grid_class_name(theme),
                            style=_ag_grid_style(
                                _asset_grid_height(len(classification_rows), maximum=560)),
                        )
                    ),
                ],
                style=_section_style(theme),
            ),
        ],
        className="d-grid gap-4 pt-3",
    )


def _asset_grid_height(row_count: int, *, maximum: int) -> str:
    return f"{min(maximum, max(220, 64 + row_count * 42))}px"


def _asset_classification_rows(locale: str = DEFAULT_LOCALE) -> list[dict]:
    if not config.use_sqlite_storage():
        return []
    from src.data.asset_freshness import evaluate_asset_freshness, freshness_label
    from src.data.sqlite_store import asset_accounts

    freshness = evaluate_asset_freshness(asset_accounts(config.active_database_path()))
    accounts = sorted(freshness["accounts"], key=lambda row: row["name"].casefold())
    accounts.sort(key=lambda row: row["last_period"] or "", reverse=True)
    return [
        {
            "account_id": row["id"],
            "Счет": row["name"],
            "asset_type_id": row["asset_type_id"] or UNCLASSIFIED_ASSET_TYPE_VALUE,
            "liquidity_class_id": row["liquidity_class_id"] or "",
            "liquidity_source": row["liquidity_source"],
            "Включать в капитал": bool(row["include_in_capital"]),
            "active": bool(row["active"]),
            "closed_period": row["closed_period"] or "",
            "Актуальность": freshness_label(
                row, locale=normalize_locale(locale)) if row["snapshot_count"] else (
                    "Awaiting first valuation" if normalize_locale(locale) == "en"
                    else "Ожидает первой оценки"),
            "freshness_status": row["freshness_status"],
            "Снимков": row["snapshot_count"],
            "Первый снимок": row["first_period"] or "",
            "Последний снимок": row["last_period"] or "",
        }
        for row in accounts
    ]


def _asset_classification_status(rows: list[dict], locale: str = DEFAULT_LOCALE) -> tuple[str, str]:
    if not config.use_sqlite_storage():
        return report_text("Классификация активов доступна в режиме SQLite.", locale), "secondary"
    total = len(rows)
    unclassified = sum(
        row.get("asset_type_id") in {None, "", UNCLASSIFIED_ASSET_TYPE_VALUE}
        for row in rows
    )
    unclassified_names = [row["Счет"] for row in rows
                          if row.get("asset_type_id") in {None, "", UNCLASSIFIED_ASSET_TYPE_VALUE}]
    pending_names = [row["Счет"] for row in rows if row.get("active", True) and not row.get("Снимков")]
    excluded = sum(not row.get("Включать в капитал", True) for row in rows)
    archived = sum(not row.get("active", True) for row in rows)
    liquidity_unclassified = sum(not row.get("liquidity_class_id") for row in rows)
    stale = sum(
        row.get("freshness_status") == "stale" and row.get("Включать в капитал", True)
        and row.get("active", True)
        for row in rows
    )
    missing_date = sum(
        row.get("freshness_status") == "missing" and row.get("Включать в капитал", True)
        and row.get("active", True)
        for row in rows
    )
    if normalize_locale(locale) == "en":
        message = (
            f"Accounts: {total}. Unclassified: {unclassified}. "
            f"Liquidity unassigned: {liquidity_unclassified}. "
            f"Stale valuations: {stale}. Unknown valuation date: {missing_date}. "
            f"Archived: {archived}. Excluded from capital: {excluded}."
            + (f" Classify: {', '.join(unclassified_names)}." if unclassified_names else "")
            + (f" Awaiting first valuation: {', '.join(pending_names)}." if pending_names else "")
        )
    else:
        message = (
            f"Счетов: {total}. Не классифицировано: {unclassified}. "
            f"Ликвидность не задана: {liquidity_unclassified}. "
            f"Устаревших оценок: {stale}. Без даты оценки: {missing_date}. "
            f"В архиве: {archived}. Исключено из капитала: {excluded}."
            + (f" Требуют классификации: {', '.join(unclassified_names)}." if unclassified_names else "")
            + (f" Ожидают первой оценки: {', '.join(pending_names)}." if pending_names else "")
        )
    return (
        message,
        "warning" if unclassified or liquidity_unclassified or stale or missing_date else "success",
    )


def _asset_classification_column_defs(
        locale: str = DEFAULT_LOCALE, *, editable: bool = True) -> list[dict]:
    if config.use_sqlite_storage():
        from src.data.sqlite_store import asset_types

        types = asset_types(config.active_database_path())
    else:
        types = []
    type_labels = {
        UNCLASSIFIED_ASSET_TYPE_VALUE: report_text("Не классифицировано", locale),
        **{
            row["id"]: row["name_en"] if normalize_locale(locale) == "en" else row["name_ru"]
            for row in types
        },
    }
    unassigned_label = report_text("Не задана", locale)
    liquidity_formatter = f"params.value || '{unassigned_label}'"
    columns = [
        {"field": "account_id", "hide": True},
        {"field": "Счет", "headerName": "Счет", "flex": 2, "minWidth": 220,
         "tooltipField": "Счет",
         "checkboxSelection": {"function": "params.data && !params.data.active"}},
        {
            "field": "asset_type_id",
            "headerName": "Тип актива",
            "editable": editable,
            "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": list(type_labels)},
            "valueFormatter": {
                "function": f"({json.dumps(type_labels, ensure_ascii=False)})[params.value] || params.value"
            },
            "flex": 1.2,
            "minWidth": 190,
        },
        {
            "field": "liquidity_class_id",
            "headerName": "Ликвидность",
            "editable": False,
            "valueFormatter": {"function": liquidity_formatter},
            "flex": 0.8,
            "minWidth": 150,
        },
        {"field": "liquidity_source", "hide": True},
        {
            "field": "Включать в капитал",
            "headerName": "Капитал",
            "editable": editable,
            "cellRenderer": "agCheckboxCellRenderer",
            "cellEditor": "agCheckboxCellEditor",
            "width": 105,
            "minWidth": 105,
        },
        {
            "field": "active",
            "headerName": "Активен",
            "editable": False,
            "cellRenderer": "agCheckboxCellRenderer",
            "width": 105,
            "minWidth": 105,
        },
        {
            "field": "closed_period",
            "headerName": "Закрыт после",
            "editable": False,
            "width": 150,
            "minWidth": 150,
        },
        {
            "field": "Актуальность",
            "headerName": "Актуальность",
            "editable": False,
            "flex": 1.15,
            "minWidth": 220,
        },
        {"field": "freshness_status", "hide": True},
        {"field": "Снимков", "headerName": "Снимков", "width": 105},
        {"field": "Первый снимок", "headerName": "Первый снимок", "width": 150},
        {"field": "Последний снимок", "headerName": "Последний снимок", "width": 160,
         "sort": "desc", "sortIndex": 0},
    ]
    return _localized_input_column_defs(columns, locale)

def _dataframe_records(data: pd.DataFrame) -> list[dict]:
    if data.empty:
        return []
    return data.fillna("0").to_dict("records")


def _merge_input_grid_rows(existing_rows: list[dict] | None, new_rows: list[dict]) -> list[dict]:
    merged = [dict(row) for row in (existing_rows or [])]
    existing_keys = {
        (str(row.get("source", "")), str(row.get("source_id", ""))) for row in merged
    }
    for row in new_rows:
        key = (str(row.get("source", "")), str(row.get("source_id", "")))
        if key not in existing_keys:
            merged.append(dict(row))
            existing_keys.add(key)
    return merged


def _mark_cross_file_duplicates(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty or "source_file" not in data:
        return data
    result = data.copy(deep=True)
    source_comments = result.get("source_comment", result["comment"]).where(
        lambda values: values.astype(str).str.strip().ne(""), result["comment"]
    )
    keys = pd.DataFrame({
        "date": result["date"].astype(str),
        "currency": result["currency"].astype(str).str.upper(),
        "amount": pd.to_numeric(result["amount"], errors="coerce").round(2),
        "direction": result["direction"].astype(str).str.lower(),
        "comment": source_comments.astype(str).str.replace(
            r"\s+", " ", regex=True).str.upper().str.strip(),
    })
    cross_file = pd.Series(False, index=result.index)
    for indexes in keys.groupby(list(keys.columns), dropna=False).groups.values():
        if result.loc[indexes, "source_file"].nunique() > 1:
            cross_file.loc[indexes] = True
    protected = result["skip_reason"].astype(str).isin({
        "internal_transfer", "duplicate_in_staging",
    })
    review = cross_file & ~protected
    result.loc[review, "duplicate_in_source"] = True
    result.loc[review, "skip_reason"] = "possible_duplicate"
    result.loc[review, "import_action"] = "review"
    return result


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


def _bank_upload_status_row(
    filename: str, locale: str, *, rows: int | None = None,
    imported: int = 0, skipped: int = 0, review: int = 0,
    error: str | None = None,
):
    english = normalize_locale(locale) == "en"
    chips = (
        [html.Span(error, className="finrep-upload-error")]
        if error else [
            html.Span(f"{rows} {'rows' if english else 'строк'}", className="finrep-upload-chip"),
            html.Span(f"{imported} import", className="finrep-upload-chip"),
            html.Span(f"{skipped} skip", className="finrep-upload-chip"),
            html.Span(
                f"{review} review",
                className=f"finrep-upload-chip{' is-review' if review else ''}",
            ),
        ]
    )
    return html.Div(
        [
            html.Div([
                html.Span(filename, className="finrep-upload-name", title=filename),
                html.Span(
                    ("Error" if english else "Ошибка") if error else ("Ready" if english else "Готово"),
                    className=f"finrep-upload-state {'is-error' if error else 'is-ready'}",
                ),
            ], className="finrep-upload-heading"),
            html.Div(chips, className="finrep-upload-details"),
        ],
        className="finrep-upload-status-row",
    )


def _transaction_category_options(locale: str = DEFAULT_LOCALE) -> list[dict]:
    if config.use_sqlite_storage():
        from src.data.sqlite_store import categories

        rows = [
            row for row in categories(config.active_database_path())
            if row["active"] and row["parent_id"] is None
        ]
        direction_labels = {
            "income": report_text("Доход", locale),
            "expense": report_text("Расход", locale),
        }
        return [
            {
                "label": f"{direction_labels[row['direction']]} · {row['name_ru']}",
                "value": row["id"],
            }
            for row in rows
        ]
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


def _active_asset_options() -> list[dict]:
    if not config.use_sqlite_storage():
        return []
    from src.data.sqlite_store import asset_accounts

    return [
        {"label": row["name"], "value": row["id"]}
        for row in asset_accounts(config.active_database_path()) if row["active"]
    ]


def _new_asset_type_options(locale: str = DEFAULT_LOCALE) -> list[dict]:
    if not config.use_sqlite_storage():
        return []
    from src.data.sqlite_store import asset_types

    english = normalize_locale(locale) == "en"
    return [{"label": row["name_en"] if english else row["name_ru"],
             "value": row["id"]}
            for row in asset_types(config.active_database_path())]


def _asset_input_records(
    year: str, month: str, locale: str = DEFAULT_LOCALE) -> list[dict]:
    data = read_asset_snapshot(year, month).copy(deep=True)
    if data.empty:
        return []
    account_details = {}
    if config.use_sqlite_storage():
        from src.data.sqlite_store import asset_accounts, effective_asset_snapshot_month

        english = normalize_locale(locale) == "en"
        account_details = {
            row["name"]: {
                "account_id": row["id"],
                "asset_type_id": row["asset_type_id"],
                "asset_type_label": (
                    row["asset_type_name_en"] if english else row["asset_type_name_ru"]
                ),
            }
            for row in asset_accounts(config.active_database_path())
        }
        period = f"{int(year):04d}-{int(month):02d}"
        balance_details = {
            (row["account_id"], row["currency_code"]): row
            for row in effective_asset_snapshot_month(config.active_database_path(), period)
        }
    unclassified = report_text("Не классифицировано", locale)
    data.insert(
        1,
        "account_id",
        data["account"].map(
            lambda account: account_details.get(account, {}).get("account_id", "")),
    )
    if config.use_sqlite_storage():
        data["source_period"] = [
            balance_details.get((account_id, currency), {}).get("source_period", "")
            for account_id, currency in zip(data["account_id"], data["currency"])
        ]
        data["age_months"] = [
            balance_details.get((account_id, currency), {}).get("age_months", 0)
            for account_id, currency in zip(data["account_id"], data["currency"])
        ]
        data["carried"] = data["source_period"].ne(period)
        english = normalize_locale(locale) == "en"
        data["valuation_status"] = data.apply(
            lambda row: (
                (f"Carried from {row['source_period']} ({row['age_months']} mo.)" if english
                 else f"Перенесено с {row['source_period']} ({row['age_months']} мес.)")
                if row["carried"] else
                (f"Confirmed for {period}" if english else f"Подтверждено за {period}")
            ), axis=1,
        )
    data.insert(
        2,
        "asset_type_id",
        data["account"].map(
            lambda account: account_details.get(account, {}).get(
                "asset_type_id") or UNCLASSIFIED_ASSET_TYPE_VALUE),
    )
    data.insert(
        3,
        "asset_type",
        data["account"].map(
            lambda account: account_details.get(account, {}).get(
                "asset_type_label") or unclassified),
    )
    data["amount_sort"] = data["amount"]
    data = data.sort_values("amount_sort", ascending=False, kind="mergesort")
    data["amount_sort"] = range(len(data), 0, -1)
    data["amount"] = data["amount"].map(_format_input_amount)
    return _dataframe_records(data)


def _statement_asset_preview(
    rows: list[dict],
    balance: dict | None,
    account_id: str | None,
    selected_period: str,
    locale: str = DEFAULT_LOCALE,
    *,
    reset: bool = True,
) -> tuple[list[dict], str, str, bool]:
    annotated = []
    for row in rows:
        clean_row = dict(row)
        if reset:
            clean_row.pop("_statement_balance_status", None)
        annotated.append(clean_row)
    if (
        not balance
        or not account_id
        or not config.use_sqlite_storage()
        or str(balance.get("as_of_date", ""))[:7] != selected_period
    ):
        return annotated, "", "secondary", False

    from src.data.sqlite_store import asset_accounts, asset_snapshot_month

    account = next((
        row for row in asset_accounts(config.active_database_path())
        if row["id"] == account_id and row["active"]
    ), None)
    if account is None:
        return annotated, "", "secondary", False
    currency = str(balance["currency"]).upper()
    current = next((
        row for row in asset_snapshot_month(config.active_database_path(), selected_period)
        if row["account_id"] == account_id and row["currency_code"] == currency
    ), None)
    new_amount = Decimal(str(balance["balance"]))
    new_value = _format_input_amount(new_amount)
    if current is None:
        message = (
            f"No saved {currency} value for {account['name']} in {selected_period}. "
            f"Applying the statement will create a row with {new_value} {currency}."
            if normalize_locale(locale) == "en"
            else f"За {selected_period} у {account['name']} нет значения в {currency}. "
            f"Применение выписки создаст строку {new_value} {currency}."
        )
        return annotated, message, "warning", True

    matched = current["amount"] == new_amount
    status = "matched" if matched else "pending"
    for row in annotated:
        if row.get("account_id") == account_id and row.get("currency") == currency:
            row["_statement_balance_status"] = status
    old_value = _format_input_amount(current["amount"])
    if normalize_locale(locale) == "en":
        message = (
            f"{account['name']}: already matches the statement, {new_value} {currency}."
            if matched
            else f"{account['name']}: current {old_value} → statement {new_value} {currency}."
        )
    else:
        message = (
            f"{account['name']}: уже совпадает с выпиской, {new_value} {currency}."
            if matched
            else f"{account['name']}: сейчас {old_value} → по выписке {new_value} {currency}."
        )
    return annotated, message, "success" if matched else "warning", True


def _statement_asset_previews(
    rows: list[dict], balances: list[dict], account_ids: list[str | None],
    selected_period: str, locale: str = DEFAULT_LOCALE,
) -> tuple[list[dict], object, str, bool]:
    annotated = [
        {key: value for key, value in row.items() if key != "_statement_balance_status"}
        for row in rows
    ]
    messages = []
    colors = []
    for balance, account_id in zip(balances, account_ids):
        annotated, message, color, visible = _statement_asset_preview(
            annotated, balance, account_id, selected_period, locale, reset=False,
        )
        if visible:
            messages.append(html.Li(f"{balance.get('source_file', '')}: {message}"))
            colors.append(color)
    return (
        annotated,
        html.Ul(messages, className="mb-0") if messages else "",
        "warning" if "warning" in colors else "success" if colors else "secondary",
        bool(messages),
    )


def _asset_input_status(year: str, month: str, locale: str = DEFAULT_LOCALE) -> tuple[str, str]:
    period = f"{int(year):04d}-{int(month):02d}"
    if config.use_sqlite_storage():
        from src.data.sqlite_store import asset_accounts, effective_asset_snapshot_month, saved_asset_months

        rows = effective_asset_snapshot_month(config.active_database_path(), period)
        carried = [row for row in rows if row["carried"]]
        if carried:
            age_unit = "mo." if normalize_locale(locale) == "en" else "мес."
            detail = ", ".join(
                f"{row['account_name']} {row['currency_code']} ({row['source_period']}, {row['age_months']} {age_unit})"
                for row in carried
            )
            return (
                f"Provisional total. Carried balances ({len(carried)}): {detail}. Review and Apply to confirm."
                if normalize_locale(locale) == "en" else
                f"Предварительный итог. Перенесённые остатки ({len(carried)}): {detail}. Проверь и нажми «Применить», чтобы подтвердить.",
                "warning",
            )
        pending = [row["name"] for row in asset_accounts(config.active_database_path())
                   if row["active"] and not row["snapshot_count"]]
        if pending:
            return (
                f"Awaiting first valuation (not included in capital): {', '.join(pending)}."
                if normalize_locale(locale) == "en" else
                f"Ожидают первой оценки и не входят в капитал: {', '.join(pending)}.",
                "warning",
            )

        if period in saved_asset_months(config.active_database_path()):
            message = (f"Saved asset snapshot for {period} loaded."
                       if normalize_locale(locale) == "en"
                       else f"Загружен сохранённый снимок активов за {period}.")
            return message, "secondary"

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


def _debt_plan_records() -> list[dict]:
    if not config.use_sqlite_storage():
        return []
    return [{**plan, "status": "Подтверждён" if plan["confirmed_payment_id"] else "План",
             "actual_date": plan["actual_date"] or datetime.now().date().isoformat(),
             "validation_error": ""}
            for plan in list_debt_payment_plans(config.active_database_path())]


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


def _asset_input_column_defs(locale: str = DEFAULT_LOCALE) -> list[dict]:
    currencies = list(config.UNIQUE_TICKERS)
    type_options = _new_asset_type_options(locale)
    type_labels = {item["value"]: item["label"] for item in type_options}
    type_labels[UNCLASSIFIED_ASSET_TYPE_VALUE] = report_text("Не классифицировано", locale)
    asset_type_class_rules = {
        f"finrep-asset-kind-{asset_type_id.replace('_', '-')}": (
            f"params.data && params.data.asset_type_id == '{asset_type_id}'"
        )
        for asset_type_id in (
            "cash", "cash_account", "deposit", "bond", "equity", "fund",
            "crypto", "real_estate", "other",
        )
    }
    asset_type_class_rules["finrep-asset-kind-unclassified"] = (
        "params.data && params.data.account && "
        "(!params.data.asset_type_id || "
        f"params.data.asset_type_id == '{UNCLASSIFIED_ASSET_TYPE_VALUE}')"
    )
    return [
        {"field": "account", "headerName": "Счет", "editable": True, "flex": 2,
         "minWidth": 220, "cellClassRules": asset_type_class_rules},
        {"field": "asset_type_id", "headerName": "Тип актива",
         "editable": {"function": "params.data && !params.data.account_id"},
         "cellEditor": "agSelectCellEditor",
         "cellEditorParams": {"values": [item["value"] for item in type_options]},
         "valueFormatter": {"function": f"({json.dumps(type_labels, ensure_ascii=False)})[params.value] || params.value"},
         "flex": 1.2, "minWidth": 190, "cellClassRules": asset_type_class_rules},
        {
            "field": "amount",
            "headerName": "Сумма",
            "editable": True,
            "cellDataType": "text",
            "flex": 1,
            "minWidth": 160,
            "cellClassRules": {
                "finrep-statement-balance-pending": (
                    "params.data && params.data._statement_balance_status == 'pending'"
                ),
                "finrep-statement-balance-matched": (
                    "params.data && params.data._statement_balance_status == 'matched'"
                ),
            },
        },
        {"field": "currency", "headerName": "Валюта", "editable": True, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": currencies}, "flex": 0.7, "minWidth": 120},
        {"field": "valuation_status", "headerName": "Статус оценки", "editable": False,
         "flex": 1.2, "minWidth": 220,
         "cellClassRules": {"finrep-asset-carried": "params.data && params.data.carried"}},
        {"field": "account_id", "hide": True},
        {"field": "source_period", "hide": True},
        {"field": "age_months", "hide": True},
        {"field": "carried", "hide": True},
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


def _format_input_amount(value) -> str:
    text = format_money_amount(value)
    sign = "-" if text.startswith("-") else ""
    unsigned = text.removeprefix("-")
    integer, separator, fraction = unsigned.partition(".")
    grouped_integer = f"{int(integer):,}".replace(",", " ")
    return f"{sign}{grouped_integer}{separator}{fraction}"


def _kaspi_import_column_defs(locale: str = DEFAULT_LOCALE) -> list[dict]:
    if config.use_sqlite_storage():
        from src.data.sqlite_store import categories as category_registry

        category_rows = [
            row for row in category_registry(config.active_database_path())
            if row["active"] and row["parent_id"] is None
        ]
        income_categories = [
            row["name_ru"] for row in category_rows if row["direction"] == "income"
        ]
        expense_categories = [
            row["name_ru"] for row in category_rows if row["direction"] == "expense"
        ]
    else:
        categories = [option["value"] for option in _transaction_category_options()]
        income_categories = [
            category for category in categories if category in config.NOT_COST_COLS
        ]
        expense_categories = [
            category for category in categories if category not in config.NOT_COST_COLS
        ]
    for label in DEBT_GRID_CATEGORIES:
        direction = DEBT_CATEGORY_ACTIONS[label][1]
        (income_categories if direction == "income" else expense_categories).append(label)
    debt_options = _debt_select_options(None) if config.use_sqlite_storage() else []
    debt_choices = [
        f"{item['value']} | {item['label'].rsplit(' | ', 1)[0]}"
        for item in debt_options if item["value"]
    ]
    category_class_rules = {
        "finrep-category-selected": (
            "params.api.__finrepSelectedColumn == 'category' && "
            "params.api.__finrepCellSelection && "
            "params.api.__finrepCellSelection.has(params.node.id)"
        ),
        "kaspi-category-income": (
            "params.colDef.context.incomeCategories.includes(params.value) && "
            "!params.colDef.context.debtCategories.includes(params.value)"
        ),
        "kaspi-category-debt": "params.colDef.context.debtCategories.includes(params.value)",
        "kaspi-category-saving": "params.value == 'Сбережения' || params.value == 'Инвестиции'",
        "kaspi-category-internal": "params.value == 'Внутренний перевод'",
        "kaspi-category-food": "params.value == 'Пища'",
        "kaspi-category-transport": "params.value == 'Транспорт'",
        "kaspi-category-communication": "params.value == 'Связь'",
        "kaspi-category-other": "params.value == 'Прочее'",
    }
    category_context = {
        "incomeCategories": income_categories,
        "expenseCategories": expense_categories,
        "debtCategories": list(DEBT_CATEGORY_ACTIONS),
        "neutralCategories": [INTERNAL_TRANSFER_CATEGORY],
    }
    manual_source_label = "Manual" if normalize_locale(locale) == "en" else "Вручную"
    return [
        {
            "field": "source",
            "headerName": "Источник",
            "checkboxSelection": True,
            "width": 145,
            "valueFormatter": {
                "function": (
                    f"params.value == 'manual_grid' ? '{manual_source_label}' : "
                    "(params.value == 'kaspi_pdf' ? 'Kaspi PDF' : "
                    "(params.value == 'bcc_pdf' ? 'BCC PDF' : "
                    "(params.value == 'ozon_pdf' ? 'Ozon PDF' : params.value)))"
                )
            },
        },
        {
            "field": "source_file",
            "headerName": "File" if normalize_locale(locale) == "en" else "Файл",
            "width": 220,
        },
        {
            "field": "category",
            "headerName": "Категория",
            "editable": True,
            "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"function": "finrepCategoryEditorParams(params)"},
            "context": category_context,
            "width": 290,
            "cellClassRules": category_class_rules,
        },
        {
            "field": "date",
            "headerName": "Дата",
            "width": 130,
            "editable": {"function": "params.data.source == 'manual_grid'"},
        },
        {
            "field": "amount",
            "headerName": "Сумма",
            "width": 120,
            "editable": {"function": "params.data.source == 'manual_grid'"},
            "cellDataType": "text",
            "cellEditor": "agTextCellEditor",
            "context": category_context,
            "valueFormatter": {
                "function": (
                    "params.data.direction == 'credit' ? '+ ' + params.value : "
                    "(params.data.direction == 'debit' ? '− ' + params.value : params.value)"
                )
            },
        },
        {"field": "import_action", "headerName": "Действие", "editable": True, "cellEditor": "agSelectCellEditor", "cellEditorParams": {"values": ["import", "skip"]}, "width": 120, "cellClassRules": {
            "text-warning": "params.value == 'review'",
            "finrep-action-selected": (
                "params.api.__finrepSelectedColumn == 'import_action' && "
                "params.api.__finrepCellSelection && "
                "params.api.__finrepCellSelection.has(params.node.id)"
            ),
        }},
        {
            "field": "currency",
            "headerName": "Валюта",
            "width": 110,
            "editable": {"function": "params.data.source == 'manual_grid'"},
            "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": list(config.UNIQUE_TICKERS)},
        },
        {"field": "counterparty", "headerName": "Новый контрагент",
         "editable": {"function": "['Возникновение дебиторской задолженности', 'Возникновение кредиторской задолженности', 'Дебиторская задолженность', 'Кредиторская задолженность'].includes(params.data.category)"},
         "width": 190, "cellEditor": "agTextCellEditor"},
        {"field": "debt_id", "headerName": "Погашаемый долг",
         "editable": {"function": "['Погашение дебиторской задолженности', 'Погашение кредиторской задолженности', 'Погашение деб. зад.', 'Погашение кред. зад.'].includes(params.data.category)"},
         "width": 300, "cellEditor": "agSelectCellEditor",
         "cellEditorParams": {"values": ["", *debt_choices]}},
        {"field": "direction", "headerName": "Направление", "hide": True},
        {"field": "bank_status", "headerName": "Статус банка", "hide": True},
        {"field": "comment", "headerName": "Комментарий", "editable": True, "flex": 1, "minWidth": 220},
        {
            "field": "validation_error",
            "headerName": "Проверка",
            "flex": 1,
            "minWidth": 240,
            "cellClassRules": {"text-danger": "Boolean(params.value)"},
        },
        {"field": "skip_reason", "headerName": "Причина skip", "width": 170},
        {"field": "duplicate_in_source", "headerName": "Дубль в CSV", "width": 130},
        {"field": "duplicate_in_staging", "headerName": "Дубль в staging", "width": 150},
        {"field": "details", "headerName": "Детали PDF", "flex": 1, "minWidth": 240},
        {"field": "source_id", "headerName": "ID", "hide": True},
        {"field": "status", "headerName": "Статус", "hide": True},
        {"field": "bank_reference", "headerName": "Reference", "hide": True},
        {"field": "bank_account_id", "headerName": "Счёт банка", "hide": True},
        {"field": "replaces_source_id", "headerName": "Заменяет pending", "hide": True},
        {"field": "possible_pending_match", "headerName": "Несколько pending", "hide": True},
        {"field": "staging_revision", "headerName": "Ревизия staging", "hide": True},
    ]


def _expense_report_layout(datasets: dict[str, DashboardDataset], theme: str, locale: str = DEFAULT_LOCALE):
    children = [
        html.H2(report_text("Аналитика расходов", locale), className="h4"),
        html.P(
            report_text("Вся история. Год и месяц в панели не ограничивают этот отчёт.", locale),
            className="small", style={"color": "var(--finrep-muted)"},
        ),
    ]
    if "expenses_empty" in datasets:
        children.append(html.Div(
            report_text("Нет расходных операций", locale), id="expenses-empty-state",
            className="finrep-first-run",
        ))
    else:
        if "expenses_missing_months" in datasets:
            missing = datasets["expenses_missing_months"]
            children.append(dbc.Alert(
                [missing.title + " " + ", ".join(missing.dataframe["Дата"].dt.strftime("%Y-%m")),
                 html.Div(report_text("Пропуски не считаются нулевыми расходами.", locale))],
                color="warning", id="expenses-missing-months",
            ))
        monthly = datasets["expenses_monthly"]
        chart = _graph_section(monthly, theme=theme, locale=locale)
        children.append(chart)
        allocation = datasets["expenses_allocation"]
        allocation_chart = _graph_section(allocation, theme=theme, locale=locale)
        notes = [html.P(report_text("Доли рассчитаны из сумм расходов внутри каждого месяца.", locale))]
        undefined = allocation.dataframe.loc[allocation.dataframe["Доля, %"].isna(), "Дата"].drop_duplicates()
        if not undefined.empty:
            notes.append(html.Div(str(report_text("Доли не определены: итог месяца отсутствует или не положителен.", locale))
                                  + " " + ", ".join(undefined.dt.strftime("%Y-%m"))))
        if allocation.dataframe["Доля, %"].lt(0).any():
            notes.append(html.Div(report_text("Отрицательные доли отражают корректировки расходов.", locale)))
        allocation_chart.children.insert(1, html.Div(notes, id="expenses-allocation-notes",
                                                    className="small", style={"color": "var(--finrep-muted)"}))
        children.append(allocation_chart)
        total_chart = _graph_section(datasets["expenses_total"], theme=theme, locale=locale)
        total_chart.children.insert(1, html.P(
            report_text("Пунктирные линии — топ-15 покупок за всю историю. Наведите курсор или коснитесь линии, чтобы прочитать комментарий.", locale),
            className="small", style={"color": "var(--finrep-muted)"},
        ))
        total_chart.children.append(html.Div(id="expenses-top-comment", role="status",
                                             style={"whiteSpace": "pre-wrap", "overflowWrap": "anywhere"}))
        children.extend([total_chart, _grid_section(datasets["top_purchases"], height="680px", theme=theme, locale=locale)])
    return html.Div(children, className="d-grid gap-3")


def _income_report_layout(datasets: dict[str, DashboardDataset], theme: str, locale: str = DEFAULT_LOCALE):
    children = [
        html.H2(report_text("Аналитика доходов", locale), className="h4"),
        html.P(
            report_text("Вся история. Год и месяц в панели не ограничивают этот отчёт.", locale),
            className="small", style={"color": "var(--finrep-muted)"},
        ),
    ]
    if "income_empty" in datasets:
        children.append(html.Div(
            report_text("Нет поступлений", locale), id="income-empty-state",
            className="finrep-first-run",
        ))
    else:
        if "income_missing_months" in datasets:
            missing = datasets["income_missing_months"]
            children.append(dbc.Alert(
                [missing.title + " " + ", ".join(missing.dataframe["Дата"].dt.strftime("%Y-%m")),
                 html.Div(report_text("Пропуски не считаются нулевыми поступлениями.", locale))],
                color="warning", id="income-missing-months",
            ))
        children.append(html.P(
            report_text("Старый доход определяется по комментарию; нераспознанный доход остаётся в отдельной группе.", locale),
            className="small", style={"color": "var(--finrep-muted)"},
        ))
        children.append(_graph_section(datasets["income_sources_monthly"], theme=theme, locale=locale))
        allocation = datasets["income_allocation"]
        allocation_chart = _graph_section(allocation, theme=theme, locale=locale)
        notes = [html.P(report_text("Доли рассчитаны из всех поступлений внутри каждого месяца.", locale))]
        undefined = allocation.dataframe.loc[allocation.dataframe["Доля, %"].isna(), "Дата"].drop_duplicates()
        if not undefined.empty:
            notes.append(html.Div(str(report_text("Доли не определены: итог месяца отсутствует или не положителен.", locale))
                                  + " " + ", ".join(undefined.dt.strftime("%Y-%m"))))
        if allocation.dataframe["Доля, %"].lt(0).any():
            notes.append(html.Div(report_text("Отрицательные доли отражают корректировки поступлений.", locale)))
        allocation_chart.children.insert(1, html.Div(notes, id="income-allocation-notes",
                                                    className="small", style={"color": "var(--finrep-muted)"}))
        children.append(allocation_chart)
        children.append(_graph_section(datasets["income_receipts_monthly"], theme=theme, locale=locale))
    return html.Div(children, className="d-grid gap-3")


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


def _capital_section(
    dataset: DashboardDataset,
    *,
    currency: str,
    height: str = "520px",
    theme: str | None = None,
    locale: str = DEFAULT_LOCALE,
):
    section = _graph_section(dataset, height=height, theme=theme, locale=locale)
    if dataset.dataframe.empty or not dataset.dataframe.attrs.get("base_period"):
        return section

    section.children.insert(
        1,
        html.Div(
            [
                html.Div(
                    [
                        html.Label(
                            _i18n_text("dashboard.cpi_base_label"),
                            htmlFor="cpi-base-period-chart",
                            className="finrep-chart-filter-label",
                        ),
                        dcc.Dropdown(
                            id="cpi-base-period-chart",
                            options=_cpi_period_options(currency),
                            value=dataset.dataframe.attrs.get("base_period"),
                            placeholder=tr("dashboard.cpi_base_placeholder", locale),
                            clearable=False,
                            className="finrep-chart-filter",
                        ),
                    ],
                    className="finrep-chart-filter-field",
                ),
                html.Div(
                    _i18n_text("dashboard.cpi_base_help"),
                    className="finrep-chart-filter-help",
                ),
            ],
            id="capital-cpi-control",
            className="finrep-chart-filter-row",
        ),
    )
    return section


def _capital_attribution_section(
    dataset: DashboardDataset,
    *,
    height: str = "520px",
    theme: str | None = None,
    locale: str = DEFAULT_LOCALE,
):
    section = _graph_section(dataset, height=height, theme=theme, locale=locale)
    if dataset.dataframe.empty:
        return section
    section.children.insert(1, html.P(
        report_text(
            "Столбцы полностью сверяются с изменением капитала. Валютная переоценка содержит только эффект курсов на начальные валютные остатки; изменения, которые нельзя надёжно разделить без привязки операций к активам, остаются в необъяснённом остатке.",
            locale,
        ),
        className="small",
        style={"color": "var(--finrep-muted)"},
    ))
    return section


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
                for label in (
                    "Капитал",
                    "Средний доход/мес",
                    "Средний расход/мес",
                    "N мес расходов",
                )
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
    if active_tab == "expenses":
        return build_expense_dashboard_data(currency, fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED)
    if active_tab == "income":
        return build_income_dashboard_data(currency, fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED)
    if active_tab == "statistics":
        return build_statistics_dashboard_data()
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
    if active_tab == "expenses":
        return f"expenses_{dataset.id}_{currency}_{timestamp}.xlsx"
    if active_tab == "income":
        return f"income_{dataset.id}_{currency}_{timestamp}.xlsx"
    if active_tab == "statistics":
        return f"statistics_{dataset.id}_{timestamp}.xlsx"
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
