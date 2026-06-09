from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from src import config
from src.data.get import get_transactions
from src.data.staging import DRAFT_COLUMNS, read_transaction_drafts, write_transaction_drafts

KASPI_SOURCE = "kaspi_pdf"
RULES_PATH = Path(config.DATA_PATH) / "import_rules" / "categories.csv"
TRANSACTION_RE = re.compile(
    r"^(?P<date>\d{2}\.\d{2}\.\d{2})\s+"
    r"(?P<sign>[+-])\s+"
    r"(?P<amount>[\d\s]+,\d{2})\s+"
    r"(?P<currency>[₸$€£]|[A-Z]{3})\s+"
    r"(?P<details>.+)$"
)
FOREIGN_AMOUNT_RE = re.compile(r"^\((?P<sign>[+-])\s+(?P<amount>[\d\s]+,\d{2})\s+(?P<currency>[A-Z]{3})\)$")
CURRENCY_SYMBOLS = {"₸": "KZT", "$": "USD", "€": "EUR", "£": "GBP"}
DEFAULT_EXPENSE_CATEGORY = "Прочее"
DEFAULT_INCOME_CATEGORY = "Доход"
INTERNAL_TRANSFER_CATEGORY = "Внутренний перевод"
INTERNAL_TRANSFER_PATTERNS = (
    "TO KASPI DEPOSIT",
    "KASPI DEPOSIT",
    "TRANSFER TO YOUR",
    "TRANSFER BETWEEN",
    "BETWEEN YOUR",
    "TO YOUR",
)


def parse_kaspi_pdf(path: str | Path) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(Path(path)))


def parse_kaspi_pdf_bytes(content: bytes) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(io.BytesIO(content)))


def _extract_rows_from_pdf(pdf_source) -> list[dict]:
    rows: list[dict] = []
    pending: dict | None = None
    with pdfplumber.open(pdf_source) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            for raw_line in text.splitlines():
                line = raw_line.strip()
                match = TRANSACTION_RE.match(line)
                if match:
                    if pending is not None:
                        rows.append(pending)
                    pending = _row_from_match(match)
                    continue
                foreign_match = FOREIGN_AMOUNT_RE.match(line)
                if foreign_match and pending is not None:
                    pending["details"] = f"{pending['details']} {line}"
        if pending is not None:
            rows.append(pending)
    return rows


def _import_frame_from_rows(rows: list[dict]) -> pd.DataFrame:
    data = pd.DataFrame(rows)
    if data.empty:
        return _empty_import_frame()
    data["is_internal_transfer"] = data["details"].map(_is_internal_transfer)
    data["category"] = data.apply(lambda row: _categorize(row["details"], row["signed_amount"]), axis=1)
    data["amount"] = data["signed_amount"].abs()
    data["comment"] = data["details"].map(_clean_comment)
    data["source"] = KASPI_SOURCE
    data["source_id"] = _source_ids(data)
    data["status"] = "draft"
    data = _add_duplicate_flags(data)
    data = _sort_import_preview(data)
    return data[_import_columns()]


def parse_kaspi_upload_contents(contents: str) -> pd.DataFrame:
    if not contents:
        return _empty_import_frame()
    _, encoded = contents.split(",", 1)
    return parse_kaspi_pdf_bytes(base64.b64decode(encoded))


def save_kaspi_import_to_staging(import_rows: list[dict], path: str | Path | None = None) -> dict:
    if not import_rows:
        return {"accepted_rows": 0, "skipped_rows": 0}
    incoming = pd.DataFrame(import_rows)
    if incoming.empty:
        return {"accepted_rows": 0, "skipped_rows": 0}

    for column in _import_columns():
        if column not in incoming.columns:
            incoming[column] = ""
    incoming = incoming[_import_columns()].copy(deep=True)
    drafts = read_transaction_drafts(path)
    existing_source_ids = set(drafts.loc[drafts["source"].eq(KASPI_SOURCE), "source_id"].astype(str))
    source_keys = _existing_source_keys()

    duplicate_mask = _as_bool_series(incoming["duplicate_in_staging"]) | _as_bool_series(incoming["duplicate_in_source"])
    duplicate_mask = duplicate_mask | incoming["source_id"].astype(str).isin(existing_source_ids)
    duplicate_mask = duplicate_mask | incoming.apply(lambda row: _source_key(row) in source_keys, axis=1)
    duplicate_mask = duplicate_mask | incoming.duplicated(subset=["source", "source_id"], keep="first")
    if "skip_reason" in incoming.columns:
        duplicate_mask = duplicate_mask | incoming["skip_reason"].astype(str).eq("internal_transfer")
    if "import_action" in incoming.columns:
        duplicate_mask = duplicate_mask | incoming["import_action"].astype(str).eq("skip")
    accepted = incoming[~duplicate_mask].copy(deep=True)
    if accepted.empty:
        return {"accepted_rows": 0, "skipped_rows": int(len(incoming))}

    draft_rows = accepted[DRAFT_COLUMNS].copy(deep=True)
    updated = pd.concat([drafts, draft_rows], ignore_index=True)
    write_transaction_drafts(updated, path)
    return {"accepted_rows": int(len(accepted)), "skipped_rows": int(duplicate_mask.sum())}



