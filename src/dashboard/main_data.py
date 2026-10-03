from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache

import pandas as pd
import plotly.graph_objects as go

from src import config, utils
from src.data.get import get_assets, get_transactions
from src.data.exchange_rates_info import get_exchange_rates_info
from src.data.get_finance import fx_network_mode, get_fx_rates, require_fx_rate
from src.data.proccess import convert_transaction
from src.model.create_tables import (
    asset_valuation_dates,
    carry_forward_asset_snapshots,
    get_balance_by_month,
)


CHART_FONT_SIZE = 13
CHART_TITLE_SIZE = 18
CHART_LABEL_SIZE = 9
ASSET_ALLOCATION_COLORS = [
    "#6F8FB8",
    "#7FAF91",
    "#9B7AAE",
    "#B08A6C",
    "#6EA6A6",
    "#A86F7A",
    "#8D985F",
    "#C4A35A",
]

COCKPIT_STATUS_LABELS = {
    "assets": "Источник: активы",
    "cash-flow": "Источник: денежный поток",
    "ok": "В норме",
    "empty": "Нет данных",
    "positive": "Положительный",
    "negative": "Отрицательный",
    "strong": "Высокий уровень",
    "watch": "Стоит проверить",
    "thin": "Низкий уровень",
    "review": "Требует сверки",
    "stale": "Оценки устарели",
}


@dataclass(frozen=True)
class DashboardDataset:
    id: str
    title: str
    dataframe: pd.DataFrame
    display_dataframe: pd.DataFrame | None = None
    figure: go.Figure | None = None
    graph_config: dict | None = None


def clear_main_dashboard_cache() -> None:
    _asset_currency_allocation_data_cached.cache_clear()
    _asset_liquidity_allocation_data_cached.cache_clear()


def build_main_dashboard_data(
    currency: str,
    fx_network_enabled: bool = True,
    year: str | None = None,
    month: str | None = None,
    cpi_base_period: str | None = None,
) -> dict[str, DashboardDataset]:
    with fx_network_mode(fx_network_enabled):
        return _build_main_dashboard_data(currency, year, month, cpi_base_period)


def _build_main_dashboard_data(
    currency: str,
    year: str | None,
    month: str | None,
    cpi_base_period: str | None,
) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    balance = get_balance_by_month(currency)

    asset_freshness = _current_asset_freshness()
    cockpit_metrics = _cockpit_metrics(
        balance, currency, year, month, asset_freshness=asset_freshness)
    yearly_stats = _create_yearly_stats(balance)
    income_expense = balance[["Доход", "Расход"]].reset_index()
    delta = balance[["Дельта"]].reset_index()
    savings_rate = _savings_rate_data(balance)
    capital_columns = [
        column
        for column in ["Капитал", "Капитал по активам", "Расхождение с активами"]
        if column in balance.columns
    ]
    capital = balance[capital_columns].reset_index()
    inflation_rate = _inflation_rate_data()
    real_asset_capital = _real_asset_capital_data(balance, currency, cpi_base_period)
    fx_revaluation = _fx_revaluation_data(balance)
    asset_currency_allocation = _asset_currency_allocation_data(currency)
    asset_liquidity_allocation = _asset_liquidity_allocation_data(currency)
    fx_info = get_exchange_rates_info(currency)
    fx_changes = _fx_changes_data(balance, currency)

    return {
        "cockpit_metrics": DashboardDataset(
            id="cockpit_metrics",
            title="Ключевые метрики",
            dataframe=cockpit_metrics,
            display_dataframe=_format_cockpit_metrics(cockpit_metrics, currency),
        ),
        "yearly_stats": DashboardDataset(
            id="yearly_stats",
            title="Итоги по годам",
            dataframe=yearly_stats,
            display_dataframe=_format_money_columns(
                yearly_stats,
                currency,
                not_money_cols=["Год", "Процент дохода"],
            ),
        ),
        "fx_rates": DashboardDataset(
            id="fx_rates",
            title="Курсы валют",
            dataframe=fx_info,
            display_dataframe=fx_info.copy(deep=True),
        ),
        "income_expense": DashboardDataset(
            id="income_expense",
            title="Доходы и расходы",
            dataframe=income_expense,
            figure=_income_expense_figure(income_expense, currency),
            graph_config={"scrollZoom": False},
        ),
        "delta": DashboardDataset(
            id="delta",
            title="Денежный поток",
            dataframe=delta,
            figure=_delta_figure(delta, currency),
        ),
        "savings_rate": DashboardDataset(
            id="savings_rate",
            title="Норма сбережений",
            dataframe=savings_rate,
            figure=_savings_rate_figure(savings_rate),
        ),
        "capital": DashboardDataset(
            id="capital",
            title="Динамика капитала",
            dataframe=capital,
            figure=_capital_figure(capital, currency),
        ),
        "inflation_rate": DashboardDataset(
            id="inflation_rate",
            title="Официальная инфляция",
            dataframe=inflation_rate,
            figure=_inflation_rate_figure(inflation_rate),
        ),
        "real_asset_capital": DashboardDataset(
            id="real_asset_capital",
            title="Покупательная способность активов",
            dataframe=real_asset_capital,
            figure=_real_asset_capital_figure(real_asset_capital, currency),
        ),
        "fx_revaluation": DashboardDataset(
            id="fx_revaluation",
            title="Валютная переоценка",
            dataframe=fx_revaluation,
            figure=_fx_revaluation_figure(fx_revaluation, currency),
        ),
        "asset_currency_allocation": DashboardDataset(
            id="asset_currency_allocation",
            title="Валютная структура активов",
            dataframe=asset_currency_allocation,
            figure=_asset_currency_allocation_figure(asset_currency_allocation),
        ),
        "asset_liquidity_allocation": DashboardDataset(
            id="asset_liquidity_allocation",
            title="Ликвидность активов",
            dataframe=asset_liquidity_allocation,
            figure=_asset_liquidity_allocation_figure(asset_liquidity_allocation),
        ),
        "fx_changes": DashboardDataset(
            id="fx_changes",
            title="Изменение курсов валют",
            dataframe=fx_changes,
            figure=_fx_changes_figure(fx_changes, currency),
        ),
    }


