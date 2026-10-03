from pathlib import Path
import sqlite3

from src import config
from src.data.sqlite_migration import migrate_core_csv
from src.data.sqlite_reconciliation import reconcile_migration
from src.data.sqlite_store import connect_database


def test_sample_reconciliation_passes_and_detects_financial_tampering(tmp_path):
    source = Path(config.SAMPLE_DATA_PATH)
    target = tmp_path / "target.sqlite3"
    audit = tmp_path / "migration.sqlite3"
    migrate_core_csv(source, target, audit)

    report = reconcile_migration(source, target, audit)
    assert report.passed
    assert not report.ready_for_cutover
    assert report.blocking_review_items == 17
    assert {check.name for check in report.checks} == {
        "cash_by_month_currency_direction_category",
        "asset_snapshots_by_period_account_currency",
        "debt_principal_by_kind_currency",
        "debt_payments_by_currency",
        "debt_cash_events_by_month_currency_kind_side",
        "investment_cash_events_by_month_currency_kind",
        "investment_position_quantity_by_ticker",
        "market_price_row_count",
        "fx_observation_row_count",
        "annual_goal_row_count",
    }
    migration = sqlite3.connect(audit)
    try:
        assert migration.execute(
            "SELECT count(*) FROM comparison_results WHERE status = 'pass'"
        ).fetchone()[0] == len(report.checks)
    finally:
        migration.close()

    with connect_database(target, writable=True) as connection:
        transaction_id = connection.execute("SELECT id FROM cash_transactions LIMIT 1").fetchone()[0]
        connection.execute(
            "UPDATE cash_transactions SET amount_minor = amount_minor + 1 WHERE id = ?",
            (transaction_id,),
        )
    changed = reconcile_migration(source, target, audit)
    failed = {check.name for check in changed.checks if not check.passed}
    assert failed == {"cash_by_month_currency_direction_category"}
