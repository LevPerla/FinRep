from pathlib import Path

from src.dashboard.app import (
    _kaspi_import_column_defs,
    _merge_input_grid_rows,
    _transaction_input_layout,
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


def test_category_column_has_multi_cell_selection_rule():
    category_column = next(
        column for column in _kaspi_import_column_defs() if column["field"] == "category"
    )

    assert category_column["field"] == "category"
    assert "finrep-category-selected" in category_column["cellClassRules"]
    assert category_column["editable"] is True
    assert category_column["context"]["neutralCategories"] == ["Внутренний перевод"]
    assert "sort" not in category_column


def test_income_category_highlight_uses_current_registry():
    category_column = next(
        column for column in _kaspi_import_column_defs() if column["field"] == "category"
    )

    assert "incomeCategories" in category_column["cellClassRules"]["kaspi-category-income"]


def test_action_column_reuses_multi_cell_clipboard():
    action = next(
        column for column in _kaspi_import_column_defs() if column["field"] == "import_action"
    )
    layout = _transaction_input_layout("RUB", "2026", "09", "dark")
    grid = _component(layout, "kaspi-import-grid")

    assert "finrep-action-selected" in action["cellClassRules"]
    assert action["cellEditorParams"]["values"] == ["import", "skip"]
    assert grid.eventListeners["cellClicked"] == ["finrepInputCellClicked(params)"]


def test_import_grid_hides_bank_status_and_has_no_sort_priority_numbers():
    columns = _kaspi_import_column_defs()
    date_column = next(column for column in columns if column["field"] == "date")
    bank_status_column = next(
        column for column in columns if column["field"] == "bank_status")

    assert "sort" not in date_column
    assert "sortIndex" not in date_column
    assert bank_status_column["hide"] is True


def test_unified_grid_shows_source_selection_and_manual_editors():
    columns = _kaspi_import_column_defs()
    source = next(column for column in columns if column["field"] == "source")
    date = next(column for column in columns if column["field"] == "date")
    amount = next(column for column in columns if column["field"] == "amount")

    assert source["checkboxSelection"] is True
    assert "manual_grid" in source["valueFormatter"]["function"]
    assert date["editable"] == {"function": "params.data.source == 'manual_grid'"}
    assert amount["editable"] == {"function": "params.data.source == 'manual_grid'"}
    assert amount["cellDataType"] == "text"
    assert amount["cellEditor"] == "agTextCellEditor"
    assert amount["context"] == next(
        column for column in columns if column["field"] == "category"
    )["context"]


def test_pdf_rows_append_without_replacing_unsaved_manual_rows():
    manual = {"source": "manual_grid", "source_id": "manual-1", "comment": "Manual"}
    pdf = {"source": "kaspi_pdf", "source_id": "pdf-1", "comment": "PDF"}

    merged = _merge_input_grid_rows([manual], [pdf, pdf])

    assert [row["comment"] for row in merged] == ["Manual", "PDF"]


def test_import_amount_column_displays_bank_direction_as_sign():
    amount_column = next(
        column for column in _kaspi_import_column_defs() if column["field"] == "amount"
    )

    formatter = amount_column["valueFormatter"]["function"]
    assert "direction == 'credit'" in formatter
    assert "'+ '" in formatter
    assert "direction == 'debit'" in formatter
    assert "'− '" in formatter


def test_category_clipboard_uses_native_events_without_permission_api(monkeypatch):
    monkeypatch.setattr(
        "src.dashboard.app._transaction_category_options",
        lambda *_: [{"label": "Прочее", "value": "Прочее"}],
    )
    layout = _transaction_input_layout("RUB", "2026", "09", "dark")
    grid = _component(layout, "kaspi-import-grid")
    script = (
        Path(__file__).resolve().parents[1] / "assets" / "dashAgGridFunctions.js"
    ).read_text(encoding="utf-8")

    assert "cellKeyDown" not in grid.eventListeners
    assert "navigator.clipboard" not in script
    assert 'addEventListener("copy"' in script
    assert 'addEventListener("paste"' in script
    assert "event.clipboardData" in script
    assert "finrepCategoryCellChanged(params)" in grid.eventListeners[
        "cellValueChanged"]
    assert "neutralCategories" in script
    assert 'setDataValue("import_action", decision[0])' in script
    assert "finrepInputCellChanged" in script
    assert 'params.data.source !== "manual_grid"' in script
    assert "colDef: params.colDef" in script
    assert 'params.node.setDataValue("category", "")' in script
    assert 'raw.replace(/^[+-]/, "")' in script
    assert "params.data.amount = normalizedAmount" in script
