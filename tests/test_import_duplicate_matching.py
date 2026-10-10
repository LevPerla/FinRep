from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src import config
from src.data import staging
from src.data.importers import common
from src.data.sqlite_store import add_cash_transaction, connect_database, initialize_database


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
    with patch.object(common, "get_transactions", return_value=_history("Shop A")):
        preview = common.import_frame_from_rows(_rows("Shop B"), statement_id="B")

    assert preview.iloc[0]["duplicate_in_source"] == False
    assert preview.iloc[0]["import_action"] == "import"
    assert common.save_import_to_staging(preview.to_dict("records"))["accepted_rows"] == 1


def test_possible_duplicate_requires_explicit_decision(import_data):
    with patch.object(common, "get_transactions", return_value=_history("Shop A")):
        preview = common.import_frame_from_rows(_rows("Shop A"), statement_id="overlap")
        with pytest.raises(ValueError, match="выбери import или skip"):
            common.save_import_to_staging(preview.to_dict("records"))

        rows = preview.to_dict("records")
        rows[0]["import_action"] = "import"
        result = common.save_import_to_staging(rows)

    assert preview.iloc[0]["skip_reason"] == "possible_duplicate"
    assert preview.iloc[0]["import_action"] == "review"
    assert result == {"accepted_rows": 1, "skipped_rows": 0}


def test_possible_duplicate_can_be_explicitly_skipped(import_data):
    with patch.object(common, "get_transactions", return_value=_history("Shop A")):
        preview = common.import_frame_from_rows(_rows("Shop A"), statement_id="overlap")
        rows = preview.to_dict("records")
        rows[0]["import_action"] = "skip"
        result = common.save_import_to_staging(rows)

    assert result == {"accepted_rows": 0, "skipped_rows": 1}
    assert staging.read_transaction_drafts().empty


def test_two_identical_purchases_in_one_statement_remain_distinct(import_data):
    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        preview = common.import_frame_from_rows(
            _rows("Same Shop", count=2), statement_id="statement-A"
        )
        first = common.save_import_to_staging(preview.to_dict("records"))
        repeated = common.save_import_to_staging(preview.to_dict("records"))

    assert preview["source_id"].nunique() == 2
    assert first == {"accepted_rows": 2, "skipped_rows": 0}
    assert repeated == {"accepted_rows": 0, "skipped_rows": 2}
    assert len(staging.read_transaction_drafts()) == 2


def test_same_rows_from_another_statement_are_not_exact_duplicates(import_data):
    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        first = common.import_frame_from_rows(_rows("Same Shop"), statement_id="A")
        second = common.import_frame_from_rows(_rows("Same Shop"), statement_id="B")

    assert first.iloc[0]["source_id"] != second.iloc[0]["source_id"]


def test_sqlite_reimport_matches_original_statement_comment_after_edit(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    rows = [{"date": "2026-10-01", "signed_amount": 100, "currency": "RUB",
             "details": "Partner distribution 4711"}]
    preview = common.import_frame_from_rows(rows, statement_id="statement-a")
    edited = preview.to_dict("records")
    edited[0]["comment"] = "My investment income"

    saved = common.save_input_grid_to_transactions(edited)

    assert saved["published_rows"] == 1
    with connect_database(database) as connection:
        assert tuple(connection.execute(
            "SELECT comment, source_comment FROM cash_transactions"
        ).fetchone()) == ("My investment income", "Partner distribution 4711")
    repeated = common.import_frame_from_rows(rows, statement_id="statement-b")
    assert repeated.iloc[0]["duplicate_in_source"] == True
    assert repeated.iloc[0]["import_action"] == "review"
    approved = repeated.to_dict("records")
    approved[0]["import_action"] = "import"
    assert common.save_input_grid_to_transactions(approved)["published_rows"] == 1


def test_sqlite_legacy_income_duplicate_uses_saved_comment(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    add_cash_transaction(
        database, transaction_id="legacy-income", occurred_on="2026-10-01",
        flow_direction="income", category_id="income.salary", amount="100",
        currency="RUB", comment="Salary October",
    )

    repeated = common.import_frame_from_rows([{
        "date": "2026-10-01", "signed_amount": 100, "currency": "RUB",
        "details": "Salary October",
    }], statement_id="another-statement")

    assert repeated.iloc[0]["duplicate_in_source"] == True


def test_statement_comment_cannot_be_rewritten_with_draft_comment(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    preview = common.import_frame_from_rows([{
        "date": "2026-10-01", "signed_amount": -100, "currency": "RUB",
        "details": "Merchant reference 4711",
    }], statement_id="statement-a")
    assert common.save_import_to_staging(preview.to_dict("records"))["accepted_rows"] == 1

    staging.update_transaction_draft(
        preview.iloc[0]["source"], preview.iloc[0]["source_id"],
        {"comment": "My note", "source_comment": "forged"},
    )

    with connect_database(database) as connection:
        assert tuple(connection.execute(
            "SELECT comment, source_comment FROM transaction_drafts"
        ).fetchone()) == ("My note", "Merchant reference 4711")


def test_csv_exported_draft_keeps_statement_comment_for_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "csv")
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    staging.write_transaction_drafts(pd.DataFrame([{
        "date": "2026-10-01", "category": "Прочее", "currency": "RUB",
        "amount": "100", "comment": "My note",
        "source_comment": "Merchant reference 4711", "source": "kaspi_pdf",
        "source_id": "prior-statement", "direction": "debit", "status": "exported",
    }]))
    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        repeated = common.import_frame_from_rows([{
            "date": "2026-10-01", "signed_amount": -100, "currency": "RUB",
            "details": "Merchant reference 4711",
        }], statement_id="another-statement")

    assert repeated.iloc[0]["duplicate_in_source"] == True


def test_sqlite_new_duplicate_after_preview_is_skipped(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    rows = [{"date": "2026-10-01", "signed_amount": -100, "currency": "RUB",
             "details": "Merchant reference 4711"}]
    first = common.import_frame_from_rows(rows, statement_id="statement-a")
    second = common.import_frame_from_rows(rows, statement_id="statement-b")
    assert common.save_input_grid_to_transactions(first.to_dict("records"))["published_rows"] == 1

    result = common.save_input_grid_to_transactions(second.to_dict("records"))
    assert result["published_rows"] == 0
    assert result["already_published_rows"] == 1
