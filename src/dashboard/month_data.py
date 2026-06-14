import pandas as pd
import plotly.graph_objects as go

from src import config, utils
from src.dashboard.main_data import DashboardDataset, _apply_dashboard_chart_layout
from src.dashboard.year_data import _format_cost_distribution
from src.data.exchange_rates_info import get_exchange_rates_info
from src.data.get import clear_data_cache, get_transactions
from src.data.get_finance import set_fx_network_enabled
from src.data.proccess import convert_transaction
from src.data.staging import ensure_monthly_transaction_csv
from src.model.create_tables import (
    get_act_liabilities,
    get_act_receivables,
    get_assets_by_currencies,
    get_balance_by_month,
    get_month_transactions,
)


def build_month_dashboard_data(
    year: str,
    month: str,
    currency: str,
    fx_network_enabled: bool = True,
) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    year = str(year)
    month = _normalize_month(month)
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    set_fx_network_enabled(fx_network_enabled)
    created_info = ensure_monthly_transaction_csv(year, month)
    if created_info["created"]:
        clear_data_cache()

    transactions = get_month_transactions(currency, year, month)
    fx_info = get_exchange_rates_info(currency)
    summary = _prepare_summary(get_balance_by_month(currency).loc[f"{year}-{month}"].reset_index())
    receivables = get_act_receivables(currency)
    liabilities = get_act_liabilities(currency)
    cost_distribution = _cost_distribution(year, month, currency)
    assets = _prepare_assets(get_assets_by_currencies(year, month))

    return {
        "month_transactions": DashboardDataset(
            id="month_transactions",
            title="Транзакции",
            dataframe=transactions,
            display_dataframe=_format_integer_table(transactions, not_num_cols=["Дата"]),
        ),
        "month_fx_rates": DashboardDataset(
            id="month_fx_rates",
            title="Курсы валют и конвертация",
            dataframe=fx_info,
            display_dataframe=fx_info.copy(deep=True),
        ),
        "month_summary": DashboardDataset(
            id="month_summary",
            title="Суммарные показатели",
            dataframe=summary,
            display_dataframe=_format_summary_metrics(summary, currency),
        ),
        "month_receivables": DashboardDataset(
            id="month_receivables",
            title="Дебиторская задолженность",
            dataframe=receivables,
            display_dataframe=utils.fill_if_empty(_format_money_columns(receivables.copy(deep=True), currency, _debt_text_columns())),
        ),
        "month_liabilities": DashboardDataset(
            id="month_liabilities",
            title="Кредиторская задолженность",
            dataframe=liabilities,
            display_dataframe=utils.fill_if_empty(_format_money_columns(liabilities.copy(deep=True), currency, _debt_text_columns())),
        ),
        "month_cost_distribution": DashboardDataset(
            id="month_cost_distribution",
            title="Распределение расходов",
            dataframe=cost_distribution,
            display_dataframe=_format_cost_distribution(cost_distribution, currency),
        ),
        "month_cost_distribution_chart": DashboardDataset(
            id="month_cost_distribution_chart",
            title="Распределение расходов",
            dataframe=cost_distribution,
            figure=_cost_distribution_figure(cost_distribution),
        ),
        "month_assets": DashboardDataset(
            id="month_assets",
            title="Распределение по счетам",
            dataframe=assets,
            display_dataframe=utils.fill_if_empty(assets.copy(deep=True)),
        ),
    }


def _prepare_summary(data: pd.DataFrame) -> pd.DataFrame:
    order = [
        "Доход",
        "Инвестиции",
        "Сбережения",
        "Дебиторская задолженность",
        "Кредиторская задолженность",
        "Погашение деб. зад.",
        "Погашение кред. зад.",
        "Расход",
        "Баланс",
        "Капитал",
        "Капитал по активам",
        "Валютная переоценка",
        "Расхождение с активами",
        "Дельта",
    ]
    display = data.drop(columns=["Дата"], errors="ignore").copy(deep=True)
    values = display[[column for column in order if column in display.columns]].iloc[0]
    rows = [
        (column, values[column], _summary_metric_status(column, values[column]), "За выбранный месяц", "money")
        for column in values.index
    ]
    return pd.DataFrame(rows, columns=["Показатель", "Значение", "Статус", "Детали", "Тип"])


def _summary_metric_status(name: str, value) -> str:
    number = pd.to_numeric(value, errors="coerce")
    if pd.isna(number):
        return "empty"
    if name in {"Расход", "Кредиторская задолженность", "Расхождение с активами"}:
        return "watch" if number > 0 else "ok"
    if name in {"Дельта", "Баланс", "Валютная переоценка"}:
        return "positive" if number >= 0 else "negative"
    return "ok" if number >= 0 else "negative"


def _format_summary_metrics(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    for index, row in data.iterrows():
        display.loc[index, "Значение"] = utils.process_num_cols(
            pd.DataFrame([{"Значение": row["Значение"]}]),
            not_num_cols=[],
            currency=currency,
        ).iloc[0]["Значение"]
    return display.drop(columns=["Тип"])


def _prepare_assets(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty or "Счет" not in data.columns:
        return data.copy(deep=True)

    display = data.copy(deep=True)
    priority = {"Всего": 0, "Всего в валюте": 1, "Всего в валюте,%": 2}
    display["__priority"] = display["Счет"].map(priority).fillna(3).astype(int)
    if "RUB" in display.columns:
        display["__rub_sort"] = display["RUB"].map(_to_number).fillna(0)
    else:
        display["__rub_sort"] = 0
    display = display.sort_values(
        ["__priority", "__rub_sort"],
        ascending=[True, False],
        kind="mergesort",
    )
    return display.drop(columns=["__priority", "__rub_sort"]).reset_index(drop=True)


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


def _cost_distribution(year: str, month: str, currency: str) -> pd.DataFrame:
    transactions = get_transactions()
    sample = transactions[
        (transactions["Год"].astype(str) == str(year))
        & (transactions["Месяц"].astype(int).astype(str) == str(int(month)))
    ].reset_index(drop=True)
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
    distribution["Среднее"] = distribution["Суммарно"] / 30
    distribution["Процент"] = distribution["Суммарно"] / distribution["Суммарно"].sum() * 100
    return distribution


def _format_money_columns(
    data: pd.DataFrame,
    currency: str,
    not_money_cols: list[str],
) -> pd.DataFrame:
    display = data.copy(deep=True)
    return utils.process_num_cols(display, not_num_cols=not_money_cols, currency=currency)


def _debt_text_columns() -> list[str]:
    return ["ID", "Контрагент", "Дата", "Комментарий", "Валюта долга", "Статус"]


def _format_integer_table(data: pd.DataFrame, not_num_cols: list[str]) -> pd.DataFrame:
    display = data.copy(deep=True)
    for column in display.columns:
        if column in not_num_cols:
            continue
        numeric = pd.to_numeric(display[column], errors="coerce").fillna(0).round().astype(int)
        display[column] = numeric.map(lambda value: f"{value:,}".replace(",", " "))
    return display.fillna(0)


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


def _normalize_month(month: str) -> str:
    return f"{int(month):02d}"
