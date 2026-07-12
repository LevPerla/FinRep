from __future__ import annotations

import base64
import io

import pandas as pd
import pdfplumber

from src.data.importers.bcc_pdf import BCC_MARKER, parse_bcc_pdf_bytes
from src.data.importers.kaspi_pdf import parse_kaspi_pdf_bytes


def parse_bank_upload_contents(contents: str) -> pd.DataFrame:
    if not contents:
        return pd.DataFrame()
    _, encoded = contents.split(",", 1)
    content = base64.b64decode(encoded)
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        first_page_text = pdf.pages[0].extract_text() if pdf.pages else ""
    if BCC_MARKER in (first_page_text or ""):
        return parse_bcc_pdf_bytes(content)
    return parse_kaspi_pdf_bytes(content)
