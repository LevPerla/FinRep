from __future__ import annotations

import io
import re
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pandas as pd
import pdfplumber

from src.data.importers.common import import_frame_from_rows

KASPI_DEPOSIT_SOURCE = "kaspi_deposit_pdf"
KASPI_DEPOSIT_MARKERS = ("DEPOSIT", "statement balance for the period", "Agreement number:")
KASPI_DEPOSIT_ACCOUNT_RE = re.compile(
    r"Account number:\s*(?P<account>KZ\d{2}722[A-Z0-9]{13})"
)
KASPI_DEPOSIT_PERIOD_RE = re.compile(
    r"statement balance for the period\s+from\s+\d{2}\.\d{2}\.\d{2}"
    r"\s+to\s+(?P<period_end>\d{2}\.\d{2}\.\d{2})",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(r"(?P<amount>\d[\d ]*,\d{2})")


def parse_kaspi_deposit_pdf(path: str | Path) -> pd.DataFrame:
    return parse_kaspi_deposit_pdf_bytes(Path(path).read_bytes())


def parse_kaspi_deposit_pdf_bytes(content: bytes) -> pd.DataFrame:
    rows = _extract_rows_from_pdf(io.BytesIO(content))
    result = import_frame_from_rows(
        rows,
        KASPI_DEPOSIT_SOURCE,
        statement_id=sha256(content).hexdigest(),
    )
    if rows and rows[0].get("statement_balance"):
        result.attrs["statement_balance"] = {
            "account_id": rows[0]["statement_account_id"],
            "balance": rows[0]["statement_balance"],
            "currency": rows[0]["currency"],
            "as_of_date": rows[0]["statement_balance_date"],
        }
    return result


def is_kaspi_deposit_statement(first_page_text: str | None) -> bool:
    text = first_page_text or ""
    return bool(KASPI_DEPOSIT_ACCOUNT_RE.search(text)) and all(
        marker in text for marker in KASPI_DEPOSIT_MARKERS
    )


def _extract_rows_from_pdf(pdf_source) -> list[dict]:
    rows: list[dict] = []
    closing_balance: str | None = None
    with pdfplumber.open(pdf_source) as pdf:
        first_page_text = pdf.pages[0].extract_text() if pdf.pages else ""
        if not is_kaspi_deposit_statement(first_page_text):
            raise ValueError("PDF не похож на выписку по депозиту Kaspi.")
        currency_match = re.search(r"Currency:\s*(?P<currency>[A-Z]{3})", first_page_text or "")
        if not currency_match:
            raise ValueError("В выписке по депозиту Kaspi не найдена валюта.")
        currency = currency_match.group("currency")
        for page in pdf.pages:
            for table in page.extract_tables():
                if table and _is_transactions_table(table[0]):
                    table_rows = table[1:]
                    parsed_rows = _rows_from_table(table_rows, currency)
                    rows.extend(parsed_rows)
                    if parsed_rows:
                        table_balance = _closing_balance_from_table(table_rows)
                        if table_balance is not None:
                            closing_balance = table_balance
    account_match = KASPI_DEPOSIT_ACCOUNT_RE.search(first_page_text or "")
    period_match = KASPI_DEPOSIT_PERIOD_RE.search(first_page_text or "")
    if rows and closing_balance and account_match and period_match:
        balance_date = pd.to_datetime(
            period_match.group("period_end"), format="%d.%m.%y"
        ).date().isoformat()
        for row in rows:
            row.update({
                "statement_account_id": account_match.group("account"),
                "statement_balance": closing_balance,
                "statement_balance_date": balance_date,
            })
    return rows


def _is_transactions_table(header: list[str | None]) -> bool:
    normalized = [re.sub(r"\s+", " ", str(value or "")).strip().lower() for value in header]
    return normalized[:5] == ["date", "amount", "transaction", "details", "deposit balance"]


def _rows_from_table(table_rows: list[list[str | None]], currency: str) -> list[dict]:
    rows = []
    for row in table_rows:
        if len(row) < 4:
            continue
        date = re.sub(r"\s+", " ", str(row[0] or "")).strip()
        amount_text = re.sub(r"\s+", " ", str(row[1] or "")).strip()
        amount_match = AMOUNT_RE.search(amount_text)
        if not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", date) or not amount_match:
            continue
        amount = float(amount_match.group("amount").replace(" ", "").replace(",", "."))
        transaction = re.sub(r"\s+", " ", str(row[2] or "")).strip()
        raw_details = re.sub(r"\s+", " ", str(row[3] or "")).strip()
        rows.append(
            {
                "date": pd.to_datetime(date, format="%d.%m.%y").date().isoformat(),
                "signed_amount": -amount if "-" in amount_text else amount,
                "currency": currency,
                "details": _transaction_details(transaction, raw_details),
            }
        )
    return rows


def _closing_balance_from_table(table_rows: list[list[str | None]]) -> str | None:
    for row in reversed(table_rows):
        if len(row) < 5:
            continue
        date = re.sub(r"\s+", " ", str(row[0] or "")).strip()
        balance_match = AMOUNT_RE.search(
            re.sub(r"\s+", " ", str(row[4] or "")).strip()
        )
        if re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", date) and balance_match:
            amount = balance_match.group("amount").replace(" ", "").replace(",", ".")
            return str(Decimal(amount))
    return None


def _transaction_details(transaction: str, details: str) -> str:
    if transaction == "Interest":
        return re.sub(r"^Kaspi Deposit\s+", "", details, flags=re.IGNORECASE)
    if transaction == "Deposit received" and "From Kaspi Gold" in details:
        return "Transfer to your deposit from Kaspi Gold via kaspi.kz"
    if transaction == "Transfers" and "Transfer on the card" in details:
        return "Transfer to your card on kaspi.kz"
    return f"{transaction} {details}".strip()
