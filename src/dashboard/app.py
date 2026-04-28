import os
from datetime import datetime
from io import BytesIO

from dash import Dash, Input, MATCH, Output, State, dcc, html
import dash_ag_grid as dag
import dash_bootstrap_components as dbc
import pandas as pd
from dash.exceptions import PreventUpdate

from src import config
from src.dashboard.main_data import DashboardDataset, build_main_dashboard_data


DEFAULT_CURRENCY = "RUB"
DEFAULT_FX_NETWORK_ENABLED = False


def create_app() -> Dash:
    app = Dash(
        __name__,
        external_stylesheets=[dbc.themes.BOOTSTRAP],
        title="FinRep Dashboard",
        suppress_callback_exceptions=True,
    )
    app.layout = create_layout()
    register_callbacks(app)
    return app


def create_layout():
    currency_options = [
        {"label": ticker, "value": ticker}
        for ticker in config.UNIQUE_TICKERS.keys()
    ]

    return dbc.Container(
        [
            dbc.Row(
                [
                    dbc.Col(
                        html.H1("FinRep Dashboard", className="h3 mb-0"),
                        xs=12,
                        md=6,
                    ),
                    dbc.Col(
                        dcc.Dropdown(
                            id="dashboard-currency",
                            options=currency_options,
                            value=DEFAULT_CURRENCY,
                            clearable=False,
                        ),
                        xs=12,
                        md=3,
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
                ],
                id="dashboard-tabs",
                active_tab="main",
            ),
            dcc.Loading(
                html.Div(id="dashboard-content", className="py-4"),
                type="circle",
            ),
        ],
        fluid=True,
    )


def register_callbacks(app: Dash) -> None:
    @app.callback(
        Output("dashboard-content", "children"),
        Input("dashboard-currency", "value"),
        Input("dashboard-tabs", "active_tab"),
    )
    def render_dashboard_content(currency: str, active_tab: str):
        if active_tab == "year":
            return _placeholder_report("Годовой отчет")
        if active_tab == "month":
            return _placeholder_report("Месячный отчет")

        datasets = build_main_dashboard_data(
            currency,
            fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
        )
        return html.Div(
            [
                _grid_section(datasets["yearly_stats"], height="300px"),
                _grid_section(datasets["fx_rates"], height="260px"),
                _graph_section(datasets["income_expense"]),
                _graph_section(datasets["delta"]),
                _graph_section(datasets["capital"]),
            ],
            className="d-grid gap-4",
        )

    @app.callback(
        Output({"type": "dataset-download", "dataset_id": MATCH}, "data"),
        Input({"type": "dataset-download-button", "dataset_id": MATCH}, "n_clicks"),
        State({"type": "dataset-download-button", "dataset_id": MATCH}, "id"),
        State("dashboard-currency", "value"),
        prevent_initial_call=True,
    )
    def download_dataset(n_clicks: int, button_id: dict, currency: str):
        if not n_clicks:
            raise PreventUpdate

        dataset_id = button_id["dataset_id"]
        datasets = build_main_dashboard_data(
            currency,
            fx_network_enabled=DEFAULT_FX_NETWORK_ENABLED,
        )
        if dataset_id not in datasets:
            raise PreventUpdate

        dataset = datasets[dataset_id]
        filename = _download_filename(dataset, currency)
        return dcc.send_bytes(_dataframe_to_xlsx_bytes(dataset.dataframe, dataset.title), filename)


def _placeholder_report(title: str):
    return html.Section(
        [
            html.H2(title, className="h5 mb-2"),
            html.Div("Этот отчет будет добавлен после MVP основного отчета.", className="text-muted"),
        ],
        className="py-4",
    )


def _graph_section(dataset: DashboardDataset, height: str = "520px"):
    return html.Section(
        [
            _section_header(dataset),
            dcc.Graph(
                id=f"{dataset.id}-graph",
                figure=dataset.figure,
                responsive=True,
                style={"height": height},
                config={"displaylogo": False, "responsive": True, "scrollZoom": True},
            ),
        ]
    )


def _grid_section(dataset: DashboardDataset, height: str = "360px"):
    data = dataset.display_dataframe if dataset.display_dataframe is not None else dataset.dataframe
    return html.Section(
        [
            _section_header(dataset),
            dag.AgGrid(
                id=f"{dataset.id}-grid",
                rowData=data.to_dict("records"),
                columnDefs=[{"field": column} for column in data.columns],
                defaultColDef={
                    "sortable": True,
                    "filter": True,
                    "resizable": True,
                },
                columnSize="sizeToFit",
                dashGridOptions={
                    "pagination": True,
                    "paginationPageSize": 12,
                },
                className="ag-theme-alpine",
                style={"height": height, "width": "100%"},
            ),
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


def _download_filename(dataset: DashboardDataset, currency: str) -> str:
    timestamp = datetime.now().strftime("%Y%m%d")
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
    app.run(host=host, port=port, debug=debug)
