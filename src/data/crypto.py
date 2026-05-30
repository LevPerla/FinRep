from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re

import pandas as pd
import requests

from src import config
from src.data.get_finance import get_actual_fx_rate, get_fallback_rate
from src.data.investments import latest_cached_prices, read_price_cache, write_price_cache


WALLET_COLUMNS = ["account", "chain", "asset", "address", "token_contract", "enabled", "label"]
BALANCE_COLUMNS = ["fetched_at", "account", "chain", "asset", "address", "balance", "source"]
REFRESH_STATUS_COLUMNS = ["fetched_at", "row_number", "account", "chain", "asset", "address", "status", "message"]
TRANSACTION_COLUMNS = [
    "date",
    "account",
    "chain",
    "asset",
    "address",
    "tx_id",
    "operation",
    "quantity",
    "fee",
    "counterparty",
    "source",
    "comment",
]

SUPPORTED_ASSETS = {"BTC", "ETH", "TON", "SOL", "LINK", "KAS", "USDT", "XRP"}
SUPPORTED_CHAINS = {"bitcoin", "ethereum", "base", "ton", "solana", "kaspa", "xrp"}
COINGECKO_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "TON": "toncoin",
    "SOL": "solana",
    "LINK": "chainlink",
    "KAS": "kaspa",
    "USDT": "tether",
    "XRP": "ripple",
}
BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "TON": "TONUSDT",
    "SOL": "SOLUSDT",
    "LINK": "LINKUSDT",
    "USDT": "USDTUSDT",
    "XRP": "XRPUSDT",
}
DEFAULT_TOKEN_CONTRACTS = {
    ("ethereum", "LINK"): "0x514910771af9ca656af840dff83e8264ecf986ca",
    ("ethereum", "USDT"): "0xdac17f958d2ee523a2206206994597c13d831ec7",
}
EVM_RPC_URLS = {
    "ethereum": [
        "https://eth.llamarpc.com",
        "https://ethereum.publicnode.com",
        "https://rpc.ankr.com/eth",
    ],
    "base": [
        "https://base.llamarpc.com",
        "https://mainnet.base.org",
        "https://base-rpc.publicnode.com",
    ],
}
TOKEN_DECIMALS = {"LINK": 18, "USDT": 6}
TON_JETTONS = {
    "USDT": {
        "symbol": "USDt",
        "master": "EQCxE6mXca2m3DqksTX4J9i5wK5Q9c3iH8YQ3q0A9m5a3rYw",
    },
}
EVM_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass(frozen=True)
class CryptoValidationIssue:
    row_number: int | None
    message: str

    def __str__(self) -> str:
        prefix = "file" if self.row_number is None else f"row {self.row_number}"
        return f"{prefix}: {self.message}"


def ensure_crypto_wallets_file(path: str | Path | None = None) -> Path:
    wallets_path = Path(path or config.CRYPTO_WALLETS_PATH)
    wallets_path.parent.mkdir(parents=True, exist_ok=True)
    if not wallets_path.exists():
        pd.DataFrame(columns=WALLET_COLUMNS).to_csv(wallets_path, sep=";", index=False, encoding="utf-8-sig")
    return wallets_path