def _cockpit_metrics(
    balance: pd.DataFrame,
    currency: str,
    year: str | None,
    month: str | None,
    asset_freshness: dict | None = None,
) -> pd.DataFrame:
    columns = ["ID", "Показатель", "Значение", "Статус", "Детали", "Тип"]
    if balance.empty:
        return pd.DataFrame(columns=columns)

    selected_row, selected_period, is_selected_month = _selected_balance_row(balance, year, month)
    sorted_balance = balance.sort_index()
    latest_row = sorted_balance.tail(1).iloc[0]
    latest_period = pd.to_datetime(sorted_balance.index[-1]).to_period("M")
    selected_period_available = not selected_row.empty
    current_capital = _latest_number(balance, "Капитал по активам")
    capital_source = "assets"
    capital_label = "Капитал по активам"
    capital_detail = "Последний доступный снимок активов"
    runway_label = "Финансовый запас по активам"
    if pd.isna(current_capital):
        current_capital = _latest_number(balance, "Капитал")
        capital_source = "cash-flow"
        capital_label = "Капитал по денежному потоку"
        capital_detail = "Накопленный денежный поток за доступную историю"
        runway_label = "Финансовый запас по денежному потоку"

    freshness_warning = bool(
        capital_source == "assets" and asset_freshness
        and asset_freshness.get("has_warning"))
    freshness_detail = ""
    if freshness_warning:
        freshness_detail = (
            f"; устаревших оценок: {asset_freshness['stale_count']}, "
            f"без даты: {asset_freshness['missing_count']}"
        )

    income = _row_number(selected_row, "Доход") if selected_period_available else pd.NA
    expense = _row_number(selected_row, "Расход") if selected_period_available else pd.NA
    delta = _row_number(selected_row, "Дельта") if selected_period_available else pd.NA
    avg_expense = float(pd.to_numeric(balance["Расход"].tail(12), errors="coerce").mean())
    runway_months = current_capital / avg_expense if avg_expense > 0 else pd.NA
    savings_rate = _bounded_percent(delta / income * 100) if pd.notna(income) and income > 0 else pd.NA
    asset_gap = _row_number(latest_row, "Расхождение с активами")
    fx_impact = _row_number(selected_row, "Валютная переоценка") if selected_period_available else pd.NA
    period_label = str(selected_period)
    period_detail = (
        "выбранный месяц"
        if is_selected_month
        else "последний доступный месяц" if selected_period_available else "нет сохранённых данных"
    )
    latest_period_suffix = f"; данные на {latest_period}" if not selected_period_available else ""
    avg_expense_label = f"{avg_expense:,.0f}".replace(",", " ") + config.UNIQUE_TICKERS[currency]
    month_detail = f"{period_label}, {period_detail}"
    cash_flow_detail = (
        f"{period_label}: доход минус расход"
        if selected_period_available
        else f"{period_label}: {period_detail}"
    )
    expense_detail = (
        f"{period_label}, средний расход за 12 месяцев: {avg_expense_label}"
        if selected_period_available
        else month_detail
    )
    savings_rate_detail = (
        f"{period_label}: денежный поток / доход"
        if selected_period_available
        else f"{period_label}: {period_detail}"
    )
    fx_detail = (
        f"{period_label}: изменение стоимости из-за курсов валют"
        if selected_period_available
        else f"{period_label}: {period_detail}"
    )

    rows = [
        (
            "capital",
            capital_label,
            current_capital,
            "stale" if freshness_warning else capital_source,
            f"{capital_detail}{latest_period_suffix}{freshness_detail}",
            "money",
        ),
        (
            "monthly_income",
            "Доход месяца",
            income,
            "empty" if pd.isna(income) or income <= 0 else "ok",
            month_detail,
            "money",
        ),
        (
            "monthly_expense",
            "Расход месяца",
            expense,
            "empty" if pd.isna(expense) else "watch" if expense > avg_expense * 1.2 and avg_expense > 0 else "ok",
            expense_detail,
            "money",
        ),
        (
            "monthly_cash_flow",
            "Денежный поток месяца",
            delta,
            "empty" if pd.isna(delta) else "positive" if delta >= 0 else "negative",
            cash_flow_detail,
            "money",
        ),
        (
            "savings_rate",
            "Норма сбережений",
            savings_rate,
            _savings_rate_status(savings_rate),
            savings_rate_detail,
            "percent",
        ),
        (
            "runway",
            runway_label,
            runway_months,
            _runway_status(runway_months),
            f"{capital_label} / средний расход за последние 12 месяцев"
            f"{latest_period_suffix}{freshness_detail}",
            "months",
        ),
        (
            "asset_gap",
            "Расхождение с активами",
            asset_gap,
            _asset_gap_status(asset_gap, current_capital),
            f"Последний снимок активов минус капитал по денежному потоку{latest_period_suffix}",
            "money",
        ),
        (
            "monthly_fx_revaluation",
            "Валютная переоценка месяца",
            fx_impact,
            "empty" if pd.isna(fx_impact) else "positive" if fx_impact >= 0 else "negative",
            fx_detail,
            "money",
        ),
    ]
    result = pd.DataFrame(rows, columns=columns)
    result.attrs["selected_period"] = period_label
    result.attrs["selected_period_available"] = selected_period_available
    result.attrs["latest_period"] = str(latest_period)
    result.attrs["asset_freshness"] = asset_freshness
    return result


