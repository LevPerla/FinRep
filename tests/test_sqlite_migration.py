from pathlib import Path
import csv
import sqlite3

import pytest

from src import config
from src.data.sqlite_migration import build_manifest, migrate_core_csv
from src.data.sqlite_store import (
    connect_database,
    publish_cash_drafts,
    transaction_drafts_snapshot,
    update_cash_drafts,
)


def test_manifest_is_deterministic_and_covers_every_csv(tmp_path):
    source = Path(config.SAMPLE_DATA_PATH)
    first, first_hash = build_manifest(source)
    second, second_hash = build_manifest(source)
    assert first == second
    assert first_hash == second_hash
    assert len(first) == len(list(source.rglob("*.csv")))
    by_path = {entry.relative_path: entry for entry in first}
    assert by_path["investments/investments.csv"].status == "excluded"
    assert by_path["investments/transactions.csv"].status == "included"
    assert all(entry.status in {"included", "excluded"} for entry in first)


def test_unknown_csv_is_visible_and_blocks_automatic_coverage(tmp_path):
    unknown = tmp_path / "custom.csv"
    unknown.write_text("a;b\n1;2\n", encoding="utf-8")
    entries, _ = build_manifest(tmp_path)
    assert [(item.relative_path, item.status, item.reason) for item in entries] == [
        ("custom.csv", "unknown", "no schema-registry route")
    ]


def test_empty_months_and_identical_files_remain_distinct(tmp_path):
    transaction_header = "Дата;Пища\n"
    for period in ("2026_01", "2026_02"):
        folder = tmp_path / "transactions_info" / period[:4]
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{period}.csv").write_text(transaction_header, encoding="utf-8")
    assets = tmp_path / "assets_info" / "2026"
    assets.mkdir(parents=True)
    (assets / "2026_01.csv").write_text("Счет;Сумма\n", encoding="utf-8")
    target = tmp_path / "target.sqlite3"
    migrate_core_csv(tmp_path, target, tmp_path / "migration.sqlite3")
    with connect_database(target) as connection:
        assert [row[0] for row in connection.execute(
            "SELECT period FROM period_states WHERE dataset = 'cash_transactions' ORDER BY period"
        )] == ["2026-01", "2026-02"]
        assert connection.execute(
            "SELECT period FROM period_states WHERE dataset = 'asset_snapshots'"
        ).fetchone()[0] == "2026-01"


