import sqlite3

import pytest

from src.data.sqlite_store import (
    confirm_debt_payment_plan, create_debt_payment_plan, create_debt_record,
    initialize_database, list_debt_payment_plans, record_debt_payment,
    update_debt_due_date,
)


def test_plan_stays_separate_until_confirmed_and_retry_does_not_duplicate(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    debt_id = create_debt_record(
        database, kind="receivable", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )["debt_id"]
    plan_id = create_debt_payment_plan(
        database, debt_id=debt_id, due_on="2026-11-01", amount="25",
        operation_key="plan-1",
    )
    assert create_debt_payment_plan(
        database, debt_id=debt_id, due_on="2026-11-01", amount="25",
        operation_key="plan-1",
    ) == plan_id
    assert list_debt_payment_plans(database)[0]["confirmed_payment_id"] is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM debt_payments").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM transaction_drafts").fetchone()[0] == 1
        assert connection.execute("SELECT status FROM debts").fetchone()[0] == "active"

    first = confirm_debt_payment_plan(database, plan_id=plan_id, occurred_on="2026-10-15")
    repeated = confirm_debt_payment_plan(database, plan_id=plan_id, occurred_on="2026-10-16")
    assert first["payment_id"] == repeated["payment_id"]
    assert list_debt_payment_plans(database)[0]["actual_date"] == "2026-10-15"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM debt_payments").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM transaction_drafts").fetchone()[0] == 2
        assert connection.execute("SELECT principal_amount_minor FROM debt_payments").fetchone()[0] == 2500