def _current_asset_freshness() -> dict | None:
    if not config.use_sqlite_storage():
        return None
    from src.data.asset_freshness import evaluate_asset_freshness
    from src.data.sqlite_store import asset_accounts

    return evaluate_asset_freshness(asset_accounts(config.active_database_path()))


def _selected_balance_row(
    balance: pd.DataFrame,
    year: str | None,
    month: str | None,
) -> tuple[pd.Series, pd.Period, bool]:
    monthly = balance.sort_index()
    if year and month:
        try:
            selected_period = pd.Period(f"{int(year):04d}-{int(month):02d}", freq="M")
            periods = pd.to_datetime(monthly.index, errors="coerce").to_period("M")
            matches = monthly[periods == selected_period]
            if not matches.empty:
                return matches.iloc[-1], selected_period, True
            return pd.Series(dtype=object), selected_period, False
        except (TypeError, ValueError):
            pass
    latest_period = pd.to_datetime(monthly.index[-1]).to_period("M")
    return monthly.iloc[-1], latest_period, False


def _latest_number(data: pd.DataFrame, column: str):
    if column not in data.columns:
        return pd.NA
    values = pd.to_numeric(data[column], errors="coerce").dropna()
    return float(values.iloc[-1]) if not values.empty else pd.NA


def _row_number(row: pd.Series, column: str) -> float:
    value = row.get(column, 0.0) if not row.empty else 0.0
    parsed = pd.to_numeric(value, errors="coerce")
    return float(parsed) if pd.notna(parsed) else 0.0


def _savings_rate_status(value) -> str:
    if pd.isna(value):
        return "empty"
    if value >= 30:
        return "strong"
    if value >= 0:
        return "ok"
    return "negative"


def _bounded_percent(value):
    if pd.isna(value):
        return pd.NA
    # Intentional display range requested by the owner: extreme savings rates
    # (e.g. -1000%) should not distort the chart scale. Keep the 0–100% clamp.
    return min(max(float(value), 0.0), 100.0)


def _runway_status(value) -> str:
    if pd.isna(value):
        return "empty"
    if value >= 12:
        return "strong"
    if value >= 6:
        return "watch"
    return "thin"


def _asset_gap_status(asset_gap: float, capital) -> str:
    if pd.isna(capital) or capital == 0:
        return "empty"
    return "ok" if abs(asset_gap) <= abs(float(capital)) * 0.03 else "review"


