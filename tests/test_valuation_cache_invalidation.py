from __future__ import annotations

import pandas as pd
import pytest
from flask import Flask, session

from src import config
from src.data import crypto, get_finance, investments
from src.data.get import clear_data_cache
from src.model.create_tables import clear_table_cache, get_asset_capital_by_month, get_balance_by_month


@pytest.fixture
def valuation_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "DEBUG", False)
    transactions = tmp_path / "transactions_info" / "2026" / "2026_09_.csv"
    transactions.parent.mkdir(parents=True)
    row = {column: "0" for column in [*config.NOT_COST_COLS, "Прочее"]}
    row["Дата"] = "01.09.2026"
    row["Прочее"] = "100|USD|synthetic"
    pd.DataFrame([row]).to_csv(transactions, sep=";", index=False, encoding="utf-8-sig")
    (tmp_path / "assets_info").mkdir()
    clear_data_cache()
    clear_table_cache()
    get_finance._FX_CACHE_DF.clear()
    yield tmp_path
    clear_data_cache()
    clear_table_cache()
    get_finance._FX_CACHE_DF.clear()


def _fx_rows(rub_usd_rate: float) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-09-01",
                "currency": "USD",
                "usd_rate": 1.0,
                "source": "synthetic",
                "fetched_at": "2026-09-01T00:00:00",
            },
            {
                "date": "2026-09-01",
                "currency": "RUB",
                "usd_rate": rub_usd_rate,
                "source": "synthetic",
                "fetched_at": "2026-09-01T00:00:00",
            },
        ]
    )


def test_fx_write_invalidates_cached_financial_tables(valuation_data):
    get_finance._write_cache(_fx_rows(0.01))
    assert get_balance_by_month("RUB").iloc[0]["Расход"] == 10000

    get_finance._write_cache(_fx_rows(0.02))

    assert get_balance_by_month("RUB").iloc[0]["Расход"] == 5000
    clear_table_cache()
    assert get_balance_by_month("RUB").iloc[0]["Расход"] == 5000


def test_price_write_invalidates_cached_investment_value(valuation_data):
    root = valuation_data
    asset_path = root / "assets_info" / "2026" / "2026_09.csv"
    asset_path.parent.mkdir(parents=True)
    pd.DataFrame([{"Счет": "Cash", "Сумма": "100|RUB"}]).to_csv(
        asset_path, sep=";", index=False, encoding="utf-8-sig"
    )
    investment_path = root / "investments" / "transactions.csv"
    investment_path.parent.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "date": "2026-09-01",
                "operation": "buy",
                "asset_type": "stocks",
                "ticker": "TEST",
                "quantity": "1",
                "price": "10",
                "currency": "RUB",
                "fee": "0",
                "account": "Synthetic",
                "comment": "",
            }
        ]
    ).to_csv(investment_path, sep=";", index=False, encoding="utf-8-sig")
    initial_prices = pd.DataFrame(
        [{"date": "2026-09-01", "ticker": "TEST", "price": 20, "currency": "RUB", "source": "synthetic", "fetched_at": "2026-09-01T00:00:00"}]
    )
    investments.write_price_cache(initial_prices)
    clear_data_cache()
    assert get_asset_capital_by_month("RUB").iloc[0]["Капитал по активам"] == 120

    updated_prices = initial_prices.copy()
    updated_prices.loc[0, "price"] = 30
    investments.write_price_cache(updated_prices)

    assert get_asset_capital_by_month("RUB").iloc[0]["Капитал по активам"] == 130


def test_crypto_balance_write_invalidates_cached_investment_value(
    valuation_data, monkeypatch
):
    from src.data import cache_invalidation

    calls = 0
    original = cache_invalidation.clear_valuation_caches

    def record_clear():
        nonlocal calls
        calls += 1
        original()

    monkeypatch.setattr(crypto, "clear_valuation_caches", record_clear)
    crypto.write_crypto_balances(
        pd.DataFrame(
            [
                {
                    "fetched_at": "2026-09-01T00:00:00",
                    "account": "Synthetic",
                    "chain": "bitcoin",
                    "asset": "BTC",
                    "address": "synthetic",
                    "balance": 1,
                    "source": "synthetic",
                }
            ]
        )
    )

    assert calls == 1


def test_fx_dataframe_cache_isolated_by_live_and_test_paths(tmp_path, monkeypatch):
    live = tmp_path / "live"
    sample = tmp_path / "sample"
    monkeypatch.setattr(config, "DATA_PATH", str(live))
    monkeypatch.setattr(config, "SAMPLE_DATA_PATH", str(sample))
    for root, rate in [(live, 0.01), (sample, 0.02)]:
        path = root / "rates" / "fx_rates.csv"
        path.parent.mkdir(parents=True)
        _fx_rows(rate).to_csv(path, sep=";", index=False)
    get_finance._FX_CACHE_DF.clear()
    app = Flask(__name__)
    app.secret_key = "synthetic"

    with app.test_request_context("/"):
        session["authenticated"] = True
        session["data_mode"] = "live"
        live_rate = get_finance._read_cache().loc[
            lambda data: data["currency"].eq("RUB"), "usd_rate"
        ].iloc[0]
    with app.test_request_context("/"):
        session["authenticated"] = True
        session["data_mode"] = "test"
        test_rate = get_finance._read_cache().loc[
            lambda data: data["currency"].eq("RUB"), "usd_rate"
        ].iloc[0]

    assert live_rate == 0.01
    assert test_rate == 0.02
    assert set(get_finance._FX_CACHE_DF) == {
        str(live / "rates" / "fx_rates.csv"),
        str(sample / "rates" / "fx_rates.csv"),
    }
