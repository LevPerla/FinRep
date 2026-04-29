import os
from datetime import datetime
from io import BytesIO
from urllib.parse import parse_qs

from dash import Dash, Input, MATCH, Output, State, ctx, dcc, html
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash.exceptions import PreventUpdate

from src import config
from src.dashboard.export import export_dashboard_page
from src.dashboard.main_data import DashboardDataset, build_main_dashboard_data
from src.dashboard.month_data import build_month_dashboard_data
from src.dashboard.planning_data import build_planning_dashboard_data
from src.dashboard.year_data import build_year_dashboard_data
from src import utils


DEFAULT_CURRENCY = "RUB"
DEFAULT_YEAR = datetime.now().strftime("%Y")
DEFAULT_MONTH = datetime.now().strftime("%m")
DEFAULT_FX_NETWORK_ENABLED = False


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
        external_stylesheets=[dbc.themes.BOOTSTRAP],
        title="FinRep Dashboard",
        suppress_callback_exceptions=True,
    )
    app.index_string = _app_index_string()
    app.layout = create_layout()
    register_callbacks(app)
    return app


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
            dbc.Tabs(
                [
                    dbc.Tab(label="Основной отчет", tab_id="main"),
                    dbc.Tab(label="Годовой отчет", tab_id="year"),
                    dbc.Tab(label="Месячный отчет", tab_id="month"),
                    dbc.Tab(label="План и прогноз", tab_id="planning"),
                ],
                id="dashboard-tabs",
                active_tab="main",
            ),
            dcc.Loading(
                html.Div(id="dashboard-content", className="py-4"),
                type="circle",
            ),
        ],
        id="dashboard-shell",
        className="finrep-shell finrep-theme-dark",
        fluid=True,
        style=_theme_shell_style("dark"),
    )


def register_callbacks(app: Dash) -> None:
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
        if tab not in {"main", "year", "month", "planning"}:
            tab = "main"
        return currency, year, month, tab

    @app.callback(
        Output("dashboard-content", "children"),
        Input("dashboard-currency", "value"),
        Input("dashboard-year", "value"),
        Input("dashboard-month", "value"),
        Input("dashboard-tabs", "active_tab"),
        Input("dashboard-theme", "data"),
    )
    def render_dashboard_content(currency: str, year: str, month: str, active_tab: str, theme: str):
        if active_tab == "year":
            try:
                datasets = build_year_dashboard_data(
                    year,
                    currency,
                    fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
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
                    fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
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
                    fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
                )
            except Exception as exc:
                return _error_state("Не удалось загрузить данные месячного отчета.", exc)

            _apply_theme_to_datasets(datasets, theme)
            return _month_report_layout(datasets, theme)

        try:
            datasets = build_main_dashboard_data(
                currency,
                fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
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
            dbc.Row(
                [
                    dbc.Col(_grid_section(datasets["year_capital_by_month"], height="560px", theme=theme), xs=12, lg=4),
                    dbc.Col(_graph_section(datasets["year_capital_chart"], height="560px", theme=theme), xs=12, lg=8),
                ],
                className="g-4",
            ),
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
            dbc.Row(
                [
                    dbc.Col(_grid_section(datasets["planning_fx_scenarios"], height="320px", theme=theme), xs=12, lg=5),
                    dbc.Col(_graph_section(datasets["planning_fx_scenarios"], height="360px", theme=theme), xs=12, lg=7),
                ],
                className="g-4",
            ),
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
            dbc.Row(
                [
                    dbc.Col(_grid_section(datasets["month_receivables"], height="300px", theme=theme), xs=12, lg=6),
                    dbc.Col(_grid_section(datasets["month_liabilities"], height="300px", theme=theme), xs=12, lg=6),
                ],
                className="g-4",
            ),
            _graph_section(datasets["month_cost_distribution_chart"], theme=theme),
            _grid_section(datasets["month_cost_distribution"], height="520px", theme=theme),
            _grid_section(datasets["month_assets"], height="1120px", theme=theme),
        ],
        className="d-grid gap-4",
    )


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
            dag.AgGrid(
                id=f"{dataset.id}-grid",
                rowData=_grid_row_data(dataset, data),
                columnDefs=_grid_column_defs(dataset, data, theme),
                defaultColDef={
                    "sortable": True,
                    "filter": True,
                    "resizable": True,
                },
                columnSize="autoSize" if dataset.id in {"fx_rates", "year_fx_rates", "month_fx_rates"} else "sizeToFit",
                dashGridOptions={
                    "pagination": False,
                },
                className="ag-theme-alpine-dark" if theme == "dark" else "ag-theme-alpine",
                style={"height": height, "width": "100%"},
            ),
        ],
        style=_section_style(theme),
    )


def _grid_row_data(dataset: DashboardDataset, data: pd.DataFrame) -> list[dict]:
    records = data.to_dict("records")
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

    simple_level_tables = {
        "year_income_by_month": {"Доход": ("__monthly_income_level", "green")},
        "year_cost_by_month": {"Расход": ("__monthly_cost_level", "red")},
        "year_capital_by_month": {"Капитал": ("__monthly_capital_level", "green")},
    }
    if dataset.id in simple_level_tables:
        _add_level_metadata(
            records,
            raw,
            {column: field for column, (field, _palette) in simple_level_tables[dataset.id].items()},
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
        },
        "month_assets": _month_asset_column_styles(data, theme),
        "year_income_by_month": {"Доход": _level_style("__monthly_income_level", "green", theme)},
        "year_cost_by_month": {"Расход": _level_style("__monthly_cost_level", "red", theme)},
        "year_capital_by_month": {"Капитал": _level_style("__monthly_capital_level", "green", theme)},
        "planning_goals": {
            "Отклонение": _sign_style("__planning_delta_sign", theme),
            "Прогресс (%)": _level_style("__planning_progress_level", "green", theme),
        },
        "planning_fx_scenarios": {
            "Капитал": _level_style("__planning_fx_capital_level", "blue", theme),
            "Изменение капитала": _sign_style("__planning_fx_delta_sign", theme),
        },
    }
    column_styles = style_by_dataset.get(dataset.id, {})
    return [
        {"field": column, "cellStyle": column_styles.get(column, _plain_cell_style(theme))}
        for column in data.columns
    ]


def _plain_cell_style(theme: str | None = None) -> dict:
    return {"backgroundColor": "#2b2b2b", "color": "#a9b7c6"} if theme == "dark" else {}


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
