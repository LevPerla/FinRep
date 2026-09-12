from pathlib import Path

import pandas as pd
import pytest

from src import config
from src.data import staging


@pytest.fixture
def export_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path / "staging" / "transaction_drafts.csv", tmp_path / "transactions_info"


def _add(drafts: Path, date: str, amount: int, source_id: str) -> None:
    staging.append_transaction_draft(
        date=date,
        category="Прочее",
        currency="RUB",
        amount=amount,
        comment=source_id,
        source="test",
        source_id=source_id,
        path=drafts,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*.csv")}


def _callback_request(app, client, trigger: str, year: str, month: str, rows=None, state=None):
    key = next(
        key
        for key in app.callback_map
        if "transaction-export-preview-grid.rowData" in key
        and "transaction-export-preview-state.data" in key
    )
    callback = app.callback_map[key]
    values = {
        "transaction-preview-export-button": 1 if trigger == "transaction-preview-export-button" else 0,
        "transaction-confirm-export-button": 1 if trigger == "transaction-confirm-export-button" else 0,
        "dashboard-year": year,
        "dashboard-month": month,
        "transaction-export-preview-grid": rows,
        "transaction-export-preview-state": state,
    }
    payload = {
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [{**item, "value": values[item["id"]]} for item in callback["inputs"]],
        "state": [{**item, "value": values[item["id"]]} for item in callback["state"]],
        "changedPropIds": [f"{trigger}.n_clicks"],
    }
    return client.post("/_dash-update-component", json=payload)


def test_stale_preview_rejects_new_draft_without_writing_or_exporting(export_paths):
    drafts, transactions = export_paths
    _add(drafts, "2026-01-01", 100, "A")
    preview, preview_state = staging.prepare_monthly_transaction_export(
        "2026", "01", path=drafts, transactions_root=transactions
    )
    _add(drafts, "2026-01-01", 200, "B")
    before = _snapshot(drafts.parents[1])

    with pytest.raises(ValueError, match="Preview устарел"):
        staging.export_monthly_transaction_drafts(
            "2026",
            "01",
            path=drafts,
            transactions_root=transactions,
            preview_rows=preview.to_dict("records"),
            preview_state=preview_state,
        )

    assert _snapshot(drafts.parents[1]) == before
    statuses = staging.read_transaction_drafts(drafts).set_index("source_id")["status"].to_dict()
    assert statuses == {"A": "draft", "B": "draft"}
    assert not staging.monthly_transaction_csv_path("2026", "01", transactions).exists()


def test_preview_for_another_month_is_rejected_before_write(export_paths):
    drafts, transactions = export_paths
    _add(drafts, "2026-01-01", 100, "JAN")
    preview, preview_state = staging.prepare_monthly_transaction_export(
        "2026", "01", path=drafts, transactions_root=transactions
    )
    _add(drafts, "2026-02-01", 200, "FEB")
    before = _snapshot(drafts.parents[1])

    with pytest.raises(ValueError, match="другого периода"):
        staging.export_monthly_transaction_drafts(
            "2026",
            "02",
            path=drafts,
            transactions_root=transactions,
            preview_rows=preview.to_dict("records"),
            preview_state=preview_state,
        )

    assert _snapshot(drafts.parents[1]) == before
    assert not staging.monthly_transaction_csv_path("2026", "02", transactions).exists()
    statuses = staging.read_transaction_drafts(drafts).set_index("source_id")["status"].to_dict()
    assert statuses == {"JAN": "draft", "FEB": "draft"}


def test_financial_cells_remain_editable_in_valid_preview(export_paths):
    drafts, transactions = export_paths
    _add(drafts, "2026-01-01", 100, "A")
    preview, preview_state = staging.prepare_monthly_transaction_export(
        "2026", "01", path=drafts, transactions_root=transactions
    )
    preview.loc[preview["Дата"] == "01.01.2026", "Прочее"] = "150|RUB|corrected in preview"

    result = staging.export_monthly_transaction_drafts(
        "2026",
        "01",
        path=drafts,
        transactions_root=transactions,
        preview_rows=preview.to_dict("records"),
        preview_state=preview_state,
    )

    saved = pd.read_csv(result["target_path"], sep=";", dtype=str, encoding="utf-8-sig").fillna("0")
    assert saved.loc[saved["Дата"] == "01.01.2026", "Прочее"].iloc[0] == "150|RUB|corrected in preview"
    assert staging.read_transaction_drafts(drafts).iloc[0]["status"] == "exported"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda rows: rows.__setitem__(0, {**rows[0], "Дата": "not-a-date"}), "Дата"),
        (lambda rows: rows.pop(), "[Сс]труктура"),
        (lambda rows: rows[0].__setitem__("Новая категория", "10|RUB|crafted"), "[Сс]труктура"),
    ],
)
def test_invalid_preview_structure_is_rejected_without_writes(export_paths, mutation, message):
    drafts, transactions = export_paths
    _add(drafts, "2026-01-01", 100, "A")
    preview, preview_state = staging.prepare_monthly_transaction_export(
        "2026", "01", path=drafts, transactions_root=transactions
    )
    rows = preview.to_dict("records")
    mutation(rows)
    before = _snapshot(drafts.parents[1])

    with pytest.raises(ValueError, match=message):
        staging.export_monthly_transaction_drafts(
            "2026",
            "01",
            path=drafts,
            transactions_root=transactions,
            preview_rows=rows,
            preview_state=preview_state,
        )

    assert _snapshot(drafts.parents[1]) == before
    assert staging.read_transaction_drafts(drafts).iloc[0]["status"] == "draft"


