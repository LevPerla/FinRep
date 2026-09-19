"""Monthly income sources and all receipts from the existing transaction history."""

import pandas as pd
import plotly.graph_objects as go

from src import config
from src.dashboard.chart_style import INCOME_SOURCE_COLORS
from src.dashboard.income_sources import classify_income_transactions
from src.dashboard.main_data import DashboardDataset, _apply_dashboard_chart_layout
from src.data.get import get_transactions
from src.data.get_finance import fx_network_mode
from src.data.proccess import convert_transaction


SOURCE_LABELS = {
    "salary": "Зарплата",
    "deposit_interest": "Проценты по депозиту",
    "unknown": "Источник не определён",
    "savings": "Сбережения",
}


def build_income_dashboard_data(currency: str, fx_network_enabled: bool = False) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    transactions = get_transactions()
    receipts = transactions.loc[
        transactions["Категория"].isin(("Доход", "Сбережения"))
        & transactions["Значение"].ne(0)
    ].copy().reset_index(drop=True)
    if receipts.empty:
        return {"income_empty": DashboardDataset(
            id="income_empty", title="Нет поступлений", dataframe=pd.DataFrame(),
        )}

    receipts["income_source"] = "savings"
    classified = classify_income_transactions(receipts)
    receipts.loc[classified.index, "income_source"] = classified["income_source"]
    with fx_network_mode(fx_network_enabled):
        if not config.DEBUG:
            receipts = convert_transaction(receipts, currency, "Значение", use_current_rate=False)

    known_months = pd.DatetimeIndex(
        transactions["Дата"].dt.to_period("M").dt.to_timestamp().unique()
    ).sort_values()
    months = pd.date_range(known_months.min(), known_months.max(), freq="MS", name="Дата")
    monthly = (
        receipts.assign(Дата=receipts["Дата"].dt.to_period("M").dt.to_timestamp())
        .groupby(["Дата", "income_source"])["Значение"].sum()
        .unstack(fill_value=0)
        .reindex(index=known_months, columns=SOURCE_LABELS, fill_value=0)
        .reindex(months)
    )
    monthly.index.name = "Дата"
    earnings = monthly[["salary", "deposit_interest", "unknown"]].sum(axis=1, min_count=1)
    totals = pd.DataFrame({
        "Дата": months,
        "Доход": earnings.to_numpy(),
        "Сбережения": monthly["savings"].to_numpy(),
        "Всего поступлений": monthly.sum(axis=1, min_count=1).to_numpy(),
    })
    sources = monthly[["salary", "deposit_interest", "unknown"]].rename(columns=SOURCE_LABELS)
    sources = sources.stack(dropna=False).rename("Доход").reset_index().rename(
        columns={"income_source": "Источник"}
    )
    datasets = {
        "income_sources_monthly": DashboardDataset(
            id="income_sources_monthly", title="Доход по источникам за месяц",
            dataframe=sources, figure=_sources_figure(monthly, currency),
        ),
        "income_receipts_monthly": DashboardDataset(
            id="income_receipts_monthly", title="Доход и сбережения за месяц",
            dataframe=totals, figure=_receipts_figure(totals, currency),
        ),
    }
    missing = months.difference(known_months)
    if not missing.empty:
        datasets["income_missing_months"] = DashboardDataset(
            id="income_missing_months", title="Нет данных за месяцы:",
            dataframe=pd.DataFrame({"Дата": missing}),
        )
    return datasets


def _sources_figure(monthly: pd.DataFrame, currency: str) -> go.Figure:
    figure = go.Figure()
    for source in ("salary", "deposit_interest", "unknown"):
        figure.add_bar(
            name=SOURCE_LABELS[source], x=monthly.index, y=monthly[source],
            marker_color=INCOME_SOURCE_COLORS[source],
            hovertemplate=("%{x|%Y-%m}<br>%{y:,.2f} " + config.UNIQUE_TICKERS[currency]
                           + "<extra>%{fullData.name}</extra>"),
        )
    _apply_dashboard_chart_layout(figure, "", range_slider=True)
    figure.update_layout(
        barmode="relative", showlegend=True, bargap=0.15, margin=dict(t=24),
        yaxis=dict(title=currency, separatethousands=True),
        xaxis=dict(type="date", tickformat="%Y-%m"),
    )
    return figure


def _receipts_figure(totals: pd.DataFrame, currency: str) -> go.Figure:
    figure = go.Figure()
    for column, source in (("Доход", "salary"), ("Сбережения", "savings")):
        figure.add_bar(
            name=column, x=totals["Дата"], y=totals[column],
            marker_color=INCOME_SOURCE_COLORS[source],
            hovertemplate=("%{x|%Y-%m}<br>%{y:,.2f} " + config.UNIQUE_TICKERS[currency]
                           + "<extra>%{fullData.name}</extra>"),
        )
    figure.add_scatter(
        name="Всего поступлений", x=totals["Дата"], y=totals["Всего поступлений"],
        mode="lines", line=dict(color="#C28C72", width=2),
        hovertemplate=("%{x|%Y-%m}<br>%{y:,.2f} " + config.UNIQUE_TICKERS[currency]
                       + "<extra>%{fullData.name}</extra>"),
    )
    _apply_dashboard_chart_layout(figure, "", range_slider=True)
    figure.update_layout(
        barmode="relative", showlegend=True, bargap=0.15, margin=dict(t=24),
        yaxis=dict(title=currency, separatethousands=True),
        xaxis=dict(type="date", tickformat="%Y-%m"),
    )
    return figure
