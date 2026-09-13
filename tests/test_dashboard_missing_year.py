import os

import pandas as pd

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard import year_data
from src.dashboard.app import _year_report_layout


def _find_component(node, component_id: str):
    if getattr(node, "id", None) == component_id:
        return node
    children = getattr(node, "children", None)
    if isinstance(children, (list, tuple)):
        for child in children:
            found = _find_component(child, component_id)
            if found is not None:
                return found
    elif children is not None:
        return _find_component(children, component_id)
    return None


def test_missing_year_returns_explicit_empty_state(monkeypatch):
    balance = pd.DataFrame(
        {"Доход": [100.0], "Расход": [50.0]},
        index=pd.DatetimeIndex(["2026-05-31"], name="Дата"),
    )
    monkeypatch.setattr(year_data, "get_balance_by_month", lambda _currency: balance)

    datasets = year_data.build_year_dashboard_data("2025", "RUB", fx_network_enabled=False)
    layout = _year_report_layout(datasets, theme="dark")

    assert set(datasets) == {"year_empty"}
    empty_state = _find_component(layout, "year-empty-state")
    assert empty_state is not None
    assert _find_component(layout, "year-empty-title").children == "Нет данных за 2025 год"

