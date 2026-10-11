from pathlib import Path
import json
from unittest.mock import Mock

import pandas as pd
import pytest

from src import config
from src.data import crypto


def provider_response(payload):
    response = Mock()
    response.iter_content.return_value = [json.dumps(payload).encode()]
    return response


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


def test_negative_provider_balance_cannot_replace_cached_csv_balance(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(pd.DataFrame([balance("A", "1")]), crypto_paths["balances"])
    monkeypatch.setattr(crypto, "_fetch_wallet_balance", lambda row, timeout: "-2")

    result = crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])
    assert crypto.read_crypto_balances(crypto_paths["balances"])["balance"].tolist() == ["1"]
    assert "non-negative" in result.attrs["errors"][0]


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
    response = provider_response({})
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
    response = provider_response(payload)
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)
    monkeypatch.setattr(crypto.requests, "post", lambda *args, **kwargs: response)

    assert float(getattr(crypto, f"_fetch_{provider}_balance")("synthetic-address", 1)) == 0


def test_provider_smallest_units_do_not_round_through_float(monkeypatch):
    satoshis = 10**18 + 1
    response = provider_response({
        "chain_stats": {"funded_txo_sum": satoshis, "spent_txo_sum": 0},
        "mempool_stats": {"funded_txo_sum": 0, "spent_txo_sum": 0},
    })
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)
    assert crypto._fetch_bitcoin_balance("synthetic-address", 1) == "10000000000.00000001"

    monkeypatch.setattr(crypto, "_evm_rpc", lambda *args: hex(10**40 + 1))
    evm = pd.Series({"chain": "ethereum", "asset": "ETH", "address": "0x" + "a" * 40})
    assert crypto._fetch_evm_balance(evm, 1) == f"{10**22}.000000000000000001"


def test_ton_usdt_matches_official_master_not_symbol(monkeypatch):
    response = provider_response({"balances": [
        {"jetton": {"symbol": "USDt", "address": "synthetic-fake-master", "decimals": 6},
         "balance": "123000000"},
        {"jetton": {"symbol": "USDt", "address": "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs", "decimals": 6},
         "balance": "1250000"},
    ]})
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    assert crypto._fetch_ton_jetton_balance("synthetic-address", "USDT", 1) == "1.25"


def test_ton_usdt_ignores_spoofed_symbol(monkeypatch):
    response = provider_response({"balances": [
        {"jetton": {"symbol": "USDt", "address": "synthetic-fake-master", "decimals": 6},
         "balance": "123000000"},
    ]})
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    assert crypto._fetch_ton_jetton_balance("synthetic-address", "USDT", 1) == "0"


def test_ton_jetton_incomplete_response_is_not_zero(monkeypatch):
    response = provider_response({})
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    with pytest.raises((KeyError, ValueError, TypeError)):
        crypto._fetch_ton_jetton_balance("synthetic-address", "USDT", 1)


def test_incomplete_response_preserves_cached_balance(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(pd.DataFrame([balance("A", "1")]), crypto_paths["balances"])
    response = provider_response({})
    monkeypatch.setattr(crypto.requests, "get", lambda *args, **kwargs: response)

    refreshed = crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])

    assert refreshed["balance"].tolist() == ["1"]
    assert crypto.read_crypto_refresh_status(crypto_paths["status"])["status"].tolist() == ["error"]


@pytest.mark.parametrize("bad_price", ["NaN", "Infinity", "-1", "0", "not-a-price"])
def test_bad_coingecko_price_falls_back_without_poisoning_cache(bad_price, monkeypatch):
    response = provider_response({"bitcoin": {"usd": bad_price}})
    seen = []

    def fake_get(*args, **kwargs):
        seen.append(kwargs)
        return response

    monkeypatch.setattr(crypto.requests, "get", fake_get)
    monkeypatch.setattr(crypto, "_fetch_binance_price", lambda *args: 123.0)

    assert crypto._fetch_crypto_price("BTC", "USD", 1) == (123.0, "binance")
    assert seen[0]["allow_redirects"] is False