def _format_cockpit_metrics(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    display["Значение"] = display["Значение"].astype(object)
    for index, row in data.iterrows():
        value = row["Значение"]
        if row["Тип"] == "money":
            display.loc[index, "Значение"] = _format_money_value(value, currency)
        elif row["Тип"] == "percent":
            display.loc[index, "Значение"] = "не рассчитано" if pd.isna(value) else f"{float(value):,.1f}%".replace(",", " ")
        elif row["Тип"] == "months":
            display.loc[index, "Значение"] = "не рассчитано" if pd.isna(value) else f"{float(value):,.1f} мес.".replace(",", " ")
    display = display.rename(columns={"Статус": "Статус ID"})
    display["Статус"] = display["Статус ID"].map(COCKPIT_STATUS_LABELS).fillna(display["Статус ID"])
    return display.drop(columns=["Тип"])


def _format_money_value(value, currency: str) -> str:
    if pd.isna(value):
        return "не рассчитано"
    return f"{float(value):,.2f}".replace(",", " ") + config.UNIQUE_TICKERS[currency]


def _create_yearly_stats(balance: pd.DataFrame) -> pd.DataFrame:
    yearly_stats = balance[["Доход", "Расход"]].resample("Y").sum()
    yearly_stats["Сальдо"] = yearly_stats["Доход"] - yearly_stats["Расход"]
    if "Валютная переоценка" in balance.columns:
        yearly_stats["Валютная переоценка"] = balance["Валютная переоценка"].resample("Y").sum(min_count=1)
    if "Расхождение с активами" in balance.columns:
        yearly_stats["Расхождение с активами"] = balance["Расхождение с активами"].resample("Y").last()
    yearly_stats.index = yearly_stats.index.strftime("%Y")
    yearly_stats.loc["Всего"] = yearly_stats.sum(axis=0)
    if "Расхождение с активами" in yearly_stats.columns:
        latest_gap = balance["Расхождение с активами"].dropna()
        yearly_stats.loc["Всего", "Расхождение с активами"] = latest_gap.iloc[-1] if not latest_gap.empty else None
    yearly_stats["Процент дохода"] = (
        yearly_stats["Доход"] / yearly_stats["Расход"] * 100
    ).round(2)
    yearly_stats = yearly_stats.reset_index().rename(columns={"Дата": "Год"})
    total = yearly_stats[yearly_stats["Год"] == "Всего"]
    by_year = yearly_stats[yearly_stats["Год"] != "Всего"].sort_values("Год", ascending=False)
    return pd.concat([total, by_year], ignore_index=True)


def _top_purchases_data(currency: str, year: str | None = None, limit: int = 15) -> pd.DataFrame:
    transactions = get_transactions()
    if year is not None:
        transactions = transactions[transactions["Год"].astype(str) == str(year)]
    purchases = transactions[~transactions["Категория"].isin(config.NOT_COST_COLS)].copy(deep=True)
    if not config.DEBUG:
        purchases = convert_transaction(purchases, to_curr=currency, target_col="Значение")

    return _rank_top_purchases(purchases, limit)


def _rank_top_purchases(purchases: pd.DataFrame, limit: int = 15) -> pd.DataFrame:
    """Rank already converted expense operations, retaining their actual dates."""
    purchases = purchases.copy(deep=True)
    purchases["Значение"] = pd.to_numeric(purchases["Значение"], errors="coerce")
    purchases = purchases[purchases["Значение"].gt(0)]
    purchases = purchases.sort_values(["Значение", "Дата"], ascending=[False, False], kind="stable").head(limit).reset_index(drop=True)
    purchases.insert(0, "№", purchases.index + 1)
    return purchases[["№", "Дата", "Категория", "Значение", "Комментарий"]].rename(
        columns={"Значение": "Сумма"}
    )


def _format_top_purchases(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    display["Дата"] = pd.to_datetime(display["Дата"], errors="coerce").dt.strftime("%Y-%m-%d")
    display["Комментарий"] = display["Комментарий"].fillna("—").astype(str)
    return utils.process_num_cols(
        display,
        not_num_cols=["№", "Дата", "Категория", "Комментарий"],
        currency=currency,
    )


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


def _income_expense_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure()
    x_dates = _month_start_dates(data)
    fig.add_trace(
        go.Scatter(
            x=x_dates,
            y=data["Доход"],
            mode="lines+markers+text",
            name="Доход",
            text=_peak_money_labels(data["Доход"], currency, max_labels=6),
            textposition="top center",
            line=dict(color="royalblue", width=2),
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x_dates,
            y=data["Расход"],
            mode="lines+markers+text",
            name="Расход",
            text=_peak_money_labels(data["Расход"], currency, max_labels=6),
            textposition="bottom center",
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
            name="Доход минус расход",
            hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}<extra></extra>",
        )
    )
    _apply_dashboard_chart_layout(fig, "Денежный поток", range_slider=True)
    fig.update_layout(annotations=_important_delta_annotations(data, currency, max_labels=6))
    return fig


def _savings_rate_data(balance: pd.DataFrame) -> pd.DataFrame:
    data = balance[["Доход", "Дельта"]].reset_index()
    income = pd.to_numeric(data["Доход"], errors="coerce")
    delta = pd.to_numeric(data["Дельта"], errors="coerce")
    # Keep the owner's 0–100% display range so extreme rates do not distort the chart.
    data["Норма сбережений"] = (delta / income * 100).where(income > 0).clip(lower=0, upper=100)
    return data[["Дата", "Норма сбережений"]]


def _savings_rate_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure(
        go.Scatter(
            x=_month_start_dates(data),
            y=data["Норма сбережений"],
            mode="lines+markers",
            name="Норма сбережений",
            hovertemplate="%{x|%Y-%m}<br>%{y:.1f}%<extra></extra>",
            line=dict(color="seagreen", width=2),
        )
    )
    fig.add_hline(y=0, line_dash="dot", line_color="rgba(120,120,120,0.7)")
    fig.add_hline(y=30, line_dash="dash", line_color="rgba(46,139,87,0.55)")
    _apply_dashboard_chart_layout(fig, "Динамика нормы сбережений", range_slider=True)
    fig.update_yaxes(ticksuffix="%", range=[0, 100])
    return fig


def _capital_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure()
    x_dates = _month_start_dates(data)
    fig.add_trace(
        go.Scatter(
            x=x_dates,
            y=data["Капитал"],
            mode="lines+markers+text",
            name="Капитал по денежному потоку",
            text=_sparse_money_labels(data["Капитал"], currency, max_labels=7),
            textposition="top center",
            line=dict(color="green", width=2),
        )
    )
    if "Капитал по активам" in data.columns:
        fig.add_trace(
            go.Scatter(
                x=x_dates,
                y=data["Капитал по активам"],
                mode="lines+markers",
                name="Капитал по активам",
                line=dict(color="royalblue", width=2),
                connectgaps=False,
            )
        )
    _apply_dashboard_chart_layout(fig, "Динамика капитала", range_slider=True)
    max_value = pd.to_numeric(data[["Капитал", "Капитал по активам"]].stack(), errors="coerce").max() if "Капитал по активам" in data.columns else pd.to_numeric(data["Капитал"], errors="coerce").max()
    if pd.notna(max_value) and max_value > 0:
        fig.update_layout(
            margin=dict(l=70, r=30, t=76, b=55),
            yaxis=dict(range=[0, max_value * 1.18], tickfont=dict(size=CHART_FONT_SIZE)),
        )
    return fig


def _real_asset_capital_data(
    balance: pd.DataFrame,
    currency: str,
    base_period: str | None,
) -> pd.DataFrame:
    columns = ["Дата", "Номинальная стоимость", "Реальная стоимость"]
    if "Капитал по активам" not in balance.columns or not config.use_sqlite_storage():
        result = pd.DataFrame(columns=columns)
        result.attrs["status"] = "unavailable"
        return result

    from src.data.inflation import effective_cpi_indexes

    indexes = effective_cpi_indexes(config.active_database_path(), currency)
    if not indexes:
        result = pd.DataFrame(columns=columns)
        result.attrs["status"] = "missing"
        result.attrs["currency"] = currency
        return result
    selected_base = base_period if base_period in indexes else max(indexes)
    latest_cpi_period = max(indexes)
    current_period = pd.Period(pd.Timestamp.now(), freq="M")
    latest_period_value = pd.Period(latest_cpi_period, freq="M")
    stale = current_period.ordinal - latest_period_value.ordinal > 3
    base_index = indexes[selected_base]
    nominal = pd.to_numeric(balance["Капитал по активам"], errors="coerce")
    data = pd.DataFrame({"Дата": pd.to_datetime(balance.index), "Номинальная стоимость": nominal})
    periods = data["Дата"].dt.to_period("M").astype(str)
    data["Реальная стоимость"] = [
        float(Decimal(str(value)) * base_index / indexes[period])
        if pd.notna(value) and period in indexes else float("nan")
        for value, period in zip(data["Номинальная стоимость"], periods)
    ]
    data = data[data["Номинальная стоимость"].notna()].reset_index(drop=True)
    missing = sorted({
        period for value, period in zip(data["Номинальная стоимость"],
                                        data["Дата"].dt.to_period("M").astype(str))
        if pd.notna(value) and period not in indexes
    })
    data.attrs["status"] = "partial" if missing else "stale" if stale else "ready"
    data.attrs["base_period"] = selected_base
    data.attrs["missing_periods"] = missing
    data.attrs["latest_cpi_period"] = latest_cpi_period
    data.attrs["stale"] = stale
    data.attrs["currency"] = currency
    return data


def _inflation_rate_data() -> pd.DataFrame:
    columns = ["Дата", *config.UNIQUE_TICKERS]
    if not config.use_sqlite_storage():
        return pd.DataFrame(columns=columns)

    from src.data.inflation import effective_cpi_indexes

    indexes_by_currency = {
        currency: effective_cpi_indexes(config.active_database_path(), currency)
        for currency in config.UNIQUE_TICKERS
    }
    observed_periods = [
        period for indexes in indexes_by_currency.values() for period in indexes
    ]
    if not observed_periods:
        return pd.DataFrame(columns=columns)
    periods = pd.period_range(min(observed_periods), max(observed_periods), freq="M")
    rows = []
    for period in periods:
        period_key = str(period)
        previous_key = str(period - 12)
        row = {"Дата": period.to_timestamp()}
        for currency, indexes in indexes_by_currency.items():
            current = indexes.get(period_key)
            previous = indexes.get(previous_key)
            row[currency] = (
                float((current / previous - Decimal("1")) * Decimal("100"))
                if current is not None and previous is not None else float("nan")
            )
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def _inflation_rate_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    currency_columns = [column for column in config.UNIQUE_TICKERS if column in data]
    values = data[currency_columns].apply(pd.to_numeric, errors="coerce") \
        if currency_columns else pd.DataFrame(index=data.index)
    valid_rows = values.notna().any(axis=1)
    visible = data.loc[valid_rows.idxmax():valid_rows[::-1].idxmax()] \
        if valid_rows.any() else data
    for index, currency in enumerate(currency_columns):
        series = pd.to_numeric(visible[currency], errors="coerce")
        if not series.notna().any():
            continue
        fig.add_trace(go.Scatter(
            x=visible.get("Дата", []),
            y=series,
            mode="lines+markers",
            name=currency,
            line=dict(color=ASSET_ALLOCATION_COLORS[index], width=2),
            connectgaps=False,
            showlegend=True,
            hovertemplate=(
                f"{currency}<br>%{{x|%Y-%m}}<br>%{{y:.2f}}%<extra></extra>"),
        ))
    _apply_dashboard_chart_layout(fig, "", range_slider=True)
    fig.update_layout(showlegend=True)
    fig.update_yaxes(title="Инфляция, %", ticksuffix="%", rangemode="normal")
    return fig


def _real_asset_capital_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure()
    if data.empty:
        _apply_dashboard_chart_layout(fig, "", range_slider=True)
        return fig
    base_period = data.attrs.get("base_period", "")
    fig.add_trace(go.Scatter(
        x=data["Дата"], y=data["Номинальная стоимость"], mode="lines+markers",
        name="Номинальная стоимость активов", line=dict(color="royalblue", width=2),
        hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}<extra></extra>",
    ))
    fig.add_trace(go.Scatter(
        x=data["Дата"], y=data["Реальная стоимость"], mode="lines+markers",
        name=f"В ценах {base_period}", line=dict(color="#B08A6C", width=2),
        connectgaps=False,
        hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}<extra></extra>",
    ))
    # The section header already names the chart. Keeping the same long title inside
    # Plotly makes it collide with the horizontal legend on narrow screens.
    _apply_dashboard_chart_layout(fig, "", range_slider=True)
    fig.update_layout(yaxis_title=config.UNIQUE_TICKERS[currency])
    return fig


