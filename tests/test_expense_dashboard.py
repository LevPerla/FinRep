import os

import pandas as pd
import pytest

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src import config
from src.dashboard import expense_data
from src.dashboard.app import _datasets_for_tab, _download_filename, _expense_report_layout, create_app
from src.dashboard.export import build_dashboard_url
from src.dashboard.i18n import localize_report_datasets
from src.data import get_finance, proccess
from src.model import create_tables


def _transactions(rows):
    return pd.DataFrame(
        rows, columns=["Дата", "Категория", "Валюта", "Значение"],
    ).assign(Дата=lambda data: pd.to_datetime(data["Дата"]))


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(config, "DEBUG", False)

    def install(rows):
        data = _transactions(rows)
        monkeypatch.setattr(expense_data, "get_transactions", lambda: data)
        return data

    return install


def test_monthly_totals_match_main_report_and_preserve_corrections(source, monkeypatch):
    rows = [("2025-01-01", category, "RUB", 1000) for category in config.NOT_COST_COLS]
    rows += [
        ("2025-01-02", "Еда", "RUB", 100.25),
        ("2025-01-03", "Еда", "RUB", -20.10),
        ("2025-01-03", "Расход", "RUB", 50),
        ("2025-02-01", "Доход", "RUB", 1000),
        ("2025-02-03", "Еда", "RUB", -5),
    ]
    original = source(rows)
    before = original.copy(deep=True)
    monkeypatch.setattr(create_tables, "get_transactions", lambda: original.copy())
    monkeypatch.setattr(create_tables, "_get_asset_capital_by_month_cached", lambda *_: pd.DataFrame())
    main = create_tables._get_balance_by_month_cached.__wrapped__("synthetic", "RUB")
    dataset = expense_data.build_expense_dashboard_data("rub")["expenses_monthly"]
    totals = dataset.dataframe.groupby("Дата")["Расход"].sum()

    assert totals.tolist() == pytest.approx([130.15, -5])
    assert totals.tolist() == pytest.approx(main["Расход"].tolist())
    assert set(dataset.dataframe["Категория"]) == {"Еда", "Расход"}
    assert dataset.figure.layout.barmode == "relative"
    pd.testing.assert_frame_equal(original, before)


def test_absent_month_is_not_zero_but_observed_month_without_expenses_is(source):
    source([
        ("2014-01-08", "Еда", "RUB", 12),
        ("2014-02-08", "Доход", "RUB", 100),
        ("2014-04-08", "Транспорт", "RUB", 5),
    ])
    datasets = expense_data.build_expense_dashboard_data("RUB")
    data = datasets["expenses_monthly"].dataframe.pivot(index="Дата", columns="Категория", values="Расход")

    assert data.loc["2014-02-01"].tolist() == [0, 0]
    assert data.loc["2014-03-01"].isna().all()
    assert data.loc["2014-04-01", "Еда"] == 0
    assert datasets["expenses_missing_months"].dataframe["Дата"].tolist() == [pd.Timestamp("2014-03-01")]


@pytest.mark.parametrize("rows", [[], [("2025-01-01", "Доход", "RUB", 500)], [("2025-01-01", "Еда", "RUB", 0)]])
def test_no_expense_operations_has_explicit_empty_state(source, rows):
    source(rows)
    datasets = expense_data.build_expense_dashboard_data("RUB")
    assert set(datasets) == {"expenses_empty"}
    layout = _expense_report_layout(datasets, "dark")
    assert layout.children[-1].id == "expenses-empty-state"


