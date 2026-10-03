import os
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import pytest

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src import config
from src.dashboard.main_data import (
    DashboardDataset,
    _inflation_rate_data,
    _inflation_rate_figure,
    _real_asset_capital_data,
)
from src.data.sqlite_store import initialize_database, save_cpi_observations


def _seed_cpi(database, observations, currency="RUB"):
    save_cpi_observations(
        database,
        currency=currency,
        observations=observations,
        source_version="test-release",
        payload_sha256="a" * 64,
        fetched_at="2026-04-01T00:00:00Z",
        published_on="2026-04-01",
    )


def test_real_asset_capital_uses_selected_base_month_and_leaves_gaps(
        tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    _seed_cpi(database, [
        {"period": "2026-01", "index_value": "100"},
        {"period": "2026-03", "index_value": "120"},
    ])
    monkeypatch.setattr(config, "active_database_path", lambda: database)
    monkeypatch.setattr(config, "use_sqlite_storage", lambda: True)
    balance = pd.DataFrame(
        {"Капитал по активам": [Decimal("5000000"), Decimal("5200000"), Decimal("5500000")]},
        index=pd.to_datetime(["2026-01-31", "2026-02-28", "2026-03-31"]),
    )

    result = _real_asset_capital_data(balance, "RUB", "2026-03")

    assert result.attrs["base_period"] == "2026-03"
    assert result.attrs["status"] == "partial"
    assert result.attrs["missing_periods"] == ["2026-02"]
    assert result.loc[0, "Реальная стоимость"] == 6000000
    assert pd.isna(result.loc[1, "Реальная стоимость"])
    assert result.loc[2, "Реальная стоимость"] == 5500000


def test_inflation_chart_uses_year_over_year_rate_and_keeps_missing_months(
        tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    _seed_cpi(database, [
        {"period": "2025-01", "index_value": "100"},
        {"period": "2025-03", "index_value": "125"},
        {"period": "2026-01", "index_value": "110"},
        {"period": "2026-03", "index_value": "150"},
    ])
    _seed_cpi(database, [
        {"period": "2025-01", "index_value": "200"},
        {"period": "2026-01", "index_value": "210"},
    ], currency="USD")
    monkeypatch.setattr(config, "active_database_path", lambda: database)
    monkeypatch.setattr(config, "use_sqlite_storage", lambda: True)

    data = _inflation_rate_data().set_index("Дата")
    assert data.loc["2026-01-01", "RUB"] == pytest.approx(10)
    assert data.loc["2026-01-01", "USD"] == pytest.approx(5)
    assert pd.isna(data.loc["2026-02-01", "RUB"])
    assert data.loc["2026-03-01", "RUB"] == pytest.approx(20)

    figure = _inflation_rate_figure(data.reset_index())
    assert figure.data[0].connectgaps is False
    assert {trace.name for trace in figure.data} == {"RUB", "USD"}
    assert list(figure.data[0].x)[0] == pd.Timestamp("2026-01-01")
    assert figure.layout.xaxis.rangeslider.visible is True
    assert figure.layout.yaxis.ticksuffix == "%"


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


def test_cpi_base_filter_is_labeled_inside_its_chart(monkeypatch):
    from src.dashboard import app as app_module

    monkeypatch.setattr(app_module, "_cpi_period_options", lambda _currency: [
        {"label": "2026-03", "value": "2026-03"},
        {"label": "2026-02", "value": "2026-02"},
    ])
    data = pd.DataFrame({
        "Дата": pd.to_datetime(["2026-03-31"]),
        "Номинальная стоимость": [100],
        "Реальная стоимость": [100],
    })
    data.attrs["base_period"] = "2026-03"
    dataset = DashboardDataset(
        id="real_asset_capital",
        title="Покупательная способность активов",
        dataframe=data,
        figure=go.Figure(),
    )

    section = app_module._real_asset_capital_section(
        dataset, currency="RUB", theme="dark")

    control = _component(section, "real-asset-cpi-control")
    dropdown = _component(control, "cpi-base-period-chart")
    assert section.children[1] is control
    assert dropdown.value == "2026-03"
    assert dropdown.options[0]["value"] == "2026-03"
    assert "Базовый месяц цен" in str(control.children[0].children[0])
    assert "покупательную способность" in str(control.children[1])
