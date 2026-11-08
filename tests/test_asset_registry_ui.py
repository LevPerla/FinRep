from decimal import Decimal

import pytest

from src.data.sqlite_store import (
    add_asset_account,
    asset_accounts,
    asset_snapshot_month,
    initialize_database,
)


def _callback(client, app, input_id, values):
    key, callback = next(
        (key, callback) for key, callback in app.callback_map.items()
        if any(item["id"] == input_id for item in callback["inputs"])
    )
    response = client.post("/_dash-update-component", json={
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [
            {**item, "value": values.get((item["id"], item["property"]))}
            for item in callback["inputs"]
        ],
        "state": [
            {**item, "value": values.get((item["id"], item["property"]), [])}
            for item in callback["state"]
        ],
        "changedPropIds": [f"{input_id}.{next(item['property'] for item in callback['inputs'] if item['id'] == input_id)}"],
    })
    assert response.status_code == 200
    return response.get_json()["response"]


def _app(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-secret")
    initialize_database(database)
    from src.dashboard.app import create_app

    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"
    return database, app, client


def test_selected_confirmation_and_cancel_preserve_other_edits(tmp_path, monkeypatch):
    from src.dashboard.app import _asset_input_records

    database, app, client, values = _delete_fixture(tmp_path, monkeypatch)
    values[("dashboard-month", "value")] = "12"
    rows = _asset_input_records("2026", "12")
    chosen = next(row for row in rows if row["account"] == "MIR example")
    chosen["amount"] = "75"
    other = next(row for row in rows if row["currency"] == "KZT")
    other["amount"] = "123"
    draft = {"account": "Unsaved", "currency": "RUB", "amount": "9"}
    rows.append(draft)
    values[("assets-input-grid", "rowData")] = rows
    values[("assets-input-grid", "selectedRows")] = [chosen]
    values[("assets-confirm-valuation-button", "n_clicks")] = 1
    result = _callback(client, app, "assets-confirm-valuation-button", values)
    assert result["assets-input-message"]["color"] == "success"
    saved = asset_snapshot_month(database, "2026-12")
    assert len(saved) == 1 and saved[0]["amount"] == Decimal("75")
    updated = result["assets-input-grid"]["rowData"]
    assert other in updated and draft in updated
    assert not next(row for row in updated if row["account"] == "MIR example")["carried"]
    values[("assets-input-grid", "rowData")] = updated
    values[("assets-input-grid", "selectedRows")] = [next(row for row in updated if row["account"] == "MIR example")]
    prompt = _callback(client, app, "assets-remove-valuation-button", values)
    assert prompt["assets-remove-valuation-confirm"]["displayed"]
    assert "MIR example · 75 RUB" in prompt["assets-remove-valuation-confirm"]["message"]
    values[("assets-remove-valuation-target", "data")] = prompt["assets-remove-valuation-target"]["data"]
    undone = _callback(client, app, "assets-remove-valuation-confirm", values)
    assert asset_snapshot_month(database, "2026-12") == []
    restored = next(row for row in undone["assets-input-grid"]["rowData"] if row["account"] == "MIR example")
    assert restored["carried"] and restored["amount"] == "62"


@pytest.mark.parametrize("case", ["test_mode", "stale", "changed_account", "invalid_amount", "no_selection", "multiple"])
def test_selected_confirmation_rejects_invalid_state(tmp_path, monkeypatch, case):
    from src.dashboard.app import _asset_input_records

    database, app, client, values = _delete_fixture(tmp_path, monkeypatch)
    values[("dashboard-month", "value")] = "12"
    rows = _asset_input_records("2026", "12")
    chosen = rows[0]
    if case == "test_mode":
        with client.session_transaction() as session:
            session["data_mode"] = "test"
    elif case == "stale":
        chosen["snapshot_row_version"] = 0
    elif case == "changed_account":
        chosen["account"] = "Changed"
    elif case == "invalid_amount":
        chosen["amount"] = "bad"
    values[("assets-input-grid", "rowData")] = rows
    values[("assets-input-grid", "selectedRows")] = [] if case == "no_selection" else rows if case == "multiple" else [chosen]
    values[("assets-confirm-valuation-button", "n_clicks")] = 1
    result = _callback(client, app, "assets-confirm-valuation-button", values)
    assert result["assets-input-message"]["color"] == "danger"
    assert asset_snapshot_month(database, "2026-12") == []


def test_create_asset_from_statement_exposes_it_in_registry(tmp_path, monkeypatch):
    database, app, client = _app(tmp_path, monkeypatch)
    values = {
        ("bank-create-asset-button", "n_clicks"): 1,
        ("bank-new-asset-name", "value"): "  New deposit  ",
        ("bank-new-asset-type", "value"): "deposit",
        ("dashboard-locale", "data"): "ru",
    }

    result = _callback(client, app, "bank-create-asset-button", values)

    account = next(row for row in asset_accounts(database) if row["name"] == "New deposit")
    assert result["asset-registry-refresh"]["data"] == account["id"]
    assert {option["value"] for option in result["assets-registry-account"]["options"]} == {account["id"]}
    assert result["bank-create-asset-message"]["color"] == "success"
    assert result["bank-new-asset-name"]["value"] == ""
    assert asset_snapshot_month(database, "2026-10") == []

    again = _callback(client, app, "bank-create-asset-button", values)
    assert again["bank-create-asset-message"]["color"] == "danger"
    assert len(asset_accounts(database)) == 1

    with client.session_transaction() as session:
        session["data_mode"] = "test"
    values[("bank-new-asset-name", "value")] = "Read-only attempt"
    blocked = _callback(client, app, "bank-create-asset-button", values)
    assert blocked["bank-create-asset-message"]["color"] == "danger"
    assert len(asset_accounts(database)) == 1


def test_add_existing_asset_to_month_preserves_drafts_until_apply(tmp_path, monkeypatch):
    database, app, client = _app(tmp_path, monkeypatch)
    add_asset_account(database, "registry-asset", "Registry asset", asset_type_id="other")
    draft = {"account": "Manual draft", "asset_type_id": "other", "amount": "7", "currency": "RUB"}
    values = {
        ("assets-add-from-registry-button", "n_clicks"): 1,
        ("dashboard-year", "value"): "2026",
        ("dashboard-month", "value"): "10",
        ("assets-input-grid", "rowData"): [draft],
        ("assets-input-grid", "selectedRows"): [],
        ("assets-registry-account", "value"): "registry-asset",
        ("assets-registry-currency", "value"): "KZT",
        ("dashboard-locale", "data"): "ru",
        ("bank-statement-balances", "data"): [],
    }

    result = _callback(client, app, "assets-add-from-registry-button", values)
    rows = result["assets-input-grid"]["rowData"]
    assert rows[0] == draft
    assert (rows[1]["account_id"], rows[1]["currency"], rows[1]["amount"]) == (
        "registry-asset", "KZT", "",
    )
    assert asset_snapshot_month(database, "2026-10") == []

    values[("assets-input-grid", "rowData")] = rows
    duplicate = _callback(client, app, "assets-add-from-registry-button", values)
    assert duplicate["assets-input-message"]["color"] == "danger"
    assert duplicate["assets-input-grid"]["rowData"] == rows

    rows[1]["amount"] = "125"
    values[("assets-input-grid", "rowData")] = rows
    values[("assets-apply-button", "n_clicks")] = 1
    applied = _callback(client, app, "assets-apply-button", values)
    assert applied["assets-input-message"]["color"] == "success"
    assert {(row["account_name"], row["currency_code"], row["amount"])
            for row in asset_snapshot_month(database, "2026-10")} == {
        ("Manual draft", "RUB", Decimal("7")),
        ("Registry asset", "KZT", Decimal("125")),
    }


def _delete_fixture(tmp_path, monkeypatch):
    from src.data.sqlite_store import upsert_asset_snapshot_batch
    from src.dashboard.app import _asset_input_records
    database, app, client = _app(tmp_path, monkeypatch)
    for account in ("Kaspi example", "MIR example"):
        add_asset_account(database, account, account, asset_type_id="cash_account")
    upsert_asset_snapshot_batch(database, [
        {"account_id": account, "currency": currency, "amount": amount, "period": "2026-11"}
        for account, currency, amount in [("Kaspi example", "RUB", "62"), ("MIR example", "RUB", "62"),
                                          ("Kaspi example", "KZT", "280983.46")]
    ], reason="synthetic remapped statement")
    rows = _asset_input_records("2026", "11")
    wrong = next(row for row in rows if row["account"] == "Kaspi example" and row["currency"] == "RUB")
    values = {
        ("assets-remove-valuation-button", "n_clicks"): 1,
        ("assets-input-grid", "selectedRows"): [wrong],
        ("assets-input-grid", "rowData"): rows,
        ("dashboard-year", "value"): "2026",
        ("dashboard-month", "value"): "11",
        ("dashboard-locale", "data"): "ru",
    }
    return database, app, client, values


def test_delete_confirmation_and_exact_target_preserve_unsaved_edits(tmp_path, monkeypatch):
    database, app, client, values = _delete_fixture(tmp_path, monkeypatch)
    request = _callback(client, app, "assets-remove-valuation-button", values)
    assert request["assets-remove-valuation-confirm"]["displayed"] is True
    assert "Kaspi example · 62 RUB" in request["assets-remove-valuation-confirm"]["message"]
    assert "2026-11" in request["assets-remove-valuation-confirm"]["message"]
    assert len(asset_snapshot_month(database, "2026-11")) == 3  # Asking/cancelling never writes.
    values[("assets-remove-valuation-target", "data")] = request["assets-remove-valuation-target"]["data"]
    rows = values[("assets-input-grid", "rowData")]
    other = next(row for row in rows if row["account"] == "MIR example")
    other["amount"] = "77"  # Unrelated unsaved edit must survive.
    draft = {"account": "Draft", "asset_type_id": "cash", "amount": "9", "currency": "RUB"}
    rows.append(draft)
    values[("assets-input-grid", "selectedRows")] = [other]  # Confirmation fixes its original target.
    values[("assets-remove-valuation-confirm", "submit_n_clicks")] = 1
    result = _callback(client, app, "assets-remove-valuation-confirm", values)
    assert result["assets-input-message"]["color"] == "success"
    assert other in result["assets-input-grid"]["rowData"]
    assert draft in result["assets-input-grid"]["rowData"]
    assert {(r["account_name"], r["currency_code"], r["amount"]) for r in asset_snapshot_month(database, "2026-11")} == {
        ("MIR example", "RUB", Decimal("62")), ("Kaspi example", "KZT", Decimal("280983.46"))}


@pytest.mark.parametrize("case", ["no_selection", "draft", "carried", "stale", "test_mode", "period_changed"])
def test_valuation_delete_rejections_do_not_write(tmp_path, monkeypatch, case):
    database, app, client, values = _delete_fixture(tmp_path, monkeypatch)
    request = _callback(client, app, "assets-remove-valuation-button", values)
    if case == "no_selection":
        values[("assets-input-grid", "selectedRows")] = []
    elif case == "draft":
        values[("assets-input-grid", "selectedRows")] = [{"account": "Draft"}]
    elif case == "carried":
        values[("dashboard-month", "value")] = "12"
    elif case == "stale":
        values[("assets-input-grid", "selectedRows")][0]["snapshot_row_version"] = 0
    elif case == "test_mode":
        with client.session_transaction() as session:
            session["data_mode"] = "test"
    else:
        values[("dashboard-month", "value")] = "12"
    result = _callback(client, app, "assets-remove-valuation-button", values)
    assert result["assets-remove-valuation-confirm"]["displayed"] is False
    assert result["assets-input-message"]["color"] == "danger"
    if case in {"test_mode", "period_changed"}:
        values[("assets-remove-valuation-target", "data")] = request["assets-remove-valuation-target"]["data"]
        values[("assets-remove-valuation-confirm", "submit_n_clicks")] = 1
        blocked = _callback(client, app, "assets-remove-valuation-confirm", values)
        assert blocked["assets-input-message"]["color"] == "danger"
    assert len(asset_snapshot_month(database, "2026-11")) == 3
