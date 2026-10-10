import sqlite3

import pytest

from src.data.sqlite_store import (
    confirm_debt_payment_plan, create_debt_payment_plan, create_debt_record,
    initialize_database, list_debt_payment_plans, record_debt_payment,
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
    with pytest.raises(ValueError, match="exceeds"):
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
        connection.execute("DELETE FROM schema_migrations WHERE version = 19")
        connection.execute("PRAGMA user_version = 18")
    initialize_database(database)
    assert list_debt_payment_plans(database) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM debts").fetchone()[0] == debt_id


def test_plan_ui_create_then_confirm(monkeypatch, tmp_path):
    from src import config
    from src.dashboard.app import create_app

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-secret")
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    debt_id = create_debt_record(
        database, kind="receivable", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )["debt_id"]
    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"
    key = next(key for key in app.callback_map if "debt-plan-message.children" in key)
    callback = app.callback_map[key]
    values = {
        "debt-plan-add-button": 1, "debt-plan-confirm-button": 0,
        "debt-plan-new-grid": [{
            "operation_id": "ui-plan-1",
            "debt": f"Мне должны | Synthetic | 100 RUB | {debt_id}",
            "due_on": "2026-11-01", "amount": 25, "comment": "Synthetic",
        }],
        "debt-plans-grid": [],
        "dashboard-refresh-token": 0,
    }
    payload = {
        "output": key,
        "outputs": [{"id": item.component_id, "property": item.component_property}
                    for item in callback["output"]],
        "inputs": [{**item, "value": values[item["id"]]} for item in callback["inputs"]],
        "state": [{**item, "value": values[item["id"]]} for item in callback["state"]],
        "changedPropIds": ["debt-plan-add-button.n_clicks"],
    }
    response = client.post("/_dash-update-component", json=payload)
    assert response.status_code == 200
    plans = list_debt_payment_plans(database)
    assert plans, response.get_json()
    plan_id = plans[0]["id"]
    payload["inputs"][1]["value"] = 1
    payload["state"][1]["value"] = [{"id": plan_id, "actual_date": "2026-10-15", "confirmed_payment_id": None}]
    payload["state"][2]["value"] = [{"id": plan_id}]
    payload["changedPropIds"] = ["debt-plan-confirm-button.n_clicks"]
    assert client.post("/_dash-update-component", json=payload).status_code == 200
    assert list_debt_payment_plans(database)[0]["confirmed_payment_id"]


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
    assert {"debt-new-grid", "debt-payment-grid", "debt-plan-new-grid", "debt-plans-grid"} <= ids
    assert not {"debt-payment-id", "debt-payment-date", "debt-plan-debt-id",
                "debt-plan-date", "debt-plan-select", "debt-plan-actual-date"} & ids
