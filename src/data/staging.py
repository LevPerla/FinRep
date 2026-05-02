from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from calendar import monthrange
from datetime import datetime
from shutil import copy2
from uuid import uuid4

import pandas as pd

from src import config

DRAFT_COLUMNS = [
    "date",
    "category",
    "currency",
    "amount",
    "comment",
    "source",
    "source_id",
    "status",
]
DRAFT_STATUSES = {"draft", "ready", "exported", "ignored"}
EXPORTABLE_STATUSES = {"draft", "ready"}
DEFAULT_SOURCE = "manual"
DEFAULT_STATUS = "draft"


@dataclass(frozen=True)
class DraftValidationIssue:
    row_number: int | None
    message: str

    def __str__(self) -> str:
        prefix = "file" if self.row_number is None else f"row {self.row_number}"
        return f"{prefix}: {self.message}"


def ensure_transaction_drafts_file(path: str | Path | None = None) -> Path:
    draft_path = _draft_path(path)
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    if not draft_path.exists():
        pd.DataFrame(columns=DRAFT_COLUMNS).to_csv(draft_path, sep=";", index=False)
    return draft_path


def read_transaction_drafts(path: str | Path | None = None) -> pd.DataFrame:
    draft_path = ensure_transaction_drafts_file(path)
    data = pd.read_csv(draft_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in DRAFT_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data = data[DRAFT_COLUMNS].copy(deep=True)
    data["currency"] = data["currency"].astype(str).str.upper()
    data["status"] = data["status"].replace("", DEFAULT_STATUS)
    return data


def write_transaction_drafts(data: pd.DataFrame, path: str | Path | None = None) -> None:
    normalized = _normalize_drafts(data)
    issues = validate_transaction_drafts(normalized)
    if issues:
        raise ValueError(_format_issues(issues))
    draft_path = ensure_transaction_drafts_file(path)
    normalized.to_csv(draft_path, sep=";", index=False)


def append_transaction_draft(
    date: str,
    category: str,
    currency: str,
    amount: float,
    comment: str = "",
    source: str = DEFAULT_SOURCE,
    source_id: str | None = None,
    status: str = DEFAULT_STATUS,
    path: str | Path | None = None,
) -> pd.DataFrame:
    data = read_transaction_drafts(path)
    source_id = source_id or _new_source_id(source)
    new_row = pd.DataFrame([
        {
            "date": date,
            "category": category,
            "currency": currency,
            "amount": amount,
            "comment": comment,
            "source": source,
            "source_id": source_id,
            "status": status,
        }
    ])
    updated = pd.concat([data, new_row], ignore_index=True)
    write_transaction_drafts(updated, path)
    return read_transaction_drafts(path)


def update_transaction_draft(source: str, source_id: str, updates: dict, path: str | Path | None = None) -> pd.DataFrame:
    data = read_transaction_drafts(path)
    mask = (data["source"] == str(source)) & (data["source_id"] == str(source_id))
    if not mask.any():
        raise KeyError(f"draft transaction not found: {source}/{source_id}")
    allowed_updates = {key: value for key, value in updates.items() if key in DRAFT_COLUMNS}
    for key, value in allowed_updates.items():
        data.loc[mask, key] = value
    write_transaction_drafts(data, path)
    return read_transaction_drafts(path)


def delete_transaction_drafts(rows: list[dict], path: str | Path | None = None) -> pd.DataFrame:
    data = read_transaction_drafts(path)
    if not rows:
        return data
    keys = {(str(row.get("source", "")), str(row.get("source_id", ""))) for row in rows}
    keep_mask = ~data.apply(lambda row: (str(row["source"]), str(row["source_id"])) in keys, axis=1)
    updated = data[keep_mask].reset_index(drop=True)
    write_transaction_drafts(updated, path)
    return read_transaction_drafts(path)


def merge_transaction_draft_rows(rows: list[dict], path: str | Path | None = None) -> pd.DataFrame:
    data = read_transaction_drafts(path)
    if not rows:
        return data
    incoming = _normalize_drafts(pd.DataFrame(rows))
    existing = data.set_index(["source", "source_id"], drop=False)
    for _, row in incoming.iterrows():
        key = (row["source"], row["source_id"])
        if key in existing.index:
            for column in DRAFT_COLUMNS:
                existing.loc[key, column] = row[column]
    updated = existing.reset_index(drop=True)
    write_transaction_drafts(updated, path)
    return read_transaction_drafts(path)


def ensure_monthly_transaction_csv(year: str, month: str, transactions_root: str | Path | None = None) -> dict:
    target_path = monthly_transaction_csv_path(year, month, transactions_root)
    if target_path.exists():
        return {"path": str(target_path), "created": False, "template_path": None}

    template_path = previous_monthly_transaction_csv_path(year, month, transactions_root)
    table = _empty_month_table_from_template(year, month, template_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(target_path, sep=";", index=False, encoding="utf-8-sig")
    return {
        "path": str(target_path),
        "created": True,
        "template_path": None if template_path is None else str(template_path),
    }


def read_monthly_transaction_csv(year: str, month: str, transactions_root: str | Path | None = None) -> pd.DataFrame:
    ensure_monthly_transaction_csv(year, month, transactions_root)
    target_path = monthly_transaction_csv_path(year, month, transactions_root)
    return _read_or_create_month_table(year, month, target_path)


def preview_monthly_transaction_export(
    year: str,
    month: str,
    path: str | Path | None = None,
    transactions_root: str | Path | None = None,
) -> pd.DataFrame:
    target_path = monthly_transaction_csv_path(year, month, transactions_root)
    table = _read_or_create_month_table(year, month, target_path)
    drafts = _exportable_month_drafts(year, month, path)
    return _merge_drafts_into_month_table(table, drafts)


def export_monthly_transaction_drafts(
    year: str,
    month: str,
    path: str | Path | None = None,
    transactions_root: str | Path | None = None,
    preview_rows: list[dict] | None = None,
) -> dict:
    target_path = monthly_transaction_csv_path(year, month, transactions_root)
    preview = _preview_rows_to_month_table(preview_rows) if preview_rows is not None else preview_monthly_transaction_export(year, month, path, transactions_root)
    drafts = _exportable_month_drafts(year, month, path)
    if drafts.empty and preview_rows is None:
        raise ValueError("Нет черновиков со статусом draft/ready для выбранного месяца.")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if target_path.exists():
        backup_path = _monthly_transaction_backup_path(target_path)
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        copy2(target_path, backup_path)

    preview.to_csv(target_path, sep=";", index=False, encoding="utf-8-sig")
    if not drafts.empty:
        _mark_month_drafts_exported(drafts, path)
    return {
        "target_path": str(target_path),
        "backup_path": None if backup_path is None else str(backup_path),
        "exported_rows": int(len(drafts)),
    }


def _monthly_transaction_backup_path(target_path: Path) -> Path:
    year = target_path.parent.name
    backup_root = Path(config.TRANSACTION_BACKUPS_PATH) / year
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return backup_root / f"{target_path.stem}.backup_{timestamp}{target_path.suffix}"


def _preview_rows_to_month_table(preview_rows: list[dict] | None) -> pd.DataFrame:
    if not preview_rows:
        raise ValueError("Preview пустой: сначала нажми Preview или заполни таблицу.")
    data = pd.DataFrame(preview_rows).fillna("0")
    if "Дата" not in data.columns:
        raise ValueError("В preview нет колонки Дата.")
    ordered_columns = ["Дата", *[column for column in data.columns if column != "Дата"]]
    return data[ordered_columns]


def monthly_transaction_csv_path(year: str, month: str, transactions_root: str | Path | None = None) -> Path:
    year = str(year)
    month = str(int(month)).zfill(2)
    root = Path(transactions_root or config.TRANSACTIONS_INFO_PATH)
    folder = root / year
    plain = folder / f"{year}_{month}.csv"
    underscored = folder / f"{year}_{month}_.csv"
    if plain.exists():
        return plain
    return underscored


def previous_monthly_transaction_csv_path(year: str, month: str, transactions_root: str | Path | None = None) -> Path | None:
    root = Path(transactions_root or config.TRANSACTIONS_INFO_PATH)
    current = pd.Period(f"{int(year):04d}-{int(month):02d}", freq="M") - 1
    for _ in range(240):
        folder = root / str(current.year)
        candidates = [folder / f"{current.year}_{current.month:02d}.csv", folder / f"{current.year}_{current.month:02d}_.csv"]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        current -= 1
    return None


def _exportable_month_drafts(year: str, month: str, path: str | Path | None = None) -> pd.DataFrame:
    data = read_transaction_drafts(path)
    dates = pd.to_datetime(data["date"], errors="coerce")
    mask = (
        dates.dt.year.eq(int(year))
        & dates.dt.month.eq(int(month))
        & data["status"].isin(EXPORTABLE_STATUSES)
    )
    return data[mask].copy(deep=True)


def _read_or_create_month_table(year: str, month: str, target_path: Path) -> pd.DataFrame:
    if target_path.exists():
        return pd.read_csv(target_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("0")

    return _empty_month_table_from_template(year, month, previous_monthly_transaction_csv_path(year, month))


def _empty_month_table_from_template(year: str, month: str, template_path: Path | None) -> pd.DataFrame:
    days = monthrange(int(year), int(month))[1]
    dates = pd.date_range(f"{year}-{str(int(month)).zfill(2)}-01", periods=days, freq="D")
    columns = _template_transaction_columns(template_path)
    table = pd.DataFrame({"Дата": dates.strftime("%d.%m.%Y")})
    for column in columns:
        if column != "Дата":
            table[column] = "0"
    return table


def _template_transaction_columns(template_path: Path | None) -> list[str]:
    if template_path is not None:
        try:
            columns = pd.read_csv(template_path, sep=";", nrows=0, encoding="utf-8-sig").columns.tolist()
            if "Дата" in columns:
                return columns
        except Exception:
            pass
    return ["Дата", *_known_transaction_categories()]


def _known_transaction_categories() -> list[str]:
    categories: list[str] = []
    root = Path(config.TRANSACTIONS_INFO_PATH)
    for csv_path in sorted(root.glob("*/*.csv")):
        try:
            columns = pd.read_csv(csv_path, sep=";", nrows=0, encoding="utf-8-sig").columns.tolist()
        except Exception:
            continue
        for column in columns:
            if column != "Дата" and column not in categories:
                categories.append(column)
    return categories


def _merge_drafts_into_month_table(table: pd.DataFrame, drafts: pd.DataFrame) -> pd.DataFrame:
    result = table.copy(deep=True).fillna("0")
    for _, draft in drafts.iterrows():
        date_label = pd.to_datetime(draft["date"]).strftime("%d.%m.%Y")
        category = str(draft["category"])
        if category not in result.columns:
            result[category] = "0"
        if not result["Дата"].astype(str).eq(date_label).any():
            result = pd.concat([result, pd.DataFrame([{"Дата": date_label}])], ignore_index=True).fillna("0")
        row_index = result.index[result["Дата"].astype(str).eq(date_label)][0]
        result.loc[row_index, category] = _append_transaction_cell(result.loc[row_index, category], _draft_to_month_cell(draft))
    result["__date_sort"] = pd.to_datetime(result["Дата"], dayfirst=True, errors="coerce")
    result = result.sort_values("__date_sort", kind="mergesort").drop(columns="__date_sort").reset_index(drop=True)
    return result


def _append_transaction_cell(existing, new_value: str) -> str:
    existing = str(existing).strip()
    if existing in {"", "0", "0.0", "nan", "None"}:
        return new_value
    return f"{existing}#{new_value}"


def _draft_to_month_cell(draft: pd.Series) -> str:
    amount = _format_amount_for_month_cell(draft["amount"])
    currency = str(draft["currency"]).upper()
    comment = str(draft.get("comment", ""))
    return f"{amount}|{currency}|{comment}"


def _format_amount_for_month_cell(value) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if pd.isna(numeric):
        raise ValueError(f"invalid amount {value!r}")
    if float(numeric).is_integer():
        return str(int(numeric))
    return f"{float(numeric):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _mark_month_drafts_exported(drafts: pd.DataFrame, path: str | Path | None = None) -> None:
    data = read_transaction_drafts(path)
    keys = {(str(row["source"]), str(row["source_id"])) for _, row in drafts.iterrows()}
    mask = data.apply(lambda row: (str(row["source"]), str(row["source_id"])) in keys, axis=1)
    data.loc[mask, "status"] = "exported"
    write_transaction_drafts(data, path)


def validate_transaction_drafts(data: pd.DataFrame | None = None, path: str | Path | None = None) -> list[DraftValidationIssue]:
    data = read_transaction_drafts(path) if data is None else _normalize_drafts(data)
    issues: list[DraftValidationIssue] = []

    missing = [column for column in DRAFT_COLUMNS if column not in data.columns]
    for column in missing:
        issues.append(DraftValidationIssue(None, f"missing required column {column!r}"))
    if missing:
        return issues

    dates = pd.to_datetime(data["date"], errors="coerce")
    for index, is_bad in enumerate(dates.isna(), start=2):
        if is_bad:
            issues.append(DraftValidationIssue(index, "invalid date"))

    for index, value in enumerate(data["category"], start=2):
        if not str(value).strip():
            issues.append(DraftValidationIssue(index, "category is required"))

    for index, value in enumerate(data["currency"], start=2):
        if str(value).upper() not in config.UNIQUE_TICKERS:
            issues.append(DraftValidationIssue(index, f"unsupported currency {value!r}"))

    amounts = pd.to_numeric(data["amount"], errors="coerce")
    for index, is_bad in enumerate(amounts.isna(), start=2):
        if is_bad:
            issues.append(DraftValidationIssue(index, "invalid amount"))

    for index, value in enumerate(data["status"], start=2):
        if str(value) not in DRAFT_STATUSES:
            issues.append(DraftValidationIssue(index, f"unsupported status {value!r}"))

    duplicate_mask = data.duplicated(subset=["source", "source_id"], keep=False) & data["source_id"].ne("")
    for index in data.index[duplicate_mask]:
        issues.append(DraftValidationIssue(index + 2, "duplicate source/source_id"))

    return issues


def find_duplicate_drafts(data: pd.DataFrame | None = None, path: str | Path | None = None) -> pd.DataFrame:
    data = read_transaction_drafts(path) if data is None else _normalize_drafts(data)
    duplicate_mask = data.duplicated(subset=["source", "source_id"], keep=False) & data["source_id"].ne("")
    return data[duplicate_mask].copy(deep=True)


def _normalize_drafts(data: pd.DataFrame) -> pd.DataFrame:
    normalized = data.copy(deep=True)
    for column in DRAFT_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[DRAFT_COLUMNS]
    normalized["currency"] = normalized["currency"].astype(str).str.upper()
    normalized["status"] = normalized["status"].replace("", DEFAULT_STATUS)
    normalized["amount"] = normalized["amount"].astype(str)
    return normalized.fillna("")


def _draft_path(path: str | Path | None = None) -> Path:
    return Path(path or config.TRANSACTION_DRAFTS_PATH)


def _new_source_id(source: str) -> str:
    return f"{source}-{uuid4().hex}"


def _format_issues(issues: list[DraftValidationIssue]) -> str:
    return "\n".join(str(issue) for issue in issues)
