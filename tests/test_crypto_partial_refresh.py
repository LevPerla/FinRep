from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from src import config
from src.data import crypto


@pytest.fixture
def crypto_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    investments = tmp_path / "investments"
    return {
        "wallets": investments / "crypto_wallets.csv",
        "balances": investments / "crypto_balances.csv",
        "status": investments / "crypto_refresh_status.csv",
    }


def wallet(account, enabled="1"):
    return {
        "account": account,
        "chain": "bitcoin",
        "asset": "BTC",
        "address": account,
        "token_contract": "",
        "enabled": enabled,
        "label": "",
    }


def balance(account, value, fetched_at="2026-01-01T00:00:00"):
    return {
        "fetched_at": fetched_at,
        "account": account,
        "chain": "bitcoin",
        "asset": "BTC",
        "address": account,
        "balance": value,
        "source": "synthetic",
    }


def write_csv(path: Path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns).to_csv(path, sep=";", index=False, encoding="utf-8-sig")


def test_partial_refresh_keeps_failed_wallet_and_updates_successful_wallet(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A"), wallet("B")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(
        pd.DataFrame([balance("A", "1"), balance("B", "2")]),
        crypto_paths["balances"],
    )

    def fetch(row, timeout):
        if row["account"] == "B":
            raise TimeoutError("synthetic timeout")
        return 1.5

    monkeypatch.setattr(crypto, "_fetch_wallet_balance", fetch)
    result = crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])

    stored = crypto.read_crypto_balances(crypto_paths["balances"]).set_index("account")
    assert set(stored.index) == {"A", "B"}
    assert float(stored.loc["A", "balance"]) == 1.5
    assert float(stored.loc["B", "balance"]) == 2
    assert stored.loc["B", "fetched_at"] == "2026-01-01T00:00:00"
    assert len(result.attrs["errors"]) == 1
    assert "row 3" in result.attrs["errors"][0]
    assert "synthetic timeout" in result.attrs["errors"][0]

    statuses = crypto.read_crypto_refresh_status(crypto_paths["status"]).set_index("account")
    assert statuses.loc["A", "status"] == "ok"
    assert statuses.loc["B", "status"] == "error"
    assert "cached balance retained" in statuses.loc["B", "message"]


def test_repeated_successful_refresh_replaces_balance_without_duplicates(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A"), wallet("B")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(pd.DataFrame([balance("A", "1"), balance("B", "2")]), crypto_paths["balances"])
    values = {"A": 1.5, "B": 2.5}
    monkeypatch.setattr(crypto, "_fetch_wallet_balance", lambda row, timeout: values[row["account"]])

    crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])
    values["A"] = 1.75
    crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])

    stored = crypto.read_crypto_balances(crypto_paths["balances"])
    assert len(stored) == 2
    assert not stored.duplicated(["account", "chain", "asset", "address"]).any()
    assert float(stored.set_index("account").loc["A", "balance"]) == 1.75


def test_failed_wallet_without_cached_balance_remains_unknown(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A"), wallet("B")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(pd.DataFrame([balance("A", "1")]), crypto_paths["balances"])

    def fetch(row, timeout):
        if row["account"] == "B":
            raise TimeoutError("synthetic timeout")
        return 1.5

    monkeypatch.setattr(crypto, "_fetch_wallet_balance", fetch)
    crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])

    stored = crypto.read_crypto_balances(crypto_paths["balances"])
    assert stored["account"].tolist() == ["A"]
    status = crypto.read_crypto_refresh_status(crypto_paths["status"])
    assert "cached balance retained" not in status.loc[status["account"] == "B", "message"].iloc[0]


def test_removed_and_disabled_wallets_are_not_kept_in_current_cache(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A"), wallet("B", enabled="0")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(
        pd.DataFrame([balance("A", "1"), balance("B", "2"), balance("REMOVED", "3")]),
        crypto_paths["balances"],
    )
    monkeypatch.setattr(crypto, "_fetch_wallet_balance", lambda row, timeout: 1.5)

    crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])

    stored = crypto.read_crypto_balances(crypto_paths["balances"])
    assert stored["account"].tolist() == ["A"]


@pytest.mark.parametrize("provider", ["bitcoin", "ton", "kaspa", "xrp"])
def test_incomplete_provider_response_is_not_a_zero_balance(provider, monkeypatch):
    response = Mock()
    response.json.return_value = {}
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)
    monkeypatch.setattr(crypto.requests, "post", lambda *args, **kwargs: response)

    with pytest.raises((KeyError, ValueError, TypeError)):
        getattr(crypto, f"_fetch_{provider}_balance")("synthetic-address", 1)


@pytest.mark.parametrize("provider,payload", [
    ("bitcoin", {"chain_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0},
                 "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0}}),
    ("ton", {"ok": True, "result": 0}),
    ("kaspa", {"balance": 0}),
    ("xrp", {"result": {"error": "actNotFound"}}),
])
def test_explicit_provider_zero_is_kept(provider, payload, monkeypatch):
    response = Mock()
    response.json.return_value = payload
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)
    monkeypatch.setattr(crypto.requests, "post", lambda *args, **kwargs: response)

    assert float(getattr(crypto, f"_fetch_{provider}_balance")("synthetic-address", 1)) == 0


def test_ton_usdt_matches_official_master_not_symbol(monkeypatch):
    response = Mock()
    response.json.return_value = {"balances": [
        {"jetton": {"symbol": "USDt", "address": "synthetic-fake-master", "decimals": 6},
         "balance": "123000000"},
        {"jetton": {"symbol": "USDt", "address": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs", "decimals": 6},
         "balance": "1250000"},
    ]}
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    assert crypto._fetch_ton_jetton_balance("synthetic-address", "USDT", 1) == "1.25"


def test_ton_usdt_ignores_spoofed_symbol(monkeypatch):
    response = Mock()
    response.json.return_value = {"balances": [
        {"jetton": {"symbol": "USDt", "address": "synthetic-fake-master", "decimals": 6},
         "balance": "123000000"},
    ]}
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    assert crypto._fetch_ton_jetton_balance("synthetic-address", "USDT", 1) == "0"


def test_ton_jetton_incomplete_response_is_not_zero(monkeypatch):
    response = Mock()
    response.json.return_value = {}
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    with pytest.raises((KeyError, ValueError, TypeError)):
        crypto._fetch_ton_jetton_balance("synthetic-address", "USDT", 1)


def test_incomplete_response_preserves_cached_balance(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(pd.DataFrame([balance("A", "1")]), crypto_paths["balances"])
    response = Mock()
    response.json.return_value = {}
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    refreshed = crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])

    assert refreshed["balance"].tolist() == ["1"]
    assert crypto.read_crypto_refresh_status(crypto_paths["status"])["status"].tolist() == ["error"]
