from __future__ import annotations

from datetime import datetime
from math import isfinite
from pathlib import Path

import pandas as pd

from src import config
from src.data.csv_storage import atomic_copy_file, atomic_write_csv

ASSET_EDITOR_COLUMNS = ["account", "amount", "currency"]


def asset_snapshot_path(year: str, month: str, assets_root: str | Path | None = None) -> Path:
    year = str(year)
    month = str(int(month)).zfill(2)
    root = Path(assets_root or config.active_data_path("assets_info"))
    return root / year / f"{year}_{month}.csv"


def ensure_asset_snapshot(year: str, month: str, assets_root: str | Path | None = None) -> dict:
    target_path = asset_snapshot_path(year, month, assets_root)
    if target_path.exists():
        return {"path": str(target_path), "created": False, "template_path": None}
    config.require_writable_mode()

    template_path = previous_asset_snapshot_path(year, month, assets_root)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if template_path is not None:
        atomic_copy_file(template_path, target_path)
    else:
        atomic_write_csv(
            pd.DataFrame(columns=["Счет", "Сумма"]),
            target_path,
            sep=";",
            index=False,
            encoding="utf-8-sig",
        )

    return {
        "path": str(target_path),
        "created": True,
        "template_path": None if template_path is None else str(template_path),
    }


def previous_asset_snapshot_path(year: str, month: str, assets_root: str | Path | None = None) -> Path | None:
    root = Path(assets_root or config.active_data_path("assets_info"))
    current = pd.Period(f"{int(year):04d}-{int(month):02d}", freq="M") - 1
    for _ in range(240):
        candidate = root / str(current.year) / f"{current.year}_{current.month:02d}.csv"
        if candidate.exists():
            return candidate
        current -= 1
    return None


def read_asset_snapshot(year: str, month: str, assets_root: str | Path | None = None) -> pd.DataFrame:
    target_path = asset_snapshot_path(year, month, assets_root)
    path = target_path
    if not path.exists():
        template = previous_asset_snapshot_path(year, month, assets_root)
        if template is not None:
            path = template
        elif config.is_test_mode():
            return pd.DataFrame(columns=ASSET_EDITOR_COLUMNS)
        else:
            ensure_asset_snapshot(year, month, assets_root)
    try:
        data = pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig", keep_default_na=False)
    except (pd.errors.ParserError, pd.errors.EmptyDataError, UnicodeError) as exc:
        raise ValueError(f"{path.name}: не удалось прочитать CSV снимка активов.") from exc
    missing = [column for column in ["Счет", "Сумма"] if column not in data.columns]
    if missing:
        raise ValueError(f"{path.name}: отсутствуют обязательные колонки: {', '.join(missing)}")

    rows = []
    for row_number, (_, row) in enumerate(data[["Счет", "Сумма"]].iterrows(), start=2):
        try:
            amount, currency = _parse_asset_cell(row["Сумма"])
        except ValueError as exc:
            raise ValueError(f"{path.name}: строка {row_number}, счёт {row['Счет']!r}: {exc}") from exc
        rows.append({"account": str(row["Счет"]), "amount": amount, "currency": currency})
    # Validate the template before creating a new LIVE snapshot from it.
    if path != target_path and not config.is_test_mode():
        ensure_asset_snapshot(year, month, assets_root)
    return pd.DataFrame(rows, columns=ASSET_EDITOR_COLUMNS)


def write_asset_snapshot(rows: list[dict], year: str, month: str, assets_root: str | Path | None = None) -> dict:
    config.require_writable_mode()
    target_path = asset_snapshot_path(year, month, assets_root)
    data = _normalize_asset_rows(pd.DataFrame(rows))
    ensure_info = ensure_asset_snapshot(year, month, assets_root)

    backup_path = None
    if target_path.exists():
        backup_path = _asset_snapshot_backup_path(target_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_copy_file(target_path, backup_path)

    output = pd.DataFrame({
        "Счет": data["account"],
        "Сумма": data.apply(lambda row: f"{_format_asset_amount(row['amount'])}|{row['currency']}", axis=1),
    })
    atomic_write_csv(output, target_path, sep=";", index=False, encoding="utf-8-sig")
    return {
        "path": str(target_path),
        "backup_path": None if backup_path is None else str(backup_path),
        "rows": int(len(output)),
        "created": bool(ensure_info["created"]),
        "template_path": ensure_info["template_path"],
    }


def _normalize_asset_rows(data: pd.DataFrame) -> pd.DataFrame:
    normalized = data.copy(deep=True)
    for column in ASSET_EDITOR_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[ASSET_EDITOR_COLUMNS].fillna("")
    normalized["account"] = normalized["account"].astype(str).str.strip()
    normalized = normalized[normalized["account"].ne("")].copy(deep=True)
    normalized["currency"] = normalized["currency"].astype(str).str.upper().str.strip()
    invalid_currencies = sorted(set(normalized["currency"]) - set(config.UNIQUE_TICKERS))
    if invalid_currencies:
        raise ValueError(f"Недопустимые валюты активов: {', '.join(invalid_currencies)}")
    amounts = pd.to_numeric(normalized["amount"].astype(str).str.replace(" ", "").str.replace(",", "."), errors="coerce")
    invalid_amounts = ~amounts.map(isfinite)
    if invalid_amounts.any():
        bad_accounts = normalized.loc[invalid_amounts, "account"].tolist()
        raise ValueError(f"Некорректная сумма у активов: {', '.join(bad_accounts[:5])}")
    normalized["amount"] = amounts.astype(float)
    return normalized.reset_index(drop=True)


def _parse_asset_cell(value) -> tuple[float, str]:
    parts = str(value).replace("\xa0", "").replace(" ", "").split("|")
    if len(parts) > 2:
        raise ValueError("Некорректный формат суммы актива: ожидается сумма или сумма|валюта.")
    # Preserve legacy defaults for empty cells and an omitted currency.
    amount = parts[0] if parts and parts[0] else "0"
    currency = parts[1] if len(parts) > 1 and parts[1] else "RUB"
    parsed_amount = pd.to_numeric(str(amount).replace(",", "."), errors="coerce")
    if not isfinite(parsed_amount):
        raise ValueError("Некорректная сумма актива: требуется конечное число.")
    currency = str(currency).upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"Недопустимая валюта актива: {currency!r}.")
    return float(parsed_amount), currency


def _format_asset_amount(value: float) -> str:
    value = float(value)
    if value.is_integer():
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _asset_snapshot_backup_path(target_path: Path) -> Path:
    year = target_path.parent.name
    backup_root = config.active_data_path("backups", "assets_info", year)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return backup_root / f"{target_path.stem}.backup_{timestamp}{target_path.suffix}"