@pytest.mark.parametrize("network_enabled", [False, True])
def test_uses_each_historical_rate_rounds_per_operation_and_scopes_network(source, monkeypatch, network_enabled):
    source([
        ("2025-01-01", "Доход", "EUR", 1000),
        ("2025-01-02", "Еда", "USD", 1.0000625),
        ("2025-01-02", "Еда", "USD", 1.0000625),
        ("2025-01-03", "Еда", "USD", 2),
    ])
    calls = []

    def rates(**kwargs):
        calls.append(kwargs)
        assert get_finance._FX_NETWORK_ENABLED.get() is network_enabled
        return pd.DataFrame({"USDRUB=X": [80, 90]}, index=pd.DatetimeIndex(["2025-01-02", "2025-01-03"], name="Дата"))

    monkeypatch.setattr(proccess, "get_rates", rates)
    monkeypatch.setattr(proccess, "get_actual_fx_rate", lambda *_: pytest.fail("Current FX must not be used"))
    previous_mode = get_finance._FX_NETWORK_ENABLED.get()
    data = expense_data.build_expense_dashboard_data("RUB", network_enabled)["expenses_monthly"].dataframe
    assert data["Расход"].tolist() == pytest.approx([340.02])
    assert len(calls) == 1
    assert calls[0]["tickers"] == ["USDRUB=X"]
    assert get_finance._FX_NETWORK_ENABLED.get() is previous_mode


def test_missing_fx_does_not_return_partial_or_zero_result(source, monkeypatch):
    source([("2025-01-02", "Еда", "USD", 1), ("2025-01-02", "Еда", "RUB", 100)])
    monkeypatch.setattr(proccess, "get_rates", lambda **_: pd.DataFrame())
    with pytest.raises(ValueError, match="Нет курса USD → RUB"):
        expense_data.build_expense_dashboard_data("RUB")


@pytest.mark.parametrize("currency", list(config.UNIQUE_TICKERS))
def test_all_supported_currencies_and_twelve_year_history(source, currency):
    dates = pd.date_range("2014-01-01", periods=144, freq="MS")
    source([(date, "Еда", currency, index + 1) for index, date in enumerate(dates)])
    dataset = expense_data.build_expense_dashboard_data(currency)["expenses_monthly"]
    assert len(dataset.dataframe) == 144
    assert dataset.dataframe["Расход"].sum() == 10440
    assert dataset.figure.layout.xaxis.rangeslider.visible is True
    assert dataset.figure.layout.xaxis.range is None
    assert dataset.figure.layout.yaxis.title.text == currency


def test_export_ignores_toolbar_period_and_localization_keeps_raw_values(source):
    source([("2014-01-01", "Еда", "RUB", 20), ("2025-01-01", "Еда", "RUB", 30)])
    datasets = _datasets_for_tab("expenses", "RUB", "2026", "09")
    dataset = datasets["expenses_monthly"]
    assert dataset.dataframe["Расход"].sum() == 50
    assert _download_filename(dataset, "RUB", "expenses", "2026", "09").startswith("expenses_expenses_monthly_RUB_")
    assert "tab=main&section=expenses" in build_dashboard_url("RUB", "expenses", "2026", "09")
    translated = localize_report_datasets(datasets, "en")
    assert translated["expenses_monthly"].title == "Monthly expenses by category"
    pd.testing.assert_frame_equal(translated["expenses_monthly"].dataframe, dataset.dataframe)
    layout = _expense_report_layout(translated, "light", locale="en")
    assert layout.children[0].children == "Expense analytics"
    assert layout.children[2].children[0].startswith("No data for months:")


def test_render_callback_returns_localized_fx_error(source, monkeypatch):
    source([("2025-01-02", "Еда", "USD", 1)])
    monkeypatch.setattr(proccess, "get_rates", lambda **_: pd.DataFrame())
    app = create_app()
    client = app.server.test_client()
    client.post("/login", data={"data_mode": "test"})
    values = ["RUB", "2026", "09", "main", "expenses", "dark", "en", 0, 0, None]
    callback = app.callback_map["dashboard-content.children"]
    response = client.post("/_dash-update-component", json={
        "output": "dashboard-content.children",
        "outputs": {"id": "dashboard-content", "property": "children"},
        "inputs": [dict(item, value=value) for item, value in zip(callback["inputs"], values)],
        "state": [dict(callback["state"][0], value=None)],
        "changedPropIds": ["dashboard-tabs.active_tab"],
    })
    assert response.status_code == 200
    rendered = response.get_json()["response"]["dashboard-content"]["children"]
    assert rendered["props"]["color"] == "danger"
    assert rendered["props"]["children"][0]["props"]["children"] == "Unable to load expense analytics."
