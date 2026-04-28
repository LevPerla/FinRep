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
        ),
        "delta": DashboardDataset(
            id="delta",
            title="Delta",
            dataframe=delta,
            figure=_delta_figure(delta),
        ),
        "capital": DashboardDataset(
            id="capital",
            title="Capital",
            dataframe=capital,
            figure=_capital_figure(capital),
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


def _income_expense_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=data["Дата"],
            y=data["Доход"],
            mode="lines+markers",
            name="Доход",
            line=dict(color="royalblue", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=data["Дата"],
            y=data["Расход"],
            mode="lines+markers",
            name="Расход",
            line=dict(color="firebrick", width=2),
        )
    )
    _apply_dashboard_chart_layout(fig, "Динамика доходов и расходов", range_slider=True)
    return fig


def _delta_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Bar(
            x=data["Дата"],
            y=data["Дельта"],
            name="Дельта",
            hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}<extra></extra>",
        )
    )
    _apply_dashboard_chart_layout(fig, "Дельты", range_slider=True)
    return fig


def _capital_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=data["Дата"],
            y=data["Капитал"],
            mode="lines+markers",
            name="Капитал",
            line=dict(color="green", width=2),
        )
    )
    _apply_dashboard_chart_layout(fig, "Динамика капитала", range_slider=True)
    return fig


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
