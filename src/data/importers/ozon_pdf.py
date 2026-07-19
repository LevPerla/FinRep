from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from src.data.importers.kaspi_pdf import _import_frame_from_rows

OZON_SOURCE = "ozon_pdf"
OZON_MARKER = "OZON Bank LLC"
AMOUNT_RE = re.compile(r"(?P<sign>[+-])(?P<currency>[A-Z]{3})(?P<amount>\d+\.\d{2})")


def parse_ozon_pdf(path: str | Path) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(Path(path)), OZON_SOURCE)


def parse_ozon_pdf_bytes(content: bytes) -> pd.DataFrame:
    return _import_frame_from_rows(_extract_rows_from_pdf(io.BytesIO(content)), OZON_SOURCE)


def _extract_rows_from_pdf(pdf_source) -> list[dict]:
    rows: list[dict] = []
    with pdfplumber.open(pdf_source) as pdf:
        first_page_text = pdf.pages[0].extract_text() if pdf.pages else ""
        if OZON_MARKER not in (first_page_text or ""):
            raise ValueError("PDF не похож на выписку Ozon Банка.")
        for page in pdf.pages:
            for table in page.extract_tables():
                if table and _is_transactions_table(table[0]):
                    rows.extend(_rows_from_table(table[2:]))
    return rows


def _is_transactions_table(header: list[str | None]) -> bool:
    normalized = [re.sub(r"\s+", " ", str(value or "")).strip().lower() for value in header]
    return (
        len(normalized) >= 4
        and normalized[0] == "date of transaction"
        and normalized[2] == "purpose of payment"
        and normalized[3] == "transaction amount"
    )


def _rows_from_table(table_rows: list[list[str | None]]) -> list[dict]:
    rows = []
    for row in table_rows:
        if len(row) < 4:
            continue
        date_match = re.match(r"(?P<date>\d{2}\.\d{2}\.\d{4})", str(row[0] or "").strip())
        amount_match = AMOUNT_RE.search(re.sub(r"\s+", "", str(row[3] or "")))
        if not date_match or not amount_match:
            continue
        amount = float(amount_match.group("amount"))
        rows.append(
            {
                "date": pd.to_datetime(date_match.group("date"), format="%d.%m.%Y").date().isoformat(),
                "signed_amount": amount if amount_match.group("sign") == "+" else -amount,
                "currency": "RUB" if amount_match.group("currency") == "RUR" else amount_match.group("currency"),
                "details": re.sub(r"\s+", " ", str(row[2] or "")).strip(),
            }
        )
    return rows
