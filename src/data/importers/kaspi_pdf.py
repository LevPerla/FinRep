from __future__ import annotations

import base64
import hashlib
import io
import json
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from src import config
from src.data.get import get_transactions
from src.data.staging import DRAFT_COLUMNS, append_transaction_draft_rows, read_transaction_drafts

KASPI_SOURCE = "kaspi_pdf"
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
    return parse_kaspi_pdf_bytes(Path(path).read_bytes())


def parse_kaspi_pdf_bytes(content: bytes) -> pd.DataFrame:
    return _import_frame_from_rows(
        _extract_rows_from_pdf(io.BytesIO(content)),
        statement_id=hashlib.sha256(content).hexdigest(),
    )


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


def _import_frame_from_rows(
    rows: list[dict],
    source: str = KASPI_SOURCE,
    statement_id: str | None = None,
) -> pd.DataFrame:
    data = pd.DataFrame(rows)
    if data.empty:
        return _empty_import_frame()
    data["is_internal_transfer"] = data["details"].map(_is_internal_transfer)
    history_categories = _history_category_lookup()
    data["category"] = data.apply(
        lambda row: _categorize(row["details"], row["signed_amount"], history_categories), axis=1
    )
    data["amount"] = data["signed_amount"].abs()
    data["direction"] = data["signed_amount"].map(
        lambda value: "credit" if float(value) > 0 else "debit"
    )
    data["comment"] = data["details"].map(_clean_comment)
    data["source"] = source
    data["source_id"] = _source_ids(data, statement_id or _rows_statement_id(rows))
    data["status"] = "draft"
    data = _add_duplicate_flags(data, source)
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
    source_keys = _existing_source_keys()

    actions = incoming["import_action"].astype(str).str.lower()
    unsupported = ~actions.isin({"import", "skip", "review"})
    if unsupported.any():
        raise ValueError("Некорректное действие импорта: выбери import или skip.")
    if actions.eq("review").any():
        raise ValueError(
            "Есть возможные дубли без решения: для каждой строки review выбери import или skip."
        )
    current_source_match = incoming.apply(
        lambda row: _source_key(row) in source_keys, axis=1
    )
    preview_source_match = _as_bool_series(incoming["duplicate_in_source"])
    if (current_source_match & ~preview_source_match).any():
        raise ValueError(
            "История операций изменилась после Preview: построй Preview заново и проверь возможные дубли."
        )

    duplicate_mask = _as_bool_series(incoming["duplicate_in_staging"])
    duplicate_mask = duplicate_mask | incoming["skip_reason"].astype(str).eq(
        "internal_transfer"
    )
    duplicate_mask = duplicate_mask | actions.eq("skip")
    accepted = incoming[~duplicate_mask].copy(deep=True)
    if accepted.empty:
        return {"accepted_rows": 0, "skipped_rows": int(len(incoming))}

    draft_rows = accepted[DRAFT_COLUMNS].copy(deep=True)
    result = append_transaction_draft_rows(draft_rows, path)
    return {
        "accepted_rows": result["accepted_rows"],
        "skipped_rows": int(duplicate_mask.sum()) + result["skipped_rows"],
    }



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


def _categorize(details: str, amount: float, history_categories: dict[str, str] | None = None) -> str:
    if _is_internal_transfer(details):
        return INTERNAL_TRANSFER_CATEGORY
    history_category = (history_categories or {}).get(_normalize_text(_clean_comment(details)))
    if history_category:
        return history_category
    rules = _load_rules()
    normalized = _normalize_text(details)
    for _, rule in rules.iterrows():
        pattern = _normalize_text(rule.get("pattern", ""))
        if pattern and pattern in normalized:
            return str(rule.get("category", DEFAULT_EXPENSE_CATEGORY))
    return DEFAULT_INCOME_CATEGORY if amount > 0 else DEFAULT_EXPENSE_CATEGORY


def _history_category_lookup() -> dict[str, str]:
    try:
        transactions = get_transactions()
    except Exception:
        return {}
    required_columns = {"Дата", "Категория", "Комментарий"}
    if transactions.empty or not required_columns.issubset(transactions.columns):
        return {}

    history = transactions.loc[:, list(required_columns)].copy(deep=True)
    history = history[history["Комментарий"].notna() & history["Категория"].notna()]
    history["__comment"] = history["Комментарий"].map(_normalize_text)
    history["__category"] = history["Категория"].astype(str).str.strip()
    history["__date"] = pd.to_datetime(history["Дата"], errors="coerce")
    history = history[history["__comment"].ne("") & history["__category"].ne("")]
    history = history.sort_values("__date", ascending=False, kind="mergesort")
    history = history.drop_duplicates("__comment", keep="first")
    return dict(zip(history["__comment"], history["__category"]))


