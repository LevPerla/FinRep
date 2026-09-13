import warnings

import pandas as pd

from src import config
from src.dashboard import year_data
from src.model import create_tables
from src.reports import year_report


def _call_without_callable_warning(function):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = function()

    matching = [
        warning
        for warning in caught
        if "provided callable" in str(warning.message).lower()
    ]
    assert matching == []
    return result


def _year_balance():
    return pd.DataFrame(
        {
            "Доход": [100.0, 200.0, 300.0],
            "Расход": [50.0, 150.0, 250.0],
        },
        index=pd.DatetimeIndex(
            ["2026-01-31", "2026-02-28", "2026-03-31"], name="Дата"
        ),
    )


def test_dashboard_year_statistics_keep_pandas_aggregation_semantics():
    stats = _call_without_callable_warning(
        lambda: year_data._income_cost_stats(_year_balance())
    )

    assert stats["Статистика"].tolist() == [
        "Сумма",
        "Среднее",
        "Медиана",
        "Ст. отклонение",
        "Минимум",
        "Максимум",
    ]
    assert stats["Доход"].tolist() == [600.0, 200.0, 200.0, 100.0, 100.0, 300.0]
    assert stats["Расход"].tolist() == [450.0, 150.0, 150.0, 100.0, 50.0, 250.0]


def test_legacy_year_report_statistics_keep_labels_without_callable_warning():
    stats = _call_without_callable_warning(
        lambda: year_report._crete_inc_cost_stats(_year_balance(), "2026", "RUB")
    )

    assert stats["Статистика"].tolist() == [
        "Сумма",
        "Среднее",
        "Медиана",
        "Ст. отклонение",
        "Минимум",
        "Максимум",
    ]


def test_cashflow_pivots_keep_sums_without_callable_warning(monkeypatch):
    rows = [
        {
            "Дата": pd.Timestamp("2026-01-01"),
            "Категория": category,
            "Валюта": "RUB",
            "Значение": 0.0,
            "Год": "2026",
            "Месяц": "1",
        }
        for category in config.NOT_COST_COLS
    ]
    rows.extend(
        [
            {
                "Дата": pd.Timestamp("2026-01-01"),
                "Категория": "Прочее",
                "Валюта": "RUB",
                "Значение": 10.25,
                "Год": "2026",
                "Месяц": "1",
            },
            {
                "Дата": pd.Timestamp("2026-01-01"),
                "Категория": "Прочее",
                "Валюта": "RUB",
                "Значение": 20.75,
                "Год": "2026",
                "Месяц": "1",
            },
        ]
    )
    transactions = pd.DataFrame(rows)
    monkeypatch.setattr(create_tables, "get_transactions", lambda: transactions.copy())
    monkeypatch.setattr(
        create_tables,
        "_get_asset_capital_by_month_cached",
        lambda *_: pd.DataFrame(columns=["Капитал по активам"]),
    )
    monkeypatch.setattr(config, "DEBUG", True)
    create_tables._get_balance_by_month_cached.cache_clear()
    create_tables._get_month_transactions_cached.cache_clear()

    balance = _call_without_callable_warning(
        lambda: create_tables._get_balance_by_month_cached("synthetic", "RUB")
    )
    month = _call_without_callable_warning(
        lambda: create_tables._get_month_transactions_cached(
            "synthetic", "RUB", "2026", "1"
        )
    )

    assert balance.iloc[0]["Расход"] == 31.0
    assert month.iloc[0]["Прочее"] == 31.0
