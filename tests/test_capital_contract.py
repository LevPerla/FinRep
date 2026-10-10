from datetime import date

import pandas as pd

from src import config
from src.dashboard import main_data
from src.data.get import clear_data_cache
from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    initialize_database,
    set_asset_account_classification,
)
from src.model import create_tables


def test_asset_capital_contains_only_explicit_snapshots(monkeypatch):
    assets = pd.DataFrame([{
        "Счет": "Счёт",
        "Год": "2026",
        "Месяц": "1",
        "Квартал": "1",
        "Валюта": "RUB",
        "Значение": 100.0,
    }])
    monkeypatch.setattr(create_tables, "get_assets", lambda: assets)
    monkeypatch.setattr(
        create_tables, "_current_asset_valuation_date", lambda: pd.Timestamp("2026-01-31"))
    create_tables._get_asset_capital_by_month_cached.cache_clear()

    result = create_tables._get_asset_capital_by_month_cached("synthetic", "RUB")

    assert result.iloc[0]["Капитал по активам"] == 100.0


def test_complete_snapshot_does_not_carry_renamed_account(monkeypatch):
    assets = pd.DataFrame([
        {
            "Счет": "Депозит",
            "Год": "2026",
            "Месяц": "1",
            "Квартал": "1",
            "Валюта": "RUB",
            "Значение": 100.0,
        },
        {
            "Счет": "Депозит - RUB",
            "Год": "2026",
            "Месяц": "2",
            "Квартал": "1",
            "Валюта": "RUB",
            "Значение": 60.0,
        },
    ])
    monkeypatch.setattr(create_tables, "get_assets", lambda: assets)
    monkeypatch.setattr(
        create_tables, "_current_asset_valuation_date", lambda: pd.Timestamp("2026-02-28"))
    create_tables._get_asset_capital_by_month_cached.cache_clear()

    result = create_tables._get_asset_capital_by_month_cached("synthetic", "RUB")

    assert result["Капитал по активам"].tolist() == [100.0, 60.0]


def test_monthly_balance_includes_user_defined_income_category(monkeypatch):
    transactions = pd.DataFrame([{
        "Дата": pd.Timestamp("2026-01-15"),
        "Категория": "Купоны",
        "Валюта": "RUB",
        "Значение": 25.0,
    }])
    monkeypatch.setattr(create_tables, "get_transactions", lambda: transactions)
    monkeypatch.setattr(create_tables, "get_income_categories", lambda: pd.DataFrame({
        "Категория": ["Купоны"], "Класс": ["passive"],
    }))
    monkeypatch.setattr(
        create_tables,
        "_get_asset_capital_by_month_cached",
        lambda *_: pd.DataFrame(columns=["Капитал по активам"]),
    )
    monkeypatch.setattr(config, "DEBUG", True)
    create_tables._get_balance_by_month_cached.cache_clear()

    result = create_tables._get_balance_by_month_cached("synthetic", "RUB")

    assert result.iloc[0]["Доход"] == 25.0
    assert result.iloc[0]["Баланс"] == 25.0


def test_fx_revaluation_uses_opening_native_exposure_only(monkeypatch):
    assets = pd.DataFrame([
        {"Счет": "USD счёт", "Год": "2025", "Месяц": "1", "Квартал": "1",
         "Валюта": "USD", "Значение": 100.0},
        {"Счет": "Закрыт", "Год": "2025", "Месяц": "1", "Квартал": "1",
         "Валюта": "GBP", "Значение": 50.0},
        {"Счет": "USD счёт", "Год": "2025", "Месяц": "2", "Квартал": "1",
         "Валюта": "USD", "Значение": 110.0},
        {"Счет": "Новый", "Год": "2025", "Месяц": "2", "Квартал": "1",
         "Валюта": "EUR", "Значение": 70.0},
    ])

    def rate(from_currency, _to_currency, as_of):
        month = pd.Timestamp(as_of).month
        return {
            ("USD", 1): 90.0,
            ("USD", 2): 100.0,
            ("GBP", 1): 120.0,
            ("EUR", 2): 95.0,
        }[(from_currency, month)]

    monkeypatch.setattr(create_tables, "_get_fx_rate_as_of", rate)

    result = create_tables._asset_fx_revaluation_by_month(assets, "RUB")

    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == 1_000.0


