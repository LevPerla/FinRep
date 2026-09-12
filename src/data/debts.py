from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pandas as pd

from src import config
from src.data.csv_storage import atomic_write_csv
from src.data.file_commit import commit_file_images, read_commit_receipt
from src.data.get import get_transactions
from src.data.money import format_money_amount, parse_money_amount, quantize_money_amount
from src.data.proccess import convert_transaction
from src.data import staging


DEBT_COLUMNS = [
    "debt_id",
    "type",
    "counterparty",
    "opened_date",
    "principal_amount",
    "principal_currency",
    "cash_amount",
    "cash_currency",
    "comment",
    "status",
]
PAYMENT_COLUMNS = [
    "payment_id",
    "debt_id",
    "date",
    "amount",
    "cash_amount",
    "cash_currency",
    "comment",
    "status",
]
DEBT_TYPES = {"receivable", "liability"}
DEBT_STATUSES = {"active", "closed"}
PAYMENT_STATUSES = {"posted"}
DEBT_DRAFT_SOURCE = "debt"
ZERO_MONEY = Decimal("0")


@dataclass(frozen=True)
class DebtValidationIssue:
    path: Path
    row_number: int | None
    message: str

    def __str__(self) -> str:
        prefix = "file" if self.row_number is None else f"row {self.row_number}"
        return f"{prefix}: {self.message}"


def ensure_debt_files(debts_path: str | Path | None = None, payments_path: str | Path | None = None) -> None:
    debt_path = _debt_path(debts_path)
    payment_path = _payment_path(payments_path)
    if config.is_test_mode():
        return
    debt_path.parent.mkdir(parents=True, exist_ok=True)
    payment_path.parent.mkdir(parents=True, exist_ok=True)
    if not debt_path.exists():
        atomic_write_csv(
            pd.DataFrame(columns=DEBT_COLUMNS), debt_path, sep=";", index=False, encoding="utf-8-sig"
        )
    if not payment_path.exists():
        atomic_write_csv(
            pd.DataFrame(columns=PAYMENT_COLUMNS), payment_path, sep=";", index=False, encoding="utf-8-sig"
        )


def read_debts(path: str | Path | None = None) -> pd.DataFrame:
    if config.is_test_mode():
        return _read_debts_unlocked(path)
    with staging.transaction_drafts_commit_lock():
        return _read_debts_unlocked(path)


def read_debt_payments(path: str | Path | None = None) -> pd.DataFrame:
    if config.is_test_mode():
        return _read_debt_payments_unlocked(path)
    with staging.transaction_drafts_commit_lock():
        return _read_debt_payments_unlocked(path)


