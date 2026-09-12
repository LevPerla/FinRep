from __future__ import annotations

import base64
import binascii
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


class BankPdfError(ValueError):
    pass


class BankPdfLimitError(BankPdfError):
    pass


class BankPdfReadError(BankPdfError):
    pass


class BankPdfUnsupportedError(BankPdfError):
    pass


class BankPdfEmptyError(BankPdfError):
    pass


def parse_bank_upload_contents(contents: str) -> pd.DataFrame:
    if not contents:
        raise BankPdfEmptyError("PDF не выбран или не содержит данных.")
    try:
        _, encoded = contents.split(",", 1)
    except ValueError as exc:
        raise BankPdfReadError("Не удалось прочитать PDF. Проверь файл и попробуй снова.") from exc
    if _decoded_base64_size(encoded) > MAX_BANK_PDF_BYTES:
        raise BankPdfLimitError(
            f"PDF слишком большой: максимум {_byte_limit_label(MAX_BANK_PDF_BYTES)}."
        )
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise BankPdfReadError("Не удалось прочитать PDF. Проверь файл и попробуй снова.") from exc
    if len(content) > MAX_BANK_PDF_BYTES:
        raise BankPdfLimitError(
            f"PDF слишком большой: максимум {_byte_limit_label(MAX_BANK_PDF_BYTES)}."
        )
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            page_count = len(pdf.pages)
            if page_count > MAX_BANK_PDF_PAGES:
                raise BankPdfLimitError(
                    f"PDF содержит {page_count} стр.; максимум {MAX_BANK_PDF_PAGES}."
                )
            first_page_text = pdf.pages[0].extract_text() if pdf.pages else ""
    except BankPdfLimitError:
        raise
    except Exception as exc:
        raise BankPdfReadError("Не удалось прочитать PDF. Проверь файл и попробуй снова.") from exc

    parser = None
    if is_kaspi_deposit_statement(first_page_text):
        parser = parse_kaspi_deposit_pdf_bytes
    elif is_bcc_statement(first_page_text):
        parser = parse_bcc_pdf_bytes
    elif OZON_MARKER in first_page_text:
        parser = parse_ozon_pdf_bytes

    try:
        data = parser(content) if parser else parse_kaspi_pdf_bytes(content)
    except Exception as exc:
        raise BankPdfReadError("Не удалось разобрать банковскую выписку. Проверь формат файла.") from exc

    if not data.empty:
        return data
    if not (first_page_text or "").strip():
        raise BankPdfEmptyError("В PDF не найден текст с операциями. Проверь содержимое файла.")
    if parser:
        raise BankPdfEmptyError("Выписка распознана, но операции в ней не найдены.")
    raise BankPdfUnsupportedError(
        "Этот PDF не похож на поддерживаемую выписку Kaspi, BCC или Ozon Банка."
    )


def _decoded_base64_size(encoded: str) -> int:
    padding = len(encoded) - len(encoded.rstrip("="))
    return (len(encoded) * 3) // 4 - padding


def _byte_limit_label(limit: int) -> str:
    if limit % MIB == 0:
        return f"{limit // MIB} MiB"
    return f"{limit} байт"
