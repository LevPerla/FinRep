from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

from src import config


TRANSACTION_COLUMNS = [
    "date",
    "operation",
    "asset_type",
    "ticker",
    "quantity",
    "price",
    "currency",
    "fee",
    "account",
    "comment",
]
INSTRUMENT_COLUMNS = ["ticker", "name", "asset_type", "currency", "provider", "exchange"]
PRICE_CACHE_COLUMNS = ["date", "ticker", "price", "currency", "source", "fetched_at"]

OPERATIONS = {"buy", "sell"}
ASSET_TYPES = {"stocks", "funds", "crypto"}
LEGACY_OPERATION_ALIASES = {"Покупка": "buy", "Продажа": "sell"}
LEGACY_ASSET_TYPE_ALIASES = {"Акции": "stocks", "Фонды": "funds", "Крипто": "crypto"}


@dataclass(frozen=True)
class InvestmentValidationIssue:
    row_number: int | None
    message: str

    def __str__(self) -> str:
        prefix = "file" if self.row_number is None else f"row {self.row_number}"
        return f"{prefix}: {self.message}"


def read_legacy_investments(path: str | Path | None = None) -> pd.DataFrame:
    legacy_path = Path(path or config.INVESTMENTS_PATH)
    data = pd.read_csv(legacy_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    return data


def migrate_legacy_investments(path: str | Path | None = None) -> pd.DataFrame:
    legacy = read_legacy_investments(path)
    rows = []
    for _, row in legacy.iterrows():
        price, currency = _parse_legacy_price(row.get("Цена", ""))
        rows.append(
            {
                "date": _format_date(row.get("Дата", "")),
                "operation": LEGACY_OPERATION_ALIASES.get(str(row.get("Тип_транзакции", "")).strip(), ""),
                "asset_type": LEGACY_ASSET_TYPE_ALIASES.get(str(row.get("Актив", "")).strip(), ""),
                "ticker": str(row.get("Тикер", "")).strip().upper(),
                "quantity": _normalize_decimal(row.get("Количество", "")),
                "price": price,
                "currency": currency,
                "fee": "0",
                "account": "",
                "comment": "",
            }
        )
    return normalize_investment_transactions(pd.DataFrame(rows))


def build_instrument_registry(transactions: pd.DataFrame | None = None) -> pd.DataFrame:
    data = read_investment_transactions() if transactions is None else normalize_investment_transactions(transactions)
    if data.empty:
        return pd.DataFrame(columns=INSTRUMENT_COLUMNS)
    registry = (
        data[["ticker", "asset_type", "currency"]]
        .drop_duplicates(subset=["ticker"], keep="first")
        .sort_values("ticker")
        .reset_index(drop=True)
    )
    registry["name"] = registry["ticker"]
    registry["provider"] = ""
    registry["exchange"] = ""
    return registry[INSTRUMENT_COLUMNS]


def read_investment_transactions(path: str | Path | None = None, legacy_path: str | Path | None = None) -> pd.DataFrame:
    transaction_path = Path(path or config.INVESTMENT_TRANSACTIONS_PATH)
    if transaction_path.exists():
        data = pd.read_csv(transaction_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
        return normalize_investment_transactions(data)
    return migrate_legacy_investments(legacy_path)


def read_instrument_registry(path: str | Path | None = None) -> pd.DataFrame:
    registry_path = Path(path or config.INVESTMENT_INSTRUMENTS_PATH)
    if not registry_path.exists():
        return build_instrument_registry()
    data = pd.read_csv(registry_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in INSTRUMENT_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data = data[INSTRUMENT_COLUMNS].copy(deep=True)
    data["ticker"] = data["ticker"].astype(str).str.strip().str.upper()
    data["asset_type"] = data["asset_type"].astype(str).str.strip().str.lower()
    data["currency"] = data["currency"].astype(str).str.strip().str.upper()
    return data


def read_price_cache(path: str | Path | None = None) -> pd.DataFrame:
    cache_path = _price_cache_path(path)
    if not cache_path.exists():
        return pd.DataFrame(columns=PRICE_CACHE_COLUMNS)
    data = pd.read_csv(cache_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in PRICE_CACHE_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data = data[PRICE_CACHE_COLUMNS].copy(deep=True)
    data["ticker"] = data["ticker"].astype(str).str.strip().str.upper()
    data["currency"] = data["currency"].astype(str).str.strip().str.upper()
    return data


def ensure_price_cache_file(path: str | Path | None = None) -> Path:
    cache_path = _price_cache_path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if not cache_path.exists():
        pd.DataFrame(columns=PRICE_CACHE_COLUMNS).to_csv(cache_path, sep=";", index=False, encoding="utf-8-sig")
    return cache_path


def write_price_cache(data: pd.DataFrame, path: str | Path | None = None) -> None:
    cache_path = ensure_price_cache_file(path)
    normalized = data.copy(deep=True)
    for column in PRICE_CACHE_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[PRICE_CACHE_COLUMNS].fillna("")
    normalized["ticker"] = normalized["ticker"].astype(str).str.strip().str.upper()
    normalized["currency"] = normalized["currency"].astype(str).str.strip().str.upper()
    normalized.to_csv(cache_path, sep=";", index=False, encoding="utf-8-sig")


def seed_price_cache_from_transactions(
    transactions: pd.DataFrame | None = None,
    path: str | Path | None = None,
) -> pd.DataFrame:
    data = read_investment_transactions() if transactions is None else normalize_investment_transactions(transactions)
    cache = read_price_cache(path)
    cached_tickers = set(cache["ticker"].astype(str).str.upper()) if not cache.empty else set()
    missing = sorted(set(data["ticker"]) - cached_tickers)
    if not missing:
        return cache

    fetched_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for ticker in missing:
        ticker_rows = data[data["ticker"] == ticker].sort_values("date", kind="mergesort")
        if ticker_rows.empty:
            continue
        last = ticker_rows.iloc[-1]
        rows.append(
            {
                "date": last["date"],
                "ticker": ticker,
                "price": last["price"],
                "currency": last["currency"],
                "source": "latest_transaction",
                "fetched_at": fetched_at,
            }
        )
    if rows:
        cache = pd.concat([cache, pd.DataFrame(rows)], ignore_index=True)
        write_price_cache(cache, path)
    return read_price_cache(path)


def latest_cached_prices(
    transactions: pd.DataFrame | None = None,
    path: str | Path | None = None,
    seed_missing: bool = True,
) -> pd.DataFrame:
    cache = seed_price_cache_from_transactions(transactions, path) if seed_missing else read_price_cache(path)
    if cache.empty:
        return pd.DataFrame(columns=PRICE_CACHE_COLUMNS)
    normalized = cache.copy(deep=True)
    normalized["date_sort"] = pd.to_datetime(normalized["date"], errors="coerce")
    normalized["fetched_sort"] = pd.to_datetime(normalized["fetched_at"], errors="coerce")
    normalized = normalized.sort_values(["ticker", "date_sort", "fetched_sort"], kind="mergesort")
    return normalized.groupby("ticker", as_index=False).tail(1).drop(columns=["date_sort", "fetched_sort"])


def update_price_cache_from_providers(
    tickers: list[str] | None = None,
    provider_order: list[str] | None = None,
    path: str | Path | None = None,
) -> pd.DataFrame:
    transactions = read_investment_transactions()
    tickers = sorted({str(ticker).strip().upper() for ticker in (tickers or transactions["ticker"].unique()) if str(ticker).strip()})
    provider_order = provider_order or ["yfinance"]
    cache = read_price_cache(path)
    rows = []
    fetched_at = datetime.now().isoformat(timespec="seconds")

    for ticker in tickers:
        instrument = transactions[transactions["ticker"] == ticker].tail(1)
        currency = instrument.iloc[0]["currency"] if not instrument.empty else ""
        for provider in provider_order:
            price = _fetch_latest_price_from_provider(ticker, provider)
            if price is None:
                continue
            rows.append(
                {
                    "date": datetime.now().date().isoformat(),
                    "ticker": ticker,
                    "price": str(float(price)),
                    "currency": currency,
                    "source": provider,
                    "fetched_at": fetched_at,
                }
            )
            break

    if rows:
        cache = pd.concat([cache, pd.DataFrame(rows)], ignore_index=True)
        write_price_cache(cache, path)
    return read_price_cache(path)


def export_legacy_investment_migration(
    transactions_path: str | Path | None = None,
    instruments_path: str | Path | None = None,
    price_cache_path: str | Path | None = None,
    legacy_path: str | Path | None = None,
) -> dict:
    transactions_path = Path(transactions_path or config.INVESTMENT_TRANSACTIONS_PATH)
    instruments_path = Path(instruments_path or config.INVESTMENT_INSTRUMENTS_PATH)
    price_cache_path = Path(price_cache_path or config.INVESTMENT_PRICE_CACHE_PATH)

    transactions = migrate_legacy_investments(legacy_path)
    issues = validate_investment_transactions(transactions)
    if issues:
        raise ValueError(_format_issues(issues))
    instruments = build_instrument_registry(transactions)
    price_cache = pd.DataFrame(columns=PRICE_CACHE_COLUMNS)

    transactions_path.parent.mkdir(parents=True, exist_ok=True)
    transactions.to_csv(transactions_path, sep=";", index=False, encoding="utf-8-sig")
    instruments_path.parent.mkdir(parents=True, exist_ok=True)
    instruments.to_csv(instruments_path, sep=";", index=False, encoding="utf-8-sig")
    price_cache_path.parent.mkdir(parents=True, exist_ok=True)
    if not price_cache_path.exists():
        price_cache.to_csv(price_cache_path, sep=";", index=False, encoding="utf-8-sig")

    return {
        "transactions_path": str(transactions_path),
        "instruments_path": str(instruments_path),
        "price_cache_path": str(price_cache_path),
        "transactions_rows": int(len(transactions)),
        "instruments_rows": int(len(instruments)),
    }


def normalize_investment_transactions(data: pd.DataFrame) -> pd.DataFrame:
    normalized = data.copy(deep=True)
    for column in TRANSACTION_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[TRANSACTION_COLUMNS].fillna("")
    normalized["date"] = normalized["date"].astype(str).str.strip()
    normalized["operation"] = normalized["operation"].astype(str).str.strip().str.lower()
    normalized["asset_type"] = normalized["asset_type"].astype(str).str.strip().str.lower()
    normalized["ticker"] = normalized["ticker"].astype(str).str.strip().str.upper()
    normalized["quantity"] = normalized["quantity"].apply(_normalize_decimal)
    normalized["price"] = normalized["price"].apply(_normalize_decimal)
    normalized["currency"] = normalized["currency"].astype(str).str.strip().str.upper()
    normalized["fee"] = normalized["fee"].replace("", "0").apply(_normalize_decimal)
    normalized["account"] = normalized["account"].astype(str).str.strip()
    normalized["comment"] = normalized["comment"].astype(str).str.strip()
    return normalized


def validate_legacy_investments(path: str | Path | None = None) -> list[InvestmentValidationIssue]:
    legacy_path = Path(path or config.INVESTMENTS_PATH)
    if not legacy_path.exists():
        return [InvestmentValidationIssue(None, "investments file does not exist")]
    try:
        data = read_legacy_investments(legacy_path)
    except Exception as exc:
        return [InvestmentValidationIssue(None, f"cannot read investments CSV: {exc}")]

    required_columns = {"Тип_транзакции", "Актив", "Тикер", "Количество", "Дата", "Цена"}
    missing = sorted(required_columns - set(data.columns))
    if missing:
        return [InvestmentValidationIssue(None, f"missing required column {column!r}") for column in missing]

    issues = []
    for row_number, row in enumerate(data.to_dict("records"), start=2):
        if not str(row.get("Тикер", "")).strip():
            issues.append(InvestmentValidationIssue(row_number, "missing ticker"))
        if str(row.get("Тип_транзакции", "")).strip() not in LEGACY_OPERATION_ALIASES:
            issues.append(InvestmentValidationIssue(row_number, f"unsupported operation {row.get('Тип_транзакции', '')!r}"))
        if str(row.get("Актив", "")).strip() not in LEGACY_ASSET_TYPE_ALIASES:
            issues.append(InvestmentValidationIssue(row_number, f"unsupported asset type {row.get('Актив', '')!r}"))
    if issues:
        return issues

    return validate_investment_transactions(migrate_legacy_investments(legacy_path))


def validate_investment_transactions(data: pd.DataFrame | None = None, path: str | Path | None = None) -> list[InvestmentValidationIssue]:
    issues: list[InvestmentValidationIssue] = []

    raw_data = read_investment_transactions(path) if data is None else data
    missing = [column for column in TRANSACTION_COLUMNS if column not in raw_data.columns]
    for column in missing:
        issues.append(InvestmentValidationIssue(None, f"missing required column {column!r}"))
    if missing:
        return issues

    data = normalize_investment_transactions(raw_data)

    dates = pd.to_datetime(data["date"], errors="coerce")
    for row_number, is_bad in enumerate(dates.isna(), start=2):
        if is_bad:
            issues.append(InvestmentValidationIssue(row_number, "bad date"))

    for row_number, value in enumerate(data["operation"], start=2):
        if value not in OPERATIONS:
            issues.append(InvestmentValidationIssue(row_number, f"unsupported operation {value!r}"))

    for row_number, value in enumerate(data["asset_type"], start=2):
        if value not in ASSET_TYPES:
            issues.append(InvestmentValidationIssue(row_number, f"unsupported asset type {value!r}"))

    for row_number, value in enumerate(data["ticker"], start=2):
        if not str(value).strip():
            issues.append(InvestmentValidationIssue(row_number, "missing ticker"))

    for row_number, value in enumerate(data["quantity"], start=2):
        if not _is_positive_number(value):
            issues.append(InvestmentValidationIssue(row_number, f"bad quantity {value!r}"))

    for row_number, value in enumerate(data["price"], start=2):
        if not _is_non_negative_number(value):
            issues.append(InvestmentValidationIssue(row_number, f"bad price {value!r}"))

    for row_number, value in enumerate(data["fee"], start=2):
        if not _is_non_negative_number(value):
            issues.append(InvestmentValidationIssue(row_number, f"bad fee {value!r}"))

    for row_number, value in enumerate(data["currency"], start=2):
        if value not in config.UNIQUE_TICKERS:
            issues.append(InvestmentValidationIssue(row_number, f"unsupported currency {value!r}"))

    return issues


def _parse_legacy_price(value) -> tuple[str, str]:
    parts = str(value).strip().split("|")
    price = _normalize_decimal(parts[0] if parts else "")
    currency = parts[1].strip().upper() if len(parts) > 1 else "RUB"
    return price, currency


def _format_date(value) -> str:
    parsed = pd.to_datetime(value, dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return str(value).strip()
    return parsed.strftime("%Y-%m-%d")


def _normalize_decimal(value) -> str:
    return str(value).strip().replace(",", ".").replace("\\xa0", "").replace("\xa0", "")


def _is_positive_number(value) -> bool:
    parsed = _to_float(value)
    return parsed is not None and parsed > 0


def _is_non_negative_number(value) -> bool:
    parsed = _to_float(value)
    return parsed is not None and parsed >= 0


def _to_float(value) -> float | None:
    try:
        return float(_normalize_decimal(value))
    except (TypeError, ValueError):
        return None


def _format_issues(issues: list[InvestmentValidationIssue]) -> str:
    return "\n".join(str(issue) for issue in issues)


def _price_cache_path(path: str | Path | None = None) -> Path:
    return Path(path or config.INVESTMENT_PRICE_CACHE_PATH)


def _fetch_latest_price_from_provider(ticker: str, provider: str) -> float | None:
    provider = str(provider).lower()
    if provider != "yfinance":
        return None
    try:
        import yfinance as yf

        data = yf.download(ticker, period="5d", progress=False, auto_adjust=False)
    except Exception:
        return None
    if data is None or data.empty:
        return None
    close = data["Close"]
    if isinstance(close, pd.DataFrame):
        close = close.iloc[:, 0]
    values = pd.to_numeric(close, errors="coerce").dropna()
    if values.empty:
        return None
    return float(values.iloc[-1])
