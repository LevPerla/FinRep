import pandas as pd

from src.data.get import _parse_transaction_value, _split_transaction_values
from src.data.staging import _draft_to_month_cell, sanitize_transaction_comment
from src.data.validation import _parse_money_cell, _split_transaction_cell


def test_transaction_reader_accepts_pipe_inside_existing_comment():
    value = _parse_transaction_value(
        "80.93|EUR|Прочие зачисления. Валюты:KZT/USD| IPS: | BCC: 1"
    )

    assert value == {
        "Значение": 80.93,
        "Валюта": "EUR",
        "Комментарий": "Прочие зачисления. Валюты:KZT/USD IPS: BCC: 1",
    }


def test_transaction_reader_distinguishes_hash_in_comment_from_next_transaction():
    assert _split_transaction_values("80.93|EUR|comment # tag") == [
        "80.93|EUR|comment # tag"
    ]
    assert _split_transaction_values("80.93|EUR|comment#1200|KZT|next") == [
        "80.93|EUR|comment",
        "1200|KZT|next",
    ]
    assert _split_transaction_cell("80.93|EUR|comment # tag") == [
        "80.93|EUR|comment # tag"
    ]


def test_transaction_comments_remove_internal_and_csv_separators():
    comment = "TRIP.COM| IPS: # BCC: 1; note\r\nnext"

    assert sanitize_transaction_comment(comment) == "TRIP.COM IPS: BCC: 1 note next"
    assert _draft_to_month_cell(pd.Series({
        "amount": 80.93,
        "currency": "eur",
        "comment": comment,
    })) == "80,93|EUR|TRIP.COM IPS: BCC: 1 note next"


def test_validation_accepts_pipe_inside_existing_transaction_comment():
    assert _parse_money_cell("80.93|EUR|comment|with|pipes", expected_parts=3) == (
        80.93,
        "EUR",
        "comment|with|pipes",
    )
