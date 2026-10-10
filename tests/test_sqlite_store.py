import csv
from decimal import Decimal
import hashlib
from pathlib import Path
import sqlite3

from flask import Flask, session
import pytest

from src import config
from src.data.sqlite_store import (
    SCHEMA_VERSION,
    StorageRevisionConflict,
    add_asset_account,
    add_asset_snapshot,
    add_cash_transaction,
    add_category,
    append_cash_drafts,
    annual_goals,
    archive_asset_accounts,
    asset_accounts,
    asset_snapshot_month,
    asset_snapshots,
    asset_types,
    backup_database,
    cash_transactions,
    categories,
    change_transaction_category,
    connect_database,
    create_category,
    create_transaction_draft,
    create_debt_record,
    fx_rates,
    initialize_database,
    liquidity_classes,
    link_transaction_source,
    publish_cash_drafts,
    publish_domain_drafts,
    publish_transaction_draft_preview,
    remove_transaction_drafts,
    record_crypto_refresh,
    record_debt_payment,
    record_investment_trade,
    replace_asset_snapshot_month,
    restore_asset_accounts_to_snapshot,
    rename_category,
    register_source_record,
    save_asset_month,
    save_fx_rate,
    save_month,
    saved_asset_months,
    saved_months,
    set_asset_account_classification,
    set_asset_account_classifications,
    set_category_active,
    transaction_drafts_snapshot,
    update_cash_drafts,
    upsert_annual_goal,
    upsert_crypto_wallet,
    void_cash_transaction,
)


def _add_transaction(database, transaction_id, direction, category, amount="1.00"):
    add_cash_transaction(
        database,
        transaction_id=transaction_id,
        occurred_on="2026-01-02",
        flow_direction=direction,
        category_id=category,
        amount=amount,
        currency="RUB",
    )


