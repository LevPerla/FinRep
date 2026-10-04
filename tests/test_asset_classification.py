from datetime import date

from src import config
from src.data.get import clear_data_cache
from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    asset_accounts,
    initialize_database,
    set_asset_account_classifications,
)


def _component(node, component_id):
    if getattr(node, "id", None) == component_id:
        return node
    children = getattr(node, "children", None)
    if not isinstance(children, (list, tuple)):
        children = [children]
    for child in children:
        found = _component(child, component_id) if child is not None else None
        if found is not None:
            return found
    return None


def _use_live_sqlite(monkeypatch, database):
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setattr(config, "DATA_PATH", str(database.parent))
    clear_data_cache()


def test_assets_input_shows_account_classification_with_history_context(tmp_path, monkeypatch):
    from src.dashboard.app import _assets_input_layout

    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(database, "account-1", "Основной счёт")
    current_period = date.today().strftime("%Y-%m")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="account-1",
        period=current_period, amount="100", currency="RUB")
    _use_live_sqlite(monkeypatch, database)

    year, month = current_period.split("-")
    layout = _assets_input_layout(year, month, "dark")
    grid = _component(layout, "asset-classification-grid")
    snapshot_grid = _component(layout, "assets-input-grid")
    message = _component(layout, "asset-classification-message")

    assert len(grid.rowData) == 1
    row = grid.rowData[0]
    assert row["account_id"] == "account-1"
    assert row["Счет"] == "Основной счёт"
    assert row["asset_type_id"] == "__unclassified__"
    assert "liquidity_choice" not in row
    assert row["liquidity_class_id"] == ""
    assert row["liquidity_source"] == "unclassified"
    assert row["Включать в капитал"] is True
    assert row["active"] is True
    assert row["closed_period"] == ""
    assert row["freshness_status"] == "fresh"
    assert row["Актуальность"].startswith("Актуально · ")
    assert row["Снимков"] == 1
    assert row["Первый снимок"] == current_period
    assert row["Последний снимок"] == current_period
    type_column = next(
        column for column in grid.columnDefs if column["field"] == "asset_type_id")
    assert type_column["editable"] is True
    assert type_column["cellEditorParams"]["values"][0] == "__unclassified__"
    assert "deposit" in type_column["cellEditorParams"]["values"]
    assert "cash" in type_column["cellEditorParams"]["values"]
    liquidity_column = next(
        column for column in grid.columnDefs if column["field"] == "liquidity_class_id")
    assert liquidity_column["editable"] is False
    assert "cellEditorParams" not in liquidity_column
    assert next(column for column in grid.columnDefs if column["field"] == "Счет")["flex"] == 2
    assert type_column["minWidth"] == 190
    assert next(column for column in grid.columnDefs if column["field"] == "active")[
        "editable"] is True
    assert next(
        column for column in grid.columnDefs if column["field"] == "closed_period"
    )["headerName"] == "Закрыт после"
    assert next(
        column for column in grid.columnDefs if column["field"] == "Последний снимок"
    )["sort"] == "desc"
    snapshot_columns = {column["field"]: column for column in snapshot_grid.columnDefs}
    assert [column["field"] for column in snapshot_grid.columnDefs[:4]] == [
        "account", "asset_type", "amount", "currency"]
    assert snapshot_columns["account"]["flex"] == 2
    assert snapshot_columns["asset_type"]["editable"] is False
    assert snapshot_columns["account"]["cellClassRules"] == snapshot_columns[
        "asset_type"]["cellClassRules"]
    assert set(snapshot_columns["asset_type"]["cellClassRules"]) == {
        "finrep-asset-kind-cash",
        "finrep-asset-kind-cash-account",
        "finrep-asset-kind-deposit",
        "finrep-asset-kind-bond",
        "finrep-asset-kind-equity",
        "finrep-asset-kind-fund",
        "finrep-asset-kind-crypto",
        "finrep-asset-kind-real-estate",
        "finrep-asset-kind-other",
        "finrep-asset-kind-unclassified",
    }
    assert snapshot_columns["amount"]["flex"] == 1
    assert snapshot_columns["currency"]["flex"] == 0.7
    assert snapshot_grid.rowData[0]["asset_type"] == "Не классифицировано"
    assert snapshot_grid.rowData[0]["asset_type_id"] == "__unclassified__"
    assert "Не классифицировано: 1" in message.children
    assert "Ликвидность не задана: 1" in message.children
    assert "Устаревших оценок: 0" in message.children
    assert snapshot_grid.style["height"] == "220px"
    assert grid.style["height"] == "220px"


