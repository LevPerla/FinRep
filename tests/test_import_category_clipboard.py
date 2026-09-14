from pathlib import Path

from src.dashboard.app import _kaspi_import_column_defs, _transaction_input_layout


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
    category_column = _kaspi_import_column_defs()[0]

    assert category_column["field"] == "category"
    assert "finrep-category-selected" in category_column["cellClassRules"]
    assert category_column["editable"] is True


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
        lambda: [{"label": "Прочее", "value": "Прочее"}],
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
