import pytest

from src.data.sqlite_store import (
    add_asset_account, asset_snapshot_month, connect_database, initialize_database,
    statement_asset_suggestions, upsert_asset_snapshot_batch,
)


def test_statement_mapping_is_scoped_latest_and_ignores_archived_target(tmp_path):
    db = tmp_path / "history.sqlite3"
    initialize_database(db)
    for account in ("first", "second"):
        add_asset_account(db, account, account, asset_type_id="cash_account")
    row = {"account_id": "first", "period": "2026-11", "amount": "10", "currency": "RUB",
           "statement": {"source": "kaspi_pdf", "account_id": "TEST-BANK-ACCOUNT"}}
    assert statement_asset_suggestions(db) == {}
    upsert_asset_snapshot_batch(db, [row], reason="statement closing balance")
    key = ("kaspi_pdf", "TEST-BANK-ACCOUNT", "RUB")
    assert statement_asset_suggestions(db) == {key: "first"}
    upsert_asset_snapshot_batch(db, [{**row, "account_id": "second"}], reason="correct selection")
    assert statement_asset_suggestions(db) == {key: "second"}
    assert ("ozon_pdf", key[1], "RUB") not in statement_asset_suggestions(db)
    assert (key[0], key[1], "USD") not in statement_asset_suggestions(db)
    with connect_database(db, writable=True) as connection:
        connection.execute("UPDATE asset_accounts SET active = 0, closed_period = '2026-11' WHERE id = 'second'")
    assert statement_asset_suggestions(db) == {}


@pytest.mark.parametrize("case", ["invalid_amount", "ambiguous"])
def test_failed_statement_batch_does_not_remember_any_selection(tmp_path, case):
    db = tmp_path / "history.sqlite3"
    initialize_database(db)
    for account in ("first", "second"):
        add_asset_account(db, account, account, asset_type_id="cash_account")
    row = {"account_id": "first", "period": "2026-11", "amount": "10", "currency": "RUB",
           "statement": {"source": "kaspi_pdf", "account_id": "TEST-BANK-ACCOUNT"}}
    other = {**row, "account_id": "second", "amount": "bad" if case == "invalid_amount" else "10"}
    with pytest.raises(ValueError):
        upsert_asset_snapshot_batch(db, [row, other], reason="statement closing balance")
    assert statement_asset_suggestions(db) == {}
    assert asset_snapshot_month(db, "2026-11") == []
