from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from src.data.importers.kaspi_pdf import _import_frame_from_rows

KASPI_DEPOSIT_SOURCE = "kaspi_deposit_pdf"
KASPI_DEPOSIT_MARKERS = ("DEPOSIT", "statement balance for the period", "Agreement number:")
KASPI_DEPOSIT_ACCOUNT_RE = re.compile(r"Account number:\s*KZ\d{2}722")
AMOUNT_RE = re.compile(r"(?P<amount>\d[\d ]*,\d{2})")


def parse_kaspi_deposit_pdf(path: str | Path) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(Path(path)), KASPI_DEPOSIT_SOURCE)


def parse_kaspi_deposit_pdf_bytes(content: bytes) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(io.BytesIO(content)), KASPI_DEPOSIT_SOURCE)


def is_kaspi_deposit_statement(first_page_text: str | None) -> bool:
    text = first_page_text or ""
    return bool(KASPI_DEPOSIT_ACCOUNT_RE.search(text)) and all(
        marker in text for marker in KASPI_DEPOSIT_MARKERS
    )


def _extract_rows_from_pdf(pdf_source) -> list[dict]:
    rows: list[dict] = []
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
                    rows.extend(_rows_from_table(table[1:], currency))
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


def _transaction_details(transaction: str, details: str) -> str:
    if transaction == "Interest":
        return re.sub(r"^Kaspi Deposit\s+", "", details, flags=re.IGNORECASE)
    if transaction == "Deposit received" and "From Kaspi Gold" in details:
        return "Transfer to your deposit from Kaspi Gold via kaspi.kz"
    if transaction == "Transfers" and "Transfer on the card" in details:
        return "Transfer to your card on kaspi.kz"
    return f"{transaction} {details}".strip()