def test_cash_drafts_preserve_direction_category_status_and_source_key(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    path = staging / "transaction_drafts.csv"
    columns = [
        "date", "category", "currency", "amount", "comment", "source", "source_id",
        "direction", "bank_status", "bank_reference", "bank_account_id", "status",
    ]
    rows = [
        ["2026-01-01", "Пища", "RUB", "12.34", "Lunch", "manual", "m1",
         "debit", "", "", "", "ready"],
        ["2026-01-02", "Доход", "USD", "25", "Salary", "bank", "b1",
         "credit", "posted", "ref", "account", "exported"],
        ["2026-01-03", "Доход", "EUR", "30", "Needs review", "manual", "m2",
         "credit", "", "", "", "draft"],
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(columns)
        writer.writerows(rows)
    target = tmp_path / "target.sqlite3"
    summary = migrate_core_csv(tmp_path, target, tmp_path / "migration.sqlite3")
    assert summary.drafts_imported == 3
    assert summary.pending_adapter_files == 0
    with connect_database(target) as connection:
        drafts = connection.execute("""SELECT flow_direction, amount_minor, currency_code,
            category_id, origin_kind, origin_key, bank_status, status
            FROM transaction_drafts ORDER BY occurred_on""").fetchall()
    assert [tuple(row) for row in drafts] == [
        ("expense", 1234, "RUB", "expense.food", "manual", "m1", None, "ready"),
        ("income", 2500, "USD", "income.salary", "bank", "b1", "posted", "exported"),
        ("income", 3000, "EUR", "income.unknown", "manual", "m2", None, "draft"),
    ]

    snapshot, revision = transaction_drafts_snapshot(target)
    unknown = next(row for row in snapshot if row["category_id"] == "income.unknown")
    with pytest.raises(sqlite3.IntegrityError, match="active category"):
        publish_cash_drafts(
            target,
            draft_ids=[unknown["id"]],
            operation_key="publish-unclassified-income",
        )

    reviewed = {**unknown, "category_id": "income.other", "status": "ready"}
    update_cash_drafts(target, rows=[reviewed], expected_revision=revision)
    result = publish_cash_drafts(
        target,
        draft_ids=[unknown["id"]],
        operation_key="publish-reviewed-income",
    )
    assert result["published_rows"] == 1


def test_legacy_debt_exceptions_are_preserved_and_reported(tmp_path):
    debts_dir = tmp_path / "debts"
    debts_dir.mkdir()
    (debts_dir / "debts.csv").write_text(
        "debt_id;type;counterparty;opened_date;principal_amount;principal_currency;"
        "cash_amount;cash_currency;comment;status\n"
        "d1;receivable;Alex;2026-02-01;100;USD;100;USD;;active\n",
        encoding="utf-8",
    )
    (debts_dir / "debt_payments.csv").write_text(
        "payment_id;debt_id;date;amount;cash_amount;cash_currency;comment;status\n"
        "p1;d1;2026-01-01;110;110;USD;;posted\n",
        encoding="utf-8",
    )
    target = tmp_path / "target.sqlite3"
    audit = tmp_path / "migration.sqlite3"
    summary = migrate_core_csv(tmp_path, target, audit)
    assert summary.debts_imported == 1
    assert summary.debt_payments_imported == 1
    assert summary.debt_issues == 2
    migration = sqlite3.connect(audit)
    try:
        assert {row[0] for row in migration.execute(
            "SELECT code FROM migration_issues WHERE blocking = 1"
        )} == {"debt_overpayment", "payment_before_opening"}
    finally:
        migration.close()


def test_crypto_files_preserve_public_wallet_observations_and_refresh_failures(tmp_path):
    investments = tmp_path / "investments"
    investments.mkdir()
    address = "bc1qexamplepublicaddress"
    (investments / "crypto_wallets.csv").write_text(
        "account;chain;asset;address;token_contract;enabled;label\n"
        f"Cold wallet;bitcoin;BTC;{address};;1;Main\n", encoding="utf-8")
    (investments / "crypto_balances.csv").write_text(
        "fetched_at;account;chain;asset;address;balance;source\n"
        f"2026-01-02T00:00:00Z;Cold wallet;bitcoin;BTC;{address};0.00123456;observer\n",
        encoding="utf-8")
    (investments / "crypto_transactions.csv").write_text(
        "date;account;chain;asset;address;tx_id;operation;quantity;fee;counterparty;source;comment\n"
        f"2026-01-01;Cold wallet;bitcoin;BTC;{address};tx-1;receive;0.0013;0.00001;sender;observer;demo\n",
        encoding="utf-8")
    (investments / "crypto_refresh_status.csv").write_text(
        "fetched_at;row_number;account;chain;asset;address;status;message\n"
        f"2026-01-02T00:00:00Z;2;Cold wallet;bitcoin;BTC;{address};ok;\n"
        "2026-01-02T00:00:00Z;3;Missing;ton;TON;public-ton-address;error;timeout\n",
        encoding="utf-8")
    target = tmp_path / "target.sqlite3"
    summary = migrate_core_csv(tmp_path, target, tmp_path / "migration.sqlite3")
    assert summary.pending_adapter_files == 0
    assert (summary.crypto_wallets_imported, summary.crypto_balances_imported,
            summary.crypto_transactions_imported, summary.crypto_refresh_results_imported,
            summary.crypto_issues) == (1, 1, 1, 2, 0)
    with connect_database(target) as connection:
        assert connection.execute(
            "SELECT quantity_text FROM crypto_balance_observations").fetchone()[0] == "0.00123456"
        assert tuple(connection.execute(
            "SELECT quantity_text, fee_text FROM crypto_transactions").fetchone()) == (
                "0.0013", "0.00001")
        failed = connection.execute("""SELECT wallet_id, observed_chain, status, message
            FROM crypto_refresh_results WHERE status = 'error'""").fetchone()
        assert tuple(failed) == (None, "ton", "error", "timeout")


def test_legacy_investments_create_instruments_before_prices(tmp_path):
    investments = tmp_path / "investments"
    investments.mkdir()
    (investments / "investments.csv").write_text(
        "Тип_транзакции;Актив;Тикер;Количество;Дата;Цена\n"
        "Покупка;Акции;ABC;1.25;01.02.2026;10.5|USD\n",
        encoding="utf-8",
    )
    (investments / "price_cache.csv").write_text(
        "date;ticker;price;currency;source;fetched_at\n"
        "2026-02-02;ABC;11.2;USD;sample;2026-02-02T00:00:00Z\n",
        encoding="utf-8",
    )
    target = tmp_path / "target.sqlite3"
    summary = migrate_core_csv(tmp_path, target, tmp_path / "migration.sqlite3")
    assert summary.pending_adapter_files == 0
    assert (summary.instruments_imported, summary.trades_imported,
            summary.market_prices_imported, summary.investment_issues) == (1, 1, 1, 0)
    with connect_database(target) as connection:
        assert tuple(connection.execute("""SELECT i.ticker, t.quantity_text, t.unit_price_text,
            t.price_currency_code FROM investment_trades t
            JOIN instruments i ON i.id = t.instrument_id""").fetchone()) == (
                "ABC", "1.25", "10.5", "USD")


def test_sample_core_migration_is_repeatable_and_reconciled(tmp_path):
    source = Path(config.SAMPLE_DATA_PATH)
    target_one = tmp_path / "target-one.sqlite3"
    target_two = tmp_path / "target-two.sqlite3"
    audit_one = tmp_path / "migration-one.sqlite3"
    audit_two = tmp_path / "migration-two.sqlite3"

    first = migrate_core_csv(source, target_one, audit_one)
    second = migrate_core_csv(source, target_two, audit_two)
    assert first == second
    assert first.cash_imported > 0
    assert first.domain_cash_events_imported > 0
    assert first.snapshots_imported > 0
    assert first.unresolved_financial_records > 0
    assert first.pending_adapter_files == 0
    assert first.auxiliary_records_imported > 0
    assert first.drafts_imported == 4
    assert first.debts_imported == 4
    assert first.debt_payments_imported == 4
    assert first.debt_issues == 0
    assert first.instruments_imported == 3
    assert first.trades_imported == 8
    assert first.market_prices_imported == 3
    assert first.investment_issues == 0
    assert first.crypto_wallets_imported == 0
    assert first.crypto_balances_imported == 0
    assert first.crypto_transactions_imported == 0
    assert first.crypto_refresh_results_imported == 0
    assert first.crypto_issues == 0

    with connect_database(target_one) as left, connect_database(target_two) as right:
        stable_queries = {
            "cash_transactions": """SELECT id, occurred_on, flow_direction, amount_minor,
                currency_code, category_id, comment, classification_method, status
                FROM cash_transactions ORDER BY id""",
            "asset_accounts": "SELECT id, name, active FROM asset_accounts ORDER BY id",
            "asset_snapshots": """SELECT id, account_id, period, currency_code, amount_minor
                FROM asset_snapshots ORDER BY id""",
            "source_batches": """SELECT id, source_kind, document_hash, parser_version, status
                FROM source_batches ORDER BY id""",
            "source_records": "SELECT id, batch_id, record_key, payload_hash FROM source_records ORDER BY id",
            "transaction_source_links": "SELECT * FROM transaction_source_links ORDER BY 1, 2, 3",
            "entity_source_links": "SELECT * FROM entity_source_links ORDER BY 1, 2, 3, 4",
            "categorization_rules": """SELECT id, priority, direction_scope, matcher_type,
                pattern, category_id, active FROM categorization_rules ORDER BY id""",
            "fx_rate_observations": """SELECT id, rate_date, currency_code, usd_per_unit_text,
                source, fetched_at, sequence FROM fx_rate_observations ORDER BY id""",
            "annual_goals": """SELECT year, currency_code, target_capital_minor,
                target_monthly_income_minor, target_monthly_expense_minor, notes
                FROM annual_goals ORDER BY year, currency_code""",
            "transaction_drafts": """SELECT id, occurred_on, draft_kind, domain_action,
                flow_direction, amount_minor, currency_code, category_id, comment,
                source_record_id, origin_kind, origin_key, bank_status, bank_reference,
                bank_account_id, status
                FROM transaction_drafts ORDER BY id""",
            "debts": """SELECT id, kind, counterparty, opened_on, principal_amount_minor,
                principal_currency_code, cash_amount_minor, cash_currency_code, comment, status
                FROM debts ORDER BY id""",
            "debt_payments": """SELECT id, debt_id, occurred_on, principal_amount_minor,
                cash_amount_minor, cash_currency_code, comment, status
                FROM debt_payments ORDER BY id""",
            "debt_cash_events": """SELECT id, occurred_on, event_kind, side,
                amount_minor, currency_code, comment FROM debt_cash_events ORDER BY id""",
            "investment_cash_events": """SELECT id, occurred_on, flow_kind,
                amount_minor, currency_code, comment FROM investment_cash_events ORDER BY id""",
            "instruments": """SELECT id, ticker, name, asset_type, quote_currency_code,
                provider, exchange, active FROM instruments ORDER BY id""",
            "investment_trades": """SELECT id, occurred_on, operation, instrument_id,
                quantity_text, unit_price_text, price_currency_code, fee_minor,
                account_label, comment FROM investment_trades ORDER BY id""",
            "market_price_observations": """SELECT id, instrument_id, price_date,
                price_text, currency_code, source, fetched_at, sequence
                FROM market_price_observations ORDER BY id""",
            "crypto_wallets": """SELECT id, account_label, chain, asset_code, address,
                token_contract, label, enabled FROM crypto_wallets ORDER BY id""",
            "crypto_balance_observations": """SELECT id, wallet_id, fetched_at,
                quantity_text, source FROM crypto_balance_observations ORDER BY id""",
            "crypto_transactions": """SELECT id, wallet_id, chain_tx_id, occurred_on,
                operation, quantity_text, fee_text, counterparty, source, comment
                FROM crypto_transactions ORDER BY id""",
            "crypto_refresh_results": """SELECT id, fetched_at, wallet_id, source_row_number,
                observed_account, observed_chain, observed_asset, observed_address, status, message
                FROM crypto_refresh_results ORDER BY id""",
        }
        for query in stable_queries.values():
            left_rows = left.execute(query).fetchall()
            right_rows = right.execute(query).fetchall()
            assert [tuple(row) for row in left_rows] == [tuple(row) for row in right_rows]
        assert left.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert left.execute("PRAGMA foreign_key_check").fetchall() == []
        assert left.execute("SELECT count(*) FROM transaction_source_links").fetchone()[0] == first.cash_imported
        assert left.execute("SELECT count(*) FROM v_cash_transactions").fetchone()[0] == first.cash_imported
        assert left.execute(
            "SELECT count(*) FROM transaction_drafts WHERE draft_kind = 'debt' AND category_id IS NULL"
        ).fetchone()[0] == 4
        assert {row[0] for row in left.execute(
            "SELECT domain_action FROM transaction_drafts"
        )} == {"receivable_opening", "liability_payment"}
        runtime_columns = {
            row["name"]
            for table in ("cash_transactions", "asset_snapshots")
            for row in left.execute(f"PRAGMA table_info({table})")
        }
        assert "relative_path" not in runtime_columns
        assert "row_number" not in runtime_columns
        assert "legacy_coordinate" not in runtime_columns

    audit = sqlite3.connect(audit_one)
    try:
        metrics = dict(audit.execute("SELECT metric, value FROM reconciliation"))
        assert metrics["source_cash_candidates"] == (
            metrics["imported_cash_transactions"] + metrics["imported_domain_cash_events"]
            + metrics["unresolved_financial_records"]
        )
        assert metrics["source_asset_snapshots"] == metrics["imported_asset_snapshots"]
        assert audit.execute("SELECT count(*) FROM raw_records").fetchone()[0] == (
            metrics["source_cash_candidates"] + metrics["source_asset_snapshots"]
            + metrics["auxiliary_records_imported"] + metrics["drafts_imported"]
            + metrics["debts_imported"] + metrics["debt_payments_imported"]
            + metrics["instruments_imported"] + metrics["trades_imported"]
            + metrics["market_prices_imported"]
            + metrics["crypto_wallets_imported"] + metrics["crypto_balances_imported"]
            + metrics["crypto_transactions_imported"]
            + metrics["crypto_refresh_results_imported"]
        )
        assert audit.execute(
            "SELECT count(*) FROM migration_issues WHERE code = 'unmapped_category'"
        ).fetchone()[0] > 0
        assert audit.execute(
            "SELECT count(*) FROM migration_issues WHERE code = 'pass_through_cash_event'"
        ).fetchone()[0] == 0
        assert audit.execute(
            "SELECT count(*) FROM migration_issues WHERE code = 'adapter_pending'"
        ).fetchone()[0] == metrics["pending_adapter_files"]
    finally:
        audit.close()
