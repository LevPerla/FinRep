import pandas as pd
import plotly.graph_objects as go
from plotly.colors import qualitative

from src import config
from src.dashboard.main_data import DashboardDataset, _apply_dashboard_chart_layout
from src.data.get import get_transactions
from src.data.get_finance import fx_network_mode
from src.data.proccess import convert_transaction


def build_expense_dashboard_data(
    currency: str,
    fx_network_enabled: bool = False,
) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    transactions = get_transactions()
    expenses = transactions.loc[
        ~transactions["Категория"].isin(config.NOT_COST_COLS)
        & transactions["Значение"].ne(0)
    ].copy().reset_index(drop=True)
    if expenses.empty:
        return {
            "expenses_empty": DashboardDataset(
                id="expenses_empty",
                title="Нет расходных операций",
                dataframe=pd.DataFrame(),
            )
        }

    with fx_network_mode(fx_network_enabled):
        if not config.DEBUG:
            expenses = convert_transaction(
                expenses, currency, "Значение", use_current_rate=False,
            )

    # Zero-filled CSV cells still identify observed months; absent months do not.
    known_months = pd.DatetimeIndex(
        transactions["Дата"].dt.to_period("M").dt.to_timestamp().unique()
    ).sort_values()
    months = pd.date_range(known_months.min(), known_months.max(), freq="MS", name="Дата")
    expenses["Дата"] = expenses["Дата"].dt.to_period("M").dt.to_timestamp()
    monthly = (
        expenses.groupby(["Дата", "Категория"])["Значение"]
        .sum()
        .unstack(fill_value=0)
        .reindex(known_months, fill_value=0)
        .reindex(months)
    )
    data = monthly.stack(dropna=False).rename("Расход").reset_index()
    figure = go.Figure()
    for index, category in enumerate(monthly.columns):
        figure.add_bar(
            name=category,
            x=monthly.index,
            y=monthly[category],
            marker_color=qualitative.Dark24[index % len(qualitative.Dark24)],
            hovertemplate=(
                "%{x|%Y-%m}<br>%{y:,.2f} "
                + config.UNIQUE_TICKERS[currency]
                + "<extra>%{fullData.name}</extra>"
            ),
        )
    title = "Расходы по категориям за месяц"
    _apply_dashboard_chart_layout(figure, "", range_slider=True)
    figure.update_layout(
        barmode="relative",
        showlegend=False,
        bargap=0.15,
        margin=dict(t=24),
        yaxis=dict(title=currency, separatethousands=True),
        xaxis=dict(type="date", tickformat="%Y-%m"),
    )
    datasets = {
        "expenses_monthly": DashboardDataset(
            id="expenses_monthly", title=title, dataframe=data, figure=figure,
        ),
    }
    datasets.update(_annual_expense_datasets(monthly, known_months, currency))
    missing_months = months.difference(known_months)
    if not missing_months.empty:
        datasets["expenses_missing_months"] = DashboardDataset(
            id="expenses_missing_months",
            title="Нет данных за месяцы:",
            dataframe=pd.DataFrame({"Дата": missing_months}),
        )
    return datasets


def _annual_expense_datasets(monthly, known_months, currency):
    annual = monthly.groupby(monthly.index.year).sum(min_count=1)
    annual.index.name = "Год"
    totals = annual.sum(axis=1, min_count=1)
    shares = annual.div(totals.where(totals.gt(0)), axis=0).mul(100)
    coverage = pd.Series(known_months.year).value_counts().reindex(annual.index, fill_value=0)
    data = annual.stack(dropna=False).rename("Расход").to_frame()
    data["Доля, %"] = shares.stack(dropna=False)
    data = data.reset_index()
    data["Месяцев с данными"] = data["Год"].map(coverage)
    dates = pd.to_datetime(annual.index.astype(str) + "-01-01")
    figure = go.Figure()
    for index, category in enumerate(annual.columns):
        figure.add_bar(
            name=category, x=dates, y=shares[category],
            marker_color=qualitative.Dark24[index % len(qualitative.Dark24)],
            customdata=annual[[category]].values,
            hovertemplate=("%{x|%Y}<br>%{y:,.2f}%<br>%{customdata[0]:,.2f} "
                           + config.UNIQUE_TICKERS[currency] + "<extra>%{fullData.name}</extra>"),
        )
    _apply_dashboard_chart_layout(figure, "", range_slider=True)
    figure.update_layout(
        barmode="relative", showlegend=False, margin=dict(t=24),
        yaxis=dict(ticksuffix="%", range=None if shares.lt(0).any().any() else [0, 100]),
        xaxis=dict(type="date", tickformat="%Y", dtick="M12"),
    )
    return {
        "expenses_allocation": DashboardDataset(
            id="expenses_allocation", title="Аллокация расходов по годам",
            dataframe=data, figure=figure,
        ),
        "expenses_year_coverage": DashboardDataset(
            id="expenses_year_coverage", title="Полнота годовых данных",
            dataframe=pd.DataFrame({"Год": annual.index, "Месяцев с данными": coverage.values,
                                    "Расход": totals.values}),
        ),
    }
