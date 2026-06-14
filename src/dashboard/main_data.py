from dataclasses import dataclass

import pandas as pd
import plotly.graph_objects as go

from src import config, utils
from src.data.get import get_assets
from src.data.exchange_rates_info import get_exchange_rates_info
from src.data.get_finance import get_fallback_rate, get_fx_rates, set_fx_network_enabled
from src.model.create_tables import get_balance_by_month


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
    year: str | None = None,
    month: str | None = None,
) -> dict[str, DashboardDataset]:
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")

    set_fx_network_enabled(fx_network_enabled)
    balance = get_balance_by_month(currency)

    cockpit_metrics = _cockpit_metrics(balance, currency, year, month)
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
    fx_revaluation = _fx_revaluation_data(balance)
    asset_currency_allocation = _asset_currency_allocation_data(currency)
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
            figure=_income_expense_figure(income_expense, currency),
            graph_config={"scrollZoom": False},
        ),
        "delta": DashboardDataset(
            id="delta",
            title="Delta",
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
            title="Capital",
            dataframe=capital,
            figure=_capital_figure(capital, currency),
        ),
        "fx_revaluation": DashboardDataset(
            id="fx_revaluation",
            title="FX Revaluation",
            dataframe=fx_revaluation,
            figure=_fx_revaluation_figure(fx_revaluation, currency),
        ),
        "asset_currency_allocation": DashboardDataset(
            id="asset_currency_allocation",
            title="Asset Currency Allocation",
            dataframe=asset_currency_allocation,
            figure=_asset_currency_allocation_figure(asset_currency_allocation),
        ),
        "fx_changes": DashboardDataset(
            id="fx_changes",
            title="FX Changes",
            dataframe=fx_changes,
            figure=_fx_changes_figure(fx_changes, currency),
        ),
    }


def _cockpit_metrics(
    balance: pd.DataFrame,
    currency: str,
    year: str | None,
    month: str | None,
) -> pd.DataFrame:
    columns = ["Показатель", "Значение", "Статус", "Детали", "Тип"]
    if balance.empty:
        return pd.DataFrame(columns=columns)

    selected_row, selected_period, is_selected_month = _selected_balance_row(balance, year, month)
    latest_row = balance.sort_index().tail(1).iloc[0]
    current_capital = _latest_number(balance, "Капитал по активам")
    capital_source = "assets"
    if pd.isna(current_capital):
        current_capital = _latest_number(balance, "Капитал")
        capital_source = "cash-flow"

    income = _row_number(selected_row, "Доход")
    expense = _row_number(selected_row, "Расход")
    delta = _row_number(selected_row, "Дельта")
    avg_expense = float(pd.to_numeric(balance["Расход"].tail(12), errors="coerce").mean())
    runway_months = current_capital / avg_expense if avg_expense > 0 else pd.NA
    savings_rate = _bounded_percent(delta / income * 100) if income > 0 else pd.NA
    asset_gap = _row_number(latest_row, "Расхождение с активами")
    fx_impact = _row_number(selected_row, "Валютная переоценка")
    period_label = str(selected_period)
    period_detail = "выбранный месяц" if is_selected_month else "последний доступный месяц"

    rows = [
        ("Капитал", current_capital, capital_source, "Последний доступный капитал по assets или cash-flow", "money"),
        ("Доход месяца", income, "ok" if income > 0 else "empty", f"{period_label}, {period_detail}", "money"),
        ("Расход месяца", expense, "watch" if expense > avg_expense * 1.2 and avg_expense > 0 else "ok", f"{period_label}, средний расход 12м: {avg_expense:,.0f} {currency}", "money"),
        ("Cash-flow месяца", delta, "positive" if delta >= 0 else "negative", f"{period_label}: доход минус расход", "money"),
        ("Норма сбережений", savings_rate, _savings_rate_status(savings_rate), f"{period_label}: cash-flow / income", "percent"),
        ("Runway", runway_months, _runway_status(runway_months), "Капитал / средний расход за последние 12 месяцев", "months"),
        ("Расхождение с активами", asset_gap, _asset_gap_status(asset_gap, current_capital), "Последний assets snapshot минус cash-flow капитал", "money"),
        ("FX impact месяца", fx_impact, "positive" if fx_impact >= 0 else "negative", f"{period_label}: валютная переоценка", "money"),
    ]
    return pd.DataFrame(rows, columns=columns)


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
    for index, row in data.iterrows():
        value = row["Значение"]
        if row["Тип"] == "money":
            display.loc[index, "Значение"] = _format_money_value(value, currency)
        elif row["Тип"] == "percent":
            display.loc[index, "Значение"] = "не рассчитано" if pd.isna(value) else f"{float(value):,.1f}%".replace(",", " ")
        elif row["Тип"] == "months":
            display.loc[index, "Значение"] = "не рассчитано" if pd.isna(value) else f"{float(value):,.1f} мес.".replace(",", " ")
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
            name="Дельта",
            hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}<extra></extra>",
        )
    )
    _apply_dashboard_chart_layout(fig, "Дельты", range_slider=True)
    fig.update_layout(annotations=_important_delta_annotations(data, currency, max_labels=6))
    return fig


def _savings_rate_data(balance: pd.DataFrame) -> pd.DataFrame:
    data = balance[["Доход", "Дельта"]].reset_index()
    income = pd.to_numeric(data["Доход"], errors="coerce")
    delta = pd.to_numeric(data["Дельта"], errors="coerce")
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
            name="Капитал cash-flow",
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
    assets = get_assets()
    if assets.empty:
        return pd.DataFrame(columns=["Дата"])

    assets = assets.copy(deep=True)
    assets["Дата"] = pd.PeriodIndex(
        year=assets["Год"].astype(int),
        month=assets["Месяц"].astype(int),
        freq="M",
    ).to_timestamp(how="end").normalize()
    assets["Значение"] = pd.to_numeric(assets["Значение"], errors="coerce").fillna(0.0)
    assets["Валюта"] = assets["Валюта"].astype(str).str.upper()
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
    for (from_currency, snapshot_date), index in assets.groupby(["Валюта", "Дата"]).groups.items():
        from_currency = str(from_currency).upper()
        if from_currency == currency:
            continue
        rate = _fx_rate_as_of(from_currency, currency, snapshot_date)
        if rate is None:
            continue
        values.loc[index] = values.loc[index] * rate
    return values


def _fx_rate_as_of(from_currency: str, to_currency: str, as_of_date) -> float | None:
    rates = get_fx_rates(from_currency, to_currency, as_of_date, as_of_date)
    if rates.empty:
        return get_fallback_rate(from_currency, to_currency)
    values = pd.to_numeric(rates.iloc[:, 0], errors="coerce").dropna()
    if values.empty:
        return get_fallback_rate(from_currency, to_currency)
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
        xaxis.update(
            rangeslider=dict(visible=True, thickness=0.08),
        )

    fig.update_layout(
        title=dict(text=title, font=dict(size=CHART_TITLE_SIZE)),
        autosize=True,
        dragmode="zoom",
        font=dict(size=CHART_FONT_SIZE),
        margin=dict(l=54, r=18, t=62, b=48),
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
