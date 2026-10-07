from unittest.mock import Mock, patch

import pandas as pd

from src import config
from src.data.importers import common, kaspi_deposit_pdf
from src.data.importers.kaspi_deposit_pdf import (
    _closing_balance_from_table,
    _extract_rows_from_pdf,
    _rows_from_table,
    is_kaspi_deposit_statement,
)
from src.data.importers.common import is_internal_transfer


def test_parse_kaspi_deposit_bytes_returns_common_import_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(
        kaspi_deposit_pdf,
        "_extract_rows_from_pdf",
        lambda _source: [
            {
                "date": "2026-09-12",
                "signed_amount": 28.42,
                "currency": "USD",
                "details": "Interest after tax 5,02 USD",
                "statement_account_id": "KZ00722R000000000000",
                "statement_balance": "40150.55",
                "statement_balance_date": "2026-08-29",
            }
        ],
    )

    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        result = kaspi_deposit_pdf.parse_kaspi_deposit_pdf_bytes(
            b"synthetic-kaspi-deposit-pdf"
        )

    row = result.iloc[0]
    assert row["source"] == "kaspi_deposit_pdf"
    assert row["date"] == "2026-09-12"
    assert row["direction"] == "credit"
    assert row["amount"] == 28.42
    assert row["comment"] == "Interest after tax 5,02 USD"
    assert row["status"] == "draft"
    assert row["import_action"] == "import"
    assert len(row["source_id"]) == 64
    assert result.attrs["statement_balance"] == {
        "account_id": "KZ00722R000000000000",
        "balance": "40150.55",
        "currency": "USD",
        "as_of_date": "2026-08-29",
    }


def test_parse_kaspi_deposit_rows_for_kzt_and_usd():
    first_page_text = (
        "DEPOSIT\nstatement balance for the period from 29.07.26 to 29.08.26\n"
        "Agreement number: D00000000-000\nAccount number: KZ00722R000000000000"
    )
    assert is_kaspi_deposit_statement(first_page_text)

    kzt_rows = _rows_from_table(
        [
            [
                "30.07.26",
                "+400 000,00 ₸",
                "Deposit received",
                "From Kaspi Gold via kaspi.kz",
                "15 054 034,17 ₸",
            ],
            [
                "01.08.26",
                "+143 337,61 ₸",
                "Interest",
                "Kaspi Deposit Interest after tax 25\n294,87 KZT",
                "15 197 371,78 ₸",
            ],
            [
                "25.08.26",
                "-350 000,00 ₸",
                "Withdrawals",
                "Via Kaspi ATM",
                "17 047 371,78 ₸",
            ],
        ],
        "KZT",
    )
    usd_rows = _rows_from_table(
        [
            [
                "01.08.26",
                "+ $28,42",
                "Interest",
                "Kaspi Deposit Interest after tax\n5,02 USD",
                "$40 150,55",
            ]
        ],
        "USD",
    )

    assert kzt_rows == [
        {
            "date": "2026-07-30",
            "signed_amount": 400000.0,
            "currency": "KZT",
            "details": "Transfer to your deposit from Kaspi Gold via kaspi.kz",
        },
        {
            "date": "2026-08-01",
            "signed_amount": 143337.61,
            "currency": "KZT",
            "details": "Interest after tax 25 294,87 KZT",
        },
        {
            "date": "2026-08-25",
            "signed_amount": -350000.0,
            "currency": "KZT",
            "details": "Withdrawals Via Kaspi ATM",
        },
    ]
    assert usd_rows == [
        {
            "date": "2026-08-01",
            "signed_amount": 28.42,
            "currency": "USD",
            "details": "Interest after tax 5,02 USD",
        }
    ]
    assert is_internal_transfer(kzt_rows[0]["details"])
    assert not is_internal_transfer(kzt_rows[1]["details"])
    assert _closing_balance_from_table(
        [["01.08.26", "+ $28,42", "Interest", "Details", "$40 150,55"]]
    ) == "40150.55"


def test_kaspi_deposit_extracts_closing_balance_metadata(monkeypatch):
    page = Mock()
    page.extract_text.return_value = (
        "DEPOSIT\nstatement balance for the period from 29.07.26 to 29.08.26\n"
        "Agreement number: D00000000-000\n"
        "Account number: KZ00722R000000000000\nCurrency: USD"
    )
    page.extract_tables.return_value = [[
        ["Date", "Amount", "Transaction", "Details", "Deposit balance"],
        ["01.08.26", "+ $28,42", "Interest", "Details", "$40 150,55"],
    ]]
    opened = Mock()
    opened.__enter__ = Mock(return_value=Mock(pages=[page]))
    opened.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(kaspi_deposit_pdf.pdfplumber, "open", Mock(return_value=opened))

    rows = _extract_rows_from_pdf(object())

    assert rows[0]["statement_account_id"] == "KZ00722R000000000000"
    assert rows[0]["statement_balance"] == "40150.55"
    assert rows[0]["statement_balance_date"] == "2026-08-29"