def _fx_revaluation_data(balance: pd.DataFrame) -> pd.DataFrame:
    if "Валютная переоценка" not in balance.columns:
        return pd.DataFrame(columns=["Дата", "Валютная переоценка"])
    return balance[["Валютная переоценка"]].reset_index()


def _fx_revaluation_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    values = pd.to_numeric(data.get("Валютная переоценка", pd.Series(dtype=float)), errors="coerce")
    colors = ["#4f714b" if value >= 0 else "#704444" for value in values.fillna(0)]
    fig = go.Figure(
        go.Bar(
            x=_month_start_dates(data) if "Дата" in data else [],
            y=values,
            name="Валютная переоценка",
            marker_color=colors,
            hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}<extra></extra>",
        )
    )
    _apply_dashboard_chart_layout(fig, "Валютная переоценка", range_slider=True)
    fig.update_layout(yaxis_title=config.UNIQUE_TICKERS[currency])
    return fig


def _fx_changes_data(balance: pd.DataFrame, currency: str) -> pd.DataFrame:
    if balance.empty:
        return pd.DataFrame(columns=["Дата"])

    start = pd.Timestamp(balance.index.min()).normalize()
    end = pd.Timestamp(balance.index.max()).normalize()
    result = pd.DataFrame({"Дата": pd.date_range(start, end, freq="M")})

    for from_currency in config.UNIQUE_TICKERS:
        if from_currency == currency:
            continue
        rates = get_fx_rates(from_currency, currency, start, end)
        if rates.empty:
            result[from_currency] = pd.NA
            continue
        values = pd.to_numeric(rates.iloc[:, 0], errors="coerce").resample("M").last()
        result = result.merge(
            values.rename(from_currency).reset_index().rename(columns={"index": "Дата"}),
            on="Дата",
            how="left",
        )
    return result


