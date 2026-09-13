from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pandas as pd
import pdfplumber

from src.data.importers.common import import_frame_from_rows


TRANSACTION_RE = re.compile(
    r"^(?P<date>\d{2}\.\d{2}\.\d{2})\s+"
    r"(?P<sign>[+-])\s+"
    r"(?P<amount>[\d\s]+,\d{2})\s+"
    r"(?P<currency>[₸$€£]|[A-Z]{3})\s+"
    r"(?P<details>.+)$"
)
FOREIGN_AMOUNT_RE = re.compile(
    r"^\((?P<sign>[+-])\s+(?P<amount>[\d\s]+,\d{2})\s+(?P<currency>[A-Z]{3})\)$"
)
CURRENCY_SYMBOLS = {"₸": "KZT", "$": "USD", "€": "EUR", "£": "GBP"}


def parse_kaspi_pdf(path: str | Path) -> pd.DataFrame:
    return parse_kaspi_pdf_bytes(Path(path).read_bytes())


def parse_kaspi_pdf_bytes(content: bytes) -> pd.DataFrame:
    return import_frame_from_rows(
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