def test_asset_classification_callback_saves_all_rows_and_refreshes_capital(
        tmp_path, monkeypatch):
    from src.dashboard.app import create_app

    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(database, "account-1", "Основной счёт")
    _use_live_sqlite(monkeypatch, database)
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-secret")

    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"

    key = next(
        key for key in app.callback_map
        if "asset-classification-message.children" in key)
    callback = app.callback_map[key]
    values = {
        "asset-classification-save-button": 1,
        "asset-classification-grid": [{
            "account_id": "account-1",
            "Счет": "Основной счёт",
            "asset_type_id": "deposit",
            "liquidity_class_id": "",
            "liquidity_source": "unclassified",
            "Включать в капитал": False,
            "active": True,
            "closed_period": "",
            "Снимков": 0,
            "Первый снимок": "",
            "Последний снимок": "",
        }],
        "dashboard-locale": "ru",
    }
    payload = {
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [
            {**item, "value": values.get(item["id"])} for item in callback["inputs"]
        ],
        "state": [
            {**item, "value": values.get(item["id"])} for item in callback["state"]
        ],
        "changedPropIds": ["asset-classification-save-button.n_clicks"],
    }

    response = client.post("/_dash-update-component", json=payload)

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert "Обновлено счетов: 1" in result["asset-classification-message"]["children"]
    account = asset_accounts(database)[0]
    assert account["asset_type_id"] == "deposit"
    assert account["liquidity_class_override_id"] is None
    assert account["liquidity_class_id"] == "A1"
    assert account["liquidity_source"] == "suggested"
    assert account["include_in_capital"] == 0


def test_asset_classification_rows_sort_by_latest_snapshot_descending(
        tmp_path, monkeypatch):
    from src.dashboard.app import _asset_classification_rows

    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    for account_id, name, period in [
        ("old", "Старый", "2024-01"),
        ("new-b", "Бета", "2026-02"),
        ("new-a", "Альфа", "2026-02"),
    ]:
        add_asset_account(database, account_id, name)
        add_asset_snapshot(
            database, snapshot_id=f"snapshot-{account_id}", account_id=account_id,
            period=period, amount="100", currency="RUB")
    add_asset_account(database, "empty", "Без снимка")
    _use_live_sqlite(monkeypatch, database)

    rows = _asset_classification_rows("ru")

    assert [row["account_id"] for row in rows] == ["new-a", "new-b", "old", "empty"]


def test_asset_input_records_show_localized_type_from_account_classification(
        tmp_path, monkeypatch):
    from src.dashboard.app import _asset_input_records

    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(database, "cash", "Кошелёк", asset_type_id="cash")
    add_asset_snapshot(
        database, snapshot_id="snapshot-cash", account_id="cash",
        period="2026-09", amount="100", currency="RUB")
    _use_live_sqlite(monkeypatch, database)

    assert _asset_input_records("2026", "09", "ru")[0]["asset_type"] == "Наличные"
    assert _asset_input_records("2026", "09", "en")[0]["asset_type"] == "Cash"
    assert _asset_input_records("2026", "09", "ru")[0]["asset_type_id"] == "cash"


def test_archived_account_is_not_carried_into_a_later_snapshot_template(
        tmp_path, monkeypatch):
    from src.data.assets_editor import read_asset_snapshot

    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(database, "closed", "Закрытый счёт", asset_type_id="cash_account")
    add_asset_account(database, "open", "Открытый счёт", asset_type_id="cash_account")
    for account_id, name in [("closed", "Закрытый счёт"), ("open", "Открытый счёт")]:
        add_asset_snapshot(
            database, snapshot_id=f"snapshot-{account_id}", account_id=account_id,
            period="2026-02", amount="100", currency="RUB")
    set_asset_account_classifications(
        database,
        [{"account_id": "closed", "asset_type_id": "cash_account",
          "include_in_capital": True, "active": False,
          "closed_period": "2026-02"}],
        reason="account closed",
    )
    _use_live_sqlite(monkeypatch, database)

    historical = read_asset_snapshot("2026", "02")
    future_template = read_asset_snapshot("2026", "03")

    assert set(historical["account"]) == {"Закрытый счёт", "Открытый счёт"}
    assert future_template["account"].tolist() == ["Открытый счёт"]