def test_provider_response_size_is_bounded_and_closed():
    response = Mock()
    response.iter_content.return_value = [b"x" * (crypto.MAX_RESPONSE_BYTES + 1)]
    with pytest.raises(ValueError, match="size limit"):
        crypto._response_json(response, 1)
    response.close.assert_called_once()


def test_slow_provider_response_hits_time_budget(monkeypatch):
    response = provider_response({"result": "1"})
    clock = iter([0, 2])
    monkeypatch.setattr(crypto, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="response time budget"):
        crypto._response_json(response, 1)
    response.close.assert_called_once()


def test_refresh_budget_stops_later_wallets_without_losing_cache(crypto_paths, monkeypatch):
    write_csv(crypto_paths["wallets"], [wallet("A"), wallet("B")], crypto.WALLET_COLUMNS)
    crypto.write_crypto_balances(pd.DataFrame([balance("B", "2")]), crypto_paths["balances"])
    clock = iter([0, 0, crypto.REFRESH_BUDGET_SECONDS + 1])
    monkeypatch.setattr(crypto, "monotonic", lambda: next(clock))
    fetched = []
    monkeypatch.setattr(crypto, "_fetch_wallet_balance", lambda row, timeout: fetched.append(row["account"]) or "1")

    result = crypto.refresh_crypto_balances(crypto_paths["wallets"], crypto_paths["balances"])

    assert fetched == ["A"]
    assert result.set_index("account").loc["B", "balance"] == "2"
    assert "TimeoutError" not in result.attrs["errors"][0]
    assert "time budget exceeded" in result.attrs["errors"][0]


def test_only_verified_evm_token_contracts_are_accepted():
    address = "0x" + "a" * 40
    ethereum_usdt = pd.DataFrame([{
        **wallet("synthetic"), "chain": "ethereum", "asset": "USDT",
        "address": address, "token_contract": "",
    }])
    assert crypto.validate_crypto_wallets(ethereum_usdt) == []
    ethereum_usdt.loc[0, "token_contract"] = "0x" + "b" * 40
    assert "unverified token contract" in str(crypto.validate_crypto_wallets(ethereum_usdt)[0])
    ethereum_usdt.loc[0, "chain"] = "base"
    assert "unverified token contract" in str(crypto.validate_crypto_wallets(ethereum_usdt)[0])


def test_evm_rpc_rejects_wrong_chain_before_balance(monkeypatch):
    methods = []

    def post(*args, **kwargs):
        methods.append(kwargs["json"]["method"])
        return provider_response({"result": "0x2105"})

    monkeypatch.setattr(crypto.requests, "post", post)
    with pytest.raises(ValueError, match="all EVM RPC providers failed"):
        crypto._evm_rpc("ethereum", "eth_getBalance", ["synthetic", "latest"], 1)
    assert methods == ["eth_chainId"] * len(crypto.EVM_RPC_URLS["ethereum"])


def test_provider_error_does_not_store_wallet_address_or_url():
    address = "synthetic-private-address"
    message = crypto._safe_error(ValueError(f"https://example.test/{address}: failed {address}"), address)
    assert address not in message
    assert "example.test" not in message


def test_direct_crypto_refresh_requires_login_and_test_mode_cannot_call_providers(monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-secret")
    from src.dashboard import app as dashboard_app

    app = dashboard_app.create_app()
    client = app.server.test_client()
    key = next(key for key in app.callback_map if "crypto-refresh-status.data" in key)
    callback = app.callback_map[key]
    payload = {
        "output": key,
        "outputs": [{"id": item.component_id, "property": item.component_property}
                    for item in callback["output"]],
        "inputs": [{"id": "crypto-refresh-button", "property": "n_clicks", "value": 1}],
        "state": [{"id": "dashboard-refresh-token", "property": "data", "value": 0}],
        "changedPropIds": ["crypto-refresh-button.n_clicks"],
    }
    assert client.post("/_dash-update-component", json=payload).status_code == 401
    client.post("/login", data={"data_mode": "test"})
    monkeypatch.setattr(dashboard_app, "refresh_crypto_balances",
                        lambda: pytest.fail("provider called in TEST"))
    response = client.post("/_dash-update-component", json=payload)
    assert response.status_code == 200
    assert "read-only" in response.get_data(as_text=True).lower() or "test" in response.get_data(as_text=True).lower()
