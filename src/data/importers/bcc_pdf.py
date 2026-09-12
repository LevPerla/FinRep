from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from src.data.importers.common import import_frame_from_rows

BCC_SOURCE = "bcc_pdf"
BCC_MARKER = "Bank CenterCredit JSC"
BCC_RU_MARKERS = ("Выписка", "по счету", "Валюта", "Описание операции")
BCC_ACCOUNT_RE = re.compile(r"KZ\d{2}856\d{13}")
AMOUNT_RE = re.compile(r"(?P<amount>-?\d+\.\d{2})(?P<currency>[A-Z]{3})")
RU_AMOUNT_RE = re.compile(
    r"(?<![\d.,])(?P<amount>[+-]?(?:\d{1,3}(?: \d{3})*|\d+),\d{2})(?!\d)"
)
RU_TRANSACTION_DATE_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def parse_bcc_pdf(path: str | Path) -> pd.DataFrame:
    return parse_bcc_pdf_bytes(Path(path).read_bytes())


def parse_bcc_pdf_bytes(content: bytes) -> pd.DataFrame:
    from hashlib import sha256

    return import_frame_from_rows(
        _extract_rows_from_pdf(io.BytesIO(content)),
        BCC_SOURCE,
        statement_id=sha256(content).hexdigest(),
    )


def _extract_rows_from_pdf(pdf_source) -> list[dict]:
    rows: list[dict] = []
    with pdfplumber.open(pdf_source) as pdf:
        first_page_text = pdf.pages[0].extract_text() if pdf.pages else ""
        if not is_bcc_statement(first_page_text):
            raise ValueError("PDF не похож на выписку Bank CenterCredit.")
        if _is_russian_statement(first_page_text):
            rows = _russian_rows_from_pages(pdf.pages, first_page_text or "")
            return _with_account_id(rows, first_page_text)
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                if _is_pending_transactions_table(table[0]) or _has_pending_rows(table[1:]):
                    rows.extend(_pending_rows_from_table(table[1:]))
                elif _is_posted_transactions_table(table[0]):
                    rows.extend(_posted_rows_from_table(table[1:]))
    return _with_account_id(rows, first_page_text)


def _with_account_id(rows: list[dict], first_page_text: str | None) -> list[dict]:
    match = BCC_ACCOUNT_RE.search(first_page_text or "")
    account_id = match.group(0) if match else ""
    for row in rows:
        row["bank_account_id"] = account_id
    return rows


def is_bcc_statement(first_page_text: str | None) -> bool:
    text = first_page_text or ""
    return BCC_MARKER in text or _is_russian_statement(text)


def _is_russian_statement(text: str | None) -> bool:
    value = text or ""
    return bool(BCC_ACCOUNT_RE.search(value)) and all(marker in value for marker in BCC_RU_MARKERS)


def _russian_rows_from_pages(pages, first_page_text: str) -> list[dict]:
    currency_match = re.search(r"Валюта\s+(?P<currency>[A-Z]{3})", first_page_text)
    if not currency_match:
        raise ValueError("В выписке Bank CenterCredit не найдена валюта счета.")
    currency = currency_match.group("currency")
    rows: list[dict] = []
    for page in pages:
        rows.extend(_russian_rows_from_page(page, currency))
    return rows


def _russian_rows_from_page(page, currency: str) -> list[dict]:
    boundaries = {
        max(0.0, min(float(page.height), float(rect["top"])))
        for rect in page.rects
        if abs(float(rect["x0"]) - 33.0) < 2.0 and abs(float(rect["x1"]) - 103.0) < 2.0
    }
    boundaries.add(0.0)
    rows = []
    ordered_boundaries = sorted(boundaries)
    for top, bottom in zip(ordered_boundaries, ordered_boundaries[1:]):
        if bottom - top < 2:
            continue
        row_top = top + 0.5
        row_bottom = bottom - 0.5
        date = _cell_text(page, 33, row_top, 103, row_bottom)
        if not RU_TRANSACTION_DATE_RE.fullmatch(date):
            continue
        details = _cell_text(page, 103, row_top, 376, row_bottom)
        amount = _cell_text(page, 376, row_top, 470, row_bottom)
        row = _russian_row_from_cells(date, details, amount, currency)
        if row:
            rows.append(row)
    return rows


def _cell_text(page, x0: float, top: float, x1: float, bottom: float) -> str:
    text = page.crop((x0, top, x1, bottom)).extract_text(x_tolerance=1, y_tolerance=3) or ""
    return re.sub(r"\s+", " ", text).strip()


def _russian_row_from_cells(date: str, details: str, amount: str, currency: str) -> dict | None:
    amount_match = RU_AMOUNT_RE.search(amount)
    if not RU_TRANSACTION_DATE_RE.fullmatch(date) or not amount_match:
        return None
    return {
        "date": pd.to_datetime(date, format="%d.%m.%Y").date().isoformat(),
        "signed_amount": float(amount_match.group("amount").replace(" ", "").replace(",", ".")),
        "currency": currency,
        "details": details,
        "bank_status": "posted",
        "bank_reference": "",
    }


def _is_posted_transactions_table(header: list[str | None]) -> bool:
    normalized = [re.sub(r"\s+", " ", str(value or "")).strip().lower() for value in header]
    return (
        len(normalized) >= 5
        and normalized[0] == "operation date"
        and normalized[2] == "operation description"
        and normalized[4] == "amount in kzt"
    )


def _is_pending_transactions_table(header: list[str | None]) -> bool:
    normalized = [re.sub(r"\s+", " ", str(value or "")).strip().lower() for value in header]
    return len(normalized) >= 5 and normalized[0] == "date" and normalized[2] == "description"


def _has_pending_rows(table_rows: list[list[str | None]]) -> bool:
    return any(len(row) >= 2 and str(row[1] or "").strip().lower() == "pending" for row in table_rows)


def _posted_rows_from_table(table_rows: list[list[str | None]]) -> list[dict]:
    rows = []
    for row in table_rows:
        if len(row) < 5:
            continue
        date = str(row[0] or "").strip()
        amount_match = AMOUNT_RE.search(re.sub(r"\s+", "", str(row[4] or "")))
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date) or not amount_match:
            continue
        rows.append(
            {
                "date": date,
                "signed_amount": float(amount_match.group("amount")),
                "currency": amount_match.group("currency"),
                "details": re.sub(r"\s+", " ", str(row[2] or "")).strip(),
                "bank_status": "posted",
                "bank_reference": re.sub(r"\s+", " ", str(row[3] or "")).strip(),
            }
        )
    return rows


def _pending_rows_from_table(table_rows: list[list[str | None]]) -> list[dict]:
    rows = []
    for row in table_rows:
        if len(row) < 4 or str(row[1] or "").strip().lower() != "pending":
            continue
        raw_date = str(row[0] or "").strip()
        date_match = re.match(r"(?P<date>\d{2}\.\d{2}\.\d{4})", raw_date)
        amount_match = AMOUNT_RE.search(re.sub(r"\s+", "", str(row[3] or "")))
        if not date_match or not amount_match:
            continue
        date = pd.to_datetime(date_match.group("date"), format="%d.%m.%Y").date().isoformat()
        details = re.sub(r"\s+", " ", str(row[2] or "")).strip()
        rows.append(
            {
                "date": date,
                "signed_amount": -float(amount_match.group("amount")),
                "currency": amount_match.group("currency"),
                "details": f"Pending {details}",
                "bank_status": "pending",
                "bank_reference": re.sub(r"\s+", " ", str(row[4] or "")).strip()
                if len(row) > 4
                else "",
            }
        )
    return rows
