from src import config
from src.data.get import clear_data_cache, get_assets, get_transactions


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