def _as_bool_series(values: pd.Series) -> pd.Series:
    return values.astype(str).str.lower().isin({"true", "1", "yes"})


def _row_from_match(match: re.Match) -> dict:
    amount = _parse_amount(match.group("amount"))
    sign = match.group("sign")
    signed_amount = amount if sign == "+" else -amount
    currency = CURRENCY_SYMBOLS.get(match.group("currency"), match.group("currency")).upper()
    date = pd.to_datetime(match.group("date"), format="%d.%m.%y").date().isoformat()
    details = match.group("details").strip()
    return {
        "date": date,
        "signed_amount": signed_amount,
        "currency": currency,
        "details": details,
    }


def _parse_amount(value: str) -> float:
    return float(value.replace(" ", "").replace(",", "."))


def _categorize(details: str, amount: float) -> str:
    if _is_internal_transfer(details):
        return INTERNAL_TRANSFER_CATEGORY
    rules = _load_rules()
    normalized = _normalize_text(details)
    for _, rule in rules.iterrows():
        pattern = _normalize_text(rule.get("pattern", ""))
        if pattern and pattern in normalized:
            return str(rule.get("category", DEFAULT_EXPENSE_CATEGORY))
    return DEFAULT_INCOME_CATEGORY if amount > 0 else DEFAULT_EXPENSE_CATEGORY


def _load_rules() -> pd.DataFrame:
    if not RULES_PATH.exists():
        return pd.DataFrame(columns=["pattern", "category"])
    return pd.read_csv(RULES_PATH, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def _is_internal_transfer(details: str) -> bool:
    normalized = _normalize_text(details)
    return any(pattern in normalized for pattern in INTERNAL_TRANSFER_PATTERNS)


def _clean_comment(details: str) -> str:
    details = re.sub(r"^(Purchases|Transfers|Replenishment|Transfer to your)\s+", "", str(details), flags=re.IGNORECASE)
    return details.strip()


def _source_ids(data: pd.DataFrame) -> pd.Series:
    base_keys = data.apply(_source_base_key, axis=1)
    occurrence_numbers = base_keys.groupby(base_keys).cumcount()
    return pd.Series(
        [hashlib.sha1(f"{base_key}|{occurrence}".encode("utf-8")).hexdigest() for base_key, occurrence in zip(base_keys, occurrence_numbers)],
        index=data.index,
    )


def _source_base_key(row: pd.Series) -> str:
    return f"{row['date']}|{row['amount']}|{row['currency']}|{_normalize_text(row['details'])}"


def _add_duplicate_flags(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy(deep=True)
    existing_drafts = read_transaction_drafts()
    existing_source_ids = set(existing_drafts.loc[existing_drafts["source"].eq(KASPI_SOURCE), "source_id"])
    source_keys = _existing_source_keys()
    result["duplicate_in_staging"] = result["source_id"].isin(existing_source_ids)
    result["duplicate_in_source"] = result.apply(lambda row: _source_key(row) in source_keys, axis=1)
    result["skip_reason"] = result.apply(_skip_reason, axis=1)
    result["import_action"] = result["skip_reason"].map(lambda value: "skip" if value else "import")
    return result


def _skip_reason(row: pd.Series) -> str:
    if bool(row.get("is_internal_transfer", False)):
        return "internal_transfer"
    if bool(row.get("duplicate_in_staging", False)):
        return "duplicate_in_staging"
    if bool(row.get("duplicate_in_source", False)):
        return "duplicate_in_source"
    return ""


def _sort_import_preview(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy(deep=True)
    result["__date_sort"] = pd.to_datetime(result["date"], errors="coerce")
    result["__action_sort"] = result["import_action"].map({"import": 0, "skip": 1}).fillna(2)
    result = result.sort_values(["__action_sort", "category", "__date_sort", "comment"], kind="mergesort")
    return result.drop(columns=["__date_sort", "__action_sort"]).reset_index(drop=True)


def _existing_source_keys() -> set[tuple[str, str, float]]:
    try:
        transactions = get_transactions()
    except Exception:
        return set()
    if transactions.empty:
        return set()
    keys = set()
    for _, row in transactions.iterrows():
        date = pd.to_datetime(row.get("Дата"), errors="coerce")
        amount = pd.to_numeric(row.get("Значение"), errors="coerce")
        currency = str(row.get("Валюта", "")).upper()
        if pd.isna(date) or pd.isna(amount) or not currency:
            continue
        keys.add((date.date().isoformat(), currency, round(abs(float(amount)), 2)))
    return keys


def _source_key(row: pd.Series) -> tuple[str, str, float]:
    return (str(row["date"]), str(row["currency"]).upper(), round(abs(float(row["amount"])), 2))


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).upper()).strip()


def _import_columns() -> list[str]:
    return [*DRAFT_COLUMNS, "details", "duplicate_in_staging", "duplicate_in_source", "skip_reason", "import_action"]


def _empty_import_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_import_columns())
