import pandas as pd

from src.data.get import clear_data_cache, get_transactions
from src.data.sqlite_store import (
    cash_transactions,
    initialize_database,
    rename_category,
    transaction_drafts_snapshot,
)
from src.data import staging
from src.dashboard.app import (
    _category_input_layout,
    _transaction_category_options,
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


def _use_sqlite(monkeypatch, database):
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    clear_data_cache()


def test_empty_sqlite_shows_all_categories_before_first_transaction(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    _use_sqlite(monkeypatch, database)

    options = _transaction_category_options()
    values = {option["value"] for option in options}
    labels = {option["label"] for option in options}
    assert {
        "Доход · Зарплата",
        "Доход · Проценты",
        "Доход · Инвест доход",
        "Доход · Прочие доходы",
    } <= labels
    assert {
        "income.salary", "income.interest", "income.investment", "income.other"
    } <= values
    assert "Income · Зарплата" in {
        option["label"] for option in _transaction_category_options("en")
    }
    assert len(options) == 14

    layout = _category_input_layout("dark")
    grid = _component(layout, "category-registry-grid")
    assert len(grid.rowData) == 14
    assert {row["Категория"] for row in grid.rowData} == {
        option["label"].split(" · ", 1)[1] for option in options
    }

    english_layout = _category_input_layout("dark", locale="en")
    english_grid = _component(english_layout, "category-registry-grid")
    assert {row["Направление"] for row in english_grid.rowData} == {"Income", "Expenses"}
    assert next(
        column for column in english_grid.columnDefs if column["field"] == "Категория"
    )["headerName"] == "Category"
    assert "New category" in str(english_layout)


def test_income_category_id_survives_draft_preview_rename_and_reload(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    _use_sqlite(monkeypatch, database)
    try:
        staging.append_transaction_draft_rows(pd.DataFrame([{
            "date": "2026-10-01",
            "category": "income.investment",
            "currency": "RUB",
            "amount": "125.50",
            "comment": "Результат сделки",
            "source": "manual",
            "source_id": "manual:investment-income",
            "status": "draft",
        }]))
        drafts, _ = transaction_drafts_snapshot(database)
        assert drafts[0]["category_id"] == "income.investment"
        assert drafts[0]["flow_direction"] == "income"

        rename_category(database, "income.investment", "Результат сделок")
        preview, state = staging.prepare_monthly_transaction_export("2026", "10")
        assert preview["category"].tolist() == ["Результат сделок"]

        staging.export_monthly_transaction_drafts(
            "2026",
            "10",
            preview_rows=preview.to_dict("records"),
            preview_state=state,
        )
        stored = cash_transactions(database)
        assert stored[0]["category_id"] == "income.investment"
        assert stored[0]["category_name_ru"] == "Результат сделок"

        clear_data_cache()
        reloaded = get_transactions()
        assert reloaded.loc[reloaded["Значение"].ne(0), "Категория"].tolist() == [
            "Результат сделок"
        ]
    finally:
        clear_data_cache()
