from src.dashboard.app import _kaspi_import_column_defs


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
