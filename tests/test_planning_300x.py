import pandas as pd

from src.dashboard import planning_data


def _balance() -> pd.DataFrame:
    index = pd.period_range("2025-03", "2026-03", freq="M").to_timestamp("M")
    expenses = [100.0] * 6 + [120.0] * 6 + [10_000.0]
    return pd.DataFrame({
        "Расход": expenses,
        "Капитал по активам": [3_000.0] * 12 + [9_999.0],
    }, index=index)


def _debts(monkeypatch):
    monkeypatch.setattr(
        planning_data,
        "get_act_receivables",
        lambda _currency: pd.DataFrame({"Дебиторская задолженность": [200.0]}),
    )
    monkeypatch.setattr(
        planning_data,
        "get_act_liabilities",
        lambda _currency: pd.DataFrame({"Кредиторская задолженность": [100.0]}),
    )


def test_300x_uses_twelve_completed_months_and_net_capital(monkeypatch):
    _debts(monkeypatch)
    monkeypatch.setattr(
        planning_data, "_current_period", lambda: pd.Period("2026-03", freq="M"))
    saved = {str(period) for period in pd.period_range("2025-03", "2026-02", freq="M")}

    result = planning_data._financial_independence_300x(
        _balance(), "RUB", saved_periods=saved).iloc[0]

    assert result["Период расходов"] == "2025-03 — 2026-02"
    assert result["Средний расход"] == 110.0
    assert result["Множитель"] == 300
    assert result["Цель"] == 33_000.0
    assert result["Период капитала"] == "2026-03"
    assert result["Активы"] == 9_999.0
    assert result["Требования"] == 200.0
    assert result["Обязательства"] == 100.0
    assert result["Чистый капитал"] == 10_099.0
    assert result["Прогресс (%)"] == 10_099.0 / 33_000.0 * 100
    assert result["Статус"] == "рассчитано"


def test_300x_does_not_treat_an_unsaved_month_as_zero(monkeypatch):
    _debts(monkeypatch)
    monkeypatch.setattr(
        planning_data, "_current_period", lambda: pd.Period("2026-03", freq="M"))
    saved = {str(period) for period in pd.period_range("2025-03", "2026-02", freq="M")}
    saved.remove("2025-08")

    result = planning_data._financial_independence_300x(
        _balance(), "RUB", saved_periods=saved).iloc[0]

    assert result["Статус"] == "недостаточно данных"
    assert "2025-08" in result["Детали"]
    assert pd.isna(result["Средний расход"])
    assert pd.isna(result["Цель"])
    assert pd.isna(result["Прогресс (%)"])


def test_300x_keeps_stale_capital_in_progress_with_warning(monkeypatch):
    _debts(monkeypatch)
    monkeypatch.setattr(
        planning_data, "_current_period", lambda: pd.Period("2026-03", freq="M"))
    saved = {str(period) for period in pd.period_range("2025-03", "2026-02", freq="M")}

    result = planning_data._financial_independence_300x(
        _balance(),
        "RUB",
        saved_periods=saved,
        asset_freshness={"has_warning": True, "stale_count": 1, "missing_count": 0},
    ).iloc[0]

    assert result["Чистый капитал"] == 10_099.0
    assert pd.notna(result["Прогресс (%)"])
    assert result["Статус"] == "рассчитано с предупреждением"
    assert "Устаревших оценок: 1" in result["Детали"]
