from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pandas as pd

from src import config
from src.data.get import get_transactions
from src.data.staging import (
    DRAFT_COLUMNS,
    append_transaction_draft_rows,
    read_transaction_drafts_snapshot,
)

DEFAULT_SOURCE = "kaspi_pdf"
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


def import_frame_from_rows(
    rows: list[dict],
    source: str = DEFAULT_SOURCE,
    statement_id: str | None = None,
) -> pd.DataFrame:
    data = pd.DataFrame(rows)
    if data.empty:
        return _empty_import_frame()
    data["is_internal_transfer"] = data["details"].map(is_internal_transfer)
    history_categories = _history_category_lookup()
    data["category"] = data.apply(
        lambda row: categorize(row["details"], row["signed_amount"], history_categories), axis=1
    )
    data["amount"] = data["signed_amount"].abs()
    data["direction"] = data["signed_amount"].map(
        lambda value: "credit" if float(value) > 0 else "debit"
    )
    data["comment"] = data["details"].map(_clean_comment)
    data["source"] = source
    data["source_id"] = _source_ids(data, statement_id or _rows_statement_id(rows))
    if "bank_status" not in data.columns:
        data["bank_status"] = "posted"
    data["bank_status"] = data["bank_status"].replace("", "posted").astype(str).str.lower()
    if "bank_reference" not in data.columns:
        data["bank_reference"] = ""
    if "bank_account_id" not in data.columns:
        data["bank_account_id"] = ""
    data["status"] = "draft"
    data = _add_duplicate_flags(data, source)
    data = _sort_import_preview(data)
    return data[_import_columns()]


def save_import_to_staging(import_rows: list[dict], path: str | Path | None = None) -> dict:
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
    current_drafts, _ = read_transaction_drafts_snapshot(path)
    current_draft_keys = set(
        zip(
            current_drafts["source"].astype(str),
            current_drafts["source_id"].astype(str),
        )
    )
    duplicate_mask = duplicate_mask | incoming.apply(
        lambda row: (str(row["source"]), str(row["source_id"]))
        in current_draft_keys,
        axis=1,
    )
    duplicate_mask = duplicate_mask | incoming["skip_reason"].astype(str).eq(
        "internal_transfer"
    )
    duplicate_mask = duplicate_mask | actions.eq("skip")
    accepted = incoming[~duplicate_mask].copy(deep=True)
    if accepted.empty:
        return {"accepted_rows": 0, "skipped_rows": int(len(incoming))}
    invalid_credit_category = (
        accepted["direction"].astype(str).str.lower().eq("credit")
        & ~accepted["category"].astype(str).isin(config.NOT_COST_COLS)
    )
    if invalid_credit_category.any():
        raise ValueError(
            "Банковское поступление нельзя сохранить как расход: выбери «Сбережения», «Доход» или другую категорию поступления."
        )

    draft_rows = accepted[DRAFT_COLUMNS].copy(deep=True)
    revisions = set(incoming["staging_revision"].astype(str))
    if len(revisions) != 1 or not next(iter(revisions)):
        raise ValueError("Preview не содержит ревизию staging: построй Preview заново.")
    replacements = {
        (str(row["source"]), str(row["source_id"])): str(row["replaces_source_id"])
        for _, row in accepted.iterrows()
        if str(row["replaces_source_id"])
    }
    result = append_transaction_draft_rows(
        draft_rows,
        path,
        expected_revision=next(iter(revisions)),
        pending_replacements=replacements,
    )
    response = {
        "accepted_rows": result["accepted_rows"],
        "skipped_rows": int(duplicate_mask.sum()) + result["skipped_rows"],
    }
    if result["replaced_pending_rows"]:
        response["replaced_pending_rows"] = result["replaced_pending_rows"]
    return response


def _as_bool_series(values: pd.Series) -> pd.Series:
    return values.astype(str).str.lower().isin({"true", "1", "yes"})


