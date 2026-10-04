from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pandas as pd

from src import config
from src.data.csv_storage import atomic_copy_file, atomic_write_csv, create_unique_backup
from src.data.money import format_money_amount, parse_money_amount

ASSET_EDITOR_COLUMNS = ["account", "amount", "currency"]


def asset_snapshot_path(year: str, month: str, assets_root: str | Path | None = None) -> Path:
    year = str(year)
    month = str(int(month)).zfill(2)
    root = Path(assets_root or config.active_data_path("assets_info"))
    return root / year / f"{year}_{month}.csv"


def ensure_asset_snapshot(year: str, month: str, assets_root: str | Path | None = None) -> dict:
    if assets_root is None and config.use_sqlite_storage():
        from src.data.sqlite_store import replace_asset_snapshot_month, saved_asset_months

        period = f"{int(year):04d}-{int(month):02d}"
        database = config.active_database_path()
        if period in saved_asset_months(database):
            return {"path": str(database), "created": False, "template_path": None}
        template = read_asset_snapshot(year, month, assets_root)
        replace_asset_snapshot_month(
            database, period=period, rows=template.to_dict("records"))
        return {"path": str(database), "created": True, "template_path": None}
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
    if assets_root is None and config.use_sqlite_storage():
        from src.data.sqlite_store import asset_snapshots

        requested = f"{int(year):04d}-{int(month):02d}"
        rows = asset_snapshots(config.active_database_path())
        periods = sorted({row["period"] for row in rows if row["period"] <= requested})
        if not periods:
            return pd.DataFrame(columns=ASSET_EDITOR_COLUMNS)
        selected = requested if requested in periods else periods[-1]
        return pd.DataFrame([
            {"account": row["account_name"], "amount": row["amount"],
             "currency": row["currency_code"]}
            for row in rows
            if row["period"] == selected
            and (row["closed_period"] is None or requested <= row["closed_period"])
        ], columns=ASSET_EDITOR_COLUMNS)
    target_path = asset_snapshot_path(year, month, assets_root)
    path = target_path
    if not path.exists():
        template = previous_asset_snapshot_path(year, month, assets_root)
        if template is not None:
            path = template
        else:
            return pd.DataFrame(columns=ASSET_EDITOR_COLUMNS)
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
    return pd.DataFrame(rows, columns=ASSET_EDITOR_COLUMNS)


def write_asset_snapshot(rows: list[dict], year: str, month: str, assets_root: str | Path | None = None) -> dict:
    config.require_writable_mode()
    if assets_root is None and config.use_sqlite_storage():
        from src.data.sqlite_store import replace_asset_snapshot_month

        normalized = _normalize_asset_rows(pd.DataFrame(rows))
        result = replace_asset_snapshot_month(
            config.active_database_path(),
            period=f"{int(year):04d}-{int(month):02d}",
            rows=normalized.to_dict("records"),
        )
        return {"path": str(config.active_database_path()), "backup_path": None,
                "rows": result["rows"], "created": result["inserted"] > 0,
                "template_path": None}
    target_path = asset_snapshot_path(year, month, assets_root)
    data = _normalize_asset_rows(pd.DataFrame(rows))
    target_existed = target_path.exists()
    template_path = None if target_existed else previous_asset_snapshot_path(year, month, assets_root)

    backup_path = None
    if target_existed:
        backup_root = config.active_data_path("backups", "assets_info", target_path.parent.name)
        backup_path = create_unique_backup(target_path, backup_root)

    output = pd.DataFrame({
        "Счет": data["account"],
        "Сумма": data.apply(lambda row: f"{_format_asset_amount(row['amount'])}|{row['currency']}", axis=1),
    })
    atomic_write_csv(output, target_path, sep=";", index=False, encoding="utf-8-sig")
    return {
        "path": str(target_path),
        "backup_path": None if backup_path is None else str(backup_path),
        "rows": int(len(output)),
        "created": not target_existed,
        "template_path": None if template_path is None else str(template_path),
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
    amounts = []
    bad_accounts = []
    for _, row in normalized.iterrows():
        try:
            amounts.append(parse_money_amount(row["amount"], field_name="asset amount"))
        except ValueError:
            bad_accounts.append(row["account"])
    if bad_accounts:
        raise ValueError(f"Некорректная сумма у активов: {', '.join(bad_accounts[:5])}")
    normalized["amount"] = pd.Series(amounts, index=normalized.index, dtype=object)
    return normalized.reset_index(drop=True)


def _parse_asset_cell(value) -> tuple[Decimal, str]:
    parts = str(value).split("|")
    if len(parts) > 2:
        raise ValueError("Некорректный формат суммы актива: ожидается сумма или сумма|валюта.")
    # Preserve legacy defaults for empty cells and an omitted currency.
    amount = parts[0].strip() if parts else ""
    currency = parts[1].strip() if len(parts) > 1 else ""
    try:
        parsed_amount = parse_money_amount(amount or "0", field_name="asset amount")
    except ValueError as exc:
        raise ValueError(f"Некорректная сумма актива: {exc}") from exc
    currency = str(currency or "RUB").upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"Недопустимая валюта актива: {currency!r}.")
    return parsed_amount, currency


def _format_asset_amount(value) -> str:
    return format_money_amount(value, decimal_separator=",", field_name="asset amount")
