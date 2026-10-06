from __future__ import annotations

import hashlib
import io
import re
from decimal import Decimal
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
KASPI_GOLD_ACCOUNT_RE = re.compile(r"Account number:\s*(?P<account>KZ\d{2}722C\d{12})")
KASPI_GOLD_PERIOD_RE = re.compile(
    r"balance statement for the period from \d{2}\.\d{2}\.\d{2} to "
    r"(?P<period_end>\d{2}\.\d{2}\.\d{2})",
    re.IGNORECASE,
)
KASPI_GOLD_BALANCE_RE = re.compile(
    r"Card balance\s+(?P<date>\d{2}\.\d{2}\.\d{2}):\s*"
    r"(?P<sign>[+-])\s*(?P<amount>[\d ]+,\d{2})\s*"
    r"(?P<currency>[₸$€£]|[A-Z]{3})"
)


def parse_kaspi_pdf(path: str | Path) -> pd.DataFrame:
    return parse_kaspi_pdf_bytes(Path(path).read_bytes())


def parse_kaspi_pdf_bytes(content: bytes) -> pd.DataFrame:
    rows = _extract_rows_from_pdf(io.BytesIO(content))
    result = import_frame_from_rows(
        rows,
        statement_id=hashlib.sha256(content).hexdigest(),
    )
    if rows and rows[0].get("statement_balance"):
        result.attrs["statement_balance"] = {
            "account_id": rows[0]["statement_account_id"],
            "balance": rows[0]["statement_balance"],
            "currency": rows[0]["currency"],
            "as_of_date": rows[0]["statement_balance_date"],
        }
    return result


def _extract_rows_from_pdf(pdf_source) -> list[dict]:
    rows: list[dict] = []
    pending: dict | None = None
    page_texts: list[str] = []
    with pdfplumber.open(pdf_source) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1, y_tolerance=3) or ""
            page_texts.append(text)
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
    balance = _statement_balance_from_text("\n".join(page_texts))
    if rows and balance:
        for row in rows:
            row.update(balance)
    return rows


def _statement_balance_from_text(text: str) -> dict[str, str] | None:
    account_match = KASPI_GOLD_ACCOUNT_RE.search(text)
    period_match = KASPI_GOLD_PERIOD_RE.search(text)
    if not account_match or not period_match:
        return None
    period_end = period_match.group("period_end")
    balance_match = next(
        (match for match in KASPI_GOLD_BALANCE_RE.finditer(text) if match.group("date") == period_end),
        None,
    )
    if not balance_match:
        return None
    amount = Decimal(balance_match.group("amount").replace(" ", "").replace(",", "."))
    if balance_match.group("sign") == "-":
        amount = -amount
    currency = balance_match.group("currency")
    return {
        "statement_account_id": account_match.group("account"),
        "statement_balance": str(amount),
        "statement_balance_date": pd.to_datetime(period_end, format="%d.%m.%y").date().isoformat(),
        "currency": CURRENCY_SYMBOLS.get(currency, currency).upper(),
    }


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