def test_v8_schema_is_strict_and_categories_match_contract(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    initialize_database(database)
    with connect_database(database) as connection:
        categories = connection.execute(
            "SELECT direction, name_ru FROM categories WHERE active = 1"
        ).fetchall()
        strict = {
            row["name"]: row["strict"]
            for row in connection.execute("PRAGMA table_list")
            if row["type"] == "table" and not row["name"].startswith("sqlite_")
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    names = {
        direction: {row["name_ru"] for row in categories if row["direction"] == direction}
        for direction in ("income", "expense")
    }
    assert names["income"] == {"Зарплата", "Проценты", "Инвест доход", "Прочие доходы"}
    assert len(names["expense"]) == 10
    assert strict and set(strict.values()) == {1}


def test_asset_account_classification_preserves_history_and_controls_capital(tmp_path):
    from src.data.get import _get_assets_sqlite_cached

    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(
        database, "cash-1", "Основной счёт", asset_type_id="cash_account")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="cash-1",
        period="2026-09", amount="123.45", currency="RUB")

    assert {row["id"] for row in asset_types(database)} == {
        "cash", "cash_account", "deposit", "bond", "equity", "fund", "crypto",
        "real_estate", "other",
    }
    assert [row["id"] for row in liquidity_classes(database)] == ["A1", "A2", "A3", "A4"]
    assert asset_accounts(database)[0]["liquidity_class_id"] == "A1"
    assert asset_accounts(database)[0]["liquidity_source"] == "suggested"
    assert len(_get_assets_sqlite_cached(str(database))) >= 1

    set_asset_account_classification(
        database, "cash-1", asset_type_id="deposit", include_in_capital=False,
        reason="Счёт исключён из согласованного капитала")
    _get_assets_sqlite_cached.cache_clear()

    account = asset_accounts(database)[0]
    assert account["asset_type_id"] == "deposit"
    assert account["liquidity_class_override_id"] is None
    assert account["liquidity_class_id"] == "A1"
    assert account["liquidity_source"] == "suggested"
    assert account["include_in_capital"] == 0
    assert [row["id"] for row in asset_snapshots(database)] == ["snapshot-1"]
    assert _get_assets_sqlite_cached(str(database)).empty
    with connect_database(database) as connection:
        event = connection.execute(
            "SELECT entity_type, action FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert tuple(event) == ("asset_account", "classification_changed")

    set_asset_account_classification(
        database, "cash-1", asset_type_id="real_estate", include_in_capital=True,
        reason="Тип уточнён")
    account = asset_accounts(database)[0]
    assert account["liquidity_class_id"] == "A4"
    assert account["liquidity_source"] == "suggested"

    with pytest.raises(ValueError, match="liquidity is determined by asset type"):
        set_asset_account_classification(
            database, "cash-1", asset_type_id="real_estate",
            include_in_capital=True, liquidity_class_override_id="A2",
            reason="Ручная ликвидность запрещена")


def test_v12_upgrade_adds_cash_and_replaces_manual_liquidity_with_type_defaults(
        tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(
        database, "property", "Квартира", asset_type_id="real_estate")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE asset_accounts SET liquidity_class_override_id = 'A2' "
            "WHERE id = 'property'")
        connection.execute(
            "DELETE FROM asset_type_liquidity_defaults "
            "WHERE asset_type_id NOT IN ('cash_account', 'deposit', 'real_estate')")
        connection.execute("DELETE FROM asset_types WHERE id = 'cash'")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 13")
        connection.execute("PRAGMA user_version = 12")

    initialize_database(database)

    account = asset_accounts(database)[0]
    assert account["liquidity_class_override_id"] is None
    assert account["liquidity_class_id"] == "A4"
    assert "cash" in {row["id"] for row in asset_types(database)}
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        defaults = dict(connection.execute(
            "SELECT asset_type_id, liquidity_class_id "
            "FROM asset_type_liquidity_defaults").fetchall())
    assert defaults == {
        "cash": "A1", "cash_account": "A1", "deposit": "A1",
        "bond": "A2", "equity": "A2", "fund": "A2", "crypto": "A2",
        "real_estate": "A4", "other": "A3",
    }


def test_asset_account_classification_batch_is_atomic_and_skips_unchanged_rows(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(database, "account-1", "Счёт 1", asset_type_id="other")
    add_asset_account(database, "account-2", "Счёт 2", asset_type_id="other")

    with pytest.raises(ValueError, match="unknown asset account"):
        set_asset_account_classifications(
            database,
            [
                {"account_id": "account-1", "asset_type_id": "deposit",
                 "include_in_capital": True},
                {"account_id": "missing", "asset_type_id": "other",
                 "include_in_capital": False},
            ],
            reason="bulk edit",
        )
    assert {row["asset_type_id"] for row in asset_accounts(database)} == {"other"}

    first = set_asset_account_classifications(
        database,
        [
            {"account_id": "account-1", "asset_type_id": "deposit",
             "include_in_capital": True},
            {"account_id": "account-2", "asset_type_id": "other",
             "include_in_capital": False},
        ],
        reason="bulk edit",
    )
    second = set_asset_account_classifications(
        database,
        [
            {"account_id": "account-1", "asset_type_id": "deposit",
             "include_in_capital": True},
            {"account_id": "account-2", "asset_type_id": "other",
             "include_in_capital": False},
        ],
        reason="bulk retry",
    )

    assert first == {"submitted": 2, "updated": 2}
    assert second == {"submitted": 2, "updated": 0}
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE entity_type = 'asset_account'"
        ).fetchone()[0] == 2


def test_archived_asset_account_preserves_history_and_blocks_new_snapshots(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(database, "account-1", "Закрытый счёт", asset_type_id="cash_account")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="account-1",
        period="2026-02", amount="0", currency="RUB")

    result = set_asset_account_classifications(
        database,
        [{"account_id": "account-1", "asset_type_id": "cash_account",
          "include_in_capital": True, "active": False,
          "closed_period": "2026-02"}],
        reason="account closed",
    )

    assert result == {"submitted": 1, "updated": 1}
    account = asset_accounts(database)[0]
    assert account["active"] == 0
    assert account["closed_period"] == "2026-02"
    assert [row["period"] for row in asset_snapshots(database)] == ["2026-02"]
    with pytest.raises(sqlite3.IntegrityError, match="closed for this period"):
        add_asset_snapshot(
            database, snapshot_id="snapshot-2", account_id="account-1",
            period="2026-03", amount="90", currency="RUB")
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT action FROM audit_events WHERE entity_id = 'account-1'"
        ).fetchone()[0] == "archived"


def test_asset_account_reopen_is_explicit_and_audited(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(database, "account-1", "Счёт", asset_type_id="cash_account")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="account-1",
        period="2026-02", amount="0", currency="RUB")
    set_asset_account_classifications(
        database,
        [{"account_id": "account-1", "asset_type_id": "cash_account",
          "include_in_capital": True, "active": False,
          "closed_period": "2026-02"}],
        reason="account closed",
    )

    set_asset_account_classifications(
        database,
        [{"account_id": "account-1", "asset_type_id": "cash_account",
          "include_in_capital": True, "active": True,
          "closed_period": None}],
        reason="account reopened",
    )
    add_asset_snapshot(
        database, snapshot_id="snapshot-2", account_id="account-1",
        period="2026-03", amount="90", currency="RUB")

    account = asset_accounts(database)[0]
    assert account["active"] == 1
    assert account["closed_period"] is None
    with connect_database(database) as connection:
        assert [row[0] for row in connection.execute(
            "SELECT action FROM audit_events WHERE entity_id = 'account-1' ORDER BY id"
        ).fetchall()] == ["archived", "reopened"]


def test_archive_and_restore_account_preserve_history_and_copy_latest_values(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(database, "account-1", "Мультивалютный счёт", asset_type_id="cash_account")
    add_asset_snapshot(
        database, snapshot_id="snapshot-rub", account_id="account-1",
        period="2026-02", amount="100", currency="RUB")
    add_asset_snapshot(
        database, snapshot_id="snapshot-usd", account_id="account-1",
        period="2026-02", amount="20", currency="USD")
    add_asset_snapshot(
        database, snapshot_id="closing-rub", account_id="account-1",
        period="2026-03", amount="0", currency="RUB")
    add_asset_snapshot(
        database, snapshot_id="closing-usd", account_id="account-1",
        period="2026-03", amount="0", currency="USD")

    archived = archive_asset_accounts(
        database, ["Мультивалютный счёт"], period="2026-03")

    assert archived == {"submitted": 1, "archived": 1, "period": "2026-03"}
    account = asset_accounts(database)[0]
    assert account["active"] == 0
    assert account["closed_period"] == "2026-03"
    assert len(asset_snapshots(database)) == 4

    restored = restore_asset_accounts_to_snapshot(
        database, ["account-1"], period="2026-04")

    assert restored == {
        "submitted": 1,
        "reopened": 1,
        "inserted": 2,
        "existing": 0,
        "period": "2026-04",
    }
    account = asset_accounts(database)[0]
    assert account["active"] == 1
    assert account["closed_period"] is None
    april = asset_snapshot_month(database, "2026-04")
    assert {(row["currency_code"], row["amount"]) for row in april} == {
        ("RUB", Decimal("0")),
        ("USD", Decimal("0")),
    }


def test_asset_account_cannot_close_before_its_last_snapshot(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(database, "account-1", "Счёт", asset_type_id="cash_account")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="account-1",
        period="2026-03", amount="100", currency="RUB")

    with pytest.raises(ValueError, match="cannot precede the last snapshot"):
        set_asset_account_classifications(
            database,
            [{"account_id": "account-1", "asset_type_id": "cash_account",
              "include_in_capital": True, "active": False,
              "closed_period": "2026-02"}],
            reason="invalid closure",
        )

    assert asset_accounts(database)[0]["active"] == 1


def test_user_category_lifecycle_preserves_historical_assignment(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)

    category_id = create_category(
        database, "Подработка", direction="income", income_class="active")
    add_cash_transaction(
        database,
        transaction_id="income-1",
        occurred_on="2026-01-02",
        flow_direction="income",
        category_id=category_id,
        amount="100.00",
        currency="RUB",
    )
    rename_category(database, category_id, "Фриланс")
    set_category_active(database, category_id, False)

    registry = {row["id"]: row for row in categories(database)}
    assert registry[category_id]["name_ru"] == "Фриланс"
    assert registry[category_id]["active"] == 0
    assert registry[category_id]["transaction_count"] == 1
    assert cash_transactions(database)[0]["category_name_ru"] == "Фриланс"
    with pytest.raises(ValueError, match="active category"):
        add_cash_transaction(
            database,
            transaction_id="income-2",
            occurred_on="2026-01-03",
            flow_direction="income",
            category_id=category_id,
            amount="10.00",
            currency="RUB",
        )

    set_category_active(database, category_id, True)
    assert {row["id"]: row for row in categories(database)}[category_id]["active"] == 1


def test_category_management_rejects_ambiguous_names_and_unsafe_deactivation(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)

    category_id = create_category(
        database, "Подработка", direction="income", income_class="active")
    with pytest.raises(ValueError, match="already exists"):
        create_category(
            database, " подработка ", direction="income", income_class="passive")
    with pytest.raises(ValueError, match="already exists"):
        create_category(database, "ПОДРАБОТКА", direction="expense")
    with pytest.raises(ValueError, match="fallback"):
        set_category_active(database, "income.other", False)

    create_transaction_draft(
        database,
        occurred_on="2026-01-02",
        flow_direction="income",
        category_id=category_id,
        amount="100.00",
        currency="RUB",
        origin_kind="manual",
        origin_key="manual-1",
    )
    with pytest.raises(ValueError, match="open transaction drafts"):
        set_category_active(database, category_id, False)


def test_expense_category_does_not_accept_income_class(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)

    with pytest.raises(ValueError, match="income class"):
        create_category(
            database, "Комиссии", direction="expense", income_class="passive")


def test_cash_draft_publish_is_atomic_and_idempotent(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    draft_id = create_transaction_draft(
        database,
        occurred_on="2026-02-03",
        flow_direction="income",
        category_id="income.salary",
        amount="123.45",
        currency="RUB",
        origin_kind="manual",
        origin_key="request-1",
        comment="Оклад",
        bank_status="posted",
        bank_reference="bank-ref",
        bank_account_id="card-1",
    )
    assert create_transaction_draft(
        database,
        occurred_on="2026-02-03",
        flow_direction="income",
        category_id="income.salary",
        amount="123.45",
        currency="RUB",
        origin_kind="manual",
        origin_key="request-1",
        comment="Оклад",
        bank_status="posted",
        bank_reference="bank-ref",
        bank_account_id="card-1",
    ) == draft_id
    with pytest.raises(ValueError, match="another payload"):
        create_transaction_draft(
            database,
            occurred_on="2026-02-03",
            flow_direction="income",
            category_id="income.salary",
            amount="999",
            currency="RUB",
            origin_kind="manual",
            origin_key="request-1",
        )

    first = publish_cash_drafts(database, draft_ids=[draft_id], operation_key="publish-1")
    second = publish_cash_drafts(database, draft_ids=[draft_id], operation_key="publish-1")
    assert first == second
    assert first["published_rows"] == 1
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM transaction_drafts WHERE id = ?", (draft_id,)
        ).fetchone()[0] == "exported"
        assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 1
        assert tuple(connection.execute("""SELECT bank_reference, bank_account_id
            FROM transaction_drafts WHERE id = ?""", (draft_id,)).fetchone()) == (
                "bank-ref", "card-1")


def test_new_cash_draft_requires_an_active_category(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)

    with pytest.raises(ValueError, match="category direction mismatch"):
        create_transaction_draft(
            database,
            occurred_on="2026-02-03",
            flow_direction="income",
            category_id="income.unknown",
            amount="10",
            currency="RUB",
            origin_kind="manual",
            origin_key="unknown-income",
        )


def test_pending_cash_draft_publish_rolls_back(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    draft_id = create_transaction_draft(
        database,
        occurred_on="2026-02-03",
        flow_direction="expense",
        category_id="expense.food",
        amount="10",
        currency="RUB",
        origin_kind="bank",
        origin_key="pending-1",
        bank_status="pending",
    )
    with pytest.raises(ValueError, match="pending bank"):
        publish_cash_drafts(database, draft_ids=[draft_id], operation_key="publish-pending")
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM transaction_drafts").fetchone()[0] == "draft"
        assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 0


def test_preview_publish_applies_edits_and_mixed_drafts_atomically(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    cash_id = create_transaction_draft(
        database, occurred_on="2026-02-03", flow_direction="expense",
        category_id="expense.food", amount="10", currency="RUB",
        origin_kind="manual", origin_key="cash-1")
    debt = create_debt_record(
        database, kind="receivable", counterparty="Friend", opened_on="2026-02-04",
        principal_amount="50", currency="RUB", operation_key="create-debt-preview")
    drafts, revision = transaction_drafts_snapshot(database)
    by_id = {row["id"]: row for row in drafts}
    rows = [
        {**by_id[cash_id], "occurred_on": "2026-02-05", "amount": "12.25",
         "currency": "RUB", "comment": "edited", "status": "ready",
         "flow_direction": "expense", "category_id": "expense.food"},
        {**by_id[debt["draft_id"]], "amount": "50", "currency": "RUB",
         "domain_action": "receivable_opening", "status": "ready"},
    ]
    first = publish_transaction_draft_preview(
        database, rows=rows, draft_ids=[cash_id, debt["draft_id"]],
        expected_revision=revision, operation_key="publish-mixed-preview")
    second = publish_transaction_draft_preview(
        database, rows=rows, draft_ids=[cash_id, debt["draft_id"]],
        expected_revision=revision, operation_key="publish-mixed-preview")
    assert first == second
    assert first["published_rows"] == 2
    with connect_database(database) as connection:
        cash = connection.execute("SELECT * FROM cash_transactions").fetchone()
        assert (cash["occurred_on"], cash["amount_minor"], cash["comment"]) == (
            "2026-02-05", 1225, "edited")
        assert connection.execute("SELECT count(*) FROM debt_cash_events").fetchone()[0] == 1
        assert {row[0] for row in connection.execute(
            "SELECT status FROM transaction_drafts")} == {"exported"}


def test_preview_publish_rolls_back_edits_when_one_domain_action_is_invalid(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    cash_id = create_transaction_draft(
        database, occurred_on="2026-02-03", flow_direction="expense",
        category_id="expense.food", amount="10", currency="RUB",
        origin_kind="manual", origin_key="cash-rollback")
    debt = create_debt_record(
        database, kind="liability", counterparty="Bank", opened_on="2026-02-04",
        principal_amount="50", currency="RUB", operation_key="create-debt-rollback")
    drafts, revision = transaction_drafts_snapshot(database)
    by_id = {row["id"]: row for row in drafts}
    rows = [
        {**by_id[cash_id], "amount": "99", "currency": "RUB", "status": "ready"},
        {**by_id[debt["draft_id"]], "amount": "50", "currency": "RUB",
         "domain_action": "unsupported", "status": "ready"},
    ]
    with pytest.raises(ValueError, match="unsupported debt"):
        publish_transaction_draft_preview(
            database, rows=rows, draft_ids=[cash_id, debt["draft_id"]],
            expected_revision=revision, operation_key="publish-invalid-preview")
    drafts, _ = transaction_drafts_snapshot(database)
    assert next(row for row in drafts if row["id"] == cash_id)["amount"] == Decimal("10")
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM debt_cash_events").fetchone()[0] == 0


def test_draft_batch_edit_remove_uses_optimistic_revision(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    empty_rows, empty_revision = transaction_drafts_snapshot(database)
    assert empty_rows == []
    appended = append_cash_drafts(
        database,
        expected_revision=empty_revision,
        rows=[
            {"draft_id": "draft-1", "occurred_on": "2026-04-01",
             "flow_direction": "expense", "category_id": "expense.food",
             "amount": "10", "currency": "RUB", "origin_kind": "bank",
             "origin_key": "row-1", "bank_status": "posted"},
            {"draft_id": "draft-2", "occurred_on": "2026-04-02",
             "flow_direction": "income", "category_id": "income.salary",
             "amount": "20", "currency": "RUB", "origin_kind": "bank",
             "origin_key": "row-2", "bank_status": "posted"},
        ],
    )
    assert appended["accepted_rows"] == 2
    with pytest.raises(StorageRevisionConflict):
        append_cash_drafts(
            database,
            expected_revision=empty_revision,
            rows=[{"occurred_on": "2026-04-03", "flow_direction": "expense",
                   "category_id": "expense.food", "amount": "1", "currency": "RUB",
                   "origin_kind": "bank", "origin_key": "row-3"}],
        )
    rows, revision = transaction_drafts_snapshot(database)
    edited = dict(next(row for row in rows if row["id"] == "draft-1"))
    edited.update(amount="11.25", comment="updated")
    next_revision = update_cash_drafts(
        database, rows=[edited], expected_revision=revision)
    changed, changed_revision = transaction_drafts_snapshot(database)
    assert changed_revision == next_revision
    assert next(row for row in changed if row["id"] == "draft-1")["amount"] == Decimal("11.25")
    with pytest.raises(StorageRevisionConflict):
        update_cash_drafts(database, rows=[edited], expected_revision=revision)
    final_revision = remove_transaction_drafts(
        database, draft_ids=["draft-2"], expected_revision=changed_revision)
    final, observed_revision = transaction_drafts_snapshot(database)
    assert observed_revision == final_revision
    assert next(row for row in final if row["id"] == "draft-2")["status"] == "ignored"


def test_asset_month_replace_is_atomic_and_audited(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(database, "cash", "Cash", asset_type_id="cash")
    add_asset_snapshot(
        database, snapshot_id="cash-prior", account_id="cash",
        period="2026-02", amount="4.00", currency="USD",
    )
    first = replace_asset_snapshot_month(
        database,
        period="2026-03",
        rows=[
            {"account": "Card", "asset_type_id": "cash_account", "currency": "RUB", "amount": "100.00"},
            {"account": "Cash", "asset_type_id": "cash", "currency": "USD", "amount": "5.00"},
        ],
    )
    second = replace_asset_snapshot_month(
        database,
        period="2026-03",
        rows=[{"account": "Card", "currency": "RUB", "amount": "125.50"}],
    )
    assert first == {"inserted": 2, "updated": 0, "deleted": 0, "rows": 2}
    assert second == {"inserted": 0, "updated": 1, "deleted": 1, "rows": 1}
    rows = asset_snapshot_month(database, "2026-03")
    assert [(row["account_name"], row["currency_code"], row["amount"])
            for row in rows] == [("Card", "RUB", Decimal("125.50"))]
    with connect_database(database) as connection:
        assert connection.execute(
            "SELECT revision FROM period_states WHERE period = '2026-03' AND dataset = 'asset_snapshots'"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type = 'asset_snapshot'"
        ).fetchone()[0] == 2


def test_asset_month_replace_reuses_migrated_account_identity(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_asset_account(
        database,
        "legacy-account-id",
        "Cash",
        asset_type_id="cash",
    )
    add_asset_snapshot(
        database,
        snapshot_id="legacy-snapshot-id",
        account_id="legacy-account-id",
        period="2026-09",
        amount="5.00",
        currency="USD",
    )

    result = replace_asset_snapshot_month(
        database,
        period="2026-10",
        rows=[{"account": "Cash", "currency": "USD", "amount": "6.00"}],
    )

    assert result == {"inserted": 1, "updated": 0, "deleted": 0, "rows": 1}
    accounts = asset_accounts(database)
    assert [(row["id"], row["name"], row["asset_type_id"], row["snapshot_count"])
            for row in accounts] == [("legacy-account-id", "Cash", "cash", 2)]


def test_annual_goal_upsert_preserves_optional_values(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    upsert_annual_goal(
        database,
        year=2027,
        currency="RUB",
        target_capital="1000000.25",
        target_monthly_income=None,
        target_monthly_expense="50000",
        target_expense_months="300",
        notes="first",
    )
    upsert_annual_goal(
        database,
        year=2027,
        currency="RUB",
        target_capital="1100000.25",
        target_monthly_income="90000",
        target_monthly_expense="50000",
        target_expense_months="240",
        notes="updated",
    )
    rows = annual_goals(database)
    assert len(rows) == 1
    assert rows[0]["target_capital"] == Decimal("1100000.25")
    assert rows[0]["target_monthly_income"] == Decimal("90000.00")
    assert rows[0]["target_monthly_expense"] == Decimal("50000.00")
    assert rows[0]["target_expense_months"] == 240
    assert rows[0]["notes"] == "updated"


@pytest.mark.parametrize("invalid_months", ["0", "-1", "12.5"])
def test_annual_goal_rejects_invalid_expense_month_target(tmp_path, invalid_months):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)

    with pytest.raises(ValueError, match="positive integer"):
        upsert_annual_goal(
            database,
            year=2027,
            currency="RUB",
            target_expense_months=invalid_months,
        )


def test_debt_commands_are_same_currency_atomic_and_prevent_overpayment(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    created = create_debt_record(
        database,
        kind="receivable",
        counterparty="Friend",
        opened_on="2026-01-10",
        principal_amount="100.00",
        currency="RUB",
        operation_key="create-debt-1",
        comment="Personal loan",
    )
    assert create_debt_record(
        database,
        kind="receivable",
        counterparty="Friend",
        opened_on="2026-01-10",
        principal_amount="100.00",
        currency="RUB",
        operation_key="create-debt-1",
        comment="Personal loan",
    ) == created
    first = record_debt_payment(
        database,
        debt_id=created["debt_id"],
        occurred_on="2026-02-01",
        amount="60",
        operation_key="pay-debt-1",
    )
    assert record_debt_payment(
        database,
        debt_id=created["debt_id"],
        occurred_on="2026-02-01",
        amount="60",
        operation_key="pay-debt-1",
    ) == first
    with pytest.raises(ValueError, match="Погашение больше остатка"):
        record_debt_payment(
            database,
            debt_id=created["debt_id"],
            occurred_on="2026-02-02",
            amount="41",
            operation_key="pay-debt-too-much",
        )
    last = record_debt_payment(
        database,
        debt_id=created["debt_id"],
        occurred_on="2026-02-02",
        amount="40",
        operation_key="pay-debt-2",
    )
    assert last["closed"] and last["remaining_minor"] == 0
    published = publish_domain_drafts(
        database,
        draft_ids=[created["draft_id"], first["draft_id"], last["draft_id"]],
        operation_key="publish-debt-events",
    )
    assert publish_domain_drafts(
        database,
        draft_ids=[created["draft_id"], first["draft_id"], last["draft_id"]],
        operation_key="publish-debt-events",
    ) == published
    with connect_database(database) as connection:
        assert connection.execute("SELECT status FROM debts").fetchone()[0] == "closed"
        assert connection.execute("SELECT count(*) FROM debt_payments").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM transaction_drafts").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM debt_cash_events").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 4


def test_investment_trade_writer_is_exact_idempotent_and_prevents_oversell(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    bought = record_investment_trade(
        database,
        occurred_on="2026-01-01",
        operation="buy",
        ticker="ABC",
        asset_type="stocks",
        quantity="1.23456789",
        unit_price="10.005",
        currency="USD",
        fee="0.10",
        operation_key="trade-buy-1",
    )
    assert record_investment_trade(
        database,
        occurred_on="2026-01-01",
        operation="buy",
        ticker="ABC",
        asset_type="stocks",
        quantity="1.23456789",
        unit_price="10.005",
        currency="USD",
        fee="0.10",
        operation_key="trade-buy-1",
    ) == bought
    sold = record_investment_trade(
        database,
        occurred_on="2026-02-01",
        operation="sell",
        ticker="ABC",
        asset_type="stocks",
        quantity="0.23456789",
        unit_price="12.50",
        currency="USD",
        operation_key="trade-sell-1",
    )
    assert sold["trade_id"] != bought["trade_id"]
    with pytest.raises(ValueError, match="exceeds"):
        record_investment_trade(
            database,
            occurred_on="2026-03-01",
            operation="sell",
            ticker="ABC",
            asset_type="stocks",
            quantity="1.00000001",
            unit_price="11",
            currency="USD",
            operation_key="trade-sell-too-much",
        )
    with connect_database(database) as connection:
        rows = connection.execute("""SELECT operation, quantity_text, unit_price_text, fee_minor
            FROM investment_trades ORDER BY occurred_on""").fetchall()
        assert [tuple(row) for row in rows] == [
            ("buy", "1.23456789", "10.005", 10),
            ("sell", "0.23456789", "12.50", 0),
        ]
        assert connection.execute("SELECT count(*) FROM instruments").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 2


def test_crypto_refresh_is_atomic_idempotent_and_preserves_last_success(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    wallet_id = upsert_crypto_wallet(
        database,
        account_label="Cold wallet",
        chain="bitcoin",
        asset_code="BTC",
        address="public-address",
    )
    first = record_crypto_refresh(
        database,
        wallet_id=wallet_id,
        fetched_at="2026-03-01T10:00:00Z",
        status="ok",
        operation_key="crypto-refresh-1",
        source="observer",
        balance="0.00123456",
        transactions=[{
            "occurred_on": "2026-02-28",
            "chain_tx_id": "tx-1",
            "operation": "receive",
            "quantity": "0.0013",
            "fee": "0.00001",
            "counterparty": "sender",
        }],
    )
    assert record_crypto_refresh(
        database,
        wallet_id=wallet_id,
        fetched_at="2026-03-01T10:00:00Z",
        status="ok",
        operation_key="crypto-refresh-1",
        source="observer",
        balance="0.00123456",
        transactions=[{
            "occurred_on": "2026-02-28", "chain_tx_id": "tx-1",
            "operation": "receive", "quantity": "0.0013", "fee": "0.00001",
            "counterparty": "sender",
        }],
    ) == first
    failed = record_crypto_refresh(
        database,
        wallet_id=wallet_id,
        fetched_at="2026-03-02T10:00:00Z",
        status="error",
        operation_key="crypto-refresh-2",
        source="observer",
        message="timeout",
    )
    assert failed["status"] == "error" and failed["observation_id"] is None
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM crypto_balance_observations").fetchone()[0] == 1
        assert connection.execute("SELECT quantity_text FROM crypto_balance_observations").fetchone()[0] == "0.00123456"
        assert connection.execute("SELECT count(*) FROM crypto_transactions").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM crypto_refresh_results").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM operation_receipts").fetchone()[0] == 2


def test_sample_money_round_trip_uses_exact_minor_units(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    sample = Path(config.SAMPLE_DATA_PATH)
    with (sample / "transactions_info/2025/2025_01_.csv").open(newline="") as source:
        amount, currency, comment = next(csv.DictReader(source, delimiter=";"))["Доход"].split("|", 2)
    with (sample / "assets_info/2025/2025_01.csv").open(newline="") as source:
        asset = next(csv.DictReader(source, delimiter=";"))
    asset_amount, asset_currency = asset["Сумма"].split("|", 1)

    initialize_database(database)
    add_cash_transaction(
        database, transaction_id="one", occurred_on="2025-01-03", flow_direction="income",
        category_id="income.salary", amount=amount, currency=currency, comment=comment,
    )
    add_cash_transaction(
        database, transaction_id="two", occurred_on="2025-01-03", flow_direction="income",
        category_id="income.salary", amount=amount, currency=currency,
    )
    add_asset_account(database, "account-1", asset["Счет"], asset_type_id="other")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="account-1", period="2025-01",
        amount=asset_amount, currency=asset_currency,
    )
    assert [row["amount"] for row in cash_transactions(database)] == [Decimal(amount), Decimal(amount)]
    assert asset_snapshots(database)[0]["amount"] == Decimal(asset_amount)
    with connect_database(database) as connection:
        row = connection.execute("SELECT * FROM v_monthly_cashflow").fetchone()
        assert row["income_minor"] == int(Decimal(amount) * 100) * 2
        assert row["balance_minor"] == row["income_minor"]
        assert connection.execute("SELECT typeof(amount_minor) FROM cash_transactions").fetchone()[0] == "integer"


def test_direction_is_event_property_and_must_match_category(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    with pytest.raises(ValueError, match="matching direction"):
        _add_transaction(database, "bad", "expense", "income.salary")
    with pytest.raises(ValueError, match="minor-unit precision"):
        _add_transaction(database, "too-precise", "income", "income.salary", "0.001")
    _add_transaction(database, "income", "income", "income.salary", "0.01")
    _add_transaction(database, "expense", "expense", "expense.other", "0.01")
    with connect_database(database, writable=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE categories SET direction = 'expense' WHERE id = 'income.salary'")
    rows = {row["id"]: row for row in cash_transactions(database)}
    assert rows["income"]["signed_amount_minor"] == 1
    assert rows["expense"]["signed_amount_minor"] == -1


def test_category_hierarchy_is_two_levels_and_same_direction(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_category(database, "income.salary.bonus", "Премия", direction="income",
                 income_class="active", parent_id="income.salary")
    with pytest.raises(sqlite3.IntegrityError, match="same direction"):
        add_category(database, "expense.bad", "Ошибка", parent_id="income.salary")
    with pytest.raises(sqlite3.IntegrityError, match="root"):
        add_category(database, "income.level3", "Третий уровень", direction="income",
                     income_class="active", parent_id="income.salary.bonus")


def test_period_revisions_are_independent_and_empty_months_survive(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    save_month(database, "2026-01")
    save_month(database, "2026-01")
    save_asset_month(database, "2026-01")
    save_month(database, "2026-02")
    assert saved_months(database) == ["2026-01", "2026-02"]
    assert saved_asset_months(database) == ["2026-01"]
    with connect_database(database) as connection:
        revisions = dict(connection.execute(
            "SELECT dataset, revision FROM period_states WHERE period = '2026-01'"
        ).fetchall())
    assert revisions == {"cash_transactions": 2, "asset_snapshots": 1}


def test_changes_are_audited_and_void_is_excluded_from_views(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_category(database, "expense.coffee", "Кофе")
    _add_transaction(database, "tx", "expense", "expense.other", "10")
    change_transaction_category(database, "tx", "expense.coffee", reason="reviewed")
    void_cash_transaction(database, "tx", reason="duplicate")
    assert cash_transactions(database) == []
    with connect_database(database) as connection:
        events = connection.execute("SELECT action, reason FROM audit_events ORDER BY id").fetchall()
        assert [tuple(row) for row in events] == [
            ("category_changed", "reviewed"), ("voided", "duplicate")
        ]
    with connect_database(database, writable=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_events")


def test_source_lineage_is_idempotent_and_many_to_many(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    digest = hashlib.sha256(b"document").hexdigest()
    first = register_source_record(
        database, source_kind="bank_pdf", document_hash=digest, parser_version="1",
        record_key="row-1", payload_hash=hashlib.sha256(b"row").hexdigest(),
    )
    assert register_source_record(
        database, source_kind="bank_pdf", document_hash=digest, parser_version="1",
        record_key="row-1", payload_hash=hashlib.sha256(b"row").hexdigest(),
    ) == first
    _add_transaction(database, "part-a", "expense", "expense.food", "6")
    _add_transaction(database, "part-b", "expense", "expense.other", "4")
    link_transaction_source(database, "part-a", first[1], role="split", allocated_amount_minor=600)
    link_transaction_source(database, "part-b", first[1], role="split", allocated_amount_minor=400)
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM source_batches").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM source_records").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM transaction_source_links").fetchone()[0] == 2


def test_fx_observations_keep_history_and_view_selects_latest(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    save_fx_rate(database, rate_date="2026-01-31", currency="RUB", usd_rate="0.012345678901",
                 source="official", fetched_at="2026-02-01T12:00:00Z")
    save_fx_rate(database, rate_date="2026-01-31", currency="RUB", usd_rate="0.0124",
                 source="official", fetched_at="2026-02-02T12:00:00Z")
    assert fx_rates(database)[0]["usd_rate"] == Decimal("0.0124")
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM fx_rate_observations").fetchone()[0] == 2


def test_foreign_keys_and_test_mode_are_enforced(tmp_path, monkeypatch):
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    monkeypatch.setattr(config, "SAMPLE_DATA_PATH", str(sample_dir))
    database = sample_dir / "synthetic.sqlite3"
    live_database = tmp_path / "live.sqlite3"
    initialize_database(database)
    initialize_database(live_database)
    with pytest.raises(sqlite3.IntegrityError):
        add_asset_snapshot(database, snapshot_id="bad", account_id="missing",
                           period="2026-01", amount="1", currency="RUB")
    app = Flask(__name__)
    app.secret_key = "test-only"
    with app.test_request_context("/"):
        session["authenticated"] = True
        session["data_mode"] = "test"
        assert saved_months(database) == []
        with pytest.raises(PermissionError, match="sample database"):
            saved_months(live_database)
        with pytest.raises(PermissionError, match="только на чтение"):
            save_month(database, "2026-01")


def test_sqlite_backup_is_consistent_and_never_overwrites(tmp_path):
    database = tmp_path / "source.sqlite3"
    backup = tmp_path / "snapshots" / "source.sqlite3"
    initialize_database(database)
    _add_transaction(database, "tx", "income", "income.salary", "123.45")
    backup_database(database, backup)
    with connect_database(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT amount_minor FROM cash_transactions").fetchone()[0] == 12345
    with pytest.raises(FileExistsError):
        backup_database(database, backup)
