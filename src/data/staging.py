from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from pathlib import Path
from calendar import monthrange
import fcntl
from hashlib import sha256
import json
from math import isfinite
import re
from threading import RLock
from time import monotonic, sleep
from uuid import uuid4

import pandas as pd

from src import config
from src.data.csv_storage import atomic_write_csv, create_unique_backup
from src.data.file_commit import commit_file_images, read_commit_receipt, recover_file_commit

TRANSACTION_BOUNDARY_RE = re.compile(r'#(?=\s*[+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:\||#|$))')

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
TRANSACTION_COMMENT_SEPARATORS = ("|", "#", ";", "\r", "\n")
DRAFT_LOCK_TIMEOUT_SECONDS = 5.0
_DRAFTS_THREAD_LOCK = RLock()


class DraftWriteBusyError(RuntimeError):
    pass


class DraftRevisionConflict(ValueError):
    pass


@dataclass(frozen=True)
class DraftValidationIssue:
    row_number: int | None
    message: str

    def __str__(self) -> str:
        prefix = "file" if self.row_number is None else f"row {self.row_number}"
        return f"{prefix}: {self.message}"


def ensure_transaction_drafts_file(path: str | Path | None = None) -> Path:
    draft_path = _draft_path(path)
    if config.is_test_mode():
        return draft_path
    with _transaction_drafts_lock(draft_path):
        return _ensure_transaction_drafts_file_unlocked(draft_path)


def _ensure_transaction_drafts_file_unlocked(draft_path: Path) -> Path:
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    if not draft_path.exists():
        atomic_write_csv(pd.DataFrame(columns=DRAFT_COLUMNS), draft_path, sep=";", index=False)
    return draft_path


def read_transaction_drafts(path: str | Path | None = None) -> pd.DataFrame:
    draft_path = _draft_path(path)
    if config.is_test_mode():
        if not draft_path.exists():
            return pd.DataFrame(columns=DRAFT_COLUMNS)
        return _read_transaction_drafts_unlocked(draft_path)
    with _transaction_drafts_lock(draft_path):
        return _read_transaction_drafts_unlocked(draft_path)


def read_transaction_drafts_snapshot(path: str | Path | None = None) -> tuple[pd.DataFrame, str]:
    draft_path = _draft_path(path)
    if config.is_test_mode():
        data = (
            _read_transaction_drafts_unlocked(draft_path)
            if draft_path.exists()
            else pd.DataFrame(columns=DRAFT_COLUMNS)
        )
        return data, _transaction_drafts_revision(data)
    with _transaction_drafts_lock(draft_path):
        data = _read_transaction_drafts_unlocked(draft_path)
        return data, _transaction_drafts_revision(data)