def read_crypto_wallets(path: str | Path | None = None) -> pd.DataFrame:
    wallets_path = ensure_crypto_wallets_file(path)
    data = pd.read_csv(wallets_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in WALLET_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data = data[WALLET_COLUMNS].copy(deep=True)
    data["chain"] = data["chain"].astype(str).str.strip().str.lower()
    data["asset"] = data["asset"].astype(str).str.strip().str.upper()
    data["enabled"] = data["enabled"].replace("", "1").astype(str).str.strip().str.lower()
    data["token_contract"] = data["token_contract"].astype(str).str.strip().str.lower()
    data["address"] = data["address"].astype(str).str.strip()
    return data


def read_crypto_balances(path: str | Path | None = None) -> pd.DataFrame:
    balance_path = Path(path or config.CRYPTO_BALANCES_PATH)
    if not balance_path.exists():
        return pd.DataFrame(columns=BALANCE_COLUMNS)
    data = pd.read_csv(balance_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in BALANCE_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data = data[BALANCE_COLUMNS].copy(deep=True)
    data["chain"] = data["chain"].astype(str).str.strip().str.lower()
    data["asset"] = data["asset"].astype(str).str.strip().str.upper()
    return data


def read_crypto_refresh_status(path: str | Path | None = None) -> pd.DataFrame:
    status_path = Path(path or config.CRYPTO_REFRESH_STATUS_PATH)
    if not status_path.exists():
        return pd.DataFrame(columns=REFRESH_STATUS_COLUMNS)
    data = pd.read_csv(status_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in REFRESH_STATUS_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    return data[REFRESH_STATUS_COLUMNS].copy(deep=True)


def write_crypto_refresh_status(data: pd.DataFrame, path: str | Path | None = None) -> None:
    status_path = Path(path or config.CRYPTO_REFRESH_STATUS_PATH)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = data.copy(deep=True)
    for column in REFRESH_STATUS_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[REFRESH_STATUS_COLUMNS].fillna("")
    normalized.to_csv(status_path, sep=";", index=False, encoding="utf-8-sig")


def write_crypto_balances(data: pd.DataFrame, path: str | Path | None = None) -> None:
    balance_path = Path(path or config.CRYPTO_BALANCES_PATH)
    balance_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = data.copy(deep=True)
    for column in BALANCE_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[BALANCE_COLUMNS].fillna("")
    normalized.to_csv(balance_path, sep=";", index=False, encoding="utf-8-sig")


def read_crypto_transactions(path: str | Path | None = None) -> pd.DataFrame:
    transactions_path = Path(path or config.CRYPTO_TRANSACTIONS_PATH)
    if not transactions_path.exists():
        return pd.DataFrame(columns=TRANSACTION_COLUMNS)
    data = pd.read_csv(transactions_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in TRANSACTION_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    return data[TRANSACTION_COLUMNS].copy(deep=True)


def write_crypto_transactions(data: pd.DataFrame, path: str | Path | None = None) -> None:
    transactions_path = Path(path or config.CRYPTO_TRANSACTIONS_PATH)
    transactions_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = data.copy(deep=True)
    for column in TRANSACTION_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[TRANSACTION_COLUMNS].fillna("")
    normalized.to_csv(transactions_path, sep=";", index=False, encoding="utf-8-sig")


def validate_crypto_wallets(data: pd.DataFrame | None = None, path: str | Path | None = None) -> list[CryptoValidationIssue]:
    wallets = read_crypto_wallets(path) if data is None else data.copy(deep=True)
    issues: list[CryptoValidationIssue] = []
    missing = [column for column in WALLET_COLUMNS if column not in wallets.columns]
    for column in missing:
        issues.append(CryptoValidationIssue(None, f"missing required column {column!r}"))
    if missing:
        return issues

    wallets = read_crypto_wallets(path) if data is None else _normalize_wallet_rows(wallets)
    for row_number, row in enumerate(wallets.to_dict("records"), start=2):
        issues.extend(_validate_wallet_row(row, row_number))
    return issues


def refresh_crypto_balances(
    wallets_path: str | Path | None = None,
    balances_path: str | Path | None = None,
    timeout: int = 20,
) -> pd.DataFrame:
    wallets = read_crypto_wallets(wallets_path)
    fetched_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    errors = []
    statuses = []
    status_rows = []
    for index, wallet in wallets.iterrows():
        if _is_disabled(wallet):
            continue
        row_number = int(index) + 2
        row_issues = _validate_wallet_row(wallet.to_dict(), int(index) + 2)
        if row_issues:
            errors.extend(str(issue) for issue in row_issues)
            for issue in row_issues:
                status_rows.append(_refresh_status_row(fetched_at, wallet, row_number, "error", issue.message))
            continue
        try:
            balance = _fetch_wallet_balance(wallet, timeout=timeout)
            rows.append(
                {
                    "fetched_at": fetched_at,
                    "account": wallet["account"],
                    "chain": wallet["chain"],
                    "asset": wallet["asset"],
                    "address": wallet["address"],
                    "balance": balance,
                    "source": _provider_name(wallet),
                }
            )
            message = f"balance={balance}"
            statuses.append(f"row {row_number}: {wallet['chain']}/{wallet['asset']} ok, {message}")
            status_rows.append(_refresh_status_row(fetched_at, wallet, row_number, "ok", message))
        except Exception as exc:
            message = f"{wallet['chain']}/{wallet['asset']} balance refresh failed: {exc}"
            errors.append(f"row {row_number}: {message}")
            status_rows.append(_refresh_status_row(fetched_at, wallet, row_number, "error", message))

    balances = pd.DataFrame(rows, columns=BALANCE_COLUMNS)
    if not balances.empty:
        write_crypto_balances(balances, balances_path)
    else:
        balances = read_crypto_balances(balances_path)
    balances.attrs["errors"] = errors
    balances.attrs["statuses"] = statuses
    if status_rows:
        write_crypto_refresh_status(pd.DataFrame(status_rows, columns=REFRESH_STATUS_COLUMNS))
    return balances


def refresh_crypto_transactions(
    wallets_path: str | Path | None = None,
    transactions_path: str | Path | None = None,
    timeout: int = 20,
) -> pd.DataFrame:
    wallets = read_crypto_wallets(wallets_path)
    issues = validate_crypto_wallets(wallets)
    if issues:
        raise ValueError("\n".join(str(issue) for issue in issues))

    rows = []
    for _, wallet in wallets.iterrows():
        if _is_disabled(wallet):
            continue
        rows.extend(_fetch_wallet_transactions(wallet, timeout=timeout))

    if not rows:
        transactions = read_crypto_transactions(transactions_path)
    else:
        incoming = pd.DataFrame(rows, columns=TRANSACTION_COLUMNS)
        existing = read_crypto_transactions(transactions_path)
        transactions = pd.concat([existing, incoming], ignore_index=True)
        transactions = transactions.drop_duplicates(subset=["chain", "asset", "address", "tx_id"], keep="last")
        transactions = transactions.sort_values(["date", "chain", "asset"], kind="mergesort")
        write_crypto_transactions(transactions, transactions_path)
    return transactions


def refresh_crypto_price_cache(
    assets: list[str] | None = None,
    currency: str = "USD",
    price_cache_path: str | Path | None = None,
    timeout: int = 20,
) -> pd.DataFrame:
    assets = sorted({str(asset).upper() for asset in (assets or SUPPORTED_ASSETS)})
    fetched_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for asset in assets:
        price, source = _fetch_crypto_price(asset, currency, timeout)
        if price is None:
            continue
        rows.append(
            {
                "date": datetime.now().date().isoformat(),
                "ticker": asset,
                "price": str(float(price)),
                "currency": currency.upper(),
                "source": source,
                "fetched_at": fetched_at,
            }
        )
    cache = read_price_cache(price_cache_path)
    if rows:
        cache = pd.concat([cache, pd.DataFrame(rows)], ignore_index=True)
        write_price_cache(cache, price_cache_path)
    return read_price_cache(price_cache_path)


def _fetch_crypto_price(asset: str, currency: str, timeout: int) -> tuple[float | None, str]:
    price = _fetch_coingecko_price(asset, currency, timeout)
    if price is not None:
        return price, "coingecko"
    price = _fetch_binance_price(asset, currency, timeout)
    if price is not None:
        return price, "binance"
    return None, ""


def _fetch_coingecko_price(asset: str, currency: str, timeout: int) -> float | None:
    coin_id = COINGECKO_IDS.get(asset)
    if not coin_id:
        return None
    try:
        response = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": coin_id, "vs_currencies": currency.lower()},
            timeout=timeout,
        )
        response.raise_for_status()
        price = response.json().get(coin_id, {}).get(currency.lower())
    except Exception:
        return None
    return None if price is None else float(price)


def _fetch_binance_price(asset: str, currency: str, timeout: int) -> float | None:
    if currency.upper() != "USD":
        return None
    if asset == "USDT":
        return 1.0
    symbol = BINANCE_SYMBOLS.get(asset)
    if not symbol:
        return None
    try:
        response = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=timeout,
        )
        response.raise_for_status()
        price = response.json().get("price")
    except Exception:
        return None
    return None if price is None else float(price)


def calculate_crypto_positions(currency: str) -> pd.DataFrame:
    balances = _latest_balances()
    if balances.empty:
        return _empty_crypto_positions()
    prices = latest_cached_prices(seed_missing=False)
    price_by_ticker = prices.set_index("ticker").to_dict("index") if not prices.empty else {}
    rows = []
    for _, balance in balances.iterrows():
        quantity = pd.to_numeric(balance["balance"], errors="coerce")
        if pd.isna(quantity) or quantity <= 0:
            continue
        ticker = str(balance["asset"]).upper()
        price_info = price_by_ticker.get(ticker, {})
        latest_price = pd.to_numeric(price_info.get("price"), errors="coerce")
        if pd.isna(latest_price):
            latest_price = 0.0
        price_currency = str(price_info.get("currency") or "USD").upper()
        market_value = float(quantity) * float(latest_price) * _conversion_rate(price_currency, currency)
        rows.append(
            {
                "ticker": ticker,
                "asset_type": "crypto",
                "account": balance["account"],
                "quantity": float(quantity),
                "average_cost": 0.0,
                "latest_price": float(latest_price),
                "currency": price_currency,
                "cost_basis": 0.0,
                "market_value": market_value,
                "unrealized_pnl": market_value,
                "realized_pnl": 0.0,
                "total_pnl": market_value,
                "allocation": 0.0,
                "price_date": price_info.get("date", ""),
                "price_source": price_info.get("source", ""),
                "sold_quantity": 0.0,
                "chain": balance["chain"],
                "address": balance["address"],
            }
        )
    positions = pd.DataFrame(rows)
    if positions.empty:
        return _empty_crypto_positions()
    return positions


def _latest_balances() -> pd.DataFrame:
    balances = read_crypto_balances()
    if balances.empty:
        return balances
    balances["fetched_sort"] = pd.to_datetime(balances["fetched_at"], errors="coerce")
    balances = balances.sort_values(["account", "chain", "asset", "address", "fetched_sort"], kind="mergesort")
    return balances.groupby(["account", "chain", "asset", "address"], as_index=False).tail(1).drop(columns=["fetched_sort"])


def _fetch_wallet_balance(wallet: pd.Series, timeout: int) -> str:
    chain = wallet["chain"]
    asset = wallet["asset"]
    address = wallet["address"]
    if chain == "bitcoin" and asset == "BTC":
        return _fetch_bitcoin_balance(address, timeout)
    if chain in EVM_RPC_URLS:
        return _fetch_evm_balance(wallet, timeout)
    if chain == "solana" and asset == "SOL":
        return _fetch_solana_balance(address, timeout)
    if chain == "ton" and asset == "TON":
        return _fetch_ton_balance(address, timeout)
    if chain == "ton" and asset in TON_JETTONS:
        return _fetch_ton_jetton_balance(address, asset, timeout)
    if chain == "kaspa" and asset == "KAS":
        return _fetch_kaspa_balance(address, timeout)
    if chain == "xrp" and asset == "XRP":
        return _fetch_xrp_balance(address, timeout)
    raise ValueError(f"balance provider is not implemented for {chain}/{asset}")


def _fetch_wallet_transactions(wallet: pd.Series, timeout: int) -> list[dict]:
    if wallet["chain"] == "bitcoin" and wallet["asset"] == "BTC":
        return _fetch_bitcoin_transactions(wallet, timeout)
    return []


def _fetch_bitcoin_balance(address: str, timeout: int) -> str:
    response = requests.get(f"https://blockstream.info/api/address/{address}", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    chain_stats = payload.get("chain_stats", {})
    mempool_stats = payload.get("mempool_stats", {})
    sats = (
        chain_stats.get("funded_txo_sum", 0)
        - chain_stats.get("spent_txo_sum", 0)
        + mempool_stats.get("funded_txo_sum", 0)
        - mempool_stats.get("spent_txo_sum", 0)
    )
    return str(float(sats) / 100_000_000)


def _fetch_bitcoin_transactions(wallet: pd.Series, timeout: int) -> list[dict]:
    response = requests.get(f"https://blockstream.info/api/address/{wallet['address']}/txs", timeout=timeout)
    response.raise_for_status()
    rows = []
    for tx in response.json():
        status = tx.get("status", {})
        block_time = status.get("block_time")
        date = datetime.fromtimestamp(block_time).date().isoformat() if block_time else datetime.now().date().isoformat()
        rows.append(
            {
                "date": date,
                "account": wallet["account"],
                "chain": wallet["chain"],
                "asset": wallet["asset"],
                "address": wallet["address"],
                "tx_id": tx.get("txid", ""),
                "operation": "transfer",
                "quantity": "",
                "fee": str(float(tx.get("fee", 0)) / 100_000_000),
                "counterparty": "",
                "source": "blockstream",
                "comment": wallet.get("label", ""),
            }
        )
    return rows


def _fetch_evm_balance(wallet: pd.Series, timeout: int) -> str:
    chain = wallet["chain"]
    asset = wallet["asset"]
    address = wallet["address"]
    if asset in {"ETH"}:
        result = _evm_rpc(chain, "eth_getBalance", [address, "latest"], timeout)
        return str(int(result, 16) / 10**18)
    token_contract = _token_contract(wallet)
    decimals = TOKEN_DECIMALS.get(asset, 18)
    data = "0x70a08231" + address.lower().replace("0x", "").rjust(64, "0")
    result = _evm_rpc(chain, "eth_call", [{"to": token_contract, "data": data}, "latest"], timeout)
    return str(int(result, 16) / 10**decimals)


def _fetch_solana_balance(address: str, timeout: int) -> str:
    response = requests.post(
        "https://api.mainnet-beta.solana.com",
        json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [address]},
        timeout=timeout,
    )
    response.raise_for_status()
    return str(response.json()["result"]["value"] / 10**9)


def _fetch_ton_balance(address: str, timeout: int) -> str:
    response = requests.get(
        "https://toncenter.com/api/v2/getAddressBalance",
        params={"address": address},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok", True):
        raise ValueError(payload)
    return str(float(payload.get("result", 0)) / 10**9)


def _fetch_ton_jetton_balance(address: str, asset: str, timeout: int) -> str:
    response = requests.get(
        f"https://tonapi.io/v2/accounts/{address}/jettons",
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    expected = TON_JETTONS[asset]
    for item in payload.get("balances", []):
        jetton = item.get("jetton", {})
        symbol = str(jetton.get("symbol", "")).upper()
        master = str(jetton.get("address", ""))
        if symbol != expected["symbol"].upper() and master != expected["master"]:
            continue
        decimals = int(jetton.get("decimals", TOKEN_DECIMALS.get(asset, 6)))
        return str(float(item.get("balance", 0)) / 10**decimals)
    return "0"


def _fetch_kaspa_balance(address: str, timeout: int) -> str:
    response = requests.get(f"https://api.kaspa.org/addresses/{address}/balance", timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    sompi = payload.get("balance", payload.get("balanceSompi", 0))
    return str(float(sompi) / 100_000_000)


def _fetch_xrp_balance(address: str, timeout: int) -> str:
    response = requests.post(
        "https://s1.ripple.com:51234/",
        json={
            "method": "account_info",
            "params": [{"account": address, "ledger_index": "validated"}],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    result = response.json().get("result", {})
    if result.get("error") == "actNotFound":
        return "0"
    if "error" in result:
        raise ValueError(result)
    drops = result.get("account_data", {}).get("Balance", 0)
    return str(float(drops) / 1_000_000)


def _evm_rpc(chain: str, method: str, params: list, timeout: int):
    errors = []
    for url in EVM_RPC_URLS[chain]:
        try:
            response = requests.post(
                url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if "error" in payload:
                raise ValueError(payload["error"])
            return payload["result"]
        except Exception as exc:
            errors.append(f"{url}: {exc}")
    raise ValueError("all EVM RPC providers failed: " + " | ".join(errors))


def _conversion_rate(from_currency: str, to_currency: str) -> float:
    if from_currency == to_currency:
        return 1.0
    rate = get_actual_fx_rate(from_currency, to_currency)
    if rate is None:
        rate = get_fallback_rate(from_currency, to_currency)
    return float(rate) if rate is not None else 1.0


def _normalize_wallet_rows(data: pd.DataFrame) -> pd.DataFrame:
    normalized = data.copy(deep=True)
    for column in WALLET_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[WALLET_COLUMNS].fillna("")
    normalized["chain"] = normalized["chain"].astype(str).str.strip().str.lower()
    normalized["asset"] = normalized["asset"].astype(str).str.strip().str.upper()
    normalized["enabled"] = normalized["enabled"].replace("", "1").astype(str).str.strip().str.lower()
    normalized["address"] = normalized["address"].astype(str).str.strip()
    normalized["token_contract"] = normalized["token_contract"].astype(str).str.strip().str.lower()
    return normalized


def _refresh_status_row(fetched_at: str, wallet: pd.Series, row_number: int, status: str, message: str) -> dict:
    return {
        "fetched_at": fetched_at,
        "row_number": row_number,
        "account": wallet.get("account", ""),
        "chain": wallet.get("chain", ""),
        "asset": wallet.get("asset", ""),
        "address": wallet.get("address", ""),
        "status": status,
        "message": message,
    }


def _validate_wallet_row(row: pd.Series | dict, row_number: int) -> list[CryptoValidationIssue]:
    if _is_disabled(row):
        return []
    issues = []
    chain = str(row.get("chain", "")).strip().lower()
    asset = str(row.get("asset", "")).strip().upper()
    address = str(row.get("address", "")).strip()
    token_contract = str(_token_contract(row)).strip()
    if not str(row.get("account", "")).strip():
        issues.append(CryptoValidationIssue(row_number, "account is required"))
    if chain not in SUPPORTED_CHAINS:
        issues.append(CryptoValidationIssue(row_number, f"unsupported chain {chain!r}"))
    if asset not in SUPPORTED_ASSETS:
        issues.append(CryptoValidationIssue(row_number, f"unsupported asset {asset!r}"))
    if not address:
        issues.append(CryptoValidationIssue(row_number, "address is required"))
    if chain in EVM_RPC_URLS and address and not EVM_ADDRESS_RE.match(address):
        issues.append(CryptoValidationIssue(row_number, f"{chain} address should look like 0x + 40 hex chars"))
    if chain == "xrp" and address and not address.startswith("r"):
        issues.append(CryptoValidationIssue(row_number, "xrp address should start with 'r'"))
    if token_contract and not EVM_ADDRESS_RE.match(token_contract):
        issues.append(CryptoValidationIssue(row_number, "token_contract should look like 0x + 40 hex chars"))
    if asset in TOKEN_DECIMALS and chain in EVM_RPC_URLS and not token_contract:
        issues.append(CryptoValidationIssue(row_number, f"token_contract is required for {asset} on {chain}"))
    return issues


def _token_contract(wallet: pd.Series | dict) -> str:
    chain = str(wallet.get("chain", "")).lower()
    asset = str(wallet.get("asset", "")).upper()
    return str(wallet.get("token_contract", "") or DEFAULT_TOKEN_CONTRACTS.get((chain, asset), "")).lower()


def _is_disabled(row: pd.Series | dict) -> bool:
    return str(row.get("enabled", "1")).strip().lower() in {"0", "false", "no", "off"}


def _provider_name(wallet: pd.Series) -> str:
    if wallet["chain"] == "bitcoin":
        return "blockstream"
    if wallet["chain"] in EVM_RPC_URLS:
        return f"{wallet['chain']}_rpc"
    if wallet["chain"] == "solana":
        return "solana_rpc"
    if wallet["chain"] == "ton":
        return "toncenter"
    if wallet["chain"] == "kaspa":
        return "kaspa_api"
    if wallet["chain"] == "xrp":
        return "xrpl_rpc"
    return wallet["chain"]


def _empty_crypto_positions() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "ticker",
            "asset_type",
            "account",
            "quantity",
            "average_cost",
            "latest_price",
            "currency",
            "cost_basis",
            "market_value",
            "unrealized_pnl",
            "realized_pnl",
            "total_pnl",
            "allocation",
            "price_date",
            "price_source",
            "sold_quantity",
            "chain",
            "address",
        ]
    )
