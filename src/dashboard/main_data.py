from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

from src import config, utils
from src.data.exchange_rates_info import get_exchange_rates_info
from src.data.get_finance import set_fx_network_enabled
from src.model.create_tables import get_balance_by_month


CHART_FONT_SIZE = 16
CHART_TITLE_SIZE = 20
CHART_LABEL_SIZE = 9


@dataclass(frozen=True)
class DashboardDataset:
    id: str
    title: str
    dataframe: pd.DataFrame
    display_dataframe: pd.DataFrame | None = None
    figure: go.Figure | None = None
    graph_config: dict | None = None


def build_main_dashboard_data(
    currency: str,
    fx_network_enabled: bool = True,
) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    set_fx_network_enabled(fx_network_enabled)
    balance = get_balance_by_month(currency)

    yearly_stats = _create_yearly_stats(balance)
    income_expense = balance[["Доход", "Расход"]].reset_index()
    delta = balance[["Дельта"]].reset_index()
    capital = balance[["Капитал"]].reset_index()
    fx_info = get_exchange_rates_info(currency)

    return {
        "yearly_stats": DashboardDataset(
            id="yearly_stats",
            title="Yearly Stats",
            dataframe=yearly_stats,
            display_dataframe=_format_money_columns(
                yearly_stats,
                currency,
                not_money_cols=["Год", "Процент дохода"],
            ),
        ),
        "fx_rates": DashboardDataset(
            id="fx_rates",
            title="FX Rates",
            dataframe=fx_info,
            display_dataframe=fx_info.copy(deep=True),
        ),
        "income_expense": DashboardDataset(
            id="income_expense",
            title="Income and Expense",
            dataframe=income_expense,
            figure=_income_expense_figure(income_expense),
            graph_config={"scrollZoom": False},
        ),
        "delta": DashboardDataset(
            id="delta",
            title="Delta",
            dataframe=delta,
            figure=_delta_figure(delta, currency),
        ),
        "capital": DashboardDataset(
            id="capital",
            title="Capital",
            dataframe=capital,
            figure=_capital_figure(capital, currency),
        ),
    }


def _create_yearly_stats(balance: pd.DataFrame) -> pd.DataFrame:
    yearly_stats = balance[["Доход", "Расход"]].resample("Y").sum()
    yearly_stats.index = yearly_stats.index.strftime("%Y")
    yearly_stats["Сальдо"] = yearly_stats["Доход"] - yearly_stats["Расход"]
    yearly_stats.loc["Всего"] = yearly_stats.sum(axis=0)
    yearly_stats["Процент дохода"] = (
        yearly_stats["Доход"] / yearly_stats["Расход"] * 100
    ).round(2)
    yearly_stats = yearly_stats.reset_index().rename(columns={"Дата": "Год"})
    total = yearly_stats[yearly_stats["Год"] == "Всего"]
    by_year = yearly_stats[yearly_stats["Год"] != "Всего"].sort_values("Год", ascending=False)
    return pd.concat([total, by_year], ignore_index=True)


def _format_money_columns(
    data: pd.DataFrame,
    currency: str,
    not_money_cols: list[str],
) -> pd.DataFrame:
    display = data.copy(deep=True)
    display["Процент дохода"] = display["Процент дохода"].astype(str) + "%"
    return utils.process_num_cols(display, not_num_cols=not_money_cols, currency=currency)


def _month_start_dates(data: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(data["Дата"]).dt.to_period("M").dt.to_timestamp()


def _income_expense_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    x_dates = _month_start_dates(data)
    fig.add_trace(
        go.Scatter(
            x=x_dates,
            y=data["Доход"],
            mode="lines+markers",
            name="Доход",
            line=dict(color="royalblue", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x_dates,
            y=data["Расход"],
            mode="lines+markers",
            name="Расход",
            line=dict(color="firebrick", width=2),
        )
    )
    _apply_dashboard_chart_layout(fig, "Динамика доходов и расходов", range_slider=True)
    return fig


def _delta_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure(
        go.Bar(
            x=_month_start_dates(data),
            y=data["Дельта"],
            name="Дельта",
            hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}<extra></extra>",
        )
    )
    _apply_dashboard_chart_layout(fig, "Дельты", range_slider=True)
    fig.update_layout(annotations=_important_delta_annotations(data, currency, max_labels=6))
    return fig


def _capital_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=_month_start_dates(data),
            y=data["Капитал"],
            mode="lines+markers+text",
            name="Капитал",
            text=_sparse_money_labels(data["Капитал"], currency, max_labels=7),
            textposition="top center",
            line=dict(color="green", width=2),
        )
    )
    _apply_dashboard_chart_layout(fig, "Динамика капитала", range_slider=True)
    max_value = pd.to_numeric(data["Капитал"], errors="coerce").max()
    if pd.notna(max_value) and max_value > 0:
        fig.update_layout(
            margin=dict(l=70, r=30, t=105, b=55),
            yaxis=dict(range=[0, max_value * 1.18], tickfont=dict(size=CHART_FONT_SIZE)),
        )
    return fig


