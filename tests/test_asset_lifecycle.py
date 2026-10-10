from decimal import Decimal

import pytest

from src.data.get import clear_data_cache
from src.data.sqlite_store import (
    add_asset_account,
    archive_asset_accounts,
    asset_snapshot_month,
    effective_asset_snapshot_month,
    initialize_database,
    replace_asset_snapshot_month,
    set_asset_account_classifications,
    upsert_asset_snapshot,
)
from src.model.create_tables import clear_table_cache, get_asset_capital_by_month


def test_new_accounts_require_type_and_start_at_first_valuation(tmp_path):
    database = tmp_path / "assets.sqlite3"
    initialize_database(database)
    with pytest.raises(ValueError, match="asset type is required"):
        add_asset_account(database, "new", "New")
    with pytest.raises(ValueError, match="asset type is required"):
        replace_asset_snapshot_month(
            database, period="2026-09",
            rows=[{"account": "New", "amount": "5", "currency": "RUB"}],
        )
    add_asset_account(database, "new", "New", asset_type_id="deposit")
    assert effective_asset_snapshot_month(database, "2026-08") == []
    assert effective_asset_snapshot_month(database, "2026-09") == []
    upsert_asset_snapshot(
        database, account_id="new", period="2026-09",
        amount="5", currency="RUB", reason="first valuation",
    )
    assert effective_asset_snapshot_month(database, "2026-08") == []
    assert effective_asset_snapshot_month(database, "2026-09")[0]["carried"] is False
    with pytest.raises(ValueError, match="asset type is required"):
        set_asset_account_classifications(database, [{
            "account_id": "new", "asset_type_id": None,
            "include_in_capital": True,
        }], reason="clear type")


def test_partial_month_carries_each_currency_without_writing_and_manual_save_confirms(tmp_path):
    database = tmp_path / "assets.sqlite3"
    initialize_database(database)
    add_asset_account(database, "cash", "Cash", asset_type_id="cash_account")
    for currency, amount in (("RUB", "100"), ("USD", "20")):
        upsert_asset_snapshot(
            database, account_id="cash", period="2026-09",
            amount=amount, currency=currency, reason="statement",
        )
    upsert_asset_snapshot(
        database, account_id="cash", period="2026-10",
        amount="110", currency="RUB", reason="new statement",
    )
    rows = effective_asset_snapshot_month(database, "2026-10")
    assert [(row["currency_code"], row["amount"], row["source_period"], row["carried"])
            for row in rows] == [
        ("RUB", Decimal("110"), "2026-10", False),
        ("USD", Decimal("20"), "2026-09", True),
    ]
    assert len(asset_snapshot_month(database, "2026-10")) == 1
    replace_asset_snapshot_month(database, period="2026-10", rows=[
        {"account": row["account_name"], "currency": row["currency_code"],
         "amount": row["amount"]} for row in rows
    ])
    assert all(not row["carried"] for row in effective_asset_snapshot_month(database, "2026-10"))


def test_capital_total_keeps_active_account_missing_from_partial_month(tmp_path, monkeypatch):
    database = tmp_path / "assets.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    for account_id, amount in (("cash", "100"), ("deposit", "200")):
        add_asset_account(database, account_id, account_id, asset_type_id="deposit")
        upsert_asset_snapshot(
            database, account_id=account_id, period="2026-09",
            amount=amount, currency="RUB", reason="statement",
        )
    upsert_asset_snapshot(
        database, account_id="cash", period="2026-10",
        amount="110", currency="RUB", reason="partial statement",
    )
    clear_data_cache()
    clear_table_cache()

    capital = get_asset_capital_by_month("RUB")

    assert capital.loc["2026-10-31", "Капитал по активам"] == 310.0
    assert len(asset_snapshot_month(database, "2026-10")) == 1


def test_archiving_requires_saved_zero_in_every_currency_and_stops_carry(tmp_path):
    database = tmp_path / "assets.sqlite3"
    initialize_database(database)
    add_asset_account(database, "cash", "Cash", asset_type_id="cash_account")
    for currency in ("RUB", "USD"):
        upsert_asset_snapshot(
            database, account_id="cash", period="2026-09",
            amount="10", currency=currency, reason="statement",
        )
    with pytest.raises(ValueError, match="save a zero balance"):
        archive_asset_accounts(database, ["Cash"], period="2026-10")
    upsert_asset_snapshot(
        database, account_id="cash", period="2026-10",
        amount="0", currency="RUB", reason="closure",
    )
    with pytest.raises(ValueError, match="USD"):
        archive_asset_accounts(database, ["Cash"], period="2026-10")
    upsert_asset_snapshot(
        database, account_id="cash", period="2026-10",
        amount="0", currency="USD", reason="closure",
    )
    archive_asset_accounts(database, ["Cash"], period="2026-10")
    assert len(effective_asset_snapshot_month(database, "2026-10")) == 2
    assert effective_asset_snapshot_month(database, "2026-11") == []
    with pytest.raises(ValueError, match="closing month balance must be zero"):
        replace_asset_snapshot_month(database, period="2026-10", rows=[
            {"account": "Cash", "currency": "RUB", "amount": "1"},
            {"account": "Cash", "currency": "USD", "amount": "0"},
        ])


def test_first_valuation_cannot_disappear_by_omitting_table_row(tmp_path):
    database = tmp_path / "assets.sqlite3"
    initialize_database(database)
    replace_asset_snapshot_month(database, period="2026-09", rows=[
        {"account": "Cash", "asset_type_id": "cash", "currency": "RUB", "amount": "10"},
    ])
    with pytest.raises(ValueError, match="cannot remove the first valuation"):
        replace_asset_snapshot_month(database, period="2026-09", rows=[])
    assert asset_snapshot_month(database, "2026-09")[0]["amount"] == Decimal("10")