def _read_transaction_drafts_unlocked(draft_path: Path) -> pd.DataFrame:
    draft_path = _ensure_transaction_drafts_file_unlocked(draft_path)
    if not draft_path.exists():
        return pd.DataFrame(columns=DRAFT_COLUMNS)
    data = pd.read_csv(draft_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    for column in DRAFT_COLUMNS:
        if column not in data.columns:
            data[column] = ""
    data = data[DRAFT_COLUMNS].copy(deep=True)
    data["currency"] = data["currency"].astype(str).str.upper()
    data["status"] = data["status"].replace("", DEFAULT_STATUS)
    return data


def write_transaction_drafts(data: pd.DataFrame, path: str | Path | None = None) -> None:
    config.require_writable_mode()
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        _write_transaction_drafts_unlocked(data, draft_path)


def _write_transaction_drafts_unlocked(data: pd.DataFrame, draft_path: Path) -> None:
    normalized = _normalize_drafts(data)
    issues = validate_transaction_drafts(normalized)
    if issues:
        raise ValueError(_format_issues(issues))
    draft_path = _ensure_transaction_drafts_file_unlocked(draft_path)
    atomic_write_csv(normalized, draft_path, sep=";", index=False)


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
    config.require_writable_mode()
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        data = _read_transaction_drafts_unlocked(draft_path)
        updated = transaction_drafts_with_appended_row(
            data,
            date=date,
            category=category,
            currency=currency,
            amount=amount,
            comment=comment,
            source=source,
            source_id=source_id,
            status=status,
        )
        _write_transaction_drafts_unlocked(updated, draft_path)
        return _read_transaction_drafts_unlocked(draft_path)


def transaction_drafts_with_appended_row(
    data: pd.DataFrame,
    *,
    date: str,
    category: str,
    currency: str,
    amount: float,
    comment: str = "",
    source: str = DEFAULT_SOURCE,
    source_id: str | None = None,
    status: str = DEFAULT_STATUS,
) -> pd.DataFrame:
    new_row = pd.DataFrame(
        [
            {
                "date": date,
                "category": category,
                "currency": currency,
                "amount": amount,
                "comment": comment,
                "source": source,
                "source_id": source_id or _new_source_id(source),
                "status": status,
            }
        ]
    )
    updated = _normalize_drafts(pd.concat([data, new_row], ignore_index=True))
    issues = validate_transaction_drafts(updated)
    if issues:
        raise ValueError(_format_issues(issues))
    return updated


def append_transaction_draft_rows(
    rows: pd.DataFrame, path: str | Path | None = None
) -> dict:
    config.require_writable_mode()
    incoming = _normalize_drafts(rows)
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        data = _read_transaction_drafts_unlocked(draft_path)
        existing_keys = set(zip(data["source"].astype(str), data["source_id"].astype(str)))
        incoming_keys = pd.Series(
            list(zip(incoming["source"].astype(str), incoming["source_id"].astype(str))),
            index=incoming.index,
        )
        duplicate_mask = incoming_keys.isin(existing_keys)
        duplicate_mask = duplicate_mask | incoming.duplicated(
            subset=["source", "source_id"], keep="first"
        )
        accepted = incoming[~duplicate_mask]
        if not accepted.empty:
            updated = pd.concat([data, accepted], ignore_index=True)
            _write_transaction_drafts_unlocked(updated, draft_path)
        return {
            "accepted_rows": int(len(accepted)),
            "skipped_rows": int(duplicate_mask.sum()),
        }


def update_transaction_draft(source: str, source_id: str, updates: dict, path: str | Path | None = None) -> pd.DataFrame:
    config.require_writable_mode()
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        data = _read_transaction_drafts_unlocked(draft_path)
        mask = (data["source"] == str(source)) & (data["source_id"] == str(source_id))
        if not mask.any():
            raise KeyError(f"draft transaction not found: {source}/{source_id}")
        allowed_updates = {key: value for key, value in updates.items() if key in DRAFT_COLUMNS}
        for key, value in allowed_updates.items():
            data.loc[mask, key] = value
        _write_transaction_drafts_unlocked(data, draft_path)
        return _read_transaction_drafts_unlocked(draft_path)


def delete_transaction_drafts(
    rows: list[dict], path: str | Path | None = None, expected_revision: str | None = None
) -> pd.DataFrame:
    config.require_writable_mode()
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        data = _read_transaction_drafts_unlocked(draft_path)
        _validate_draft_revision(data, expected_revision)
        if not rows:
            return data
        keys = {(str(row.get("source", "")), str(row.get("source_id", ""))) for row in rows}
        keep_mask = ~data.apply(lambda row: (str(row["source"]), str(row["source_id"])) in keys, axis=1)
        updated = data[keep_mask].reset_index(drop=True)
        _write_transaction_drafts_unlocked(updated, draft_path)
        return _read_transaction_drafts_unlocked(draft_path)


def merge_transaction_draft_rows(
    rows: list[dict], path: str | Path | None = None, expected_revision: str | None = None
) -> pd.DataFrame:
    config.require_writable_mode()
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        data = _read_transaction_drafts_unlocked(draft_path)
        _validate_draft_revision(data, expected_revision)
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
        _write_transaction_drafts_unlocked(updated, draft_path)
        return _read_transaction_drafts_unlocked(draft_path)


def ensure_monthly_transaction_csv(year: str, month: str, transactions_root: str | Path | None = None) -> dict:
    target_path = monthly_transaction_csv_path(year, month, transactions_root)
    if target_path.exists():
        return {"path": str(target_path), "created": False, "template_path": None}
    config.require_writable_mode()

    template_path = previous_monthly_transaction_csv_path(year, month, transactions_root)
    table = _empty_month_table_from_template(year, month, template_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(table, target_path, sep=";", index=False, encoding="utf-8-sig")
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
    preview, _ = prepare_monthly_transaction_export(year, month, path, transactions_root)
    return preview


def prepare_monthly_transaction_export(
    year: str,
    month: str,
    path: str | Path | None = None,
    transactions_root: str | Path | None = None,
) -> tuple[pd.DataFrame, dict]:
    preview, _, preview_state = _monthly_transaction_export_snapshot(
        year, month, path, transactions_root
    )
    return preview, preview_state


def _monthly_transaction_export_snapshot(
    year: str,
    month: str,
    path: str | Path | None = None,
    transactions_root: str | Path | None = None,
    draft_data: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    target_path = monthly_transaction_csv_path(year, month, transactions_root)
    table = _read_or_create_month_table(year, month, target_path)
    drafts = _exportable_month_drafts(year, month, path, data=draft_data)
    preview = _merge_drafts_into_month_table(table, drafts)
    year_key = str(int(year)).zfill(4)
    month_key = str(int(month)).zfill(2)
    revision_payload = {
        "data_mode": config.get_data_mode(),
        "year": year_key,
        "month": month_key,
        "month_table": _frame_revision_payload(table),
        "drafts": _frame_revision_payload(drafts[DRAFT_COLUMNS]),
    }
    revision = sha256(
        json.dumps(
            revision_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    preview_state = {
        "version": 1,
        "data_mode": config.get_data_mode(),
        "year": year_key,
        "month": month_key,
        "revision": revision,
        "draft_ids": [
            {"source": str(row["source"]), "source_id": str(row["source_id"])}
            for _, row in drafts.iterrows()
        ],
    }
    return preview, drafts, preview_state


def export_monthly_transaction_drafts(
    year: str,
    month: str,
    path: str | Path | None = None,
    transactions_root: str | Path | None = None,
    preview_rows: list[dict] | None = None,
    preview_state: dict | None = None,
) -> dict:
    config.require_writable_mode()
    draft_path = _draft_path(path)
    target_path = monthly_transaction_csv_path(year, month, transactions_root)
    with _transaction_drafts_lock(draft_path):
        completed = _completed_monthly_export(
            preview_state, draft_path, year, month, target_path
        )
        if completed is not None:
            return completed
        all_drafts = _read_transaction_drafts_unlocked(draft_path)
        server_preview, drafts, current_state = _monthly_transaction_export_snapshot(
            year, month, path, transactions_root, draft_data=all_drafts
        )
        if preview_rows is not None:
            _validate_preview_state(preview_state, current_state)
            expected_columns = server_preview.columns.tolist()
            received_columns = pd.DataFrame(preview_rows, dtype=object).columns.tolist()
            if set(received_columns) != set(expected_columns) or len(received_columns) != len(expected_columns):
                raise ValueError("Структура Preview изменилась: построй Preview заново.")
            preview = _preview_rows_to_month_table(preview_rows, year, month)
            preview = preview[expected_columns]
            if preview["Дата"].astype(str).tolist() != server_preview["Дата"].astype(str).tolist():
                raise ValueError("Структура дат Preview изменилась: построй Preview заново.")
        else:
            preview = _preview_rows_to_month_table(server_preview.to_dict("records"), year, month)
        if drafts.empty and preview_rows is None:
            raise ValueError("Нет черновиков со статусом draft/ready для выбранного месяца.")
        if not drafts.empty:
            issues = validate_transaction_drafts(all_drafts)
            if issues:
                raise ValueError(_format_issues(issues))

        target_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path = None
        if target_path.exists():
            backup_root = config.active_data_path(
                "backups", "transactions_info", target_path.parent.name
            )
            backup_path = create_unique_backup(target_path, backup_root)

        updated_drafts = _drafts_with_exported_status(all_drafts, drafts)
        result = {
            "target_path": str(target_path),
            "backup_path": None if backup_path is None else str(backup_path),
            "exported_rows": int(len(drafts)),
        }
        receipt = {
            "version": 1,
            "data_mode": current_state["data_mode"],
            "year": current_state["year"],
            "month": current_state["month"],
            "revision": current_state["revision"],
            "draft_ids": current_state["draft_ids"],
            "result": result,
        }
        commit_file_images(
            _transaction_export_journal_path(draft_path),
            {
                target_path: _csv_bytes(preview, encoding="utf-8-sig"),
                draft_path: _csv_bytes(updated_drafts),
            },
            receipt_path=_transaction_export_receipt_path(draft_path),
            receipt=receipt,
        )
        _clear_transaction_report_caches()
        return result


def _completed_monthly_export(
    preview_state: dict | None,
    draft_path: Path,
    year: str,
    month: str,
    target_path: Path,
) -> dict | None:
    if not isinstance(preview_state, dict):
        return None
    receipt = read_commit_receipt(_transaction_export_receipt_path(draft_path))
    if receipt is None:
        return None
    requested_identity = {
        "data_mode": config.get_data_mode(),
        "year": str(int(year)).zfill(4),
        "month": str(int(month)).zfill(2),
    }
    identity_fields = ("data_mode", "year", "month", "revision", "draft_ids")
    if (
        all(receipt.get(field) == preview_state.get(field) for field in identity_fields)
        and all(receipt.get(field) == value for field, value in requested_identity.items())
    ):
        result = receipt.get("result")
        if (
            isinstance(result, dict)
            and Path(str(result.get("target_path", ""))).resolve() == target_path.resolve()
        ):
            return result
    return None


def _csv_bytes(data: pd.DataFrame, encoding: str = "utf-8") -> bytes:
    return data.to_csv(sep=";", index=False).encode(encoding)


def _preview_rows_to_month_table(
    preview_rows: list[dict] | None,
    year: str | None = None,
    month: str | None = None,
) -> pd.DataFrame:
    if not preview_rows:
        raise ValueError("Preview пустой: сначала нажми Preview или заполни таблицу.")
    data = pd.DataFrame(preview_rows, dtype=object)
    if "Дата" not in data.columns:
        raise ValueError("В preview нет колонки Дата.")
    if year is not None and month is not None:
        dates = pd.to_datetime(data["Дата"], format="%d.%m.%Y", errors="coerce")
        for row_number, date in enumerate(dates, start=2):
            if pd.isna(date):
                raise ValueError(f"row {row_number}, column 'Дата': invalid date")
            if date.year != int(year) or date.month != int(month):
                raise ValueError(
                    f"row {row_number}, column 'Дата': дата не относится к {int(year):04d}-{int(month):02d}"
                )
    for column in data.columns:
        if column == "Дата":
            continue
        for row_number, value in enumerate(data[column], start=2):
            if value is None or (isinstance(value, str) and value == ""):
                continue
            for part in TRANSACTION_BOUNDARY_RE.split(str(value)):
                amount_text = (part.split("|", 1)[0].replace(",", ".")
                               .replace("\\xa0", "").replace("\xa0", "").replace(" ₽", ""))
                amount = pd.to_numeric(amount_text, errors="coerce")
                if not isfinite(amount):
                    raise ValueError(f"row {row_number}, column {column!r}: amount must be finite")
    data = data.fillna("0")
    ordered_columns = ["Дата", *[column for column in data.columns if column != "Дата"]]
    return data[ordered_columns]


def _frame_revision_payload(data: pd.DataFrame) -> dict:
    frame = data.copy(deep=True)
    values = [
        ["" if pd.isna(value) else str(value) for value in row]
        for row in frame.itertuples(index=False, name=None)
    ]
    return {"columns": [str(column) for column in frame.columns], "rows": values}


def _transaction_drafts_revision(data: pd.DataFrame) -> str:
    normalized = _normalize_drafts(data)
    payload = _frame_revision_payload(normalized[DRAFT_COLUMNS])
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_draft_revision(data: pd.DataFrame, expected_revision: str | None) -> None:
    if expected_revision is None:
        return
    if expected_revision != _transaction_drafts_revision(data):
        raise DraftRevisionConflict(
            "Черновики изменились после загрузки таблицы. Сохранение отменено, чтобы не потерять данные."
        )


def _validate_preview_state(preview_state: dict | None, current_state: dict) -> None:
    if not isinstance(preview_state, dict):
        raise ValueError("Сначала нажми Preview, затем подтверди экспорт.")
    if preview_state.get("data_mode") != current_state["data_mode"]:
        raise ValueError("Preview создан для другого режима данных: построй Preview заново.")
    if (
        str(preview_state.get("year", "")) != current_state["year"]
        or str(preview_state.get("month", "")) != current_state["month"]
    ):
        raise ValueError("Preview создан для другого периода: построй Preview заново.")
    if (
        preview_state.get("version") != current_state["version"]
        or preview_state.get("revision") != current_state["revision"]
        or preview_state.get("draft_ids") != current_state["draft_ids"]
    ):
        raise ValueError("Preview устарел: данные изменились, построй Preview заново.")


def monthly_transaction_csv_path(year: str, month: str, transactions_root: str | Path | None = None) -> Path:
    year = str(year)
    month = str(int(month)).zfill(2)
    root = Path(transactions_root or config.active_data_path("transactions_info"))
    folder = root / year
    plain = folder / f"{year}_{month}.csv"
    underscored = folder / f"{year}_{month}_.csv"
    if plain.exists():
        return plain
    return underscored


def previous_monthly_transaction_csv_path(year: str, month: str, transactions_root: str | Path | None = None) -> Path | None:
    root = Path(transactions_root or config.active_data_path("transactions_info"))
    current = pd.Period(f"{int(year):04d}-{int(month):02d}", freq="M") - 1
    for _ in range(240):
        folder = root / str(current.year)
        candidates = [folder / f"{current.year}_{current.month:02d}.csv", folder / f"{current.year}_{current.month:02d}_.csv"]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        current -= 1
    return None


def _exportable_month_drafts(
    year: str,
    month: str,
    path: str | Path | None = None,
    data: pd.DataFrame | None = None,
) -> pd.DataFrame:
    data = read_transaction_drafts(path) if data is None else data
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
    root = config.active_data_path("transactions_info")
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
    comment = sanitize_transaction_comment(draft.get("comment", ""))
    return f"{amount}|{currency}|{comment}"


def _format_amount_for_month_cell(value) -> str:
    numeric = pd.to_numeric(value, errors="coerce")
    if not isfinite(numeric):
        raise ValueError(f"invalid amount {value!r}")
    if float(numeric).is_integer():
        return str(int(numeric))
    return f"{float(numeric):.2f}".rstrip("0").rstrip(".").replace(".", ",")


def _mark_month_drafts_exported(drafts: pd.DataFrame, path: str | Path | None = None) -> None:
    config.require_writable_mode()
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        _mark_month_drafts_exported_unlocked(drafts, draft_path)


def _mark_month_drafts_exported_unlocked(drafts: pd.DataFrame, draft_path: Path) -> None:
    data = _read_transaction_drafts_unlocked(draft_path)
    updated = _drafts_with_exported_status(data, drafts)
    _write_transaction_drafts_unlocked(updated, draft_path)


def _drafts_with_exported_status(data: pd.DataFrame, drafts: pd.DataFrame) -> pd.DataFrame:
    data = data.copy(deep=True)
    keys = {(str(row["source"]), str(row["source_id"])) for _, row in drafts.iterrows()}
    mask = data.apply(lambda row: (str(row["source"]), str(row["source_id"])) in keys, axis=1)
    data.loc[mask, "status"] = "exported"
    return _normalize_drafts(data)


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
    for index, is_bad in enumerate(~amounts.map(isfinite), start=2):
        if is_bad:
            issues.append(DraftValidationIssue(index, "invalid amount: must be finite"))

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
    normalized["comment"] = normalized["comment"].map(sanitize_transaction_comment)
    return normalized.fillna("")


def sanitize_transaction_comment(value) -> str:
    if value is None or pd.isna(value):
        return ""
    comment = str(value)
    for separator in TRANSACTION_COMMENT_SEPARATORS:
        comment = comment.replace(separator, " ")
    return " ".join(comment.split())


def _draft_path(path: str | Path | None = None) -> Path:
    return Path(path or config.active_data_path("staging", "transaction_drafts.csv"))


def _draft_lock_path(draft_path: Path) -> Path:
    return draft_path.with_name(f".{draft_path.name}.lock")


def _transaction_export_journal_path(draft_path: Path) -> Path:
    return draft_path.with_name(f".{draft_path.name}.export-commit.json")


def _transaction_export_receipt_path(draft_path: Path) -> Path:
    return draft_path.with_name(f".{draft_path.name}.last-export.json")


@contextmanager
def transaction_drafts_commit_lock(path: str | Path | None = None):
    draft_path = _draft_path(path)
    with _transaction_drafts_lock(draft_path):
        yield draft_path


@contextmanager
def _transaction_drafts_lock(draft_path: Path):
    deadline = monotonic() + DRAFT_LOCK_TIMEOUT_SECONDS
    remaining = max(0.0, deadline - monotonic())
    if not _DRAFTS_THREAD_LOCK.acquire(timeout=remaining):
        raise DraftWriteBusyError("Черновики сейчас сохраняются в другой операции. Повтори попытку.")

    lock_file = None
    try:
        lock_path = _draft_lock_path(draft_path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if monotonic() >= deadline:
                    raise DraftWriteBusyError(
                        "Черновики сейчас сохраняются в другом процессе. Повтори попытку."
                    )
                sleep(min(0.05, max(0.0, deadline - monotonic())))
        recovered_transaction_export = False
        for journal_path in sorted(
            draft_path.parent.glob(f".{draft_path.name}.*-commit.json")
        ):
            recovered = recover_file_commit(journal_path)
            if recovered is not None and journal_path == _transaction_export_journal_path(
                draft_path
            ):
                recovered_transaction_export = True
        if recovered_transaction_export:
            _clear_transaction_report_caches()
        yield
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()
        _DRAFTS_THREAD_LOCK.release()


def _new_source_id(source: str) -> str:
    return f"{source}-{uuid4().hex}"


def _clear_transaction_report_caches() -> None:
    from src.data.get import clear_data_cache
    from src.model.create_tables import clear_table_cache

    clear_data_cache()
    clear_table_cache()


def _format_issues(issues: list[DraftValidationIssue]) -> str:
    return "\n".join(str(issue) for issue in issues)
