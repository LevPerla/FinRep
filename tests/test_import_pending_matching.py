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


def _bank_row(
    status: str,
    *,
    date: str,
    reference: str,
    details: str = "Pending Shop A",
    amount: float = -100.0,
) -> dict:
    if status == "posted":
        details = details.removeprefix("Pending ")
    return {
        "date": date,
        "signed_amount": amount,
        "currency": "RUB",
        "details": details,
        "bank_status": status,
        "bank_reference": reference,
    }


def _preview(rows: list[dict], statement_id: str) -> pd.DataFrame:
    with patch.object(kaspi_pdf, "get_transactions", return_value=pd.DataFrame()):
        return kaspi_pdf._import_frame_from_rows(
            rows, source="bcc_pdf", statement_id=statement_id
        )


def _save(rows: list[dict], statement_id: str) -> dict:
    return kaspi_pdf.save_kaspi_import_to_staging(
        _preview(rows, statement_id).to_dict("records")
    )


def test_pending_is_visible_but_not_exportable(import_data):
    result = _save(
        [_bank_row("pending", date="2026-09-01", reference="P-1")], "pending"
    )

    drafts = staging.read_transaction_drafts()
    exportable = staging._exportable_month_drafts("2026", "09", data=drafts)

    assert result == {"accepted_rows": 1, "skipped_rows": 0}
    assert drafts.iloc[0]["bank_status"] == "pending"
    assert exportable.empty


def test_matching_reference_replaces_pending_even_after_seven_days(import_data):
    _save([_bank_row("pending", date="2026-09-01", reference="REF-1")], "pending")
    preview = _preview(
        [_bank_row("posted", date="2026-09-20", reference="REF-1")], "posted"
    )
    result = kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))

    drafts = staging.read_transaction_drafts()
    assert preview.iloc[0]["replaces_source_id"]
    assert result == {
        "accepted_rows": 1,
        "skipped_rows": 0,
        "replaced_pending_rows": 1,
    }
    assert len(drafts) == 1
    assert drafts.iloc[0]["bank_status"] == "posted"
    assert drafts.iloc[0]["date"] == "2026-09-20"
    month = staging.preview_monthly_transaction_export("2026", "09")
    saved_day = month.loc[month["Дата"].eq("20.09.2026"), "Прочее"].iloc[0]
    assert saved_day == "100|RUB|Shop A"


def test_unique_semantic_match_within_seven_days_replaces_pending(import_data):
    _save([_bank_row("pending", date="2026-09-01", reference="P-1")], "pending")
    preview = _preview(
        [_bank_row("posted", date="2026-09-08", reference="POST-1")], "posted"
    )

    result = kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))

    assert preview.iloc[0]["skip_reason"] == "replaces_pending"
    assert result["replaced_pending_rows"] == 1
    assert len(staging.read_transaction_drafts()) == 1


def test_semantic_match_outside_seven_days_does_not_replace(import_data):
    _save([_bank_row("pending", date="2026-09-01", reference="P-1")], "pending")
    preview = _preview(
        [_bank_row("posted", date="2026-09-09", reference="POST-1")], "posted"
    )

    result = kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))

    assert preview.iloc[0]["replaces_source_id"] == ""
    assert result == {"accepted_rows": 1, "skipped_rows": 0}
    assert len(staging.read_transaction_drafts()) == 2


def test_semantic_match_does_not_cross_bcc_accounts(import_data):
    pending = _bank_row("pending", date="2026-09-01", reference="P-1")
    pending["bank_account_id"] = "KZ008560000000000001"
    _save([pending], "pending")
    posted = _bank_row("posted", date="2026-09-02", reference="P-1")
    posted["bank_account_id"] = "KZ008560000000000002"
    preview = _preview([posted], "posted")

    assert preview.iloc[0]["replaces_source_id"] == ""


def test_ambiguous_pending_matches_require_review_and_preserve_posted_rows(import_data):
    _save([_bank_row("pending", date="2026-09-01", reference="P-1")], "pending-1")
    _save([_bank_row("pending", date="2026-09-02", reference="P-2")], "pending-2")
    posted_rows = [
        _bank_row("posted", date="2026-09-03", reference="POST-1"),
        _bank_row("posted", date="2026-09-03", reference="POST-2"),
    ]
    preview = _preview(posted_rows, "posted")

    assert preview["import_action"].tolist() == ["review", "review"]
    with pytest.raises(ValueError, match="выбери import или skip"):
        kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))

    decided = preview.to_dict("records")
    for row in decided:
        row["import_action"] = "import"
    result = kaspi_pdf.save_kaspi_import_to_staging(decided)

    drafts = staging.read_transaction_drafts()
    assert result == {"accepted_rows": 2, "skipped_rows": 0}
    assert len(drafts) == 4
    assert len(staging._exportable_month_drafts("2026", "09", data=drafts)) == 2


def test_pending_replacement_rejects_stale_staging(import_data):
    _save([_bank_row("pending", date="2026-09-01", reference="REF-1")], "pending")
    preview = _preview(
        [_bank_row("posted", date="2026-09-02", reference="REF-1")], "posted"
    )
    staging.append_transaction_draft(
        "2026-09-03", "Прочее", "RUB", 50, source_id="concurrent"
    )

    with pytest.raises(staging.DraftRevisionConflict, match="изменились"):
        kaspi_pdf.save_kaspi_import_to_staging(preview.to_dict("records"))

    drafts = staging.read_transaction_drafts()
    assert set(drafts["bank_status"]) == {"pending", ""}
    assert len(drafts) == 2
