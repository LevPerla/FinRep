import pandas as pd

from src import config
from src.data.get import clear_data_cache, get_assets, get_transactions
from src.model.create_tables import clear_table_cache, get_balance_by_month


def test_missing_transaction_and_asset_roots_are_empty_and_not_created(tmp_path, monkeypatch):
    data_root = tmp_path / "missing-live"
    monkeypatch.setattr(config, "DATA_PATH", str(data_root))
    clear_data_cache()

    try:
        transactions = get_transactions()
        assets = get_assets()
    finally:
        clear_data_cache()

    assert list(transactions.columns) == [
        "Дата",
        "Категория",
        "Валюта",
        "Значение",
        "Комментарий",
        "Год",
        "Квартал",
        "Месяц",
    ]
    assert list(assets.columns) == [
        "Счет",
        "Валюта",
        "Значение",
        "Год",
        "Квартал",
        "Месяц",
    ]
    assert transactions.empty
    assert assets.empty
    assert not data_root.exists()


def test_first_expense_month_has_zero_income_and_negative_balance(tmp_path, monkeypatch):
    data_root = tmp_path / "live"
    month_path = data_root / "transactions_info" / "2026" / "2026_01_.csv"
    month_path.parent.mkdir(parents=True)
    pd.DataFrame(
        {
            "Дата": ["01.01.2026"],
            "Прочее": ["100|RUB|Первый расход"],
        }
    ).to_csv(month_path, sep=";", index=False)
    monkeypatch.setattr(config, "DATA_PATH", str(data_root))
    clear_data_cache()
    clear_table_cache()

    try:
        balance = get_balance_by_month("RUB")
    finally:
        clear_data_cache()
        clear_table_cache()

    assert len(balance) == 1
    row = balance.iloc[0]
    assert row["Доход"] == 0
    assert row["Сбережения"] == 0
    assert row["Дебиторская задолженность"] == 0
    assert row["Погашение деб. зад."] == 0
    assert row["Кредиторская задолженность"] == 0
    assert row["Погашение кред. зад."] == 0
    assert row["Расход"] == 100
    assert row["Баланс"] == -100
    assert row["Капитал"] == -100
    assert row["Дельта"] == -100
