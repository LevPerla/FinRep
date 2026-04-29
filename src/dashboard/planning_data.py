from pathlib import Path

import pandas as pd
import plotly.graph_objects as go

from src import config, utils
from src.dashboard.main_data import DashboardDataset, _apply_dashboard_chart_layout
from src.data.get import get_assets
from src.data.get_finance import get_actual_fx_rate, set_fx_network_enabled
from src.model.create_tables import get_balance_by_month

GOALS_PATH = Path("data/plans/goals.csv")
GOALS_COLUMNS = [
    "year",
    "currency",
    "target_capital",
    "target_monthly_income",
    "target_monthly_expense",
    "notes",
]
LEGACY_GOALS_COLUMNS = {
    "target_income": "target_monthly_income",
    "target_expense": "target_monthly_expense",
}
FX_SHOCKS = [-20, -10, 0, 10, 20]


def build_planning_dashboard_data(
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
    goals = _load_goals()
    goal_row = _goal_for_year_currency(goals, year, currency)

    goals_progress = _goals_progress(balance, goal_row, year, currency)
    forecast = _capital_forecast(balance, year)
    runway = _runway(balance)
    fx_scenarios = _fx_scenarios(currency)

    return {
        "planning_goals": DashboardDataset(
            id="planning_goals",
            title="Цели года",
            dataframe=goals_progress,
            display_dataframe=_format_goals_progress(goals_progress, currency),
        ),
        "planning_capital_forecast": DashboardDataset(
            id="planning_capital_forecast",
            title="Прогноз капитала на 12 месяцев",
            dataframe=forecast,
            display_dataframe=_format_money_columns(forecast, currency, ["Дата", "Тип"]),
            figure=_forecast_figure(forecast, currency),
        ),
        "planning_runway": DashboardDataset(
            id="planning_runway",
            title="Runway",
            dataframe=runway,
            display_dataframe=_format_runway(runway, currency),
        ),
        "planning_fx_scenarios": DashboardDataset(
            id="planning_fx_scenarios",
            title="FX-сценарии",
            dataframe=fx_scenarios,
            display_dataframe=_format_fx_scenarios(fx_scenarios, currency),
            figure=_fx_scenarios_figure(fx_scenarios, currency),
        ),
    }


def _load_goals() -> pd.DataFrame:
    if not GOALS_PATH.exists():
        return pd.DataFrame(columns=GOALS_COLUMNS)
    goals = pd.read_csv(GOALS_PATH, sep=";")
    for legacy_column, new_column in LEGACY_GOALS_COLUMNS.items():
        if new_column not in goals.columns and legacy_column in goals.columns:
            goals[new_column] = goals[legacy_column]
    for column in GOALS_COLUMNS:
        if column not in goals.columns:
            goals[column] = pd.NA
    goals = goals[GOALS_COLUMNS].copy(deep=True)
    goals["year"] = goals["year"].astype(str)
    goals["currency"] = goals["currency"].astype(str).str.upper()
    for column in ["target_capital", "target_monthly_income", "target_monthly_expense"]:
        goals[column] = pd.to_numeric(goals[column], errors="coerce")
    return goals


def _goal_for_year_currency(goals: pd.DataFrame, year: str, currency: str) -> pd.Series:
    year_goals = goals[goals["year"] == str(year)]
    if year_goals.empty:
        return pd.Series({column: pd.NA for column in GOALS_COLUMNS})

    exact_match = year_goals[year_goals["currency"] == currency]
    if not exact_match.empty:
        goal = exact_match.iloc[-1].copy(deep=True)
        goal["source_currency"] = currency
        return goal

    rub_match = year_goals[year_goals["currency"] == "RUB"]
    source = rub_match.iloc[-1].copy(deep=True) if not rub_match.empty else year_goals.iloc[-1].copy(deep=True)
    source_currency = str(source.get("currency", currency)).upper()
    converted = source.copy(deep=True)
    converted["source_currency"] = source_currency
    converted["currency"] = currency
    for column in ["target_capital", "target_monthly_income", "target_monthly_expense"]:
        converted[column] = _convert_goal_value(converted.get(column), source_currency, currency)
    return converted


def _convert_goal_value(value, source_currency: str, target_currency: str):
    if pd.isna(value) or source_currency == target_currency:
        return value
    rate = get_actual_fx_rate(source_currency, target_currency)
    if rate is None:
        return pd.NA
    return float(value) * float(rate)


def _goals_progress(balance: pd.DataFrame, goal: pd.Series, year: str, currency: str) -> pd.DataFrame:
    current_capital = _latest_value(balance, "Капитал")
    year_balance = _year_slice(balance, year)
    actual_income = _column_sum(year_balance, "Доход")
    actual_expense = _column_sum(year_balance, "Расход")
    elapsed_months = max(len(year_balance), 1)
    avg_income = actual_income / elapsed_months
    avg_expense = actual_expense / elapsed_months
    rows = [
        ("Капитал", current_capital, goal.get("target_capital"), "money"),
        ("Средний доход/мес", avg_income, goal.get("target_monthly_income"), "money"),
        ("Средний расход/мес", avg_expense, goal.get("target_monthly_expense"), "money"),
    ]
    result = pd.DataFrame(rows, columns=["Показатель", "Факт", "Цель", "Тип"])
    result["Отклонение"] = result["Факт"] - result["Цель"]
    result["Прогресс (%)"] = result.apply(
        lambda row: row["Факт"] / row["Цель"] * 100 if pd.notna(row["Цель"]) and row["Цель"] != 0 else pd.NA,
        axis=1,
    )
    result["Год"] = str(year)
    result["Валюта"] = currency
    result["Источник целей"] = goal.get("source_currency", goal.get("currency", currency))
    return result[["Год", "Валюта", "Источник целей", "Показатель", "Факт", "Цель", "Отклонение", "Прогресс (%)", "Тип"]]


def _capital_forecast(balance: pd.DataFrame, year: str) -> pd.DataFrame:
    if balance.empty:
        return pd.DataFrame(columns=["Дата", "Капитал", "Тип"])

    monthly = balance.sort_index().copy(deep=True)
    monthly_dates = pd.to_datetime(monthly.index).to_period("M").to_timestamp()
    last_date = pd.to_datetime(monthly.index.max()).to_period("M").to_timestamp()
    try:
        selected_year = int(year)
    except (TypeError, ValueError):
        selected_year = last_date.year
    start_date = pd.Timestamp(year=selected_year - 1, month=1, day=1)
    last_capital = float(monthly["Капитал"].iloc[-1])
    avg_balance = float(pd.to_numeric(monthly["Баланс"].tail(12), errors="coerce").mean())

    rows = [
        {"Дата": date, "Капитал": capital, "Тип": "Факт"}
        for date, capital in zip(monthly_dates, monthly["Капитал"])
        if date >= start_date
    ]
    for step in range(1, 13):
        rows.append(
            {
                "Дата": last_date + pd.DateOffset(months=step),
                "Капитал": last_capital + avg_balance * step,
                "Тип": "Прогноз",
            }
        )
    return pd.DataFrame(rows)


def _runway(balance: pd.DataFrame) -> pd.DataFrame:
    if balance.empty:
        return pd.DataFrame(columns=["Капитал", "Средний расход", "Runway, мес.", "Runway, лет", "Статус"])

    current_capital = _latest_value(balance, "Капитал")
    avg_expense = float(pd.to_numeric(balance["Расход"].tail(12), errors="coerce").mean())
    if pd.isna(avg_expense) or avg_expense <= 0:
        runway_months = pd.NA
        runway_years = pd.NA
        status = "не рассчитано"
    else:
        runway_months = current_capital / avg_expense
        runway_years = runway_months / 12
        status = "рассчитано"
    return pd.DataFrame(
        [
            {
                "Капитал": current_capital,
                "Средний расход": avg_expense,
                "Runway, мес.": runway_months,
                "Runway, лет": runway_years,
                "Статус": status,
            }
        ]
    )


def _fx_scenarios(target_currency: str) -> pd.DataFrame:
    assets = get_assets()
    if assets.empty:
        return pd.DataFrame(columns=["Сценарий", "Шок (%)", "Капитал"])

    latest_year = assets["Год"].astype(int).max()
    latest_month = assets[assets["Год"].astype(int) == latest_year]["Месяц"].astype(int).max()
    latest_assets = assets[
        (assets["Год"].astype(int) == latest_year)
        & (assets["Месяц"].astype(int) == latest_month)
    ].copy(deep=True)

    rows = []
    for shock in FX_SHOCKS:
        total = 0.0
        for _, asset in latest_assets.iterrows():
            amount = pd.to_numeric(asset.get("Значение"), errors="coerce")
            if pd.isna(amount):
                continue
            asset_currency = str(asset.get("Валюта", target_currency)).upper()
            rate = 1.0 if asset_currency == target_currency else get_actual_fx_rate(asset_currency, target_currency)
            if rate is None:
                continue
            shock_multiplier = 1.0 if asset_currency == target_currency else _target_currency_shock_multiplier(shock)
            total += float(amount) * float(rate) * shock_multiplier
        rows.append(
            {
                "Сценарий": _shock_label(shock, target_currency),
                "Что меняется": _shock_description(shock, target_currency),
                "Шок выбранной валюты (%)": shock,
                "Капитал": total,
                "Год": str(latest_year),
                "Месяц": f"{latest_month:02d}",
            }
        )
    result = pd.DataFrame(rows)
    base_capital = result.loc[result["Шок выбранной валюты (%)"].eq(0), "Капитал"].iloc[0] if not result.empty else 0
    result["Изменение капитала"] = result["Капитал"] - base_capital
    return result


def _forecast_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure()
    if data.empty:
        _apply_dashboard_chart_layout(fig, "Прогноз капитала на 12 месяцев")
        return fig

    fact = data[data["Тип"] == "Факт"]
    forecast = data[data["Тип"] == "Прогноз"]
    fig.add_trace(
        go.Scatter(
            x=fact["Дата"],
            y=fact["Капитал"],
            mode="lines+markers",
            name="Факт",
            line=dict(color="#6897bb", width=2),
            hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}" + config.UNIQUE_TICKERS[currency] + "<extra></extra>",
        )
    )
    connector = fact.tail(1)
    fig.add_trace(
        go.Scatter(
            x=pd.concat([connector["Дата"], forecast["Дата"]]),
            y=pd.concat([connector["Капитал"], forecast["Капитал"]]),
            mode="lines+markers",
            name="Прогноз",
            line=dict(color="#b6d7a8", width=2, dash="dash"),
            hovertemplate="%{x|%Y-%m}<br>%{y:,.0f}" + config.UNIQUE_TICKERS[currency] + "<extra></extra>",
        )
    )
    _apply_dashboard_chart_layout(fig, "Факт с начала прошлого года и прогноз на 12 месяцев")
    return fig


