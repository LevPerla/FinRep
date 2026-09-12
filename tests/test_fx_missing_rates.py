from __future__ import annotations

import pandas as pd
import pytest

from src import config
from src.dashboard import main_data, planning_data
from src.data import crypto, exchange_rates_info, investment_calculations, proccess
from src.data.get import clear_data_cache
from src.model import create_tables


def test_transaction_conversion_rejects_missing_rate(monkeypatch):
    transactions = pd.DataFrame(
        [
            {"Дата": pd.Timestamp("2026-09-01"), "Валюта": "RUB", "Значение": 1000.0},
            {"Дата": pd.Timestamp("2026-09-01"), "Валюта": "USD", "Значение": 100.0},
        ]
    )
    monkeypatch.setattr(proccess, "get_rates", lambda **_: pd.DataFrame())
    monkeypatch.setattr(proccess, "get_fallback_rate", lambda *_: None)

    with pytest.raises(ValueError, match="Нет курса USD → RUB"):
        proccess.convert_transaction(transactions, "RUB", "Значение")


def test_monthly_balance_never_mixes_unconverted_currency(tmp_path, monkeypatch):
    transaction_path = tmp_path / "transactions_info" / "2026" / "2026_09_.csv"
    transaction_path.parent.mkdir(parents=True)
    row = {column: "0" for column in [*config.NOT_COST_COLS, "Прочее"]}
    row["Дата"] = "01.09.2026"
    row["Прочее"] = "100|USD|fixture"
    pd.DataFrame([row]).to_csv(transaction_path, sep=";", index=False, encoding="utf-8-sig")
    (tmp_path / "assets_info").mkdir()

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "DEBUG", False)
    monkeypatch.setattr(proccess, "get_rates", lambda **_: pd.DataFrame())
    monkeypatch.setattr(proccess, "get_fallback_rate", lambda *_: None)
    clear_data_cache()
    create_tables.clear_table_cache()

    with pytest.raises(ValueError, match="Нет курса USD → RUB.*2026-09-01"):
        create_tables.get_balance_by_month("RUB")

    clear_data_cache()
    create_tables.clear_table_cache()


def test_asset_capital_rejects_missing_rate(monkeypatch):
    assets = pd.DataFrame(
        [{"Валюта": "USD", "Дата": pd.Timestamp("2026-09-30"), "Значение": 100.0}]
    )
    monkeypatch.setattr(create_tables, "_get_fx_rate_as_of", lambda *_: None)

    with pytest.raises(ValueError, match="Нет курса USD → RUB.*2026-09-30"):
        create_tables._convert_asset_values_as_of_snapshot(assets, "RUB")


@pytest.mark.parametrize("module", [investment_calculations, crypto])
def test_portfolio_conversion_rejects_missing_rate(module, monkeypatch):
    monkeypatch.setattr(module, "get_actual_fx_rate", lambda *_: None)
    monkeypatch.setattr(module, "get_fallback_rate", lambda *_: None)

    with pytest.raises(ValueError, match="Нет курса USD → RUB"):
        module._conversion_rate("USD", "RUB")


def test_asset_allocation_rejects_missing_rate(monkeypatch):
    assets = pd.DataFrame(
        [{"Валюта": "USD", "Дата": pd.Timestamp("2026-09-30"), "Значение": 100.0}]
    )
    monkeypatch.setattr(main_data, "_fx_rate_as_of", lambda *_: None)

    with pytest.raises(ValueError, match="Нет курса USD → RUB.*2026-09-30"):
        main_data._convert_asset_allocation_values(assets, "RUB")


def test_fx_scenario_rejects_partial_total(monkeypatch):
    assets = pd.DataFrame(
        [
            {"Год": "2026", "Месяц": "9", "Валюта": "RUB", "Значение": 1000.0},
            {"Год": "2026", "Месяц": "9", "Валюта": "USD", "Значение": 100.0},
        ]
    )
    monkeypatch.setattr(planning_data, "get_assets", lambda: assets)
    monkeypatch.setattr(planning_data, "get_actual_fx_rate", lambda *_: None)

    with pytest.raises(ValueError, match="Нет курса USD → RUB"):
        planning_data._fx_scenarios("RUB")


def test_conversion_summary_marks_missing_rate_unavailable(monkeypatch):
    transactions = pd.DataFrame(
        [{"Валюта": "USD", "Значение": 100.0}]
    )
    monkeypatch.setattr(exchange_rates_info, "get_transactions", lambda: transactions)
    monkeypatch.setattr(exchange_rates_info, "get_actual_fx_rate", lambda *_: None)

    summary = exchange_rates_info.get_currency_conversion_summary("RUB")

    assert summary.loc[0, "Сумма в RUB"] == "Недоступно"
    assert summary.loc[0, "Курс конвертации"] == "Недоступно"
