import os

import pandas as pd

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src import config
from src.dashboard.app import _month_report_layout
from src.dashboard.main_data import DashboardDataset
from src.dashboard.month_data import build_month_dashboard_data
from src.data.get import clear_data_cache
from src.data.sqlite_store import add_asset_account, initialize_database, upsert_asset_snapshot
from src.data.staging import monthly_transaction_csv_path
from src.model.create_tables import clear_table_cache


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


def test_missing_month_report_uses_direct_save_instruction_in_sqlite(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    dataset = DashboardDataset(
        id="month_empty",
        title="Месяц не сохранён",
        dataframe=pd.DataFrame([
            {"Год": "2026", "Месяц": "09", "Валюта": "RUB"}
        ]),
    )

    layout = _month_report_layout({"month_empty": dataset}, theme="dark")

    assert "сохраните транзакции" in str(layout)
    assert "Preview" not in str(layout)


def test_month_without_saved_transactions_still_shows_carried_assets(tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    add_asset_account(database, "account", "Счёт", asset_type_id="cash_account")
    upsert_asset_snapshot(
        database, account_id="account", period="2026-09",
        amount="100", currency="RUB", reason="first valuation",
    )
    clear_data_cache()
    clear_table_cache()
    try:
        datasets = build_month_dashboard_data("2026", "10", "RUB", fx_network_enabled=False)
        layout = _month_report_layout(datasets, theme="dark")
        assert "month_empty" in datasets
        assert "month_assets" in datasets
        assert "Предварительный итог" in str(datasets["month_assets"].display_dataframe)
        assert "Счёт" in str(datasets["month_assets"].dataframe)
        assert _find_component(layout, "month-empty-state") is not None
        assert "Операции не сохранены за 2026-10" in str(layout)
        assert "Счёт" in str(layout)
    finally:
        clear_data_cache()
        clear_table_cache()
