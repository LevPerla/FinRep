from datetime import date

from src import config
from src.data.get import clear_data_cache
from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    asset_accounts,
    initialize_database,
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
    assert row["liquidity_choice"] == "__automatic__"
    assert row["liquidity_class_id"] == ""
    assert row["liquidity_source"] == "unclassified"
    assert row["Включать в капитал"] is True
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
    liquidity_column = next(
        column for column in grid.columnDefs if column["field"] == "liquidity_choice")
    assert liquidity_column["cellEditorParams"]["values"] == [
        "__automatic__", "A1", "A2", "A3", "A4"]
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
            "liquidity_choice": "A2",
            "liquidity_class_id": "",
            "liquidity_source": "unclassified",
            "Включать в капитал": False,
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
    assert account["liquidity_class_override_id"] == "A2"
    assert account["liquidity_class_id"] == "A2"
    assert account["liquidity_source"] == "manual"
    assert account["include_in_capital"] == 0
