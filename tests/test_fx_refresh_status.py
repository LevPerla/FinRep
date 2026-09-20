import os
import importlib

import pandas as pd
import pytest

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.main_data import DashboardDataset

app_module = importlib.import_module("src.dashboard.app")


@pytest.mark.parametrize("fails,expected_status", [(False, "done"), (True, "error")])
def test_fx_refresh_reports_completion_or_error_without_real_network(monkeypatch, fails, expected_status):
    calls = []

    def build(currency, fx_network_enabled=False):
        calls.append((currency, fx_network_enabled))
        if fails:
            raise ValueError("synthetic missing FX rate")
        return {"expenses_empty": DashboardDataset(
            id="expenses_empty", title="Нет расходных операций", dataframe=pd.DataFrame()
        )}

    monkeypatch.setattr(app_module, "build_expense_dashboard_data", build)
    monkeypatch.setattr(app_module, "clear_table_cache", lambda: None)
    monkeypatch.setattr(app_module, "clear_main_dashboard_cache", lambda: None)
    app = app_module.create_app()
    client = app.server.test_client()
    assert client.post("/login", data={"data_mode": "live", "password": "test-password"}).status_code in (302, 303)
    output = "..dashboard-content.children...fx-refresh-result.data.."
    callback = app.callback_map[output]
    values = ["RUB", "2026", "09", "main", "expenses", "dark", "ru", 0, 1, None]
    response = client.post("/_dash-update-component", json={
        "output": output,
        "outputs": [
            {"id": "dashboard-content", "property": "children"},
            {"id": "fx-refresh-result", "property": "data"},
        ],
        "inputs": [dict(item, value=value) for item, value in zip(callback["inputs"], values)],
        "state": [dict(callback["state"][0], value=None)],
        "changedPropIds": ["refresh-fx-rates.n_clicks"],
    })
    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["fx-refresh-result"]["data"] == {"request": 1, "status": expected_status}
    assert calls == [("RUB", True)]
    if fails:
        assert result["dashboard-content"]["children"]["props"]["color"] == "danger"