def _fx_scenarios_figure(data: pd.DataFrame, currency: str) -> go.Figure:
    fig = go.Figure(
        go.Bar(
            x=data["Сценарий"] if not data.empty else [],
            y=data["Капитал"] if not data.empty else [],
            customdata=data[["Что меняется", "Изменение капитала"]] if not data.empty else [],
            marker=dict(color="#6897bb"),
            hovertemplate=(
                "%{x}<br>%{customdata[0]}<br>"
                "Капитал: %{y:,.0f}" + config.UNIQUE_TICKERS[currency] + "<br>"
                "Изменение: %{customdata[1]:,.0f}" + config.UNIQUE_TICKERS[currency] + "<extra></extra>"
            ),
        )
    )
    _apply_dashboard_chart_layout(fig, "FX-сценарии")
    return fig


def _format_goals_progress(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    money_mask = display["Тип"] == "money"
    percent_mask = display["Тип"] == "percent"
    for column in ["Факт", "Цель", "Отклонение"]:
        display.loc[money_mask, column] = display.loc[money_mask, column].map(lambda value: _format_money(value, currency))
        display.loc[percent_mask, column] = display.loc[percent_mask, column].map(_format_percent)
    display["Прогресс (%)"] = display["Прогресс (%)"].map(_format_percent)
    return display.drop(columns=["Тип"])


def _format_money_columns(data: pd.DataFrame, currency: str, not_money_cols: list[str]) -> pd.DataFrame:
    display = data.copy(deep=True)
    display["Дата"] = pd.to_datetime(display["Дата"], errors="coerce").dt.strftime("%Y-%m-%d")
    return utils.process_num_cols(display, not_num_cols=not_money_cols, currency=currency)


def _format_runway(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    for column in ["Капитал", "Средний расход"]:
        display[column] = display[column].map(lambda value: _format_money(value, currency))
    display["Runway, мес."] = display["Runway, мес."].map(lambda value: "не рассчитано" if pd.isna(value) else f"{value:,.1f} мес.".replace(",", " "))
    display["Runway, лет"] = display["Runway, лет"].map(lambda value: "не рассчитано" if pd.isna(value) else f"{value:,.1f} лет".replace(",", " "))
    return display


def _format_fx_scenarios(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    display = data.copy(deep=True)
    if not display.empty:
        display["Капитал"] = display["Капитал"].map(lambda value: _format_money(value, currency))
        display["Изменение капитала"] = display["Изменение капитала"].map(lambda value: _format_money(value, currency))
    return display


def _format_money(value, currency: str) -> str:
    if pd.isna(value):
        return "не задано"
    return f"{float(value):,.2f}".replace(",", " ") + config.UNIQUE_TICKERS[currency]


def _format_percent(value) -> str:
    if pd.isna(value):
        return "не задано"
    return f"{float(value):,.2f}%".replace(",", " ")


def _latest_value(data: pd.DataFrame, column: str) -> float:
    if data.empty or column not in data.columns:
        return 0.0
    return float(pd.to_numeric(data[column], errors="coerce").dropna().iloc[-1])


def _year_slice(data: pd.DataFrame, year: str) -> pd.DataFrame:
    try:
        return data.loc[str(year)]
    except KeyError:
        return data.iloc[0:0]


def _column_sum(data: pd.DataFrame, column: str) -> float:
    if data.empty or column not in data.columns:
        return 0.0
    return float(pd.to_numeric(data[column], errors="coerce").sum())


def _target_currency_shock_multiplier(shock: int) -> float:
    denominator = 1 + shock / 100
    if denominator <= 0:
        return pd.NA
    return 1 / denominator


def _shock_label(shock: int, currency: str) -> str:
    return f"{currency} 0%" if shock == 0 else f"{currency} {shock:+d}%"


def _shock_description(shock: int, currency: str) -> str:
    if shock == 0:
        return "Без изменения курсов"
    direction = "укрепляется" if shock > 0 else "слабеет"
    return f"{currency} {direction} на {abs(shock)}% к остальным валютам"
