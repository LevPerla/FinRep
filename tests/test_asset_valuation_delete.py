import json
from decimal import Decimal

import pytest

from src.data.sqlite_store import (
    add_asset_account, archive_asset_accounts, asset_accounts, asset_snapshot_month,
    connect_database, effective_asset_snapshot_month, initialize_database,
    upsert_asset_snapshot_batch,
)


@pytest.fixture
def balances(tmp_path):
    database = tmp_path / "assets.sqlite3"
    initialize_database(database)
    for account in ("Kaspi example", "MIR example"):
        add_asset_account(database, account, account, asset_type_id="cash_account")
    upsert_asset_snapshot_batch(database, [
        {"account_id": "Kaspi example", "currency": "RUB", "amount": "62", "period": "2026-11"},
        {"account_id": "MIR example", "currency": "RUB", "amount": "62", "period": "2026-11"},
        {"account_id": "Kaspi example", "currency": "KZT", "amount": "280983.46", "period": "2026-11"},
    ], reason="synthetic statement balances")
    return database


def mistaken_row(database):
    return next(row for row in asset_snapshot_month(database, "2026-11")
                if row["account_name"] == "Kaspi example" and row["currency_code"] == "RUB")


def remove(database, row, period="2026-11"):
    from src.data.sqlite_store import delete_asset_valuation
    return delete_asset_valuation(database, snapshot_id=row["id"], period=period,
                                  expected_row_version=row["row_version"], reason="mistaken statement target")


def test_delete_first_mistaken_currency_preserves_other_balances_and_audits(balances):
    row = mistaken_row(balances)
    remove(balances, row)
    expected = {("Kaspi example", "KZT", Decimal("280983.46")), ("MIR example", "RUB", Decimal("62"))}
    for period in ("2026-11", "2026-12"):
        assert {(r["account_name"], r["currency_code"], r["amount"])
                for r in effective_asset_snapshot_month(balances, period)} == expected
    assert all(account["active"] for account in asset_accounts(balances))
    with connect_database(balances) as connection:
        audit = connection.execute("SELECT * FROM audit_events WHERE entity_id = ?", (row["id"],)).fetchone()
        assert audit["action"] == "removed"
        assert json.loads(audit["before_json"])["amount_minor"] == 6200
        assert audit["reason"] == "mistaken statement target"
    with pytest.raises(ValueError, match="changed|измен"):
        remove(balances, row)


def test_delete_current_restores_previous_carry_and_keeps_future(balances):
    row = mistaken_row(balances)
    upsert_asset_snapshot_batch(balances, [
        {"account_id": "Kaspi example", "currency": "RUB", "amount": "10", "period": "2026-10"},
        {"account_id": "Kaspi example", "currency": "RUB", "amount": "20", "period": "2026-12"},
    ], reason="synthetic history")
    remove(balances, row)
    restored = next(r for r in effective_asset_snapshot_month(balances, "2026-11")
                    if r["account_id"] == "Kaspi example" and r["currency_code"] == "RUB")
    assert restored["amount"] == Decimal("10") and restored["source_period"] == "2026-10"
    assert asset_snapshot_month(balances, "2026-12")[0]["amount"] == Decimal("20")


def test_delete_rejects_other_period_stale_version_and_closing_balance(balances):
    row = mistaken_row(balances)
    with pytest.raises(ValueError):
        remove(balances, row, "2026-12")
    upsert_asset_snapshot_batch(balances, [
        {"account_id": "Kaspi example", "currency": "RUB", "amount": "63", "period": "2026-11"},
    ], reason="concurrent correction")
    with pytest.raises(ValueError, match="changed|измен"):
        remove(balances, row)
    assert mistaken_row(balances)["amount"] == Decimal("63")
    upsert_asset_snapshot_batch(balances, [
        {"account_id": "Kaspi example", "currency": currency, "amount": "0", "period": "2026-11"}
        for currency in ("RUB", "KZT")
    ], reason="account closure")
    archive_asset_accounts(balances, ["Kaspi example"], period="2026-11")
    with pytest.raises(ValueError, match="closing|закры"):
        remove(balances, mistaken_row(balances))
    assert len(asset_snapshot_month(balances, "2026-11")) == 3


def test_confirm_one_carried_valuation_and_cancel_it(balances):
    from src.data.sqlite_store import confirm_asset_valuation
    row = mistaken_row(balances)
    result = confirm_asset_valuation(balances, snapshot_id=row["id"], period="2026-12",
                                    expected_row_version=row["row_version"], amount="65")
    saved = asset_snapshot_month(balances, "2026-12")
    assert len(saved) == 1 and saved[0]["amount"] == Decimal("65")
    assert saved[0]["account_id"] == row["account_id"]
    assert result["action"] == "inserted"
    remove(balances, saved[0], "2026-12")
    restored = next(r for r in effective_asset_snapshot_month(balances, "2026-12") if r["id"] == row["id"])
    assert restored["carried"] and restored["amount"] == Decimal("62")


@pytest.mark.parametrize("case", ["same_month", "stale", "newer_source", "already_confirmed", "invalid_amount"])
def test_confirm_carried_rejects_stale_state_without_overwriting(balances, case):
    from src.data.sqlite_store import confirm_asset_valuation
    row = mistaken_row(balances)
    if case in {"stale", "newer_source", "already_confirmed"}:
        period = "2026-11" if case == "stale" else "2026-12"
        upsert_asset_snapshot_batch(balances, [{"account_id": row["account_id"], "currency": "RUB",
                                               "amount": "99", "period": period}], reason="concurrent update")
    period = "2026-11" if case == "same_month" else "2027-01" if case == "newer_source" else "2026-12"
    before = asset_snapshot_month(balances, period)
    with pytest.raises(ValueError):
        confirm_asset_valuation(balances, snapshot_id=row["id"], period=period,
                                expected_row_version=row["row_version"], amount="bad" if case == "invalid_amount" else "62")
    assert asset_snapshot_month(balances, period) == before
