import pandas as pd

from src import config
from src.data import proccess
from src.model import create_tables


def test_conversion_rounds_only_money_column_with_half_up(monkeypatch):
    transactions = pd.DataFrame(
        [
            {
                "Дата": pd.Timestamp("2026-01-01"),
                "Валюта": "RUB",
                "Значение": 1.005,
                "Количество": 0.123456789,
                "Курс": 1.23456789,
            },
            {
                "Дата": pd.Timestamp("2026-01-01"),
                "Валюта": "USD",
                "Значение": 1.0,
                "Количество": 0.987654321,
                "Курс": 9.87654321,
            },
        ]
    )
    monkeypatch.setattr(proccess, "get_actual_fx_rate", lambda *_: 1.005)

    converted = proccess.convert_transaction(
        transactions, "RUB", "Значение", use_current_rate=True
    )

    assert converted["Значение"].tolist() == [1.01, 1.01]
    assert converted["Количество"].tolist() == [0.123456789, 0.987654321]
    assert converted["Курс"].tolist() == [1.23456789, 9.87654321]


def test_round_result_false_preserves_unrounded_conversion(monkeypatch):
    transactions = pd.DataFrame(
        [{"Дата": pd.Timestamp("2026-01-01"), "Валюта": "USD", "Значение": 1.0}]
    )
    monkeypatch.setattr(proccess, "get_actual_fx_rate", lambda *_: 1.005)

    converted = proccess.convert_transaction(
        transactions,
        "RUB",
        "Значение",
        use_current_rate=True,
        round_result=False,
    )

    assert converted.loc[0, "Значение"] == 1.005


def test_month_aggregate_sums_converted_transaction_cents(monkeypatch):
    rows = []
    for category in config.NOT_COST_COLS:
        rows.append(
            {
                "Дата": pd.Timestamp("2026-01-01"),
                "Категория": category,
                "Валюта": "RUB",
                "Значение": 0.0,
            }
        )
    rows.extend(
        {
            "Дата": pd.Timestamp("2026-01-01"),
            "Категория": "Прочее",
            "Валюта": "RUB",
            "Значение": 0.335,
        }
        for _ in range(3)
    )
    monkeypatch.setattr(create_tables, "get_transactions", lambda: pd.DataFrame(rows))
    monkeypatch.setattr(
        create_tables,
        "_get_asset_capital_by_month_cached",
        lambda *_: pd.DataFrame(columns=["Капитал по активам"]),
    )
    monkeypatch.setattr(config, "DEBUG", False)
    create_tables._get_balance_by_month_cached.cache_clear()

    result = create_tables._get_balance_by_month_cached("synthetic", "RUB")

    assert result.iloc[0]["Расход"] == 1.02
    assert result.iloc[0]["Баланс"] == -1.02


def test_asset_conversion_rounds_each_monetary_value_half_up(monkeypatch):
    assets = pd.DataFrame(
        [
            {
                "Валюта": "USD",
                "Дата": pd.Timestamp("2026-01-31"),
                "Значение": 1.0,
            }
        ]
    )
    monkeypatch.setattr(create_tables, "_get_fx_rate_as_of", lambda *_: 1.005)

    converted = create_tables._convert_asset_values_as_of_snapshot(assets, "RUB")

    assert converted.iloc[0] == 1.01