def _asset_currency_allocation_data(currency: str) -> pd.DataFrame:
    return _asset_currency_allocation_data_cached(str(config.active_data_path()), str(currency).upper()).copy(deep=True)


@lru_cache(maxsize=None)
def _asset_currency_allocation_data_cached(data_root: str, currency: str) -> pd.DataFrame:
    assets = get_assets()
    if assets.empty:
        return pd.DataFrame(columns=["Дата"])

    assets = assets.copy(deep=True)
    assets["Дата"] = pd.PeriodIndex(
        year=assets["Год"].astype(int),
        month=assets["Месяц"].astype(int),
        freq="M",
    ).to_timestamp(how="end").normalize()
    assets["Дата оценки"] = asset_valuation_dates(assets)
    assets["Значение"] = pd.to_numeric(assets["Значение"], errors="coerce").fillna(0.0)
    assets["Валюта"] = assets["Валюта"].astype(str).str.upper()
    assets = carry_forward_asset_snapshots(assets)
    assets["value_in_target"] = _convert_asset_allocation_values(assets, currency)

    values = (
        assets
        .pivot_table(index="Дата", columns="Валюта", values="value_in_target", aggfunc="sum")
        .sort_index()
    )
    if values.empty:
        return pd.DataFrame(columns=["Дата"])

    totals = values.sum(axis=1)
    allocation = values.div(totals.where(totals.ne(0)), axis=0).mul(100).fillna(0.0)
    allocation = allocation.loc[:, allocation.sum(axis=0).ne(0)]
    return allocation.reset_index().round(2)


