from __future__ import annotations

from src.data.importers import common
from src.data.sqlite_store import connect_database, initialize_database


def _configure_sqlite(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    return database


def test_reviewed_multi_month_import_is_published_without_month_preview(
    tmp_path, monkeypatch
):
    database = _configure_sqlite(tmp_path, monkeypatch)
    preview = common.import_frame_from_rows(
        [
            {
                "date": "2026-09-30",
                "signed_amount": -100,
                "currency": "RUB",
                "details": "Shop September",
            },
            {
                "date": "2026-10-01",
                "signed_amount": -200,
                "currency": "RUB",
                "details": "Shop October",
            },
        ],
        statement_id="two-months",
    )

    first = common.save_import_to_transactions(preview.to_dict("records"))
    second = common.save_import_to_transactions(preview.to_dict("records"))

    assert first["published_rows"] == 2
    assert first["pending_rows"] == 0
    assert second["published_rows"] == 0
    assert second["already_published_rows"] == 2
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 2
        assert connection.execute(
            "SELECT group_concat(period, ',') FROM period_states "
            "WHERE dataset = 'cash_transactions' ORDER BY period"
        ).fetchone()[0] == "2026-09,2026-10"
        assert {
            row[0] for row in connection.execute(
                "SELECT DISTINCT status FROM transaction_drafts"
            )
        } == {"exported"}


def test_pending_import_stays_in_staging(tmp_path, monkeypatch):
    database = _configure_sqlite(tmp_path, monkeypatch)
    preview = common.import_frame_from_rows(
        [{
            "date": "2026-10-02",
            "signed_amount": -50,
            "currency": "RUB",
            "details": "Pending shop",
            "bank_status": "pending",
        }],
        statement_id="pending",
    )

    result = common.save_import_to_transactions(preview.to_dict("records"))

    assert result["published_rows"] == 0
    assert result["pending_rows"] == 1
    assert len(result["pending_keys"]) == 1
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 0
        assert connection.execute(
            "SELECT status FROM transaction_drafts"
        ).fetchone()[0] == "draft"


def test_sqlite_input_layout_replaces_month_preview_with_direct_save(
    tmp_path, monkeypatch
):
    _configure_sqlite(tmp_path, monkeypatch)
    from src.dashboard.app import _transaction_input_layout

    layout = _transaction_input_layout("RUB", "2026", "10", "dark")
    components = {
        getattr(component, "id", None): component for component in layout._traverse()
    }

    assert components["transaction-save-import-button"].children == "Сохранить транзакции"
    assert "ms-auto" in components["transaction-save-import-button"].className
    assert "transaction-confirm-export-button" not in components
    assert "transaction-export-preview-grid" not in components


def test_direct_save_callback_clears_published_rows(tmp_path, monkeypatch):
    database = _configure_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    preview = common.import_frame_from_rows(
        [{
            "date": "2026-10-03",
            "signed_amount": -75,
            "currency": "RUB",
            "details": "Direct callback",
        }],
        statement_id="callback",
    )
    app = create_app()
    client = app.server.test_client()
    client.post(
        "/login",
        data={"password": "synthetic-password", "data_mode": "live"},
    )
    key, callback = next(
        (key, callback)
        for key, callback in app.callback_map.items()
        if callback["inputs"]
        and callback["inputs"][0]["id"] == "transaction-save-import-button"
    )
    values = {
        "transaction-save-import-button": 1,
        "kaspi-import-grid": preview.to_dict("records"),
        "dashboard-locale": "ru",
    }
    response = client.post("/_dash-update-component", json={
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [{**item, "value": values[item["id"]]} for item in callback["inputs"]],
        "state": [{**item, "value": values[item["id"]]} for item in callback["state"]],
        "changedPropIds": ["transaction-save-import-button.n_clicks"],
    })

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["kaspi-import-grid"]["rowData"] == []
    assert result["kaspi-import-message"]["color"] == "success"
    assert "Сохранено транзакций: 1" in result["kaspi-import-message"]["children"]
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 1
