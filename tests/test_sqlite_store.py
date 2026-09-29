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
    add_asset_account,
    add_asset_snapshot,
    add_cash_transaction,
    add_category,
    asset_snapshots,
    backup_database,
    cash_transactions,
    change_transaction_category,
    connect_database,
    fx_rates,
    initialize_database,
    link_transaction_source,
    register_source_record,
    save_asset_month,
    save_fx_rate,
    save_month,
    saved_asset_months,
    saved_months,
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


def test_v4_schema_is_strict_and_categories_match_contract(tmp_path):
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
    add_asset_account(database, "account-1", asset["Счет"])
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
