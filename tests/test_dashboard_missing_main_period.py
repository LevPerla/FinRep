import os

import pandas as pd

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.app import _grid_section, _main_report_layout
from src.dashboard.main_data import DashboardDataset, _cockpit_metrics, _format_cockpit_metrics


MONTHLY_IDS = {
    "monthly_income",
    "monthly_expense",
    "monthly_cash_flow",
    "savings_rate",
    "monthly_fx_revaluation",
}


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


def _balance() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Доход": [100.0, 120.0],
            "Расход": [50.0, 60.0],
            "Дельта": [50.0, 60.0],
            "Капитал": [900.0, 960.0],
            "Капитал по активам": [950.0, 1_000.0],
            "Расхождение с активами": [50.0, 40.0],
            "Валютная переоценка": [10.0, -5.0],
        },
        index=pd.DatetimeIndex(["2026-01-31", "2026-02-28"], name="Дата"),
    )


def test_missing_selected_month_does_not_reuse_latest_month_metrics():
    metrics = _cockpit_metrics(_balance(), "RUB", "2026", "03").set_index("ID")

    assert metrics.attrs["selected_period"] == "2026-03"
    assert metrics.attrs["selected_period_available"] is False
    assert metrics.loc[list(MONTHLY_IDS), "Значение"].isna().all()
    assert metrics.loc[list(MONTHLY_IDS), "Статус"].eq("empty").all()
    assert metrics.loc["capital", "Значение"] == 1_000.0
    assert metrics.loc["runway", "Значение"] > 0
    assert "2026-02" in metrics.loc["capital", "Детали"]
    assert "2026-02" in metrics.loc["asset_gap", "Детали"]


def test_missing_selected_month_keeps_main_layout_and_shows_notice():
    freshness = {
        "has_warning": True,
        "stale_count": 1,
        "missing_count": 0,
        "stale_accounts": ["Депозит"],
        "missing_accounts": [],
    }
    raw_metrics = _cockpit_metrics(
        _balance(), "RUB", "2026", "03", asset_freshness=freshness)
    display_metrics = _format_cockpit_metrics(raw_metrics, "RUB")
    datasets = {
        "cockpit_metrics": DashboardDataset(
            id="cockpit_metrics",
            title="Ключевые метрики",
            dataframe=raw_metrics,
            display_dataframe=display_metrics,
        )
    }
    for dataset_id in (
        "yearly_stats",
        "fx_rates",
        "income_expense",
        "delta",
        "savings_rate",
        "capital",
        "capital_components",
        "inflation_rate",
        "real_asset_capital",
        "capital_change_after_flows",
        "fx_revaluation",
        "asset_currency_allocation",
        "asset_liquidity_allocation",
        "fx_changes",
        "top_purchases",
    ):
        datasets[dataset_id] = DashboardDataset(
            id=dataset_id,
            title=dataset_id,
            dataframe=pd.DataFrame(),
        )
    for dataset_id in ("fx_revaluation", "fx_changes"):
        datasets[dataset_id] = DashboardDataset(
            id=dataset_id,
            title=dataset_id,
            dataframe=pd.DataFrame({"Дата": [pd.Timestamp("2026-02-28")]}),
        )

    layout = _main_report_layout(
        datasets,
        theme="dark",
        currency="RUB",
        year="2026",
        month="03",
    )

    notice = _find_component(layout, "main-missing-month-notice")
    assert notice is not None
    assert "2026-03" in str(notice.children)
    freshness_notice = _find_component(layout, "main-asset-freshness-notice")
    assert freshness_notice is not None
    assert "Депозит" in str(freshness_notice.children)
    assert _find_component(layout, "main-metrics-primary") is not None
    assert _find_component(layout, "capital_components-grid") is None
    assert _find_component(layout, "real_asset_capital-graph") is None

    fx_revaluation_index = next(
        index for index, section in enumerate(layout.children)
        if _find_component(section, "fx_revaluation-graph") is not None
    )
    fx_changes_index = next(
        index for index, section in enumerate(layout.children)
        if _find_component(section, "fx_changes-graph") is not None
    )
    assert fx_changes_index == fx_revaluation_index + 1


def test_yearly_totals_table_has_no_height_cap():
    data = pd.DataFrame({"Год": [str(year) for year in range(2016, 2027)]})
    dataset = DashboardDataset(
        id="yearly_stats", title="Итоги по годам", dataframe=data)

    section = _grid_section(dataset, height="none", theme="dark")

    assert section.children[1].style["maxHeight"] == "none"
