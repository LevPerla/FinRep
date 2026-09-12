from __future__ import annotations

import pandas as pd

from src.dashboard import main_data
from src.model import create_tables


def _assets(*periods: tuple[str, str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Счет": f"Account {index}",
                "Год": year,
                "Месяц": month,
                "Квартал": "3",
                "Валюта": "EUR",
                "Значение": 100.0,
            }
            for index, (year, month) in enumerate(periods)
        ]
    )


def test_asset_valuation_date_clamps_only_current_month(monkeypatch):
    assets = _assets(("2026", "08"), ("2026", "09"), ("2026", "10"))
    monkeypatch.setattr(
        create_tables,
        "_current_asset_valuation_date",
        lambda: pd.Timestamp("2026-09-12"),
    )

    dates = create_tables.asset_valuation_dates(assets)

    assert dates.dt.strftime("%Y-%m-%d").tolist() == [
        "2026-08-31",
        "2026-09-12",
        "2026-10-31",
    ]


def test_asset_capital_uses_today_for_current_month_fx_but_keeps_month_end_index(monkeypatch):
    assets = _assets(("2026", "09"))
    requested_dates = []
    monkeypatch.setattr(create_tables, "get_assets", lambda: assets)
    monkeypatch.setattr(create_tables, "current_investment_value", lambda *_: 0)
    monkeypatch.setattr(
        create_tables,
        "_current_asset_valuation_date",
        lambda: pd.Timestamp("2026-09-12"),
    )

    def rate(_from_currency, _to_currency, as_of_date):
        requested_dates.append(pd.Timestamp(as_of_date))
        return 90.0

    monkeypatch.setattr(create_tables, "_get_fx_rate_as_of", rate)
    create_tables._get_asset_capital_by_month_cached.cache_clear()

    result = create_tables._get_asset_capital_by_month_cached("synthetic", "RUB")

    assert requested_dates == [pd.Timestamp("2026-09-12")]
    assert result.index.tolist() == [pd.Timestamp("2026-09-30")]
    assert result.iloc[0]["Капитал по активам"] == 9000.0


def test_asset_allocation_uses_today_for_current_month_fx_but_keeps_month_end_axis(monkeypatch):
    assets = _assets(("2026", "09"))
    requested_dates = []
    monkeypatch.setattr(main_data, "get_assets", lambda: assets)
    monkeypatch.setattr(
        create_tables,
        "_current_asset_valuation_date",
        lambda: pd.Timestamp("2026-09-12"),
    )

    def rate(_from_currency, _to_currency, as_of_date):
        requested_dates.append(pd.Timestamp(as_of_date))
        return 90.0

    monkeypatch.setattr(main_data, "_fx_rate_as_of", rate)
    main_data._asset_currency_allocation_data_cached.cache_clear()

    result = main_data._asset_currency_allocation_data_cached("synthetic", "RUB")

    assert requested_dates == [pd.Timestamp("2026-09-12")]
    assert result["Дата"].tolist() == [pd.Timestamp("2026-09-30")]
    assert result["EUR"].tolist() == [100.0]
