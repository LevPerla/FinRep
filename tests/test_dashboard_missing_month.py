import os

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src import config
from src.dashboard.app import _month_report_layout
from src.dashboard.month_data import build_month_dashboard_data
from src.data.staging import monthly_transaction_csv_path


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


def test_missing_month_report_is_read_only_and_has_input_route(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    target = monthly_transaction_csv_path("2026", "09")

    datasets = build_month_dashboard_data("2026", "09", "RUB", fx_network_enabled=False)
    layout = _month_report_layout(datasets, theme="dark")

    assert not target.exists()
    assert set(datasets) == {"month_empty"}
    assert _find_component(layout, "month-empty-state") is not None
    action = _find_component(layout, "month-empty-input-link")
    assert action.href == "?currency=RUB&year=2026&month=09&tab=input"