def test_preview_state_is_bound_to_data_mode(export_paths):
    drafts, transactions = export_paths
    _add(drafts, "2026-01-01", 100, "A")
    preview, preview_state = staging.prepare_monthly_transaction_export(
        "2026", "01", path=drafts, transactions_root=transactions
    )
    preview_state["data_mode"] = "test"
    before = _snapshot(drafts.parents[1])

    with pytest.raises(ValueError, match="другого режима"):
        staging.export_monthly_transaction_drafts(
            "2026",
            "01",
            path=drafts,
            transactions_root=transactions,
            preview_rows=preview.to_dict("records"),
            preview_state=preview_state,
        )

    assert _snapshot(drafts.parents[1]) == before


def test_dashboard_callback_keeps_stale_preview_visible_and_does_not_write(
    export_paths, monkeypatch
):
    drafts, transactions = export_paths
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    _add(drafts, "2026-01-01", 100, "A")
    app = create_app()
    client = app.server.test_client()
    client.post(
        "/login",
        data={"password": "synthetic-password", "data_mode": "live"},
    )
    preview_response = _callback_request(
        app, client, "transaction-preview-export-button", "2026", "01"
    )
    assert preview_response.status_code == 200
    preview_result = preview_response.get_json()["response"]
    rows = preview_result["transaction-export-preview-grid"]["rowData"]
    columns = preview_result["transaction-export-preview-grid"]["columnDefs"]
    state = preview_result["transaction-export-preview-state"]["data"]
    assert next(column for column in columns if column["field"] == "Дата")["editable"] is False
    assert next(column for column in columns if column["field"] == "Прочее")["editable"] is True

    _add(drafts, "2026-01-01", 200, "B")
    before = _snapshot(drafts.parents[1])
    confirm_response = _callback_request(
        app,
        client,
        "transaction-confirm-export-button",
        "2026",
        "01",
        rows=rows,
        state=state,
    )

    assert confirm_response.status_code == 200
    confirm_result = confirm_response.get_json()["response"]
    assert confirm_result["transaction-export-preview-grid"]["rowData"] == rows
    message = confirm_result["transaction-export-message"]
    assert message["color"] == "danger"
    assert "Preview устарел" in message["children"]
    assert _snapshot(drafts.parents[1]) == before
    assert not staging.monthly_transaction_csv_path("2026", "01", transactions).exists()
