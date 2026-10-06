from unittest.mock import patch

import pandas as pd

from src import config
from src.data.importers import common, kaspi_pdf
from src.data.importers.kaspi_pdf import _statement_balance_from_text


def test_parse_kaspi_bytes_returns_common_import_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        kaspi_pdf,
        "_extract_rows_from_pdf",
        lambda _source: [
            {
                "date": "2026-09-12",
                "signed_amount": -1250.5,
                "currency": "KZT",
                "details": "Purchases Synthetic shop",
            }
        ],
    )

    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        result = kaspi_pdf.parse_kaspi_pdf_bytes(b"synthetic-kaspi-pdf")

    row = result.iloc[0]
    assert row["source"] == "kaspi_pdf"
    assert row["date"] == "2026-09-12"
    assert row["direction"] == "debit"
    assert row["amount"] == 1250.5
    assert row["comment"] == "Synthetic shop"
    assert row["status"] == "draft"
    assert row["import_action"] == "import"
    assert len(row["source_id"]) == 64


def test_kaspi_gold_statement_balance_metadata():
    text = (
        "Kaspi Gold\nbalance statement for the period from 27.09.26 to 04.10.26\n"
        "Card number: *0000\nAccount number:KZ00722C000000000000\nCurrency: KZT\n"
        "Card balance 04.10.26: + 211 923,85 ₸"
    )

    assert _statement_balance_from_text(text) == {
        "statement_account_id": "KZ00722C000000000000",
        "statement_balance": "211923.85",
        "statement_balance_date": "2026-10-04",
        "currency": "KZT",
    }
