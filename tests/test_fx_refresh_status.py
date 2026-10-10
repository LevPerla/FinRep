import os
import importlib

import pandas as pd

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.main_data import DashboardDataset

app_module = importlib.import_module("src.dashboard.app")


def test_fx_refresh_keeps_dashboard_offline_until_background_result(monkeypatch):
    calls = []

    def build(currency, fx_network_enabled=False):
        calls.append((currency, fx_network_enabled))
        return {"expenses_empty": DashboardDataset(
            id="expenses_empty", title="Нет расходных операций", dataframe=pd.DataFrame()
        )}

    monkeypatch.setattr(app_module, "build_expense_dashboard_data", build)
    monkeypatch.setattr(app_module, "clear_data_cache", lambda: None)
    monkeypatch.setattr(app_module, "clear_table_cache", lambda: None)
    monkeypatch.setattr(app_module, "clear_main_dashboard_cache", lambda: None)
    monkeypatch.setattr(app_module, "_start_reference_refresh",
                        lambda **kwargs: calls.append(kwargs) or True)
    app = app_module.create_app()
    client = app.server.test_client()
    assert client.post("/login", data={"data_mode": "live", "password": "test-password"}).status_code in (302, 303)

    start_output = "..dashboard-refresh-token.data...reference-refresh-poll.disabled.."
    start = app.callback_map[start_output]
    response = client.post("/_dash-update-component", json={
        "output": start_output,
        "outputs": [
            {"id": "dashboard-refresh-token", "property": "data"},
            {"id": "reference-refresh-poll", "property": "disabled"},
        ],
        "inputs": [dict(item, value=value) for item, value in zip(start["inputs"], [None, 1])],
        "state": [dict(start["state"][0], value=0)],
        "changedPropIds": ["refresh-fx-rates.n_clicks"],
    })
    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["dashboard-refresh-token"]["data"] == 1
    assert result["reference-refresh-poll"]["disabled"] is False
    assert calls == [{"force_fx": True, "include_cpi": False}]

    render_output = "..dashboard-content.children...fx-refresh-result.data.."
    render = app.callback_map[render_output]
    values = ["RUB", "2026", "09", None, "main", "expenses", "dark", "ru", 1, None, None]
    response = client.post("/_dash-update-component", json={
        "output": render_output,
        "outputs": [
            {"id": "dashboard-content", "property": "children"},
            {"id": "fx-refresh-result", "property": "data"},
        ],
        "inputs": [dict(item, value=value) for item, value in zip(render["inputs"], values)],
        "state": [dict(render["state"][0], value=None)],
        "changedPropIds": ["dashboard-refresh-token.data"],
    })
    assert response.status_code == 200
    assert calls[-1] == ("RUB", False)

    monkeypatch.setattr(app_module, "_take_reference_refresh_result", lambda: (
        True, {"request": "auto", "status": "done"}, app_module.no_update))
    poll_output = next(key for key in app.callback_map
                       if key.startswith("..dashboard-refresh-token.data@")
                       and "reference-refresh-poll.disabled" in key)
    poll = app.callback_map[poll_output]
    response = client.post("/_dash-update-component", json={
        "output": poll_output,
        "outputs": [
            {"id": "dashboard-refresh-token", "property": "data"},
            {"id": "fx-refresh-result", "property": "data"},
            {"id": "cpi-refresh-result", "property": "data"},
            {"id": "reference-refresh-poll", "property": "disabled"},
        ],
        "inputs": [dict(poll["inputs"][0], value=1)],
        "state": [dict(poll["state"][0], value=1)],
        "changedPropIds": ["reference-refresh-poll.n_intervals"],
    })
    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["dashboard-refresh-token"]["data"] == 2
    assert result["fx-refresh-result"]["data"] == {"request": "auto", "status": "done"}
    assert result["reference-refresh-poll"]["disabled"] is True