def test_capital_attribution_reconciles_and_keeps_fx_separate(monkeypatch):
    monkeypatch.setattr(main_data, "get_income_categories", lambda: pd.DataFrame({
        "Категория": ["Зарплата", "Проценты"],
        "Класс": ["active", "passive"],
    }))
    balance = pd.DataFrame({
        "Капитал по активам": [100.0, 175.0],
        "Баланс": [100.0, 55.0],
        "Зарплата": [100.0, 50.0],
        "Проценты": [0.0, 5.0],
        "Валютная переоценка": [float("nan"), 10.0],
    }, index=pd.to_datetime(["2026-01-31", "2026-02-28"]))

    result = main_data._capital_attribution_data(balance)
    row = result.iloc[1]

    assert row["Изменение капитала"] == 75.0
    assert row["Внешний поток"] == 50.0
    assert row["Пассивный доход"] == 5.0
    assert row["Валютная переоценка"] == 10.0
    assert row["Переоценка и необъяснённые изменения"] == 10.0
    assert row["Изменение капитала"] == sum(row[column] for column in [
        "Внешний поток", "Пассивный доход", "Валютная переоценка",
        "Переоценка и необъяснённые изменения",
    ])


def test_first_asset_month_does_not_invent_zero_fx_revaluation():
    balance = pd.DataFrame({
        "Доход": [100.0],
        "Расход": [50.0],
        "Дельта": [50.0],
        "Баланс": [50.0],
        "Капитал": [50.0],
        "Капитал по активам": [100.0],
        "Расхождение с активами": [50.0],
        "Валютная переоценка": [float("nan")],
    }, index=pd.to_datetime(["2026-01-31"]))

    metrics = main_data._cockpit_metrics(
        balance, "RUB", "2026", "01").set_index("ID")

    assert pd.isna(metrics.loc["monthly_fx_revaluation", "Значение"])
    assert metrics.loc["monthly_fx_revaluation", "Статус"] == "empty"


def test_carried_balance_marks_capital_provisional_not_stale():
    balance = pd.DataFrame({
        "Доход": [100.0], "Расход": [0.0], "Дельта": [100.0],
        "Баланс": [100.0], "Капитал": [100.0],
        "Капитал по активам": [600.0], "Расхождение с активами": [500.0],
        "Валютная переоценка": [0.0],
    }, index=pd.to_datetime(["2026-10-31"]))
    freshness = {"carried_count": 1, "stale_count": 0, "missing_count": 0,
                 "has_warning": True}

    metrics = main_data._cockpit_metrics(
        balance, "RUB", "2026", "10", asset_freshness=freshness).set_index("ID")

    assert metrics.loc["capital", "Статус"] == "provisional"
    assert "перенесённых остатков: 1" in metrics.loc["capital", "Детали"]


def test_capital_components_include_carried_active_snapshots(
        tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(database, "cash", "Основной счёт", asset_type_id="cash_account")
    add_asset_account(database, "duplicate", "Внешняя оценка портфеля", asset_type_id="other")
    add_asset_account(database, "historical", "Закрытый старый счёт", asset_type_id="other")
    set_asset_account_classification(
        database,
        "cash",
        asset_type_id="cash_account",
        include_in_capital=True,
        reason="test",
    )
    set_asset_account_classification(
        database,
        "duplicate",
        asset_type_id="equity",
        include_in_capital=False,
        reason="test",
    )
    add_asset_snapshot(
        database, snapshot_id="cash-old", account_id="cash",
        period="2026-01", amount="90", currency="RUB")
    add_asset_snapshot(
        database, snapshot_id="historical-old", account_id="historical",
        period="2026-01", amount="700", currency="RUB")
    add_asset_snapshot(
        database, snapshot_id="cash-new", account_id="cash",
        period="2026-02", amount="100", currency="RUB")
    add_asset_snapshot(
        database, snapshot_id="duplicate-current", account_id="duplicate",
        period="2026-02", amount="500", currency="RUB")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    clear_data_cache()
    main_data.clear_main_dashboard_cache()

    result = main_data._capital_components_data("RUB")

    assert set(result["Счет"]) == {"Основной счёт", "Закрытый старый счёт"}
    assert set(result["Период оценки"]) == {max("2026-02", date.today().strftime("%Y-%m"))}
    assert dict(zip(result["Счет"], result["В валюте отчёта"])) == {
        "Основной счёт": 100.0, "Закрытый старый счёт": 700.0,
    }
    assert result.attrs["total"] == 800.0
