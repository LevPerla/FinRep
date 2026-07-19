import pandas as pd

from src import config
from src.data.get import clear_data_cache, get_assets, get_transactions
from src.model.create_tables import clear_table_cache, get_balance_by_month


EXPECTED_PERIODS = [period.strftime("%Y-%m") for period in pd.period_range("2025-01", "2026-05", freq="M")]


def test_sample_data_has_continuous_nonzero_months_and_multiple_currencies(monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(config.PROJECT_PATH / "sample_data"))
    clear_data_cache()
    transactions = get_transactions()
    transactions["period"] = transactions["Дата"].dt.to_period("M").astype(str)

    monthly_income = transactions[transactions["Категория"] == "Доход"].groupby("period")["Значение"].sum()
    monthly_expenses = transactions[~transactions["Категория"].isin(config.NOT_COST_COLS)].groupby("period")["Значение"].sum()

    assert monthly_income.index.tolist() == EXPECTED_PERIODS
    assert monthly_expenses.index.tolist() == EXPECTED_PERIODS
    assert monthly_income.gt(0).all()
    assert monthly_expenses.gt(0).all()
    assert {"RUB", "USD", "KZT"}.issubset(set(transactions.loc[transactions["Значение"].ne(0), "Валюта"]))


def test_sample_asset_allocation_has_no_monthly_gaps(monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(config.PROJECT_PATH / "sample_data"))
    clear_data_cache()
    assets = get_assets()
    assets["period"] = assets["Год"] + "-" + assets["Месяц"].astype(int).astype(str).str.zfill(2)

    assert sorted(assets["period"].unique()) == EXPECTED_PERIODS
    assert assets.groupby("period").size().eq(4).all()
    assert assets.groupby("period")["Валюта"].nunique().eq(3).all()


def test_sample_cashflow_reconciles_with_assets(monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(config.PROJECT_PATH / "sample_data"))
    clear_data_cache()
    clear_table_cache()

    transactions = get_transactions()
    monthly = get_balance_by_month("RUB")

    housing_periods = transactions.loc[
        (transactions["Категория"] == "Жилье") & transactions["Значение"].gt(0), "Дата"
    ].dt.to_period("M")
    capital_gap = monthly["Расхождение с активами"]
    revaluation = monthly["Валютная переоценка"]

    assert sorted(housing_periods.astype(str).unique()) == EXPECTED_PERIODS
    assert monthly["Баланс"].gt(0).all()
    assert abs(capital_gap.iloc[0] - monthly["Капитал"].iloc[0]) < 1
    assert capital_gap.between(100_000, 200_000).all()
    assert capital_gap.iloc[-1] / monthly["Капитал"].iloc[-1] < 0.1
    assert abs(revaluation.iloc[0]) < 1
    assert revaluation.iloc[1:].gt(0).sum() >= 6
    assert revaluation.iloc[1:].lt(0).sum() >= 4
    assert revaluation.iloc[1:].abs().between(4_000, 30_000).all()


def test_sample_data_stays_lightweight():
    sample_root = config.PROJECT_PATH / "sample_data"
    total_size = sum(path.stat().st_size for path in sample_root.rglob("*") if path.is_file())

    assert total_size < 1_000_000
