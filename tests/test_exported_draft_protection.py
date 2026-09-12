from __future__ import annotations

import pandas as pd
import pytest

from src import config
from src.data import staging


def _component(node, component_id: str):
    if getattr(node, "id", None) == component_id:
        return node
    if isinstance(node, (list, tuple)):
        for child in node:
            found = _component(child, component_id)
            if found is not None:
                return found
    elif hasattr(node, "children"):
        return _component(node.children, component_id)
    return None


@pytest.fixture
def exported_case(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    staging.append_transaction_draft(
        "2026-09-01",
        "Прочее",
        "RUB",
        100,
        "Shop A",
        source="kaspi_pdf",
        source_id="statement-row-A",
    )
    staging.export_monthly_transaction_drafts("2026", "09")
    month_path = staging.monthly_transaction_csv_path("2026", "09")
    return tmp_path, month_path


def test_exported_row_cannot_be_edited_through_grid_merge(exported_case):
    _, month_path = exported_case
    before_month = month_path.read_bytes()
    rows, revision = staging.read_transaction_drafts_snapshot()
    edited = rows.to_dict("records")
    edited[0]["amount"] = "150"

    with pytest.raises(ValueError, match="нельзя изменить через staging"):
        staging.merge_transaction_draft_rows(edited, expected_revision=revision)

    assert staging.read_transaction_drafts().iloc[0]["amount"] == "100"
    assert month_path.read_bytes() == before_month


def test_exported_row_cannot_be_edited_through_single_row_api(exported_case):
    _, month_path = exported_case
    before_month = month_path.read_bytes()

    with pytest.raises(ValueError, match="нельзя изменить через staging"):
        staging.update_transaction_draft(
            "kaspi_pdf", "statement-row-A", {"amount": "150"}
        )

    assert staging.read_transaction_drafts().iloc[0]["amount"] == "100"
    assert month_path.read_bytes() == before_month


def test_cleanup_archives_exported_row_and_preserves_duplicate_id(exported_case):
    _, month_path = exported_case
    before_month = month_path.read_bytes()
    rows, revision = staging.read_transaction_drafts_snapshot()

    staging.delete_transaction_drafts(
        rows.to_dict("records"), expected_revision=revision
    )

    archived = staging.read_transaction_drafts()
    duplicate = staging.append_transaction_draft_rows(
        pd.DataFrame(
            [
                {
                    **archived.iloc[0].to_dict(),
                    "status": "draft",
                }
            ]
        )
    )
    assert archived.iloc[0]["status"] == "archived"
    assert duplicate["accepted_rows"] == 0
    assert duplicate["skipped_rows"] == 1
    assert month_path.read_bytes() == before_month


def test_cleanup_archives_exported_and_physically_deletes_unposted(exported_case):
    _, month_path = exported_case
    before_month = month_path.read_bytes()
    staging.append_transaction_draft(
        "2026-09-02", "Прочее", "RUB", 50, source_id="draft-B"
    )
    rows, revision = staging.read_transaction_drafts_snapshot()

    staging.delete_transaction_drafts(
        rows.to_dict("records"), expected_revision=revision
    )

    saved = staging.read_transaction_drafts()
    assert saved[["source_id", "status"]].to_dict("records") == [
        {"source_id": "statement-row-A", "status": "archived"}
    ]
    assert month_path.read_bytes() == before_month


def test_unchanged_exported_row_does_not_block_draft_edit(exported_case):
    staging.append_transaction_draft(
        "2026-09-02", "Прочее", "RUB", 50, source_id="draft-B"
    )
    rows, revision = staging.read_transaction_drafts_snapshot()
    edited = rows.to_dict("records")
    next(row for row in edited if row["source_id"] == "draft-B")["amount"] = "75"

    staging.merge_transaction_draft_rows(edited, expected_revision=revision)

    saved = staging.read_transaction_drafts().set_index("source_id")
    assert saved.loc["statement-row-A", "amount"] == "100"
    assert saved.loc["draft-B", "amount"] == "75"


def test_staging_controls_are_absent_but_import_rows_can_be_excluded(exported_case):
    from src.dashboard.app import _transaction_input_layout

    layout = _transaction_input_layout("RUB", "2026", "09", "light")
    import_grid = _component(layout, "kaspi-import-grid")

    assert _component(layout, "transaction-drafts-grid") is None
    assert _component(layout, "kaspi-save-button") is None
    assert _component(layout, "transaction-delete-button") is None
    assert "finrep-import-grid" in import_grid.className
    assert [column["field"] for column in import_grid.columnDefs[:4]] == [
        "category",
        "date",
        "amount",
        "import_action",
    ]
    action_column = next(
        column for column in import_grid.columnDefs if column["field"] == "import_action"
    )
    assert action_column["editable"] is True
    assert action_column["cellEditorParams"]["values"] == ["import", "skip"]


def test_month_save_is_presented_as_primary_non_destructive_action(exported_case):
    from src.dashboard.app import _transaction_input_layout

    layout = _transaction_input_layout("RUB", "2026", "09", "light")
    button = _component(layout, "transaction-confirm-export-button")
    message = _component(layout, "transaction-export-message")

    assert button.children == "Сохранить месяц"
    assert button.color == "primary"
    assert button.outline is False
    assert "«Сохранить месяц»" in message.children
