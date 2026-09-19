import os

import pandas as pd
import pytest

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src import config
from src.dashboard import income_data
from src.dashboard.app import _datasets_for_tab, _download_filename, _income_report_layout
from src.dashboard.export import build_dashboard_url
from src.dashboard.i18n import localize_report_datasets, localize_export_dataframe
from src.data import get_finance, proccess


@pytest.fixture
def source(monkeypatch):
    monkeypatch.setattr(config, "DEBUG", False)

    def install(rows):
        data = pd.DataFrame(rows, columns=["Дата", "Категория", "Валюта", "Значение", "Комментарий"])
        data["Дата"] = pd.to_datetime(data["Дата"])
        monkeypatch.setattr(income_data, "get_transactions", lambda: data)
        return data

    return install


def test_sources_and_savings_reconcile_without_changing_history(source):
    original = source([
        ("2025-01-03", "Доход", "RUB", 1000, "Зарплата"),
        ("2025-01-04", "Доход", "RUB", 50, "Проценты"),
        ("2025-01-05", "Доход", "RUB", 25, "подарок"),
        ("2025-01-06", "Доход", "RUB", -10, "Премия"),
        ("2025-01-07", "Сбережения", "RUB", 500, "перевод"),
        ("2025-01-08", "Еда", "RUB", 300, ""),
    ])
    before = original.copy(deep=True)
    datasets = income_data.build_income_dashboard_data("RUB")
    sources = datasets["income_sources_monthly"].dataframe.set_index("Источник")["Доход"]
    total = datasets["income_receipts_monthly"].dataframe.iloc[0]

    assert sources.to_dict() == {
        "Зарплата": 990, "Проценты по депозиту": 50, "Источник не определён": 25,
    }
    assert total["Доход"] == 1065
    assert total["Сбережения"] == 500
    assert total["Всего поступлений"] == 1565
    pd.testing.assert_frame_equal(original, before)


def test_missing_month_is_null_but_observed_month_without_receipts_is_zero(source):
    source([
        ("2014-01-01", "Доход", "RUB", 100, "Зарплата"),
        ("2014-02-01", "Еда", "RUB", 5, ""),
        ("2014-04-01", "Сбережения", "RUB", 30, ""),
    ])
    datasets = income_data.build_income_dashboard_data("RUB")
    totals = datasets["income_receipts_monthly"].dataframe.set_index("Дата")
    assert totals.loc["2014-02-01"].tolist() == [0, 0, 0]
    assert totals.loc["2014-03-01"].isna().all()
    assert totals.loc["2014-04-01", "Всего поступлений"] == 30
    assert datasets["income_missing_months"].dataframe["Дата"].tolist() == [pd.Timestamp("2014-03-01")]
    assert _income_report_layout(datasets, "dark").children[2].id == "income-missing-months"


@pytest.mark.parametrize("rows", [[], [("2025-01-01", "Еда", "RUB", 15, "")]])
def test_empty_income_state(source, rows):
    source(rows)
    datasets = income_data.build_income_dashboard_data("RUB")
    assert set(datasets) == {"income_empty"}
    assert _income_report_layout(datasets, "dark").children[-1].id == "income-empty-state"


@pytest.mark.parametrize("network_enabled", [False, True])
def test_historical_fx_is_per_operation_and_network_mode_is_scoped(source, monkeypatch, network_enabled):
    source([
        ("2025-01-02", "Доход", "USD", 1.0000625, "Зарплата"),
        ("2025-01-02", "Доход", "USD", 1.0000625, "Проценты"),
        ("2025-01-03", "Сбережения", "USD", 2, ""),
    ])
    calls = []

    def rates(**kwargs):
        calls.append(kwargs)
        assert get_finance._FX_NETWORK_ENABLED.get() is network_enabled
        return pd.DataFrame({"USDRUB=X": [80, 90]}, index=pd.DatetimeIndex(["2025-01-02", "2025-01-03"], name="Дата"))

    monkeypatch.setattr(proccess, "get_rates", rates)
    monkeypatch.setattr(proccess, "get_actual_fx_rate", lambda *_: pytest.fail("Current FX must not be used"))
    previous = get_finance._FX_NETWORK_ENABLED.get()
    datasets = income_data.build_income_dashboard_data("RUB", network_enabled)
    totals = datasets["income_receipts_monthly"].dataframe
    assert totals["Доход"].sum() == pytest.approx(160.02)
    assert totals["Сбережения"].sum() == pytest.approx(180)
    assert totals["Всего поступлений"].sum() == pytest.approx(340.02)
    assert len(calls) == 1
    assert calls[0]["tickers"] == ["USDRUB=X"]
    assert get_finance._FX_NETWORK_ENABLED.get() is previous


def test_missing_fx_never_returns_partial_total(source, monkeypatch):
    source([
        ("2025-01-02", "Доход", "USD", 1, "Зарплата"),
        ("2025-01-02", "Сбережения", "RUB", 100, ""),
    ])
    monkeypatch.setattr(proccess, "get_rates", lambda **_: pd.DataFrame())
    with pytest.raises(ValueError, match="Нет курса USD → RUB"):
        income_data.build_income_dashboard_data("RUB")


@pytest.mark.parametrize("currency", list(config.UNIQUE_TICKERS))
def test_full_history_and_all_report_currencies(source, currency):
    dates = pd.date_range("2014-01-01", periods=144, freq="MS")
    source([(date, "Доход", currency, index + 1, "Зарплата") for index, date in enumerate(dates)])
    dataset = income_data.build_income_dashboard_data(currency)["income_sources_monthly"]
    assert len(dataset.dataframe) == 144 * 3
    assert dataset.dataframe["Доход"].sum() == 10440
    assert dataset.figure.layout.xaxis.rangeslider.visible is True
    assert dataset.figure.layout.yaxis.title.text == currency


def test_income_export_and_localization_preserve_values(source):
    source([("2025-01-01", "Доход", "RUB", 100, "Зарплата")])
    datasets = _datasets_for_tab("income", "RUB", "2026", "09")
    assert _download_filename(datasets["income_sources_monthly"], "RUB", "income", "2026", "09").startswith("income_")
    assert "tab=main&section=income" in build_dashboard_url("RUB", "income", "2026", "09")
    localized = localize_report_datasets(datasets, "en")
    assert localized["income_sources_monthly"].title == "Monthly income by source"
    assert [trace.name for trace in localized["income_sources_monthly"].figure.data] == [
        "Salary", "Deposit interest", "Unknown source",
    ]
    pd.testing.assert_frame_equal(localized["income_sources_monthly"].dataframe, datasets["income_sources_monthly"].dataframe)
    exported = localize_export_dataframe(datasets["income_sources_monthly"].dataframe, "en")
    assert exported.loc[0, "Source"] == "Salary"
    assert exported.loc[0, "Income"] == 100
