from src.data.importers.kaspi_deposit_pdf import (
    _rows_from_table,
    is_kaspi_deposit_statement,
)
from src.data.importers.kaspi_pdf import _is_internal_transfer


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
    assert _is_internal_transfer(kzt_rows[0]["details"])
    assert not _is_internal_transfer(kzt_rows[1]["details"])
