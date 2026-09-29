from pathlib import Path
import csv
import sqlite3

from src import config
from src.data.sqlite_migration import build_manifest, migrate_core_csv
from src.data.sqlite_store import connect_database


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
    ]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(columns)
        writer.writerows(rows)
    target = tmp_path / "target.sqlite3"
    summary = migrate_core_csv(tmp_path, target, tmp_path / "migration.sqlite3")
    assert summary.drafts_imported == 2
    assert summary.pending_adapter_files == 0
    with connect_database(target) as connection:
        drafts = connection.execute("""SELECT flow_direction, amount_minor, currency_code,
            category_id, origin_kind, origin_key, bank_status, status
            FROM transaction_drafts ORDER BY occurred_on""").fetchall()
    assert [tuple(row) for row in drafts] == [
        ("expense", 1234, "RUB", "expense.food", "manual", "m1", None, "ready"),
        ("income", 2500, "USD", "income.salary", "bank", "b1", "posted", "exported"),
    ]


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
    assert first.snapshots_imported > 0
    assert first.unresolved_financial_records > 0
    assert first.pending_adapter_files > 0
    assert first.auxiliary_records_imported > 0
    assert first.drafts_imported == 4

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
                source_record_id, origin_kind, origin_key, bank_status, status
                FROM transaction_drafts ORDER BY id""",
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
            metrics["imported_cash_transactions"] + metrics["unresolved_financial_records"]
        )
        assert metrics["source_asset_snapshots"] == metrics["imported_asset_snapshots"]
        assert audit.execute("SELECT count(*) FROM raw_records").fetchone()[0] == (
            metrics["source_cash_candidates"] + metrics["source_asset_snapshots"]
            + metrics["auxiliary_records_imported"] + metrics["drafts_imported"]
        )
        assert audit.execute(
            "SELECT count(*) FROM migration_issues WHERE code = 'unmapped_category'"
        ).fetchone()[0] > 0
        assert audit.execute(
            "SELECT count(*) FROM migration_issues WHERE code = 'pass_through_cash_event'"
        ).fetchone()[0] > 0
        assert audit.execute(
            "SELECT count(*) FROM migration_issues WHERE code = 'adapter_pending'"
        ).fetchone()[0] == metrics["pending_adapter_files"]
    finally:
        audit.close()
