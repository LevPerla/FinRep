import pandas as pd
import pytest

from src import config
from src.dashboard import planning_data
from src.data import crypto, investments


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path


def _investment_row(**overrides):
    row = {
        "date": "2026-01-01",
        "operation": "buy",
        "asset_type": "crypto",
        "ticker": "TEST",
        "quantity": "0.123456789",
        "price": "0.000012345678",
        "currency": "USD",
        "fee": "1.005",
        "account": "",
        "comment": "",
    }
    row.update(overrides)
    return row


def test_investment_fee_is_money_but_quantity_and_unit_price_keep_precision():
    normalized = investments.normalize_investment_transactions(pd.DataFrame([_investment_row()]))

    assert normalized.loc[0, "fee"] == "1.01"
    assert normalized.loc[0, "quantity"] == "0.123456789"
    assert normalized.loc[0, "price"] == "0.000012345678"


def test_price_cache_keeps_quote_precision(data_root):
    path = data_root / "investments" / "price_cache.csv"
    investments.write_price_cache(
        pd.DataFrame(
            [{"date": "2026-01-01", "ticker": "TEST", "price": "0,000012345678", "currency": "USD"}]
        ),
        path,
    )

    assert investments.read_price_cache(path).loc[0, "price"] == "0.000012345678"


def test_crypto_quantities_and_network_fee_keep_precision(data_root):
    path = data_root / "investments" / "crypto_transactions.csv"
    crypto.write_crypto_transactions(
        pd.DataFrame([{"quantity": "0.123456789", "fee": "0.000001234567"}]), path
    )

    saved = crypto.read_crypto_transactions(path).iloc[0]
    assert saved["quantity"] == "0.123456789"
    assert saved["fee"] == "0.000001234567"


def test_new_planning_goals_use_money_rounding_and_exact_storage(data_root):
    planning_data.save_goal_targets(
        "2026",
        "RUB",
        [
            {"Показатель": "Капитал", "Цель": "99 999 999 999 999,99"},
            {"Показатель": "Средний доход/мес", "Цель": "2.675"},
            {"Показатель": "Средний расход/мес", "Цель": "1.005"},
        ],
    )

    saved = pd.read_csv(data_root / "plans" / "goals.csv", sep=";", dtype=str, keep_default_na=False).iloc[0]
    assert saved["target_capital"] == "99999999999999.99"
    assert saved["target_monthly_income"] == "2.68"
    assert saved["target_monthly_expense"] == "1.01"


def test_saving_goal_preserves_untouched_legacy_precision(data_root):
    path = data_root / "plans" / "goals.csv"
    path.parent.mkdir(parents=True)
    pd.DataFrame(
        [{"year": "2025", "currency": "RUB", "target_capital": "100.005", "notes": "legacy"}]
    ).to_csv(path, sep=";", index=False)

    planning_data.save_goal_targets("2026", "RUB", [{"Показатель": "Капитал", "Цель": "200.005"}])

    saved = pd.read_csv(path, sep=";", dtype=str, keep_default_na=False)
    assert saved.loc[saved["year"] == "2025", "target_capital"].iloc[0] == "100.005"
    assert saved.loc[saved["year"] == "2026", "target_capital"].iloc[0] == "200.01"


@pytest.mark.parametrize("ambiguous", ["1,234.56", "1.234,56", "RUB 100", "--1"])
def test_ambiguous_goal_does_not_replace_existing_file(data_root, ambiguous):
    path = data_root / "plans" / "goals.csv"
    path.parent.mkdir(parents=True)
    path.write_text("year;currency;target_capital\n2025;RUB;100\n", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(ValueError, match="invalid or ambiguous"):
        planning_data.save_goal_targets("2026", "RUB", [{"Показатель": "Капитал", "Цель": ambiguous}])

    assert path.read_bytes() == before