def test_plan_confirmation_rechecks_actual_outstanding(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    debt_id = create_debt_record(
        database, kind="liability", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )["debt_id"]
    plan_id = create_debt_payment_plan(
        database, debt_id=debt_id, due_on="2026-11-01", amount="60",
        operation_key="plan-1",
    )
    record_debt_payment(
        database, debt_id=debt_id, occurred_on="2026-10-05", amount="50",
        operation_key="actual-1",
    )
    with pytest.raises(ValueError, match="Погашение больше остатка"):
        confirm_debt_payment_plan(database, plan_id=plan_id, occurred_on="2026-10-15")
    assert list_debt_payment_plans(database)[0]["confirmed_payment_id"] is None
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM debt_payments").fetchone()[0] == 1


def test_confirmation_recovers_after_payment_was_written_but_plan_not_linked(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    debt_id = create_debt_record(
        database, kind="receivable", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )["debt_id"]
    plan_id = create_debt_payment_plan(
        database, debt_id=debt_id, due_on="2026-11-01", amount="25",
        operation_key="plan-1",
    )
    recorded = record_debt_payment(
        database, debt_id=debt_id, occurred_on="2026-10-15", amount="25",
        operation_key=f"debt-plan-confirm:{plan_id}",
    )
    recovered = confirm_debt_payment_plan(database, plan_id=plan_id, occurred_on="2026-10-16")
    assert recovered["payment_id"] == recorded["payment_id"]
    assert list_debt_payment_plans(database)[0]["confirmed_payment_id"] == recorded["payment_id"]


def test_v18_database_upgrades_without_erasing_debts(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    debt_id = create_debt_record(
        database, kind="receivable", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )["debt_id"]
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE debt_payment_plans")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 19")
        connection.execute("PRAGMA user_version = 18")
    initialize_database(database)
    assert list_debt_payment_plans(database) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM debts").fetchone()[0] == debt_id


def test_debt_due_date_is_saved_and_cannot_precede_opening(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    created = create_debt_record(
        database, kind="receivable", counterparty="Иван",
        opened_on="2026-10-01", due_on="2026-11-01",
        principal_amount="100", currency="RUB", operation_key="open",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT due_on FROM debts WHERE id = ?", (created["debt_id"],)).fetchone()[0] == "2026-11-01"
    update_debt_due_date(database, debt_id=created["debt_id"], due_on="2026-12-01")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT due_on FROM debts WHERE id = ?", (created["debt_id"],)).fetchone()[0] == "2026-12-01"
    with pytest.raises(ValueError, match="раньше даты начала"):
        create_debt_record(
            database, kind="receivable", counterparty="Иван",
            opened_on="2026-10-01", due_on="2026-09-30",
            principal_amount="100", currency="RUB", operation_key="bad-date",
        )


def test_v20_database_adds_optional_debt_due_date(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    created = create_debt_record(
        database, kind="receivable", counterparty="Иван",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE debts DROP COLUMN due_on")
        connection.execute("DELETE FROM schema_migrations WHERE version = 21")
        connection.execute("PRAGMA user_version = 20")
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT due_on FROM debts WHERE id = ?", (created["debt_id"],)).fetchone()[0] is None


def test_existing_debt_due_date_can_be_edited_in_grid(tmp_path, monkeypatch):
    from src import config
    from src.dashboard.app import _debt_grid_records, create_app

    database = tmp_path / "finrep.sqlite3"
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-secret")
    initialize_database(database)
    created = create_debt_record(
        database, kind="receivable", counterparty="Иван",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )
    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"
    key = next(key for key in app.callback_map if "debt-input-message.children" in key)
    callback = app.callback_map[key]
    row = {**_debt_grid_records("RUB")[0], "due_date": "2026-11-01"}
    values = {
        "debt-add-button": 1, "debt-payment-button": 0,
        "dashboard-currency": "RUB", "debt-new-grid": [row],
        "debt-payment-grid": [],
    }
    payload = {
        "output": key,
        "outputs": [{"id": item.component_id, "property": item.component_property}
                    for item in callback["output"]],
        "inputs": [{**item, "value": values[item["id"]]} for item in callback["inputs"]],
        "state": [{**item, "value": values[item["id"]]} for item in callback["state"]],
        "changedPropIds": ["debt-add-button.n_clicks"],
    }
    assert client.post("/_dash-update-component", json=payload).status_code == 200
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT due_on FROM debts WHERE id = ?", (created["debt_id"],)).fetchone()[0] == "2026-11-01"
    payload["state"][1]["value"] = [{**row, "due_date": "2026-09-01"}]
    result = client.post("/_dash-update-component", json=payload).get_json()["response"]
    assert result["debt-input-message"]["color"] == "danger"
    assert "раньше даты начала" in result["debt-new-grid"]["rowData"][0]["validation_error"]
    payload["state"][1]["value"] = [{**row, "due_date": "не дата"}]
    result = client.post("/_dash-update-component", json=payload).get_json()["response"]
    assert result["debt-input-message"]["color"] == "danger"
    assert "ISO date" in result["debt-new-grid"]["rowData"][0]["validation_error"]


def test_debt_entry_uses_editable_grids_without_standalone_forms(monkeypatch, tmp_path):
    from src import config
    from src.dashboard.app import _debt_input_layout

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    initialize_database(tmp_path / "finrep.sqlite3")
    layout = _debt_input_layout("RUB", "dark")

    def components(node):
        if isinstance(node, (list, tuple)):
            for child in node:
                yield from components(child)
        elif hasattr(node, "to_plotly_json"):
            yield node
            if hasattr(node, "children"):
                yield from components(node.children)

    ids = {component.id for component in components(layout) if getattr(component, "id", None)}
    assert {"debt-new-grid", "debt-payment-grid", "debt-transaction-drafts-grid"} <= ids
    assert not {"debt-plan-new-grid", "debt-plans-grid", "debt-migrate-button"} & ids
    grid = next(component for component in components(layout) if getattr(component, "id", None) == "debt-new-grid")
    columns = {column["field"]: column for column in grid.columnDefs}
    assert {"opened_date", "due_date", "counterparty", "outstanding_amount"} <= columns.keys()
    assert columns["due_date"]["headerName"] == "Дата ожидаемого погашения"
    assert columns["due_date"]["cellDataType"] == "dateString"
    assert columns["due_date"]["cellEditor"] == "agDateStringCellEditor"
    assert columns["counterparty"]["cellEditor"] == "agTextCellEditor"
    assert grid.dashGridOptions["singleClickEdit"] is True
    payment_grid = next(component for component in components(layout) if getattr(component, "id", None) == "debt-payment-grid")
    amount_column = next(column for column in payment_grid.columnDefs if column["field"] == "amount")
    assert amount_column["width"] >= 180
    draft_grid = next(component for component in components(layout) if getattr(component, "id", None) == "debt-transaction-drafts-grid")
    assert next(column for column in draft_grid.columnDefs if column["field"] == "comment")["editable"] is True
    assert "debt-draft-save-button" in ids
    assert not {"debt-payment-id", "debt-payment-date", "debt-plan-debt-id",
                "debt-plan-date", "debt-plan-select", "debt-plan-actual-date"} & ids
