import pandas as pd

from src.dashboard import planning_data
from src.dashboard.app import _runway_section
from src.dashboard.main_data import DashboardDataset


def _balance() -> pd.DataFrame:
    index = pd.period_range("2025-03", "2026-03", freq="M").to_timestamp("M")
    expenses = [100.0] * 6 + [120.0] * 6 + [10_000.0]
    return pd.DataFrame({
        "Расход": expenses,
        "Капитал по активам": [3_000.0] * 12 + [9_999.0],
    }, index=index)


def test_asset_runway_uses_twelve_completed_months_and_asset_capital(monkeypatch):
    monkeypatch.setattr(
        planning_data, "_current_period", lambda: pd.Period("2026-03", freq="M"))
    result = planning_data._asset_runway_data(
        _balance(), target_months=300).iloc[0]

    assert result["Период расходов"] == "2025-03 — 2026-02"
    assert result["Средний расход"] == 110.0
    assert result["Период капитала"] == "2026-03"
    assert result["Капитал по активам"] == 9_999.0
    assert result["Финансовый запас, мес."] == 9_999.0 / 110.0
    assert result["Финансовый запас, лет"] == 9_999.0 / 110.0 / 12
    assert result["Цель, мес."] == 300
    assert result["Прогресс от цели (%)"] == (9_999.0 / 110.0) / 300 * 100
    assert result["Статус"] == "рассчитано"


def test_asset_runway_counts_a_calendar_month_without_transactions_as_zero(monkeypatch):
    monkeypatch.setattr(
        planning_data, "_current_period", lambda: pd.Period("2026-03", freq="M"))
    balance = _balance().drop(pd.Timestamp("2025-08-31"))

    result = planning_data._asset_runway_data(
        balance, target_months=300).iloc[0]

    assert result["Статус"] == "рассчитано"
    assert result["Средний расход"] == (5 * 100.0 + 6 * 120.0) / 12
    assert pd.notna(result["Финансовый запас, мес."])
    assert pd.notna(result["Прогресс от цели (%)"])


def test_asset_runway_facts_do_not_depend_on_target(monkeypatch):
    monkeypatch.setattr(
        planning_data, "_current_period", lambda: pd.Period("2026-03", freq="M"))

    result = planning_data._asset_runway_data(_balance(), target_months=None).iloc[0]

    assert result["Средний расход"] == 110.0
    assert result["Финансовый запас, мес."] == 9_999.0 / 110.0
    assert result["Финансовый запас, лет"] == 9_999.0 / 110.0 / 12
    assert pd.isna(result["Прогресс от цели (%)"])


def test_asset_runway_keeps_stale_capital_in_progress_with_warning(monkeypatch):
    monkeypatch.setattr(
        planning_data, "_current_period", lambda: pd.Period("2026-03", freq="M"))
    result = planning_data._asset_runway_data(
        _balance(),
        asset_freshness={"has_warning": True, "stale_count": 1, "missing_count": 0},
        target_months=300,
    ).iloc[0]

    assert result["Капитал по активам"] == 9_999.0
    assert pd.notna(result["Прогресс от цели (%)"])
    assert result["Статус"] == "рассчитано с предупреждением"
    assert "Устаревших оценок: 1" in result["Детали"]


def test_expense_month_goal_uses_asset_runway_fact_and_user_target():
    balance = _balance().assign(
        Доход=200.0,
        Капитал=2_500.0,
    )
    runway = pd.DataFrame([{
        "Финансовый запас, мес.": 90.9,
        "Прогресс от цели (%)": 90.9 / 240 * 100,
        "Период расходов": "2025-03 — 2026-02",
        "Статус": "рассчитано",
        "Детали": "",
    }])

    goals = planning_data._goals_progress(
        balance,
        pd.Series({"target_expense_months": "240"}),
        "2026",
        "RUB",
        runway,
    )

    target = goals.set_index("Показатель").loc["N мес расходов"]
    assert target["Факт"] == 90.9
    assert target["Цель"] == 240
    assert target["Прогресс (%)"] == 90.9 / 240 * 100


def test_asset_runway_panel_contains_only_requested_metrics():
    display = pd.DataFrame([{
        "Финансовый запас, мес.": "87.0 мес.",
        "Финансовый запас, лет": "7.3 лет",
        "Капитал по активам": "7 529 591.23₽",
        "Средний расход": "86 510.93₽",
        "Прогресс от цели (%)": "29.01%",
        "Детали": "",
    }])
    dataset = DashboardDataset(
        id="planning_runway",
        title="Финансовый запас по активам",
        dataframe=display,
        display_dataframe=display,
    )

    section = _runway_section(dataset, theme="dark", locale="ru")
    cards = section.children[1].children

    assert [card.children[0].children for card in cards] == [
        "Финансовый запас по активам, месяцев",
        "Финансовый запас по активам, лет",
        "Капитал по активам",
        "Средний расход/мес",
        "Прогресс от цели",
    ]
