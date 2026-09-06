from __future__ import annotations

import base64
import io

import pandas as pd
import pdfplumber

from src.data.importers.bcc_pdf import is_bcc_statement, parse_bcc_pdf_bytes
from src.data.importers.kaspi_deposit_pdf import (
    is_kaspi_deposit_statement,
    parse_kaspi_deposit_pdf_bytes,
)
from src.data.importers.kaspi_pdf import parse_kaspi_pdf_bytes
from src.data.importers.ozon_pdf import OZON_MARKER, parse_ozon_pdf_bytes

MIB = 1024 * 1024
MAX_BANK_PDF_BYTES = 10 * MIB
MAX_BANK_PDF_PAGES = 50
BANK_PDF_UPLOAD_LIMIT_LABEL = f"{MAX_BANK_PDF_BYTES // MIB} MiB и {MAX_BANK_PDF_PAGES} страниц"
# A 10 MiB file becomes about 13.4 MiB after base64 encoding. Keep a coarse
# request-level backstop above that so the callback can return the exact error.
MAX_BANK_PDF_REQUEST_BYTES = 32 * MIB


class BankPdfLimitError(ValueError):
    pass


def parse_bank_upload_contents(contents: str) -> pd.DataFrame:
    if not contents:
        return pd.DataFrame()
    _, encoded = contents.split(",", 1)
    if _decoded_base64_size(encoded) > MAX_BANK_PDF_BYTES:
        raise BankPdfLimitError(
            f"PDF слишком большой: максимум {_byte_limit_label(MAX_BANK_PDF_BYTES)}."
        )
    content = base64.b64decode(encoded, validate=True)
    if len(content) > MAX_BANK_PDF_BYTES:
        raise BankPdfLimitError(
            f"PDF слишком большой: максимум {_byte_limit_label(MAX_BANK_PDF_BYTES)}."
        )
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        page_count = len(pdf.pages)
        if page_count > MAX_BANK_PDF_PAGES:
            raise BankPdfLimitError(
                f"PDF содержит {page_count} стр.; максимум {MAX_BANK_PDF_PAGES}."
            )
        first_page_text = pdf.pages[0].extract_text() if pdf.pages else ""
    if is_kaspi_deposit_statement(first_page_text):
        return parse_kaspi_deposit_pdf_bytes(content)
    if is_bcc_statement(first_page_text):
        return parse_bcc_pdf_bytes(content)
    if OZON_MARKER in (first_page_text or ""):
        return parse_ozon_pdf_bytes(content)
    return parse_kaspi_pdf_bytes(content)


def _decoded_base64_size(encoded: str) -> int:
    padding = len(encoded) - len(encoded.rstrip("="))
    return (len(encoded) * 3) // 4 - padding


def _byte_limit_label(limit: int) -> str:
    if limit % MIB == 0:
        return f"{limit // MIB} MiB"
    return f"{limit} байт"
