from decimal import Decimal

from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    archive_asset_accounts,
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
        "changedPropIds": [f"{input_id}.n_clicks"],
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


def test_copy_previous_month_adds_only_missing_active_balances(tmp_path, monkeypatch):
    database, app, client = _app(tmp_path, monkeypatch)
    for account_id, name in (("deposit", "Deposit"), ("card", "Card"), ("old", "Archived")):
        add_asset_account(database, account_id, name, asset_type_id="other")
    for account_id, currency, amount in (
        ("deposit", "RUB", "100"),
        ("deposit", "USD", "50"),
        ("card", "KZT", "200"),
        ("old", "RUB", "0"),
    ):
        add_asset_snapshot(
            database, snapshot_id=f"{account_id}-{currency}", account_id=account_id,
            period="2025-12", amount=amount, currency=currency,
        )
    add_asset_snapshot(
        database, snapshot_id="current-deposit-rub", account_id="deposit",
        period="2026-01", amount="111", currency="RUB",
    )
    archive_asset_accounts(database, ["Archived"], period="2025-12")
    edited = {"account": "Deposit", "amount": "999", "currency": "RUB"}
    draft = {"account": "Manual draft", "amount": "7", "currency": "RUB"}
    values = {
        ("assets-copy-previous-button", "n_clicks"): 1,
        ("dashboard-year", "value"): "2026",
        ("dashboard-month", "value"): "01",
        ("assets-input-grid", "rowData"): [edited, draft],
        ("assets-input-grid", "selectedRows"): [],
        ("dashboard-locale", "data"): "ru",
        ("bank-statement-balances", "data"): [],
    }

    result = _callback(client, app, "assets-copy-previous-button", values)
    rows = result["assets-input-grid"]["rowData"]
    assert rows[:2] == [edited, draft]
    assert {(row["account"], row["currency"], Decimal(row["amount"].replace(" ", "")))
            for row in rows[2:]} == {
        ("Deposit", "USD", Decimal("50")), ("Card", "KZT", Decimal("200")),
    }
    assert "2025-12: 2" in result["assets-input-message"]["children"]
    assert [(row["account_id"], row["amount"])
            for row in asset_snapshot_month(database, "2026-01")] == [
        ("deposit", Decimal("111")),
    ]

    values[("assets-input-grid", "rowData")] = rows
    repeated = _callback(client, app, "assets-copy-previous-button", values)
    assert repeated["assets-input-grid"]["rowData"] == rows
    assert "2025-12: 0" in repeated["assets-input-message"]["children"]

    values[("dashboard-month", "value")] = "03"
    missing = _callback(client, app, "assets-copy-previous-button", values)
    assert missing["assets-input-message"]["color"] == "warning"
    assert "2026-02" in missing["assets-input-message"]["children"]
    assert missing["assets-input-grid"]["rowData"] == rows
