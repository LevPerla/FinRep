from decimal import Decimal

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


def test_capital_change_excludes_external_flows_but_keeps_passive_income(monkeypatch):
    monkeypatch.setattr(main_data, "get_income_categories", lambda: pd.DataFrame({
        "Категория": ["Зарплата", "Проценты", "Инвест доход", "Прочие доходы"],
        "Класс": ["active", "passive", "passive", "active"],
    }))
    balance = pd.DataFrame({
        "Капитал по активам": [100.0, 170.0, 160.0],
        "Баланс": [100.0, 60.0, -20.0],
        "Зарплата": [100.0, 50.0, 0.0],
        "Проценты": [0.0, 10.0, 0.0],
        "Инвест доход": [0.0, 0.0, 0.0],
        "Прочие доходы": [0.0, 0.0, 0.0],
    }, index=pd.to_datetime(["2026-01-31", "2026-02-28", "2026-03-31"]))
    real = pd.DataFrame({
        "Дата": pd.to_datetime(["2026-01-31", "2026-02-28", "2026-03-31"]),
        "Номинальная стоимость": [100.0, 170.0, 160.0],
        "Реальная стоимость": [90.0, 160.0, 160.0],
    })
    real.attrs.update({
        "status": "ready",
        "base_period": "2026-03",
        "deflators": {
            "2026-01": Decimal("0.9"),
            "2026-02": Decimal("0.8"),
            "2026-03": Decimal("1"),
        },
    })

    result = main_data._capital_change_after_flows_data(balance, real)

    assert result["Внешний поток"].tolist() == [100.0, 50.0, -20.0]
    assert pd.isna(result.iloc[0]["Номинальное изменение после потоков"])
    assert result.iloc[1]["Номинальное изменение после потоков"] == 20.0
    assert result.iloc[2]["Номинальное изменение после потоков"] == 10.0
    assert result.iloc[1]["Реальное изменение после потоков"] == 30.0
    assert result.iloc[2]["Реальное изменение после потоков"] == 20.0


def test_capital_change_is_unavailable_without_complete_cpi(monkeypatch):
    monkeypatch.setattr(main_data, "get_income_categories", lambda: pd.DataFrame({
        "Категория": [], "Класс": [],
    }))
    balance = pd.DataFrame({
        "Капитал по активам": [100.0, 110.0],
        "Баланс": [100.0, 10.0],
    }, index=pd.to_datetime(["2026-01-31", "2026-02-28"]))
    real = pd.DataFrame({
        "Дата": pd.to_datetime(["2026-01-31", "2026-02-28"]),
        "Номинальная стоимость": [100.0, 110.0],
        "Реальная стоимость": [100.0, float("nan")],
    })
    real.attrs["status"] = "partial"

    result = main_data._capital_change_after_flows_data(balance, real)

    assert result.attrs["status"] == "partial"
    assert result["Реальное изменение после потоков"].isna().all()


def test_capital_components_show_only_latest_included_explicit_snapshots(
        tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(database, "cash", "Основной счёт")
    add_asset_account(database, "duplicate", "Внешняя оценка портфеля")
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

    assert result["Счет"].tolist() == ["Основной счёт"]
    assert result["Период оценки"].tolist() == ["2026-02"]
    assert result["Тип актива"].tolist() == ["Расчётный счёт"]
    assert result["В валюте отчёта"].tolist() == [100.0]
    assert result.attrs["total"] == 100.0