def _load_rules() -> pd.DataFrame:
    rules_path = config.active_data_path("import_rules", "categories.csv")
    if not rules_path.exists():
        return pd.DataFrame(columns=["pattern", "category"])
    return pd.read_csv(rules_path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def _is_internal_transfer(details: str) -> bool:
    normalized = _normalize_text(details)
    return any(pattern in normalized for pattern in INTERNAL_TRANSFER_PATTERNS)


def _clean_comment(details: str) -> str:
    details = re.sub(
        r"^(Purchases|Transfers|Replenishment|Transfer to your|Pending)\s+",
        "",
        str(details),
        flags=re.IGNORECASE,
    )
    return details.strip()


def _source_ids(data: pd.DataFrame, statement_id: str) -> pd.Series:
    return pd.Series(
        [
            hashlib.sha256(
                f"{statement_id}|{row_number}|{_source_base_key(row)}".encode("utf-8")
            ).hexdigest()
            for row_number, (_, row) in enumerate(data.iterrows())
        ],
        index=data.index,
    )


def _source_base_key(row: pd.Series) -> str:
    return (
        f"{row['date']}|{row['signed_amount']}|{row['currency']}|"
        f"{_normalize_text(row['details'])}"
    )


def _rows_statement_id(rows: list[dict]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _add_duplicate_flags(data: pd.DataFrame, source: str = KASPI_SOURCE) -> pd.DataFrame:
    result = data.copy(deep=True)
    existing_drafts = read_transaction_drafts()
    existing_source_ids = set(existing_drafts.loc[existing_drafts["source"].eq(source), "source_id"])
    source_keys = _existing_source_keys()
    result["duplicate_in_staging"] = result["source_id"].isin(existing_source_ids)
    result["duplicate_in_source"] = result.apply(lambda row: _source_key(row) in source_keys, axis=1)
    result["skip_reason"] = result.apply(_skip_reason, axis=1)
    result["import_action"] = result["skip_reason"].map(_default_import_action)
    return result


def _skip_reason(row: pd.Series) -> str:
    if bool(row.get("is_internal_transfer", False)):
        return "internal_transfer"
    if bool(row.get("duplicate_in_staging", False)):
        return "duplicate_in_staging"
    if bool(row.get("duplicate_in_source", False)):
        return "possible_duplicate"
    return ""


def _default_import_action(skip_reason: str) -> str:
    if skip_reason == "possible_duplicate":
        return "review"
    return "skip" if skip_reason else "import"


def _sort_import_preview(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy(deep=True)
    result["__date_sort"] = pd.to_datetime(result["date"], errors="coerce")
    result["__action_sort"] = result["import_action"].map({"import": 0, "skip": 1}).fillna(2)
    result = result.sort_values(["__action_sort", "category", "__date_sort", "comment"], kind="mergesort")
    return result.drop(columns=["__date_sort", "__action_sort"]).reset_index(drop=True)


def _existing_source_keys() -> set[tuple[str, str, float, str, str]]:
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
        comment = _normalize_text(row.get("Комментарий", ""))
        direction = _stored_transaction_direction(str(row.get("Категория", "")))
        if pd.isna(date) or pd.isna(amount) or not currency:
            continue
        keys.add(
            (
                date.date().isoformat(),
                currency,
                round(abs(float(amount)), 2),
                comment,
                direction,
            )
        )
    return keys


def _source_key(row: pd.Series) -> tuple[str, str, float, str, str]:
    return (
        str(row["date"]),
        str(row["currency"]).upper(),
        round(abs(float(row["amount"])), 2),
        _normalize_text(row.get("comment", "")),
        str(row.get("direction", "")).lower(),
    )


def _stored_transaction_direction(category: str) -> str:
    if category in {"Доход", "Погашение деб. зад.", "Кредиторская задолженность"}:
        return "credit"
    return "debit"


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).upper()).strip()


def _import_columns() -> list[str]:
    return [
        *DRAFT_COLUMNS,
        "direction",
        "details",
        "duplicate_in_staging",
        "duplicate_in_source",
        "skip_reason",
        "import_action",
    ]


def _empty_import_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_import_columns())
