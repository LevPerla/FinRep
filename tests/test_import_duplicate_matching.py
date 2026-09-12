from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src import config
from src.data import staging
from src.data.importers import kaspi_pdf


@pytest.fixture
def import_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path


def _history(comment: str = "Shop A") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Дата": pd.Timestamp("2026-09-01"),
                "Категория": "Прочее",
                "Валюта": "RUB",
                "Значение": 100.0,
                "Комментарий": comment,
            }
        ]
    )


def _rows(details: str = "Shop B", count: int = 1) -> list[dict]:
    return [
        {
            "date": "2026-09-01",
            "signed_amount": -100.0,
            "currency": "RUB",
            "details": details,
        }
        for _ in range(count)
    ]


def test_same_amount_at_different_merchant_is_imported(import_data):
    with patch.object(kaspi_pdf, "get_transactions", return_value=_history("Shop A")):
        preview = kaspi_pdf._import_frame_from_rows(_rows("Shop B"), statement_id="B")

    assert preview.iloc[0]["duplicate_in_source"] == False
    assert preview.iloc[0]["import_action"] == "import"
    assert kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))["accepted_rows"] == 1


def test_possible_duplicate_requires_explicit_decision(import_data):
    with patch.object(kaspi_pdf, "get_transactions", return_value=_history("Shop A")):
        preview = kaspi_pdf._import_frame_from_rows(_rows("Shop A"), statement_id="overlap")
        with pytest.raises(ValueError, match="выбери import или skip"):
            kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))

        rows = preview.to_dict("records")
        rows[0]["import_action"] = "import"
        result = kaspi_pdf.save_kaspi_import_to_staging(rows)

    assert preview.iloc[0]["skip_reason"] == "possible_duplicate"
    assert preview.iloc[0]["import_action"] == "review"
    assert result == {"accepted_rows": 1, "skipped_rows": 0}


def test_possible_duplicate_can_be_explicitly_skipped(import_data):
    with patch.object(kaspi_pdf, "get_transactions", return_value=_history("Shop A")):
        preview = kaspi_pdf._import_frame_from_rows(_rows("Shop A"), statement_id="overlap")
        rows = preview.to_dict("records")
        rows[0]["import_action"] = "skip"
        result = kaspi_pdf.save_kaspi_import_to_staging(rows)

    assert result == {"accepted_rows": 0, "skipped_rows": 1}
    assert staging.read_transaction_drafts().empty


def test_two_identical_purchases_in_one_statement_remain_distinct(import_data):
    with patch.object(kaspi_pdf, "get_transactions", return_value=pd.DataFrame()):
        preview = kaspi_pdf._import_frame_from_rows(
            _rows("Same Shop", count=2), statement_id="statement-A"
        )
        first = kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))
        repeated = kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))

    assert preview["source_id"].nunique() == 2
    assert first == {"accepted_rows": 2, "skipped_rows": 0}
    assert repeated == {"accepted_rows": 0, "skipped_rows": 2}
    assert len(staging.read_transaction_drafts()) == 2


def test_same_rows_from_another_statement_are_not_exact_duplicates(import_data):
    with patch.object(kaspi_pdf, "get_transactions", return_value=pd.DataFrame()):
        first = kaspi_pdf._import_frame_from_rows(_rows("Same Shop"), statement_id="A")
        second = kaspi_pdf._import_frame_from_rows(_rows("Same Shop"), statement_id="B")

    assert first.iloc[0]["source_id"] != second.iloc[0]["source_id"]
