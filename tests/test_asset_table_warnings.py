import warnings

import pandas as pd

from src.model import create_tables


def _asset_rows():
    return pd.DataFrame(
        [
            {
                "Счет": "Bank",
                "Год": "2026",
                "Месяц": "9",
                "Квартал": "3",
                "Валюта": "EUR",
                "Значение": 20.0,
            },
            {
                "Счет": "Cash",
                "Год": "2026",
                "Месяц": "9",
                "Квартал": "3",
                "Валюта": "USD",
                "Значение": 10.0,
            },
        ]
    )


def test_asset_currency_conversion_uses_owned_slice_without_chained_assignment(
    monkeypatch,
):
    monkeypatch.setattr(create_tables, "get_assets", _asset_rows)
    monkeypatch.setattr(create_tables, "current_investment_value", lambda *_: 0)
    monkeypatch.setattr(
        create_tables,
        "_get_fx_rate_as_of",
        lambda source, target, _date: {
            ("EUR", "USD"): 2.0,
            ("USD", "EUR"): 0.5,
        }[(source, target)],
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = create_tables.get_assets_by_currencies("2026", "9")

    copy_warnings = [
        warning
        for warning in caught
        if issubclass(warning.category, pd.errors.SettingWithCopyWarning)
    ]
    assert copy_warnings == []

    total = result.set_index("Счет").loc["Всего"]
    assert total["EUR"] == "25.00€"
    assert total["USD"] == "50.00$"