def categorize(details: str, amount: float, history_categories: dict[str, str] | None = None) -> str:
    if is_internal_transfer(details):
        return INTERNAL_TRANSFER_CATEGORY
    history_category = (history_categories or {}).get(_normalize_text(_clean_comment(details)))
    if history_category:
        return _category_for_direction(history_category, amount)
    rules = _load_rules()
    normalized = _normalize_text(details)
    for _, rule in rules.iterrows():
        pattern = _normalize_text(rule.get("pattern", ""))
        if pattern and pattern in normalized:
            return _category_for_direction(
                str(rule.get("category", DEFAULT_EXPENSE_CATEGORY)), amount
            )
    return DEFAULT_INCOME_CATEGORY if amount > 0 else DEFAULT_EXPENSE_CATEGORY


def _category_for_direction(category: str, signed_amount: float) -> str:
    if signed_amount > 0 and category not in config.NOT_COST_COLS:
        return "Сбережения"
    return category


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


def is_internal_transfer(details: str) -> bool:
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


def _add_duplicate_flags(data: pd.DataFrame, source: str = DEFAULT_SOURCE) -> pd.DataFrame:
    result = data.copy(deep=True)
    existing_drafts, staging_revision = read_transaction_drafts_snapshot()
    existing_source_ids = set(existing_drafts.loc[existing_drafts["source"].eq(source), "source_id"])
    source_keys = _existing_source_keys()
    result["duplicate_in_staging"] = result["source_id"].isin(existing_source_ids)
    result["duplicate_in_source"] = result.apply(lambda row: _source_key(row) in source_keys, axis=1)
    result["replaces_source_id"] = ""
    result["possible_pending_match"] = False
    if source == "bcc_pdf":
        result = _add_pending_matches(result, existing_drafts)
    result["staging_revision"] = staging_revision
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
    if bool(row.get("possible_pending_match", False)):
        return "possible_pending_match"
    if str(row.get("replaces_source_id", "")):
        return "replaces_pending"
    return ""


def _default_import_action(skip_reason: str) -> str:
    if skip_reason in {"possible_duplicate", "possible_pending_match"}:
        return "review"
    if skip_reason == "replaces_pending":
        return "import"
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


def _add_pending_matches(data: pd.DataFrame, drafts: pd.DataFrame) -> pd.DataFrame:
    result = data.copy(deep=True)
    pending = drafts[
        drafts["source"].eq("bcc_pdf") & drafts["bank_status"].eq("pending")
    ].copy(deep=True)
    if pending.empty:
        return result
    for index, row in result.iterrows():
        if str(row.get("bank_status", "")) != "posted":
            continue
        candidates = _pending_candidates(row, pending)
        if len(candidates) == 1:
            result.at[index, "replaces_source_id"] = str(candidates.iloc[0]["source_id"])
        elif len(candidates) > 1:
            result.at[index, "possible_pending_match"] = True
    return result


def _pending_candidates(row: pd.Series, pending: pd.DataFrame) -> pd.DataFrame:
    reference = _normalize_text(row.get("bank_reference", ""))
    account_id = str(row.get("bank_account_id", ""))
    if reference:
        reference_matches = pending[
            pending["bank_reference"].map(_normalize_text).eq(reference)
            & pending["bank_account_id"].astype(str).eq(account_id)
        ]
        if not reference_matches.empty:
            return reference_matches

    row_date = pd.to_datetime(row.get("date"), errors="coerce")
    pending_dates = pd.to_datetime(pending["date"], errors="coerce")
    pending_amounts = pd.to_numeric(pending["amount"], errors="coerce")
    if pd.isna(row_date):
        return pending.iloc[0:0]
    mask = (
        pending["currency"].astype(str).str.upper().eq(str(row.get("currency", "")).upper())
        & pending["bank_account_id"].astype(str).eq(str(row.get("bank_account_id", "")))
        & pending_amounts.round(2).eq(round(abs(float(row.get("amount", 0))), 2))
        & pending["direction"].astype(str).str.lower().eq(
            str(row.get("direction", "")).lower()
        )
        & pending["comment"].map(_normalize_text).eq(_normalize_text(row.get("comment", "")))
        & (row_date - pending_dates).dt.days.between(0, 7)
    )
    return pending[mask]


def _import_columns() -> list[str]:
    return [
        *DRAFT_COLUMNS,
        "details",
        "duplicate_in_staging",
        "duplicate_in_source",
        "skip_reason",
        "import_action",
        "replaces_source_id",
        "possible_pending_match",
        "staging_revision",
    ]


def _empty_import_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_import_columns())