def _sparse_money_labels(values: pd.Series, currency: str, max_labels: int = 7) -> list[str]:
    if values.empty:
        return []

    count = len(values)
    label_count = min(max_labels, count)
    min_gap = max(2, count // max(label_count + 1, 1))
    if label_count <= 1:
        label_indexes = {count - 1}
    else:
        candidates = [round(index * (count - 1) / (label_count - 1)) for index in range(label_count)]
        label_indexes = []
        for candidate in candidates:
            if not label_indexes or candidate - label_indexes[-1] >= min_gap:
                label_indexes.append(candidate)
        label_indexes = [index for index in label_indexes if count - 1 - index >= min_gap]
        label_indexes.append(count - 1)
        label_indexes = set(label_indexes)

    symbol = config.UNIQUE_TICKERS[currency]
    labels = []
    for index, value in enumerate(values):
        if index not in label_indexes or pd.isna(value):
            labels.append("")
            continue
        labels.append(f"{value:,.0f}".replace(",", " ") + symbol)
    return labels


def _important_money_labels(values: pd.Series, currency: str, max_labels: int = 7) -> list[str]:
    if values.empty:
        return []

    numeric = pd.to_numeric(values, errors="coerce")
    label_indexes = set(numeric.abs().nlargest(min(max_labels, len(numeric))).index)
    label_indexes.add(numeric.index[-1])
    symbol = config.UNIQUE_TICKERS[currency]

    labels = []
    for index, value in numeric.items():
        if index not in label_indexes or pd.isna(value):
            labels.append("")
            continue
        labels.append(f"{value:,.0f}".replace(",", " ") + symbol)
    return labels


def _important_delta_annotations(data: pd.DataFrame, currency: str, max_labels: int = 6) -> list[dict]:
    if data.empty or "Дельта" not in data:
        return []

    values = pd.to_numeric(data["Дельта"], errors="coerce")
    important_indexes = set(values.abs().nlargest(min(max_labels, len(values))).index)
    important_indexes.add(values.index[-1])
    symbol = config.UNIQUE_TICKERS[currency]
    annotations = []

    for index in sorted(important_indexes):
        value = values.loc[index]
        if pd.isna(value):
            continue
        label = f"{value:,.0f}".replace(",", " ") + symbol
        annotations.append(
            dict(
                x=pd.to_datetime(data.loc[index, "Дата"]).to_period("M").to_timestamp(),
                y=value,
                text=label,
                showarrow=True,
                arrowhead=1,
                arrowsize=0.8,
                arrowwidth=1,
                arrowcolor="#5f6bff",
                ax=0,
                ay=-28 if value >= 0 else 28,
                font=dict(size=12, color="#243b63"),
                bgcolor="rgba(255,255,255,0.82)",
                bordercolor="rgba(36,59,99,0.18)",
                borderwidth=1,
                borderpad=3,
            )
        )
    return annotations


def _apply_dashboard_chart_layout(fig: go.Figure, title: str, range_slider: bool = False) -> None:
    xaxis = dict(
        tickfont=dict(size=CHART_FONT_SIZE),
        fixedrange=False,
    )
    if range_slider:
        xaxis.update(
            rangeslider=dict(visible=True, thickness=0.08),
        )

    fig.update_layout(
        title=dict(text=title, font=dict(size=CHART_TITLE_SIZE)),
        autosize=True,
        dragmode="zoom",
        font=dict(size=CHART_FONT_SIZE),
        margin=dict(l=70, r=30, t=70, b=55),
        xaxis=xaxis,
        yaxis=dict(tickfont=dict(size=CHART_FONT_SIZE)),
        legend=dict(font=dict(size=CHART_FONT_SIZE)),
        uniformtext=dict(minsize=CHART_LABEL_SIZE, mode="show"),
    )
