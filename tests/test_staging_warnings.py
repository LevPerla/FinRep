import warnings

import pandas as pd

from src.data import staging


def test_first_draft_append_does_not_concat_an_empty_frame():
    empty = pd.DataFrame(columns=staging.DRAFT_COLUMNS)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = staging.transaction_drafts_with_appended_row(
            empty,
            date="2026-09-12",
            category="Прочее",
            currency="rub",
            amount=10.25,
            comment="synthetic",
            source="manual",
            source_id="manual:sim-07d4",
        )

    concat_warnings = [
        warning
        for warning in caught
        if "dataframe concatenation with empty or all-na entries"
        in str(warning.message).lower()
    ]
    assert concat_warnings == []
    assert result.columns.tolist() == staging.DRAFT_COLUMNS
    assert result.to_dict("records") == [
        {
            "date": "2026-09-12",
            "category": "Прочее",
            "currency": "RUB",
            "amount": "10.25",
            "comment": "synthetic",
            "source": "manual",
            "source_id": "manual:sim-07d4",
            "direction": "",
            "bank_status": "",
            "bank_reference": "",
            "bank_account_id": "",
            "status": "draft",
        }
    ]