def _convert_asset_allocation_values(assets: pd.DataFrame, currency: str) -> pd.Series:
    values = assets["Значение"].copy()
    valuation_column = (
        "Дата FX" if "Дата FX" in assets.columns
        else "Дата оценки" if "Дата оценки" in assets.columns
        else "Дата"
    )
    for (from_currency, snapshot_date), index in assets.groupby(["Валюта", valuation_column]).groups.items():
        from_currency = str(from_currency).upper()
        if from_currency == currency:
            continue
        rate = _fx_rate_as_of(from_currency, currency, snapshot_date)
        if rate is None:
            require_fx_rate(rate, from_currency, currency, snapshot_date)
        values.loc[index] = values.loc[index] * rate
    return values


def _fx_rate_as_of(from_currency: str, to_currency: str, as_of_date) -> float | None:
    rates = get_fx_rates(from_currency, to_currency, as_of_date, as_of_date)
    if rates.empty:
        return None
    values = pd.to_numeric(rates.iloc[:, 0], errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.iloc[-1])


def _asset_currency_allocation_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if data.empty or "Дата" not in data.columns:
        _apply_dashboard_chart_layout(fig, "Динамика аллокации активов по валютам", range_slider=True)
        return fig

    x_dates = pd.to_datetime(data["Дата"])
    for index, asset_currency in enumerate([column for column in data.columns if column != "Дата"]):
        fig.add_trace(
            go.Bar(
                x=x_dates,
                y=data[asset_currency],
                name=asset_currency,
                marker_color=ASSET_ALLOCATION_COLORS[index % len(ASSET_ALLOCATION_COLORS)],
                marker_line=dict(color="rgba(220, 220, 220, 0.35)", width=0.7),
                hovertemplate=f"{asset_currency}<br>%{{x|%Y-%m}}<br>%{{y:,.2f}}%<extra></extra>",
            )
        )

    _apply_dashboard_chart_layout(fig, "Динамика аллокации активов по валютам", range_slider=True)
    fig.update_layout(barmode="stack", yaxis=dict(range=[0, 100], ticksuffix="%"))
    return fig


def _asset_liquidity_allocation_data(currency: str) -> pd.DataFrame:
    if not config.use_sqlite_storage():
        return pd.DataFrame(columns=["Дата"])
    return _asset_liquidity_allocation_data_cached(
        str(config.active_database_path()), str(currency).upper()).copy(deep=True)


@lru_cache(maxsize=None)
def _asset_liquidity_allocation_data_cached(
        database_path: str, currency: str) -> pd.DataFrame:
    from src.data.sqlite_store import connect_database

    with connect_database(database_path) as connection:
        rows = connection.execute("""SELECT v.period, v.account_id, v.currency_code,
            v.amount_minor, c.minor_unit, v.liquidity_class_id
            FROM v_asset_snapshots v
            JOIN currencies c ON c.code = v.currency_code
            WHERE v.include_in_capital = 1
            ORDER BY v.period, v.account_id""").fetchall()
    if not rows:
        return pd.DataFrame(columns=["Дата"])

    assets = pd.DataFrame([dict(row) for row in rows])
    periods = pd.PeriodIndex(assets["period"], freq="M")
    assets["Дата"] = periods.to_timestamp(how="end").normalize()
    assets["Год"] = periods.year
    assets["Месяц"] = periods.month
    assets["Дата оценки"] = asset_valuation_dates(assets)
    assets["Валюта"] = assets["currency_code"].astype(str).str.upper()
    assets["Значение"] = assets.apply(
        lambda row: float(row["amount_minor"]) / (10 ** int(row["minor_unit"])), axis=1)
    assets["Группа"] = assets["liquidity_class_id"].fillna("Не задана")
    assets = carry_forward_asset_snapshots(assets, account_column="account_id")
    assets["value_in_target"] = _convert_asset_allocation_values(assets, currency)

    values = assets.pivot_table(
        index="Дата", columns="Группа", values="value_in_target", aggfunc="sum")
    if values.empty:
        return pd.DataFrame(columns=["Дата"])
    column_order = [
        column for column in ["A1", "A2", "A3", "A4", "Не задана"]
        if column in values.columns
    ]
    values = values.reindex(columns=column_order).sort_index()
    totals = values.sum(axis=1)
    allocation = values.div(totals.where(totals.ne(0)), axis=0).mul(100).fillna(0.0)
    return allocation.reset_index().round(2)


