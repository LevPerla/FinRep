from datetime import date

from src import config
from src.dashboard import main_data
from src.data.asset_freshness import evaluate_asset_freshness, freshness_label
from src.data import sqlite_store


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


def test_archived_account_is_not_checked_for_freshness():
    account = _account("deposit", "2020-01")
    account.update({"active": 0, "closed_period": "2020-01"})

    result = evaluate_asset_freshness([account], as_of=date(2026, 10, 3))

    evaluated = result["accounts"][0]
    assert evaluated["freshness_status"] == "archived"
    assert evaluated["valuation_age_days"] is None
    assert evaluated["stale_threshold_days"] is None
    assert result["stale_count"] == 0
    assert result["has_warning"] is False
    assert freshness_label(evaluated) == "В архиве · 2020-01"


def test_dashboard_freshness_uses_accounts_from_latest_complete_snapshot(monkeypatch):
    accounts = [
        _account("deposit", "2022-09"),
        _account("cash_account", "2026-10"),
    ]
    monkeypatch.setattr(config, "use_sqlite_storage", lambda: True)
    monkeypatch.setattr(config, "active_database_path", lambda: "unused.sqlite3")
    monkeypatch.setattr(sqlite_store, "asset_accounts", lambda _path: accounts)

    result = main_data._current_asset_freshness()

    assert [row["last_period"] for row in result["accounts"]] == ["2026-10"]


def test_dashboard_freshness_is_relative_to_selected_report_month(monkeypatch):
    accounts = [_account("deposit", "2026-01")]
    monkeypatch.setattr(config, "use_sqlite_storage", lambda: True)
    monkeypatch.setattr(config, "active_database_path", lambda: "unused.sqlite3")
    monkeypatch.setattr(sqlite_store, "asset_accounts", lambda _path: accounts)

    result = main_data._current_asset_freshness("2026", "02")

    assert result["as_of"] == "2026-02-28"
    assert result["stale_count"] == 0
    assert result["has_warning"] is False
