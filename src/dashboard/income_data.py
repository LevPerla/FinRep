"""Monthly income sources and all receipts from the existing transaction history."""

import pandas as pd
import plotly.graph_objects as go

from src import config
from src.dashboard.chart_style import income_category_color
from src.dashboard.income_sources import classify_income_transactions
from src.dashboard.main_data import DashboardDataset, _apply_dashboard_chart_layout
from src.data.get import get_income_categories, get_transactions
from src.data.get_finance import fx_network_mode
from src.data.proccess import convert_transaction


CLASSIFIED_SOURCE_LABELS = {
    "salary": "Зарплата",
    "deposit_interest": "Проценты",
    "unknown": "Доход без категории",
}


def build_income_dashboard_data(currency: str, fx_network_enabled: bool = False) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    transactions = get_transactions()
    registry = get_income_categories()
    category_classes = registry.set_index("Категория")["Класс"].to_dict()
    income_categories = list(category_classes)
    accepted_categories = {*income_categories, "Доход", "Сбережения"}
    receipts = transactions.loc[
        transactions["Категория"].isin(accepted_categories)
        & transactions["Значение"].ne(0)
    ].copy().reset_index(drop=True)
    if receipts.empty:
        return {"income_empty": DashboardDataset(
            id="income_empty", title="Нет поступлений", dataframe=pd.DataFrame(),
        )}

    receipts["income_source"] = receipts["Категория"].replace({
        "Доход": config.UNCLASSIFIED_INCOME_LABEL,
        "Сбережения": "Прочие доходы",
    })
    classified = classify_income_transactions(receipts)
    receipts.loc[classified.index, "income_source"] = classified["income_source"].map(
        CLASSIFIED_SOURCE_LABELS)
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
        .reindex(index=known_months, columns=income_categories, fill_value=0)
        .reindex(months)
    )
    monthly.index.name = "Дата"
    active = _sum_class(monthly, category_classes, "active")
    passive = _sum_class(monthly, category_classes, "passive")
    unclassified = _sum_class(monthly, category_classes, "unclassified")
    total_income = monthly.sum(axis=1, min_count=1)
    totals = pd.DataFrame({
        "Дата": months,
        "Активный доход": active.to_numpy(),
        "Пассивный доход": passive.to_numpy(),
        "Доход без категории": unclassified.to_numpy(),
        "Всего доходов": total_income.to_numpy(),
        "Доля пассивного дохода, %": passive.div(total_income.where(total_income.gt(0))).mul(100).to_numpy(),
    })
    sources = monthly.stack(dropna=False).rename("Доход").reset_index().rename(
        columns={"income_source": "Источник"}
    )
    datasets = {
        "income_sources_monthly": DashboardDataset(
            id="income_sources_monthly", title="Доход по источникам за месяц",
            dataframe=sources, figure=_sources_figure(monthly, currency),
        ),
        "income_receipts_monthly": DashboardDataset(
            id="income_receipts_monthly", title="Активный и пассивный доход за месяц",
            dataframe=totals, figure=_receipts_figure(totals, currency),
        ),
    }
    datasets["income_allocation"] = _allocation_dataset(monthly, currency)
    missing = months.difference(known_months)
    if not missing.empty:
        datasets["income_missing_months"] = DashboardDataset(
            id="income_missing_months", title="Нет данных за месяцы:",
            dataframe=pd.DataFrame({"Дата": missing}),
        )
    return datasets


def _sources_figure(monthly: pd.DataFrame, currency: str) -> go.Figure:
    figure = go.Figure()
    for source in monthly.columns:
        figure.add_bar(
            name=source, x=monthly.index, y=monthly[source],
            marker_color=income_category_color(source),
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
    for column, color in (
        ("Активный доход", income_category_color("Зарплата")),
        ("Пассивный доход", income_category_color("Проценты")),
        ("Доход без категории", income_category_color(config.UNCLASSIFIED_INCOME_LABEL)),
    ):
        figure.add_bar(
            name=column, x=totals["Дата"], y=totals[column],
            marker_color=color,
            hovertemplate=("%{x|%Y-%m}<br>%{y:,.2f} " + config.UNIQUE_TICKERS[currency]
                           + "<extra>%{fullData.name}</extra>"),
        )
    figure.add_scatter(
        name="Доля пассивного дохода, %", x=totals["Дата"],
        y=totals["Доля пассивного дохода, %"], yaxis="y2",
        mode="lines", line=dict(color="#C28C72", width=2),
        hovertemplate="%{x|%Y-%m}<br>%{y:,.2f}%<extra>%{fullData.name}</extra>",
    )
    _apply_dashboard_chart_layout(figure, "", range_slider=True)
    figure.update_layout(
        barmode="relative", showlegend=True, bargap=0.15, margin=dict(t=24),
        yaxis=dict(title=currency, separatethousands=True),
        yaxis2=dict(title="%", overlaying="y", side="right", rangemode="tozero"),
        xaxis=dict(type="date", tickformat="%Y-%m"),
    )
    return figure


def _allocation_dataset(monthly: pd.DataFrame, currency: str) -> DashboardDataset:
    totals = monthly.sum(axis=1, min_count=1)
    shares = monthly.div(totals.where(totals.gt(0)), axis=0).mul(100)
    data = monthly.stack(dropna=False).rename("Сумма").to_frame()
    data["Доля, %"] = shares.stack(dropna=False)
    data = data.reset_index().rename(columns={"income_source": "Источник"})
    figure = go.Figure()
    for source in monthly.columns:
        figure.add_bar(
            name=source, x=monthly.index, y=shares[source],
            marker_color=income_category_color(source),
            customdata=monthly[[source]].values,
            hovertemplate=("%{x|%Y-%m}<br>%{y:,.2f}%<br>%{customdata[0]:,.2f} "
                           + config.UNIQUE_TICKERS[currency] + "<extra>%{fullData.name}</extra>"),
        )
    _apply_dashboard_chart_layout(figure, "", range_slider=True)
    figure.update_layout(
        barmode="relative", showlegend=True, margin=dict(t=24),
        yaxis=dict(ticksuffix="%", range=None if shares.lt(0).any().any() else [0, 100]),
        xaxis=dict(type="date", tickformat="%Y-%m"),
    )
    return DashboardDataset(
        id="income_allocation", title="Аллокация доходов по месяцам",
        dataframe=data, figure=figure,
    )


def _sum_class(monthly: pd.DataFrame, classes: dict[str, str], income_class: str) -> pd.Series:
    columns = [category for category, value in classes.items() if value == income_class]
    if not columns:
        return pd.Series(0.0, index=monthly.index)
    return monthly[columns].sum(axis=1, min_count=1)
