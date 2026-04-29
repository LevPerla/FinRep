import numpy as np
import pandas as pd
import plotly.graph_objects as go

from src import config, utils
from src.dashboard.main_data import DashboardDataset, _apply_dashboard_chart_layout
from src.data.exchange_rates_info import get_exchange_rates_info
from src.data.get import get_transactions
from src.data.get_finance import set_fx_network_enabled
from src.data.proccess import convert_transaction
from src.model.create_tables import get_balance_by_month


def build_year_dashboard_data(
    year: str,
    currency: str,
    fx_network_enabled: bool = True,
) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    year = str(year)
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    set_fx_network_enabled(fx_network_enabled)
    balance = get_balance_by_month(currency)
    year_balance = balance.loc[year]

    quarter_stats = _quarter_stats(year_balance)
    cost_distribution = _cost_distribution(year, currency)
    income_by_month = year_balance[["Доход"]].reset_index()
    cost_by_month = year_balance[["Расход"]].reset_index()
    income_cost_stats = _income_cost_stats(year_balance)
    capital_by_month = year_balance[["Капитал"]].reset_index()
    fx_info = get_exchange_rates_info(currency)

    return {
        "year_quarter_stats": DashboardDataset(
            id="year_quarter_stats",
            title="Итоги по кварталам",
            dataframe=quarter_stats,
            display_dataframe=_format_money_columns(
                quarter_stats,
                currency,
                not_money_cols=["Квартал"],
            ),
        ),
        "year_fx_rates": DashboardDataset(
            id="year_fx_rates",
            title="Курсы валют и конвертация",
            dataframe=fx_info,
            display_dataframe=fx_info.copy(deep=True),
        ),
        "year_cost_distribution": DashboardDataset(
            id="year_cost_distribution",
            title="Распределение расходов",
            dataframe=cost_distribution,
            display_dataframe=_format_cost_distribution(cost_distribution, currency),
        ),
        "year_cost_distribution_chart": DashboardDataset(
            id="year_cost_distribution_chart",
            title="Распределение расходов",
            dataframe=cost_distribution,
            figure=_cost_distribution_figure(cost_distribution),
        ),
        "year_income_expense": DashboardDataset(
            id="year_income_expense",
            title="Динамика доходов/расходов",
            dataframe=income_by_month.merge(cost_by_month, on="Дата", how="outer"),
            figure=_income_expense_figure(income_by_month, cost_by_month),
        ),
        "year_income_by_month": DashboardDataset(
            id="year_income_by_month",
            title="Доходы по месяцам",
            dataframe=income_by_month,
            display_dataframe=_format_date_columns(
                _format_money_columns(income_by_month, currency, ["Дата"]),
                ["Дата"],
            ),
        ),
        "year_cost_by_month": DashboardDataset(
            id="year_cost_by_month",
            title="Расходы по месяцам",
            dataframe=cost_by_month,
            display_dataframe=_format_date_columns(
                _format_money_columns(cost_by_month, currency, ["Дата"]),
                ["Дата"],
            ),
        ),
        "year_income_cost_stats": DashboardDataset(
            id="year_income_cost_stats",
            title="Описательные статистики",
            dataframe=income_cost_stats,
            display_dataframe=_format_money_columns(income_cost_stats, currency, ["Статистика"]),
        ),
        "year_capital_by_month": DashboardDataset(
            id="year_capital_by_month",
            title="Капитал по месяцам",
            dataframe=capital_by_month,
            display_dataframe=_format_date_columns(
                _format_money_columns(capital_by_month, currency, ["Дата"]),
                ["Дата"],
            ),
        ),
        "year_capital_chart": DashboardDataset(
            id="year_capital_chart",
            title="Динамика капитала",
            dataframe=capital_by_month,
            figure=_capital_figure(capital_by_month),
        ),
    }


def _quarter_stats(year_balance: pd.DataFrame) -> pd.DataFrame:
    quarter_stats = (
        year_balance[["Доход", "Расход"]]
        .resample("Q")
        .sum()
        .reset_index()
        .rename(
            columns={
                "Доход": "Общий доход",
                "Расход": "Общий расход",
                "Дата": "Квартал",
            }
        )
    )
    quarter_stats["Квартал"] = quarter_stats["Квартал"].dt.quarter.astype(str)
    quarter_stats["Сальдо"] = quarter_stats["Общий доход"] - quarter_stats["Общий расход"]
    total = pd.DataFrame(
        [
            {
                "Квартал": "Всего",
                "Общий доход": quarter_stats["Общий доход"].sum(),
                "Общий расход": quarter_stats["Общий расход"].sum(),
                "Сальдо": quarter_stats["Сальдо"].sum(),
            }
        ]
    )
    return pd.concat([quarter_stats, total], ignore_index=True)


