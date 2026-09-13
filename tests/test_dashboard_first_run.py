import os

import pandas as pd

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.app import _main_report_layout
from src.dashboard.main_data import DashboardDataset


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


def test_empty_main_report_shows_one_first_run_route_to_input():
    empty_metrics = DashboardDataset(
        id="cockpit_metrics",
        title="Ключевые метрики",
        dataframe=pd.DataFrame(),
    )

    layout = _main_report_layout(
        {"cockpit_metrics": empty_metrics},
        theme="dark",
        currency="EUR",
        year="2026",
        month="09",
    )

    first_run = _find_component(layout, "main-first-run")
    action = _find_component(layout, "main-first-run-input-link")
    mobile_hint = _find_component(layout, "main-first-run-mobile-hint")

    assert first_run is not None
    assert action is not None
    assert action.href == "?currency=EUR&year=2026&month=09&tab=input"
    assert action.children.children == "Перейти к вводу данных"
    assert "Ещё → Ввод данных" in mobile_hint.children
