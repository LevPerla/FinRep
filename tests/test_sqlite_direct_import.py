from __future__ import annotations

import pytest

from src.data.importers import common
from src.data.get import clear_data_cache
from src.data.sqlite_store import (
    add_cash_transaction,
    append_cash_drafts,
    connect_database,
    initialize_database,
)


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


def test_direct_save_rechecks_history_without_requesting_preview(
        tmp_path, monkeypatch):
    database = _configure_sqlite(tmp_path, monkeypatch)
    preview = common.import_frame_from_rows(
        [{
            "date": "2026-10-02",
            "signed_amount": -50,
            "currency": "RUB",
            "details": "Already saved elsewhere",
        }],
        statement_id="stale-history",
    )
    add_cash_transaction(
        database,
        transaction_id="existing-transaction",
        occurred_on="2026-10-02",
        flow_direction="expense",
        category_id="expense.other",
        amount="50",
        currency="RUB",
        comment="Already saved elsewhere",
    )
    clear_data_cache()

    result = common.save_import_to_transactions(preview.to_dict("records"))

    assert result["published_rows"] == 0
    assert result["already_published_rows"] == 1
    assert result["skipped_rows"] == 1
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM cash_transactions").fetchone()[0] == 1

def test_direct_save_uses_current_staging_revision(tmp_path, monkeypatch):
    database = _configure_sqlite(tmp_path, monkeypatch)
    preview = common.import_frame_from_rows(
        [{
            "date": "2026-10-03",
            "signed_amount": -75,
            "currency": "RUB",
            "details": "Fresh direct save",
        }],
        statement_id="stale-revision",
    )
    append_cash_drafts(
        database,
        rows=[{
            "origin_kind": "manual",
            "origin_key": "unrelated-draft",
            "occurred_on": "2026-10-01",
            "flow_direction": "expense",
            "category_id": "expense.other",
            "amount": "1",
            "currency": "RUB",
            "comment": "Unrelated draft",
        }],
    )

    result = common.save_import_to_transactions(preview.to_dict("records"))

    assert result["published_rows"] == 1
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM cash_transactions").fetchone()[0] == 1

def test_neutral_category_is_skipped_even_if_client_action_is_import(
        tmp_path, monkeypatch):
    database = _configure_sqlite(tmp_path, monkeypatch)
    preview = common.import_frame_from_rows(
        [{
            "date": "2026-10-04",
            "signed_amount": -100,
            "currency": "RUB",
            "details": "Unrecognized transfer",
        }],
        statement_id="manual-neutral",
    )
    rows = preview.to_dict("records")
    rows[0].update(
        category="Внутренний перевод", import_action="import", skip_reason="")

    result = common.save_import_to_transactions(rows)

    assert result["published_rows"] == 0
    assert result["skipped_rows"] == 1
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM cash_transactions").fetchone()[0] == 0


def test_manual_grid_row_has_new_identity_without_financial_defaults():
    first = common.new_manual_grid_row()
    second = common.new_manual_grid_row()

    assert first["source"] == common.MANUAL_GRID_SOURCE
    assert first["source_id"] != second["source_id"]
    assert {first[field] for field in ("date", "category", "currency", "amount", "comment", "direction")} == {""}
    assert first["import_action"] == "import"


def test_copied_grid_row_keeps_user_fields_but_gets_new_identity():
    source = common.new_manual_grid_row()
    source.update({
        "date": "2026-10-05",
        "category": "Прочее",
        "currency": "RUB",
        "amount": "125",
        "comment": "Synthetic",
        "direction": "debit",
        "bank_reference": "must-not-copy",
    })

    copied = common.new_manual_grid_row(source)

    assert copied["source_id"] != source["source_id"]
    assert copied["source"] == common.MANUAL_GRID_SOURCE
    assert copied["bank_reference"] == ""
    assert {field: copied[field] for field in ("date", "category", "currency", "amount", "comment", "direction")} == {
        field: source[field] for field in ("date", "category", "currency", "amount", "comment", "direction")
    }


def test_tabular_paste_accepts_optional_header_and_derives_direction():
    rows = common.parse_manual_grid_rows(
        "Дата\tСумма\tВалюта\tКатегория\tКомментарий\n"
        "2026-10-05\t-12 500\tKZT\tПища\tMagnum\n"
        "2026-10-06\t17 709,25\tKZT\tПроценты\tДепозит"
    )

    assert [(row["amount"], row["direction"]) for row in rows] == [
        ("12500", "debit"), ("17709.25", "credit")]
    assert len({row["source_id"] for row in rows}) == 2


def test_tabular_paste_rejects_malformed_batch_before_returning_rows():
    with pytest.raises(ValueError, match="Строка 2"):
        common.parse_manual_grid_rows(
            "2026-10-05\t-100\tRUB\tПрочее\tOk\n"
            "2026-10-06\tbroken\tRUB\tПрочее\tBad"
        )