def _asset_liquidity_allocation_figure(data: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    title = "Динамика распределения активов по ликвидности"
    if data.empty or "Дата" not in data.columns:
        _apply_dashboard_chart_layout(fig, title, range_slider=True)
        return fig

    x_dates = pd.to_datetime(data["Дата"])
    for index, liquidity_class in enumerate(
            column for column in data.columns if column != "Дата"):
        fig.add_trace(go.Bar(
            x=x_dates,
            y=data[liquidity_class],
            name=liquidity_class,
            marker_color=ASSET_ALLOCATION_COLORS[index % len(ASSET_ALLOCATION_COLORS)],
            marker_line=dict(color="rgba(220, 220, 220, 0.35)", width=0.7),
            hovertemplate=(
                f"{liquidity_class}<br>%{{x|%Y-%m}}<br>%{{y:,.2f}}%<extra></extra>"),
        ))
    _apply_dashboard_chart_layout(fig, title, range_slider=True)
    fig.update_layout(barmode="stack", yaxis=dict(range=[0, 100], ticksuffix="%"))
    return fig


def _fx_changes_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure()
    if data.empty or "Дата" not in data.columns:
        _apply_dashboard_chart_layout(fig, "Динамика курсов валют", range_slider=True)
        return fig

    x_dates = pd.to_datetime(data["Дата"])
    for from_currency in [column for column in data.columns if column != "Дата"]:
        fig.add_trace(
            go.Scatter(
                x=x_dates,
                y=data[from_currency],
                mode="lines+markers+text",
                name=f"{from_currency}/{currency}",
                text=_peak_rate_labels(data[from_currency], max_labels=5),
                textposition="top center",
                hovertemplate=f"{from_currency}/{currency}<br>%{{x|%Y-%m}}<br>%{{y:,.4f}}<extra></extra>",
            )
        )

    _apply_dashboard_chart_layout(fig, "Динамика курсов валют", range_slider=True)
    fig.update_layout(yaxis_title=f"1 валюта в {currency}")
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


def _sparse_rate_labels(values: pd.Series, max_labels: int = 5) -> list[str]:
    if values.empty:
        return []

    numeric = pd.to_numeric(values, errors="coerce")
    count = len(numeric)
    label_count = min(max_labels, count)
    if label_count <= 1:
        label_indexes = {count - 1}
    else:
        label_indexes = {round(index * (count - 1) / (label_count - 1)) for index in range(label_count)}

    labels = []
    for index, value in enumerate(numeric):
        if index not in label_indexes or pd.isna(value):
            labels.append("")
            continue
        labels.append(f"{value:,.4f}".rstrip("0").rstrip("."))
    return labels


def _peak_money_labels(values: pd.Series, currency: str, max_labels: int = 6) -> list[str]:
    numeric = pd.to_numeric(values, errors="coerce")
    indexes = _peak_label_indexes(numeric, max_labels, min_distance=5)
    symbol = config.UNIQUE_TICKERS[currency]
    return [
        f"{value:,.0f}".replace(",", " ") + symbol if index in indexes and pd.notna(value) else ""
        for index, value in enumerate(numeric)
    ]


def _peak_rate_labels(values: pd.Series, max_labels: int = 5) -> list[str]:
    numeric = pd.to_numeric(values, errors="coerce")
    indexes = _peak_label_indexes(numeric, max_labels, min_distance=5)
    return [
        f"{value:,.2f}" if index in indexes and pd.notna(value) else ""
        for index, value in enumerate(numeric)
    ]


def _peak_label_indexes(values: pd.Series, max_labels: int, min_distance: int = 5) -> set[int]:
    if values.empty or max_labels <= 0:
        return set()

    non_zero = values.dropna()
    non_zero = non_zero[non_zero.ne(0)]
    if non_zero.empty:
        return set()

    selected_positions: list[int] = []
    ranked = non_zero.abs().sort_values(ascending=False)
    for index in ranked.index:
        position = values.index.get_loc(index)
        if any(abs(position - selected) <= min_distance for selected in selected_positions):
            continue
        selected_positions.append(position)
        if len(selected_positions) >= max_labels:
            break
    return set(selected_positions)


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
        xaxis["rangeslider"] = dict(visible=True, thickness=0.08)

    fig.update_layout(
        title=dict(text=title, font=dict(size=CHART_TITLE_SIZE)),
        autosize=True,
        dragmode="zoom",
        font=dict(size=CHART_FONT_SIZE),
        margin=dict(l=54, r=18, t=62, b=72 if range_slider else 48),
        xaxis=xaxis,
        yaxis=dict(tickfont=dict(size=CHART_FONT_SIZE)),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
            font=dict(size=12),
            itemclick="toggle",
            itemdoubleclick="toggleothers",
        ),
        uniformtext=dict(minsize=CHART_LABEL_SIZE, mode="show"),
    )
