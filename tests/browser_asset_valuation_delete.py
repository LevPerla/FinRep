"""Opt-in UI check using synthetic SQLite only: pytest tests/browser_asset_valuation_delete.py."""
import os
from threading import Thread

from dash import dcc, html
import dash_ag_grid as dag
from playwright.sync_api import sync_playwright
import pytest
from werkzeug.serving import make_server

from src.dashboard.app import _asset_snapshot_input_layout
from src.data.sqlite_store import asset_snapshot_month, upsert_asset_snapshot_batch
from test_asset_registry_ui import _delete_fixture


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("width", [390, 1280])
def test_asset_valuation_confirmation_cancel_and_delete(tmp_path, monkeypatch, theme, width):
    database, app, client, _ = _delete_fixture(tmp_path, monkeypatch)
    upsert_asset_snapshot_batch(database, [{"account_id": "MIR example", "currency": "RUB",
                                           "amount": "50", "period": "2026-10"}], reason="synthetic history")
    app.layout = html.Div([
        dcc.Dropdown(id="dashboard-year", options=["2026"], value="2026"),
        dcc.Dropdown(id="dashboard-month", options=["11"], value="11"),
        dcc.Store(id="dashboard-locale", data="ru"),
        dcc.Store(id="bank-statement-balances", data=[]),
        dag.AgGrid(id="asset-classification-grid", style={"display": "none"}),
        _asset_snapshot_input_layout("2026", "11", theme),
    ], className=f"finrep-theme-{theme}")
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
            page.wait_for_selector("#assets-input-grid .ag-row")
            # Two identical account labels are disambiguated by the known test amount.
            wrong = page.locator('#assets-input-grid .ag-center-cols-container .ag-row').filter(has_text="Kaspi example").filter(has_text="62")
            wrong.locator('[col-id="account"]').click()
            assert wrong.get_attribute("aria-selected") == "true"
            button = page.locator("#assets-remove-valuation-button")
            assert button.is_visible() and button.is_enabled()
            bounds = button.bounding_box()
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
            with page.expect_event("dialog") as first:
                button.click()
            assert "Kaspi example · 62 RUB" in first.value.message
            assert "2026-11" in first.value.message
            first.value.dismiss()
            assert len(asset_snapshot_month(database, "2026-11")) == 3
            with page.expect_event("dialog") as confirmed:
                button.click()
            confirmed.value.accept()
            page.wait_for_function("document.querySelector('#assets-input-message').textContent.includes('Оценка удалена')")
            assert {(r["account_name"], r["currency_code"]) for r in asset_snapshot_month(database, "2026-11")} == {
                ("MIR example", "RUB"), ("Kaspi example", "KZT")}
            assert page.locator('#assets-input-grid .ag-center-cols-container .ag-row').count() == 2
            mir = page.locator('#assets-input-grid .ag-center-cols-container .ag-row').filter(has_text="MIR example")
            mir.locator('[col-id="account"]').click()
            undo = page.get_by_role("button", name="Отменить оценку", exact=True)
            with page.expect_event("dialog") as row_prompt:
                undo.click()
            assert "MIR example · 62 RUB" in row_prompt.value.message
            with page.expect_response(lambda response: response.url.endswith("/_dash-update-component")
                                      and "assets-remove-valuation-confirm.submit_n_clicks" in (response.request.post_data or "")):
                row_prompt.value.accept()
            mir.locator('[col-id="account"]').click()
            confirm = page.get_by_role("button", name="Подтвердить оценку", exact=True)
            assert len(asset_snapshot_month(database, "2026-11")) == 1
            bounds = confirm.bounding_box()
            assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
            assert page.locator('#assets-input-grid button').count() == 0
            confirm.click()
            page.wait_for_function("document.querySelector('#assets-input-message').textContent.includes('Оценка подтверждена')")
            undo.wait_for(state="visible")
            saved = asset_snapshot_month(database, "2026-11")
            assert len(saved) == 2
            assert next(row for row in saved if row["account_name"] == "MIR example")["amount"] == 50
            assert errors == []
            page.screenshot(path=str(tmp_path / f"assets-{theme}-{width}.png"), full_page=True)
            browser.close()
    finally:
        server.shutdown()
        thread.join()
