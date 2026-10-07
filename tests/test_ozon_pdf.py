from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from src import config
from src.data.importers import common, ozon_pdf
from src.data.importers.ozon_pdf import OZON_SOURCE, parse_ozon_pdf

FIXTURE = Path(__file__).parent / "fixtures" / "bank_statements" / "ozon_synthetic.pdf"


def test_parse_ozon_statement(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    data = parse_ozon_pdf(FIXTURE)

    assert len(data) == 4
    assert set(data["source"]) == {OZON_SOURCE}
    assert set(data["currency"]) == {"RUB"}
    assert data["amount"].sum() == 15359.0
    assert not data["category"].eq("Доход").any()

    ozon_order = data.loc[data["details"].str.contains("19585537-0126", regex=False)].iloc[0]
    assert ozon_order["date"] == "2026-06-24"
    assert ozon_order["amount"] == 384.0


def test_parse_ozon_statement_exposes_outgoing_balance(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    first_page = Mock()
    first_page.extract_text.return_value = (
        "OZON Bank LLC\nAccount number: № 40817810000000000001, has been opened 01.01.2026\n"
        "Statement period: 01.09.2026 – 30.09.2026\nIncoming balance: RUR 0.00"
    )
    first_page.extract_tables.return_value = [[
        ["Date of transaction", "Document", "Purpose of payment", "Transaction amount", None],
        [None, None, None, "Russian ruble", "Currency"],
        ["30.09.2026 12:00:00", "1", "Transfer", "+ RUR 200.00", "+ RUR 200.00"],
    ]]
    last_page = Mock()
    last_page.extract_text.return_value = "Outgoing balance: RUR 1 234.56"
    last_page.extract_tables.return_value = [[
        ["29.09.2026 12:00:00", "2", "Continuation transfer", "- RUR 50.00", "- RUR 50.00"],
    ]]
    opened = Mock()
    opened.__enter__ = Mock(return_value=Mock(pages=[first_page, last_page]))
    opened.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(ozon_pdf.pdfplumber, "open", Mock(return_value=opened))

    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        result = ozon_pdf.parse_ozon_pdf_bytes(b"synthetic-ozon-pdf")

    assert len(result) == 2
    assert "Continuation transfer" in set(result["details"])
    assert result.attrs["statement_balance"] == {
        "account_id": "40817810000000000001",
        "balance": "1234.56",
        "currency": "RUB",
        "as_of_date": "2026-09-30",
    }
