"""Opt-in category styling regression on synthetic data only."""
import os
from threading import Thread

from dash import html
import dash_ag_grid as dag
from playwright.sync_api import expect, sync_playwright
import pytest
from werkzeug.serving import make_server

from src.dashboard.app import _ag_grid_class_name, _kaspi_import_column_defs
from src.data.sqlite_store import create_category
from test_asset_registry_ui import _app


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [390, 1280])
def test_import_categories_have_color_and_refresh_after_edit(tmp_path, monkeypatch, theme, width):
    database, app, client = _app(tmp_path, monkeypatch)
    create_category(database, "Пользовательский расход", direction="expense")
    expected = {
        "На себя": "expense", "Быт и товары для дома": "expense",
        "Пользовательский расход": "expense", "Зарплата": "income",
        "Пища": "food", "Транспорт": "transport", "Связь": "communication",
        "Прочее": "other", "Сбережения": "saving", "Внутренний перевод": "internal",
    }
    category = next(c for c in _kaspi_import_column_defs() if c["field"] == "category")
    app.layout = html.Div(dag.AgGrid(
        id="colors", columnDefs=[category],
        rowData=[{"category": name, "source": "kaspi_pdf", "direction": "debit", "amount": "-10"}
                 for name in expected],
        dashGridOptions={"rowSelection": "multiple", "stopEditingWhenCellsLoseFocus": True},
        className=_ag_grid_class_name(theme), style={"height": "700px"},
    ), className=f"finrep-theme-{theme}")
    server = make_server("127.0.0.1", 0, app.server, threaded=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=os.environ.get("FINREP_TEST_CHROMIUM"))
            context = browser.new_context(viewport={"width": width, "height": 900})
            context.add_cookies([{"name": "session", "value": client.get_cookie("session").value,
                                 "domain": "127.0.0.1", "path": "/"}])
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.server_port}/")
            for index, (name, color) in enumerate(expected.items()):
                cell = page.locator(f'.ag-row[row-index="{index}"] [col-id="category"]')
                expect(cell).to_have_text(name)
                assert f"kaspi-category-{color}" in cell.get_attribute("class")
                assert cell.evaluate("el => getComputedStyle(el).backgroundColor") != "rgba(0, 0, 0, 0)"
            first = page.locator('.ag-row[row-index="0"] [col-id="category"]')
            first.click()
            assert "kaspi-category-expense" in first.get_attribute("class")
            first.dblclick()
            page.locator('.ag-cell-editor .ag-picker-field-wrapper').click()
            page.get_by_text("Пища", exact=True).last.click()
            page.mouse.click(width - 10, 800)
            expect(first).to_have_text("Пища")
            assert "kaspi-category-food" in first.get_attribute("class")
            assert "kaspi-category-expense" not in first.get_attribute("class")
            assert errors == []
            page.screenshot(path=str(tmp_path / f"category-colors-{theme}-{width}.png"))
            browser.close()
    finally:
        server.shutdown()
        thread.join()
