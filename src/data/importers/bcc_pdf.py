from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from src.data.importers.kaspi_pdf import _import_frame_from_rows

BCC_SOURCE = "bcc_pdf"
BCC_MARKER = "Bank CenterCredit JSC"
AMOUNT_RE = re.compile(r"(?P<amount>-?\d+\.\d{2})(?P<currency>[A-Z]{3})")


def parse_bcc_pdf(path: str | Path) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(Path(path)), BCC_SOURCE)


def parse_bcc_pdf_bytes(content: bytes) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(io.BytesIO(content)), BCC_SOURCE)


def _extract_rows_from_pdf(pdf_source) -> list[dict]:
    rows: list[dict] = []
    with pdfplumber.open(pdf_source) as pdf:
        first_page_text = pdf.pages[0].extract_text() if pdf.pages else ""
        if BCC_MARKER not in (first_page_text or ""):
            raise ValueError("PDF не похож на выписку Bank CenterCredit.")
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table:
                    continue
                if _is_posted_transactions_table(table[0]):
                    rows.extend(_posted_rows_from_table(table[1:]))
                elif _is_pending_transactions_table(table[0]):
                    rows.extend(_pending_rows_from_table(table[1:]))
    return rows


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
            }
        )
    return rows
