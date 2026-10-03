from datetime import date

import pandas as pd

from src.data.asset_freshness import evaluate_asset_freshness, freshness_label
from src.model.create_tables import carry_forward_asset_snapshots


def _account(asset_type_id: str, period: str | None, *, included: bool = True) -> dict:
    return {
        "id": asset_type_id,
        "name": asset_type_id,
        "asset_type_id": asset_type_id,
        "last_period": period,
        "active": 1,
        "include_in_capital": int(included),
    }


def test_cash_snapshot_warns_only_after_35_days():
    on_boundary = evaluate_asset_freshness(
        [_account("cash_account", "2026-08")], as_of=date(2026, 10, 5))
    after_boundary = evaluate_asset_freshness(
        [_account("cash_account", "2026-08")], as_of=date(2026, 10, 6))

    assert on_boundary["accounts"][0]["valuation_age_days"] == 35
    assert on_boundary["accounts"][0]["freshness_status"] == "fresh"
    assert after_boundary["accounts"][0]["freshness_status"] == "stale"
    assert after_boundary["has_warning"] is True


def test_market_and_property_thresholds_are_independent():
    market = evaluate_asset_freshness(
        [_account("equity", "2026-09")], as_of=date(2026, 10, 8))
    property_on_boundary = evaluate_asset_freshness(
        [_account("real_estate", "2025-09")], as_of=date(2026, 9, 30))

    assert market["accounts"][0]["stale_threshold_days"] == 7
    assert market["accounts"][0]["freshness_status"] == "stale"
    assert property_on_boundary["accounts"][0]["valuation_age_days"] == 365
    assert property_on_boundary["accounts"][0]["freshness_status"] == "fresh"


def test_missing_date_is_distinct_and_excluded_asset_does_not_warn_total():
    result = evaluate_asset_freshness(
        [
            _account("deposit", None),
            _account("crypto", "2025-01", included=False),
        ],
        as_of=date(2026, 10, 3),
    )

    assert result["missing_count"] == 1
    assert result["stale_count"] == 0
    assert result["has_warning"] is True
    assert freshness_label(result["accounts"][0]) == "Дата оценки неизвестна"


def test_carried_value_keeps_original_valuation_date():
    assets = pd.DataFrame([
        {
            "Счет": "Депозит", "Валюта": "RUB", "Значение": 100.0,
            "Дата": pd.Timestamp("2026-01-31"),
            "Дата оценки": pd.Timestamp("2026-01-31"),
        },
        {
            "Счет": "Счёт", "Валюта": "RUB", "Значение": 50.0,
            "Дата": pd.Timestamp("2026-03-31"),
            "Дата оценки": pd.Timestamp("2026-03-31"),
        },
    ])

    carried = carry_forward_asset_snapshots(assets)
    deposit = carried[carried["Счет"] == "Депозит"].sort_values("Дата")

    assert deposit["Дата"].dt.strftime("%Y-%m").tolist() == [
        "2026-01", "2026-02", "2026-03"]
    assert deposit["Значение"].tolist() == [100.0, 100.0, 100.0]
    assert deposit["Дата оценки"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-01-31", "2026-01-31", "2026-01-31"]
    assert deposit["Дата FX"].dt.strftime("%Y-%m-%d").tolist() == [
        "2026-01-31", "2026-02-28", "2026-03-31"]