def _read_debts_unlocked(path: str | Path | None = None) -> pd.DataFrame:
    ensure_debt_files(path, None)
    if not _debt_path(path).exists():
        return pd.DataFrame(columns=DEBT_COLUMNS)
    data = pd.read_csv(_debt_path(path), sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    return _normalize_debts(data)


def _read_debt_payments_unlocked(path: str | Path | None = None) -> pd.DataFrame:
    ensure_debt_files(None, path)
    if not _payment_path(path).exists():
        return pd.DataFrame(columns=PAYMENT_COLUMNS)
    data = pd.read_csv(_payment_path(path), sep=";", dtype=str, encoding="utf-8-sig").fillna("")
    return _normalize_payments(data)


def write_debts(data: pd.DataFrame, path: str | Path | None = None) -> None:
    config.require_writable_mode()
    normalized = _normalize_debts(data)
    _raise_if_issues(validate_debt_rows(normalized, read_debt_payments()))
    debt_path = _debt_path(path)
    debt_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(
        _money_storage_frame(normalized, ["principal_amount", "cash_amount"]),
        debt_path,
        sep=";",
        index=False,
        encoding="utf-8-sig",
    )


def write_debt_payments(data: pd.DataFrame, path: str | Path | None = None) -> None:
    config.require_writable_mode()
    normalized = _normalize_payments(data)
    _raise_if_issues(validate_debt_rows(read_debts(), normalized))
    payment_path = _payment_path(path)
    payment_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(
        _money_storage_frame(normalized, ["amount", "cash_amount"]),
        payment_path,
        sep=";",
        index=False,
        encoding="utf-8-sig",
    )


def create_debt(
    debt_type: str,
    counterparty: str,
    opened_date: str,
    principal_amount,
    principal_currency: str,
    cash_amount=None,
    cash_currency: str | None = None,
    comment: str = "",
    create_draft: bool = True,
    operation_id: str | None = None,
) -> dict:
    config.require_writable_mode()
    operation_id = str(operation_id or uuid4().hex)
    with staging.transaction_drafts_commit_lock() as draft_path:
        completed = _completed_debt_create(operation_id, draft_path)
        if completed is not None:
            return completed

        debts = _read_debts_unlocked()
        payments = _read_debt_payments_unlocked()
        debt_id = f"debt-{uuid4().hex[:12]}"
        principal_amount_value = _to_positive_money(principal_amount, "principal_amount")
        cash_amount_value = (
            principal_amount_value
            if cash_amount in {None, ""}
            else _to_positive_money(cash_amount, "cash_amount")
        )
        cash_currency_value = (
            principal_currency
            if cash_amount in {None, ""} or not cash_currency
            else cash_currency
        )
        row = {
            "debt_id": debt_id,
            "type": debt_type,
            "counterparty": counterparty,
            "opened_date": opened_date,
            "principal_amount": principal_amount_value,
            "principal_currency": principal_currency,
            "cash_amount": cash_amount_value,
            "cash_currency": cash_currency_value,
            "comment": comment,
            "status": "active",
        }
        normalized = _normalize_debts(pd.concat([debts, pd.DataFrame([row])], ignore_index=True))
        _raise_if_issues(validate_debt_rows(normalized, payments))

        images = {_debt_path(): _csv_bytes(normalized)}
        if create_draft:
            debt = normalized[normalized["debt_id"].eq(debt_id)].iloc[0]
            drafts = staging._read_transaction_drafts_unlocked(draft_path)
            updated_drafts = staging.transaction_drafts_with_appended_row(
                drafts,
                date=debt["opened_date"],
                category=_debt_category(debt["type"]),
                currency=debt["cash_currency"],
                amount=format_money_amount(debt["cash_amount"]),
                comment=_debt_comment(debt),
                source=DEBT_DRAFT_SOURCE,
                source_id=f"{debt_id}:open",
                status="ready",
            )
            images[draft_path] = _csv_bytes(updated_drafts, encoding="utf-8")

        result = {"debt_id": debt_id, "draft_created": create_draft}
        commit_file_images(
            _debt_create_journal_path(draft_path),
            images,
            receipt_path=_debt_create_receipt_path(draft_path),
            receipt={"version": 1, "operation_id": operation_id, "result": result},
        )
        return result


def create_debt_payment(
    debt_id: str,
    date: str,
    amount,
    cash_amount=None,
    cash_currency: str | None = None,
    comment: str = "",
    create_draft: bool = True,
    operation_id: str | None = None,
) -> dict:
    config.require_writable_mode()
    operation_id = str(operation_id or uuid4().hex)
    with staging.transaction_drafts_commit_lock() as draft_path:
        completed = _completed_debt_payment(operation_id, draft_path)
        if completed is not None:
            return completed

        debts = _read_debts_unlocked()
        payments = _read_debt_payments_unlocked()
        debt_rows = debts[debts["debt_id"].eq(str(debt_id))]
        if debt_rows.empty:
            raise KeyError(f"debt not found: {debt_id}")
        debt = debt_rows.iloc[0]
        amount_value = _to_positive_money(amount, "amount")
        cash_amount_value = (
            amount_value
            if cash_amount in {None, ""}
            else _to_positive_money(cash_amount, "cash_amount")
        )
        cash_currency_value = (
            debt["principal_currency"]
            if cash_amount in {None, ""} or not cash_currency
            else cash_currency
        )
        outstanding = _outstanding_by_debt(debts, payments).get(str(debt_id), ZERO_MONEY)
        if amount_value > outstanding:
            raise ValueError(
                f"Погашение больше остатка: {amount_value:g} > "
                f"{outstanding:g} {debt['principal_currency']}"
            )

        payment_id = f"payment-{uuid4().hex[:12]}"
        row = {
            "payment_id": payment_id,
            "debt_id": debt_id,
            "date": date,
            "amount": amount_value,
            "cash_amount": cash_amount_value,
            "cash_currency": cash_currency_value,
            "comment": comment,
            "status": "posted",
        }
        normalized_payments = _normalize_payments(
            pd.concat([payments, pd.DataFrame([row])], ignore_index=True)
        )
        updated_debts = _close_repaid_debts(debts, normalized_payments)
        _raise_if_issues(validate_debt_rows(updated_debts, normalized_payments))

        images = {
            _debt_path(): _csv_bytes(updated_debts),
            _payment_path(): _csv_bytes(normalized_payments),
        }
        if create_draft:
            payment = normalized_payments[
                normalized_payments["payment_id"].eq(payment_id)
            ].iloc[0]
            drafts = staging._read_transaction_drafts_unlocked(draft_path)
            updated_drafts = staging.transaction_drafts_with_appended_row(
                drafts,
                date=payment["date"],
                category=_payment_category(debt["type"]),
                currency=payment["cash_currency"],
                amount=format_money_amount(payment["cash_amount"]),
                comment=_payment_comment(debt, payment),
                source=DEBT_DRAFT_SOURCE,
                source_id=f"{debt_id}:{payment_id}",
                status="ready",
            )
            images[draft_path] = _csv_bytes(updated_drafts, encoding="utf-8")

        result = {"payment_id": payment_id, "draft_created": create_draft}
        commit_file_images(
            _debt_payment_journal_path(draft_path),
            images,
            receipt_path=_debt_payment_receipt_path(draft_path),
            receipt={"version": 1, "operation_id": operation_id, "result": result},
        )
        return result


def create_debt_payment_from_cash(
    debt_id: str,
    date: str,
    cash_amount,
    cash_currency: str,
    comment: str = "",
    create_draft: bool = True,
    operation_id: str | None = None,
) -> dict:
    debts = read_debts()
    debt_rows = debts[debts["debt_id"].eq(str(debt_id))]
    if debt_rows.empty:
        raise KeyError(f"debt not found: {debt_id}")
    debt = debt_rows.iloc[0]
    cash_amount_value = _to_positive_money(cash_amount, "cash_amount")
    cash_currency = str(cash_currency).upper()
    debt_amount = _cash_to_debt_amount(
        cash_amount_value,
        cash_currency,
        str(debt["principal_currency"]),
        date,
    )
    return create_debt_payment(
        debt_id=debt_id,
        date=date,
        amount=debt_amount,
        cash_amount=cash_amount_value,
        cash_currency=cash_currency,
        comment=comment,
        create_draft=create_draft,
        operation_id=operation_id,
    )


def active_debt_balances(debt_type: str, currency: str | None = None) -> pd.DataFrame:
    debts = read_debts()
    payments = read_debt_payments()
    if debts.empty:
        return _active_balance_empty(currency)

    balances = _debt_balance_table(debts, payments)
    balances = balances[
        (balances["type"].eq(debt_type))
        & (balances["status"].ne("closed"))
        & (balances["outstanding_amount"] > 0)
    ].copy(deep=True)
    if balances.empty:
        return _active_balance_empty(currency)

    balances["paid_amount"] = balances["paid_amount"].map(quantize_money_amount)
    balances["outstanding_amount"] = balances["outstanding_amount"].map(quantize_money_amount)
    if currency is not None:
        balances[f"outstanding_{currency}"] = _convert_outstanding(balances, currency)
    return balances.reset_index(drop=True)


def migrate_legacy_debts(create_files_only_if_missing: bool = True) -> dict:
    config.require_writable_mode()
    if create_files_only_if_missing and _existing_debt_rows_count() > 0:
        return {"created_debts": 0, "created_payments": 0, "skipped": "debt files already contain rows"}

    transactions = get_transactions()
    debt_rows = transactions[transactions["Категория"].isin(["Дебиторская задолженность", "Кредиторская задолженность"])].copy(deep=True)
    payment_rows = transactions[transactions["Категория"].isin(["Погашение деб. зад.", "Погашение кред. зад."])].copy(deep=True)
    debt_rows = debt_rows[debt_rows["Значение"].ne(0)]
    payment_rows = payment_rows[payment_rows["Значение"].ne(0)]

    debts = []
    payments = []
    debt_id_by_key = {}
    for (category, comment, currency), group in debt_rows.groupby(["Категория", "Комментарий", "Валюта"], dropna=False):
        debt_id = f"legacy-{_slug(category)}-{_slug(comment or 'no-comment')}-{str(currency).lower()}"
        debt_type = "receivable" if category == "Дебиторская задолженность" else "liability"
        debt_id_by_key[(debt_type, str(comment), str(currency).upper())] = debt_id
        debts.append(
            {
                "debt_id": debt_id,
                "type": debt_type,
                "counterparty": str(comment) or "Legacy",
                "opened_date": pd.to_datetime(group["Дата"].min()).date().isoformat(),
                "principal_amount": group["Значение"].sum(),
                "principal_currency": str(currency).upper(),
                "cash_amount": group["Значение"].sum(),
                "cash_currency": str(currency).upper(),
                "comment": "Migrated from legacy transaction categories",
                "status": "active",
            }
        )

    for _, row in payment_rows.iterrows():
        debt_type = "receivable" if row["Категория"] == "Погашение деб. зад." else "liability"
        key = (debt_type, str(row["Комментарий"]), str(row["Валюта"]).upper())
        debt_id = debt_id_by_key.get(key)
        if not debt_id:
            continue
        payments.append(
            {
                "payment_id": f"legacy-payment-{uuid4().hex[:12]}",
                "debt_id": debt_id,
                "date": pd.to_datetime(row["Дата"]).date().isoformat(),
                "amount": row["Значение"],
                "cash_amount": row["Значение"],
                "cash_currency": str(row["Валюта"]).upper(),
                "comment": str(row["Комментарий"]),
                "status": "posted",
            }
        )

    debts_df = _close_repaid_debts(_normalize_debts(pd.DataFrame(debts)), _normalize_payments(pd.DataFrame(payments)))
    payments_df = _normalize_payments(pd.DataFrame(payments))
    _raise_if_issues(validate_debt_rows(debts_df, payments_df))
    _debt_path().parent.mkdir(parents=True, exist_ok=True)
    atomic_write_csv(debts_df, _debt_path(), sep=";", index=False, encoding="utf-8-sig")
    atomic_write_csv(payments_df, _payment_path(), sep=";", index=False, encoding="utf-8-sig")
    return {"created_debts": int(len(debts_df)), "created_payments": int(len(payments_df)), "skipped": ""}


def validate_debt_files() -> list[DebtValidationIssue]:
    if not _debt_path().exists() and not _payment_path().exists():
        return []
    debts = _read_raw_csv(_debt_path(), DEBT_COLUMNS)
    payments = _read_raw_csv(_payment_path(), PAYMENT_COLUMNS)
    return validate_debt_rows(debts, payments)


def validate_debt_rows(debts: pd.DataFrame, payments: pd.DataFrame) -> list[DebtValidationIssue]:
    issues: list[DebtValidationIssue] = []
    issues.extend(_missing_columns(_debt_path(), debts, DEBT_COLUMNS))
    issues.extend(_missing_columns(_payment_path(), payments, PAYMENT_COLUMNS))
    if issues:
        return issues

    debts = _normalize_debts(debts)
    payments = _normalize_payments(payments)

    duplicate_debts = debts["debt_id"].ne("") & debts.duplicated("debt_id", keep=False)
    for index in debts.index[duplicate_debts]:
        issues.append(DebtValidationIssue(_debt_path(), index + 2, "duplicate debt_id"))

    duplicate_payments = payments["payment_id"].ne("") & payments.duplicated("payment_id", keep=False)
    for index in payments.index[duplicate_payments]:
        issues.append(DebtValidationIssue(_payment_path(), index + 2, "duplicate payment_id"))

    for index, row in debts.iterrows():
        _validate_debt_row(index, row, issues)

    known_debt_ids = set(debts["debt_id"])
    for index, row in payments.iterrows():
        _validate_payment_row(index, row, known_debt_ids, issues)

    outstanding = _outstanding_by_debt(debts, payments)
    for debt_id, value in outstanding.items():
        if value.is_finite() and value < ZERO_MONEY:
            index = debts.index[debts["debt_id"].eq(debt_id)][0]
            issues.append(DebtValidationIssue(_debt_path(), index + 2, "payments exceed principal"))

    return issues


def _debt_balance_table(debts: pd.DataFrame, payments: pd.DataFrame) -> pd.DataFrame:
    debts = _normalize_debts(debts)
    payments = _normalize_payments(payments)
    if payments.empty:
        paid = pd.Series(dtype=object, name="paid_amount")
    else:
        paid = payments.groupby("debt_id")["amount"].agg(
            lambda values: sum(values, ZERO_MONEY)
        ).rename("paid_amount")
    result = debts.merge(paid, left_on="debt_id", right_index=True, how="left")
    result["paid_amount"] = result["paid_amount"].map(
        lambda value: ZERO_MONEY if pd.isna(value) else value
    )
    result["outstanding_amount"] = result["principal_amount"] - result["paid_amount"]
    return result


def _outstanding_by_debt(debts: pd.DataFrame, payments: pd.DataFrame) -> dict[str, Decimal]:
    table = _debt_balance_table(debts, payments)
    return dict(zip(table["debt_id"].astype(str), table["outstanding_amount"]))


def _close_repaid_debts(debts: pd.DataFrame, payments: pd.DataFrame) -> pd.DataFrame:
    updated = _normalize_debts(debts)
    outstanding = _outstanding_by_debt(updated, payments)
    for debt_id, value in outstanding.items():
        if value.is_finite() and value <= ZERO_MONEY:
            updated.loc[updated["debt_id"].eq(debt_id), "status"] = "closed"
    return updated


def _convert_outstanding(data: pd.DataFrame, currency: str) -> pd.Series:
    result = data["outstanding_amount"].map(quantize_money_amount)
    different_currency = data["principal_currency"].ne(currency)
    if not different_currency.any():
        return result
    conversion = pd.DataFrame(
        {
            "Дата": pd.Timestamp.today().normalize(),
            "Валюта": data.loc[different_currency, "principal_currency"],
            "Значение": data.loc[different_currency, "outstanding_amount"].map(float),
        },
        index=data.index[different_currency],
    )
    converted = convert_transaction(
        conversion,
        to_curr=currency,
        target_col="Значение",
        use_current_rate=True,
        round_result=False,
    )
    result.loc[different_currency] = converted["Значение"].map(quantize_money_amount)
    return result


def _cash_to_debt_amount(
    cash_amount: Decimal, cash_currency: str, debt_currency: str, date: str
) -> Decimal:
    cash_currency = str(cash_currency).upper()
    debt_currency = str(debt_currency).upper()
    if cash_currency == debt_currency:
        return quantize_money_amount(cash_amount)

    conversion = pd.DataFrame(
        {
            "Дата": [pd.to_datetime(date)],
            "Валюта": [cash_currency],
            "Значение": [float(cash_amount)],
        }
    )
    converted = convert_transaction(
        conversion,
        to_curr=debt_currency,
        target_col="Значение",
        use_current_rate=False,
        round_result=False,
    )
    converted_currency = str(converted.iloc[0]["Валюта"]).upper()
    if converted_currency != debt_currency:
        raise ValueError(f"Не удалось конвертировать {cash_currency} в {debt_currency} для погашения.")
    value = converted.iloc[0]["Значение"]
    try:
        return quantize_money_amount(value)
    except ValueError as exc:
        raise ValueError(
            f"Не удалось конвертировать {cash_currency} в {debt_currency} для погашения."
        ) from exc


def _append_debt_draft(date: str, category: str, currency: str, amount, comment: str, source_id: str):
    return staging.append_transaction_draft(
        date=date,
        category=category,
        currency=currency,
        amount=amount,
        comment=comment,
        source=DEBT_DRAFT_SOURCE,
        source_id=source_id,
        status="ready",
    )


def _debt_category(debt_type: str) -> str:
    return "Дебиторская задолженность" if debt_type == "receivable" else "Кредиторская задолженность"


def _payment_category(debt_type: str) -> str:
    return "Погашение деб. зад." if debt_type == "receivable" else "Погашение кред. зад."


def _debt_comment(debt: pd.Series) -> str:
    suffix = f" - {debt['comment']}" if str(debt.get("comment", "")).strip() else ""
    return f"{debt['debt_id']} | {debt['counterparty']}{suffix}"


def _payment_comment(debt: pd.Series, payment: pd.Series) -> str:
    suffix = f" - {payment['comment']}" if str(payment.get("comment", "")).strip() else ""
    return f"{debt['debt_id']} | {debt['counterparty']}{suffix}"


def _normalize_debts(data: pd.DataFrame) -> pd.DataFrame:
    normalized = data.copy(deep=True)
    for column in DEBT_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[DEBT_COLUMNS].fillna("")
    normalized["type"] = normalized["type"].astype(str).str.strip().str.lower()
    normalized["principal_currency"] = normalized["principal_currency"].astype(str).str.strip().str.upper()
    normalized["cash_currency"] = normalized["cash_currency"].astype(str).str.strip().str.upper()
    normalized["status"] = normalized["status"].replace("", "active").astype(str).str.strip().str.lower()
    normalized["principal_amount"] = _money_series(normalized["principal_amount"], "principal_amount")
    normalized["cash_amount"] = _money_series(normalized["cash_amount"], "cash_amount")
    return normalized


def _normalize_payments(data: pd.DataFrame) -> pd.DataFrame:
    normalized = data.copy(deep=True)
    for column in PAYMENT_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
    normalized = normalized[PAYMENT_COLUMNS].fillna("")
    normalized["cash_currency"] = normalized["cash_currency"].astype(str).str.strip().str.upper()
    normalized["status"] = normalized["status"].replace("", "posted").astype(str).str.strip().str.lower()
    normalized["amount"] = _money_series(normalized["amount"], "amount")
    normalized["cash_amount"] = _money_series(normalized["cash_amount"], "cash_amount")
    return normalized


def _money_series(series: pd.Series, field_name: str) -> pd.Series:
    values = []
    for value in series:
        try:
            values.append(parse_money_amount(value, field_name=field_name))
        except ValueError:
            values.append(Decimal("NaN"))
    return pd.Series(values, index=series.index, dtype=object)


def _to_positive_money(value, name: str) -> Decimal:
    try:
        parsed = quantize_money_amount(value, field_name=name)
    except ValueError as exc:
        raise ValueError(f"{name} must be finite and positive") from exc
    if parsed <= ZERO_MONEY:
        raise ValueError(f"{name} must be finite and positive")
    return parsed


def _money_storage_frame(data: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    result = data.copy(deep=True)
    for column in columns:
        result[column] = result[column].map(
            lambda value: format_money_amount(value, field_name=column)
        )
    return result


def _validate_debt_row(index: int, row: pd.Series, issues: list[DebtValidationIssue]) -> None:
    if not str(row["debt_id"]).strip():
        issues.append(DebtValidationIssue(_debt_path(), index + 2, "debt_id is required"))
    if row["type"] not in DEBT_TYPES:
        issues.append(DebtValidationIssue(_debt_path(), index + 2, f"unsupported type {row['type']!r}"))
    if not str(row["counterparty"]).strip():
        issues.append(DebtValidationIssue(_debt_path(), index + 2, "counterparty is required"))
    if pd.isna(pd.to_datetime(row["opened_date"], errors="coerce")):
        issues.append(DebtValidationIssue(_debt_path(), index + 2, "invalid opened_date"))
    if not _is_positive_money(row["principal_amount"]):
        issues.append(DebtValidationIssue(_debt_path(), index + 2, "principal_amount must be finite and positive"))
    if row["principal_currency"] not in config.UNIQUE_TICKERS:
        issues.append(DebtValidationIssue(_debt_path(), index + 2, f"unsupported principal_currency {row['principal_currency']!r}"))
    if not _is_positive_money(row["cash_amount"]):
        issues.append(DebtValidationIssue(_debt_path(), index + 2, "cash_amount must be finite and positive"))
    if row["cash_currency"] not in config.UNIQUE_TICKERS:
        issues.append(DebtValidationIssue(_debt_path(), index + 2, f"unsupported cash_currency {row['cash_currency']!r}"))
    if row["status"] not in DEBT_STATUSES:
        issues.append(DebtValidationIssue(_debt_path(), index + 2, f"unsupported status {row['status']!r}"))


def _validate_payment_row(index: int, row: pd.Series, known_debt_ids: set[str], issues: list[DebtValidationIssue]) -> None:
    if not str(row["payment_id"]).strip():
        issues.append(DebtValidationIssue(_payment_path(), index + 2, "payment_id is required"))
    if row["debt_id"] not in known_debt_ids:
        issues.append(DebtValidationIssue(_payment_path(), index + 2, f"unknown debt_id {row['debt_id']!r}"))
    if pd.isna(pd.to_datetime(row["date"], errors="coerce")):
        issues.append(DebtValidationIssue(_payment_path(), index + 2, "invalid date"))
    if not _is_positive_money(row["amount"]):
        issues.append(DebtValidationIssue(_payment_path(), index + 2, "amount must be finite and positive"))
    if not _is_positive_money(row["cash_amount"]):
        issues.append(DebtValidationIssue(_payment_path(), index + 2, "cash_amount must be finite and positive"))
    if row["cash_currency"] not in config.UNIQUE_TICKERS:
        issues.append(DebtValidationIssue(_payment_path(), index + 2, f"unsupported cash_currency {row['cash_currency']!r}"))
    if row["status"] not in PAYMENT_STATUSES:
        issues.append(DebtValidationIssue(_payment_path(), index + 2, f"unsupported status {row['status']!r}"))


def _missing_columns(path: Path, data: pd.DataFrame, columns: list[str]) -> list[DebtValidationIssue]:
    missing = [column for column in columns if column not in data.columns]
    return [DebtValidationIssue(path, None, f"missing required column {column!r}") for column in missing]


def _is_positive_money(value) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > ZERO_MONEY


def _active_balance_empty(currency: str | None) -> pd.DataFrame:
    columns = DEBT_COLUMNS + ["paid_amount", "outstanding_amount"]
    if currency is not None:
        columns.append(f"outstanding_{currency}")
    return pd.DataFrame(columns=columns)


def _raise_if_issues(issues: list[DebtValidationIssue]) -> None:
    if issues:
        raise ValueError("\n".join(str(issue) for issue in issues))


def _existing_debt_rows_count() -> int:
    total = 0
    for path in [_debt_path(), _payment_path()]:
        if not path.exists():
            continue
        try:
            total += len(pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna(""))
        except Exception:
            total += 1
    return total


def _read_raw_csv(path: Path, columns: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=columns)
    return pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def _debt_path(path: str | Path | None = None) -> Path:
    return Path(path or config.active_data_path("debts", "debts.csv"))


def _payment_path(path: str | Path | None = None) -> Path:
    return Path(path or config.active_data_path("debts", "debt_payments.csv"))


def _debt_create_journal_path(draft_path: Path) -> Path:
    return draft_path.with_name(f".{draft_path.name}.debt-create-commit.json")


def _debt_create_receipt_path(draft_path: Path) -> Path:
    return draft_path.with_name(f".{draft_path.name}.last-debt-create.json")


def _completed_debt_create(operation_id: str, draft_path: Path) -> dict | None:
    receipt = read_commit_receipt(_debt_create_receipt_path(draft_path))
    if receipt is None or receipt.get("operation_id") != operation_id:
        return None
    result = receipt.get("result")
    return result if isinstance(result, dict) else None


def _debt_payment_journal_path(draft_path: Path) -> Path:
    return draft_path.with_name(f".{draft_path.name}.debt-payment-commit.json")


def _debt_payment_receipt_path(draft_path: Path) -> Path:
    return draft_path.with_name(f".{draft_path.name}.last-debt-payment.json")


def _completed_debt_payment(operation_id: str, draft_path: Path) -> dict | None:
    receipt = read_commit_receipt(_debt_payment_receipt_path(draft_path))
    if receipt is None or receipt.get("operation_id") != operation_id:
        return None
    result = receipt.get("result")
    return result if isinstance(result, dict) else None


def _csv_bytes(data: pd.DataFrame, encoding: str = "utf-8-sig") -> bytes:
    return data.to_csv(sep=";", index=False).encode(encoding)


def _slug(value) -> str:
    text = "".join(char.lower() if char.isalnum() else "-" for char in str(value))
    return "-".join(part for part in text.split("-") if part)[:48] or "legacy"