def test_unified_grid_saves_valid_rows_and_keeps_invalid_rows(
        tmp_path, monkeypatch):
    database = _configure_sqlite(tmp_path, monkeypatch)
    valid = common.new_manual_grid_row()
    valid.update({
        "date": "2026-10-05", "category": "Прочее", "currency": "RUB",
        "amount": "125", "comment": "Synthetic", "direction": "debit",
    })
    invalid = common.new_manual_grid_row()
    invalid.update({
        "date": "2026-10-05", "currency": "RUB", "amount": "25",
        "direction": "debit",
    })
    neutral = common.new_manual_grid_row()
    neutral.update({"category": "Внутренний перевод", "import_action": "skip"})

    result = common.save_input_grid_to_transactions([valid, invalid, neutral])

    assert result["published_rows"] == 1
    assert result["skipped_rows"] == 1
    assert result["invalid_rows"] == 1
    assert result["remaining_rows"][0]["source_id"] == invalid["source_id"]
    assert "выбери категорию" in result["remaining_rows"][0]["validation_error"]
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM cash_transactions").fetchone()[0] == 1

    retry = common.save_input_grid_to_transactions([valid])
    assert retry["published_rows"] == 0
    assert retry["already_published_rows"] == 1
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM cash_transactions").fetchone()[0] == 1


def test_empty_input_grid_save_is_rejected(tmp_path, monkeypatch):
    _configure_sqlite(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="Нет операций для сохранения"):
        common.save_input_grid_to_transactions([])


def test_manual_grid_save_derives_expense_direction_from_negative_amount(
        tmp_path, monkeypatch):
    database = _configure_sqlite(tmp_path, monkeypatch)
    row = common.new_manual_grid_row()
    row.update({
        "date": "2026-10-07", "category": "Прочее", "currency": "RUB",
        "amount": "-75", "comment": "Signed input", "direction": "",
    })

    result = common.save_input_grid_to_transactions([row])

    assert result["published_rows"] == 1
    with connect_database(database) as connection:
        stored = connection.execute(
            "SELECT flow_direction, amount_minor FROM cash_transactions").fetchone()
    assert tuple(stored) == ("expense", 7500)


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
    assert components["transaction-grid-add-button"].children == "Добавить строку"
    assert components["transaction-grid-copy-button"].children == "Копировать строку"
    assert components["transaction-grid-delete-button"].children == "Удалить строку"
    assert components["transaction-grid-paste-button"].children == "Вставить строки"
    assert components["transaction-paste-modal"].is_open is False
    assert layout.children[0].style["display"] == "none"
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


def test_unified_grid_callback_adds_and_copies_rows(tmp_path, monkeypatch):
    _configure_sqlite(tmp_path, monkeypatch)
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

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
        and callback["inputs"][0]["id"] == "transaction-grid-add-button"
    )

    def invoke(trigger, rows, selected=None, paste_text=""):
        input_values = {
            "transaction-grid-add-button": int(trigger == "transaction-grid-add-button"),
            "transaction-grid-copy-button": int(trigger == "transaction-grid-copy-button"),
            "transaction-grid-delete-button": int(trigger == "transaction-grid-delete-button"),
            "transaction-grid-paste-button": int(trigger == "transaction-grid-paste-button"),
            "transaction-paste-apply-button": int(trigger == "transaction-paste-apply-button"),
            "transaction-paste-cancel-button": int(trigger == "transaction-paste-cancel-button"),
        }
        state_values = {
            "kaspi-import-grid": rows,
            "transaction-paste-text": paste_text,
            "dashboard-locale": "ru",
        }
        response = client.post("/_dash-update-component", json={
            "output": key,
            "outputs": [
                {"id": item.component_id, "property": item.component_property}
                for item in callback["output"]
            ],
            "inputs": [
                {**item, "value": input_values[item["id"]]}
                for item in callback["inputs"]
            ],
            "state": [
                {**item, "value": (
                    selected if item["property"] == "selectedRows"
                    else state_values[item["id"]]
                )}
                for item in callback["state"]
            ],
            "changedPropIds": [f"{trigger}.n_clicks"],
        })
        assert response.status_code == 200
        return response.get_json()["response"]

    added = invoke("transaction-grid-add-button", [])
    added_rows = added["kaspi-import-grid"]["rowData"]
    assert len(added_rows) == 1
    assert added_rows[0]["source"] == common.MANUAL_GRID_SOURCE

    source = dict(added_rows[0])
    source.update({
        "date": "2026-10-08", "category": "Прочее", "currency": "RUB",
        "amount": "50", "direction": "debit",
    })
    copied = invoke("transaction-grid-copy-button", [source], selected=[source])
    copied_rows = copied["kaspi-import-grid"]["rowData"]
    assert len(copied_rows) == 2
    assert copied_rows[1]["source_id"] != source["source_id"]
    assert copied_rows[1]["amount"] == "50"

    not_selected = invoke("transaction-grid-delete-button", copied_rows)
    assert not_selected["kaspi-import-message"]["color"] == "warning"
    assert "Выбери одну строку" in not_selected["kaspi-import-message"]["children"]

    deleted = invoke(
        "transaction-grid-delete-button", copied_rows, selected=[copied_rows[0]]
    )
    remaining = deleted["kaspi-import-grid"]["rowData"]
    assert [row["source_id"] for row in remaining] == [copied_rows[1]["source_id"]]
    assert deleted["kaspi-import-message"]["children"] == "Несохранённая строка удалена."
