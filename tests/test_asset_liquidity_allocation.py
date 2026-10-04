import pytest

from src.dashboard.main_data import _asset_liquidity_allocation_data_cached
from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    initialize_database,
)


def test_asset_liquidity_allocation_uses_effective_class_and_keeps_unassigned(tmp_path):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    accounts = [
        ("cash", "Счёт", "cash_account", "100"),
        ("equity", "Портфель", "equity", "100"),
        ("property", "Квартира", "real_estate", "200"),
        ("unknown", "Другое", "other", "100"),
    ]
    for account_id, name, asset_type_id, amount in accounts:
        add_asset_account(database, account_id, name, asset_type_id=asset_type_id)
        add_asset_snapshot(
            database, snapshot_id=f"snapshot-{account_id}", account_id=account_id,
            period="2026-09", amount=amount, currency="RUB")
    result = _asset_liquidity_allocation_data_cached(str(database), "RUB")

    assert list(result.columns) == ["Дата", "A1", "A2", "A3", "A4"]
    assert result.iloc[0]["A1"] == pytest.approx(20.0)
    assert result.iloc[0]["A2"] == pytest.approx(20.0)
    assert result.iloc[0]["A4"] == pytest.approx(40.0)
    assert result.iloc[0]["A3"] == pytest.approx(20.0)


def test_asset_liquidity_allocation_excludes_assets_outside_capital(tmp_path):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(
        database, "cash", "Счёт", asset_type_id="cash_account",
        include_in_capital=False)
    add_asset_snapshot(
        database, snapshot_id="snapshot-cash", account_id="cash",
        period="2026-09", amount="100", currency="RUB")

    result = _asset_liquidity_allocation_data_cached(str(database), "RUB")

    assert result.empty


def test_asset_liquidity_allocation_does_not_carry_missing_account(tmp_path):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    add_asset_account(database, "deposit", "Депозит", asset_type_id="deposit")
    add_asset_account(database, "property", "Квартира", asset_type_id="real_estate")
    add_asset_snapshot(
        database, snapshot_id="snapshot-deposit", account_id="deposit",
        period="2026-01", amount="100", currency="RUB")
    add_asset_snapshot(
        database, snapshot_id="snapshot-property", account_id="property",
        period="2026-03", amount="100", currency="RUB")

    result = _asset_liquidity_allocation_data_cached(str(database), "RUB")
    latest = result.sort_values("Дата").iloc[-1]

    assert latest["A1"] == pytest.approx(0.0)
    assert latest["A4"] == pytest.approx(100.0)
