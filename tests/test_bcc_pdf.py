from pathlib import Path

from src import config
from src.data.importers.bcc_pdf import (
    BCC_SOURCE,
    _russian_row_from_cells,
    is_bcc_statement,
    parse_bcc_pdf,
)

FIXTURE = Path(__file__).parent / "fixtures" / "bank_statements" / "bcc_synthetic.pdf"


def test_parse_bcc_statement_includes_pending_transactions(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    data = parse_bcc_pdf(FIXTURE)

    assert len(data) == 2
    assert set(data["source"]) == {BCC_SOURCE}
    assert set(data["currency"]) == {"KZT"}

    pending = data.loc[
        data["date"].eq("2026-07-19")
        & data["details"].eq('Pending TOO "KS KOMPANI"')
        & data["amount"].eq(47990.0)
    ].iloc[0]
    assert pending["category"] != "Доход"
    assert pending["comment"] == 'TOO "KS KOMPANI"'
    assert pending["bank_status"] == "pending"
    assert pending["bank_reference"] == "SYN-BCC-002"

    cafe = data.loc[data["details"].eq("Purchase ARYSTAN CAFE")].iloc[0]
    assert cafe["date"] == "2026-07-05"
    assert cafe["amount"] == 1600.0
    assert cafe["category"] != "Доход"
    assert cafe["bank_status"] == "posted"
    assert cafe["bank_reference"] == "SYN-BCC-001"


def test_parse_russian_bcc_account_statement_row():
    first_page_text = (
        "Выписка\nпо счету KZ008560000000000000\nВалюта EUR\n"
        "Дата Описание операции Сумма в EUR Комиссия, EUR"
    )
    assert is_bcc_statement(first_page_text)

    row = _russian_row_from_cells(
        "29.07.2026",
        "Retail. 26.07.2026 00:00:00, JPN, Tokyo, Trip.com, Карта: 446375******8407",
        "-1 135,43",
        "EUR",
    )

    assert row == {
        "date": "2026-07-29",
        "signed_amount": -1135.43,
        "currency": "EUR",
        "details": "Retail. 26.07.2026 00:00:00, JPN, Tokyo, Trip.com, Карта: 446375******8407",
        "bank_status": "posted",
        "bank_reference": "",
    }