def _cost_distribution(year: str, currency: str) -> pd.DataFrame:
    transactions = get_transactions()
    sample = transactions[transactions["Год"].astype(str) == str(year)].reset_index(drop=True)
    if not config.DEBUG:
        sample = convert_transaction(sample, to_curr=currency, target_col="Значение")

    costs = sample[~sample["Категория"].isin(config.NOT_COST_COLS)]
    if costs.empty:
        return pd.DataFrame(columns=["Категория", "Суммарно", "Среднее", "Процент"])

    distribution = (
        costs.groupby("Категория", as_index=False)["Значение"]
        .sum()
        .rename(columns={"Значение": "Суммарно"})
        .sort_values("Суммарно", ascending=False)
    )
    month_count = max(sample["Месяц"].astype(int).max(), 1)
    distribution["Среднее"] = distribution["Суммарно"] / month_count
    distribution["Процент"] = distribution["Суммарно"] / distribution["Суммарно"].sum() * 100
    return distribution


def _income_cost_stats(year_balance: pd.DataFrame) -> pd.DataFrame:
    stats = (
        year_balance[["Доход", "Расход"]]
        .agg(
            {
                "Доход": ["sum", "mean", np.median, np.std, np.min, np.max],
                "Расход": ["sum", "mean", np.median, np.std, np.min, np.max],
            },
            axis=0,
        )
        .rename(
            index={
                "sum": "Сумма",
                "mean": "Среднее",
                "median": "Медиана",
                "std": "Ст. отклонение",
                "amin": "Минимум",
                "amax": "Максимум",
            }
        )
        .reset_index()
        .rename(columns={"index": "Статистика"})
    )
    return stats


def _format_money_columns(
    data: pd.DataFrame,
    currency: str,
    not_money_cols: list[str],
) -> pd.DataFrame:
    display = data.copy(deep=True)
    return utils.process_num_cols(display, not_num_cols=not_money_cols, currency=currency)


def _format_date_columns(data: pd.DataFrame, date_cols: list[str]) -> pd.DataFrame:
    display = data.copy(deep=True)
    for column in date_cols:
        if column in display.columns:
            display[column] = pd.to_datetime(display[column], errors="coerce").dt.strftime("%Y-%m-%d")
    return display


def _format_cost_distribution(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    if display.empty:
        return display
    for column in ["Суммарно", "Среднее"]:
        display[column] = (
            display[column].astype(float).map("{:,.2f}".format).str.replace(",", " ")
            + config.UNIQUE_TICKERS[currency]
        )
    display["Процент"] = display["Процент"].astype(float).map("{:,.2f}%".format)
    return display


def _month_start_dates(data: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(data["Дата"]).dt.to_period("M").dt.to_timestamp()


def _cost_distribution_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Bar(
            x=data["Категория"],
            y=data["Суммарно"],
            name="Суммарно",
            hovertemplate="%{x}<br>%{y:,.0f}<extra></extra>",
        )
    )
    _apply_dashboard_chart_layout(fig, "Распределение расходов")
    return fig


def _income_expense_figure(income: pd.DataFrame, cost: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    income_dates = _month_start_dates(income)
    cost_dates = _month_start_dates(cost)
    fig.add_trace(
        go.Scatter(
            x=income_dates,
            y=income["Доход"],
            mode="lines+markers",
            name="Доход",
            line=dict(color="royalblue", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=cost_dates,
            y=cost["Расход"],
            mode="lines+markers",
            name="Расход",
            line=dict(color="firebrick", width=2),
        )
    )
    _apply_dashboard_chart_layout(fig, "Динамика доходов/расходов")
    return fig


def _capital_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=_month_start_dates(data),
            y=data["Капитал"],
            mode="lines+markers",
            name="Капитал",
            line=dict(color="green", width=2),
        )
    )
    _apply_dashboard_chart_layout(fig, "Динамика капитала")
    return fig
