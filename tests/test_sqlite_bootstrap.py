from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from src import config
from src.data import sqlite_store
from src.data.sqlite_bootstrap import ensure_default_live_database
from src.data.sqlite_store import SCHEMA_VERSION, initialize_database


def _use_default_sqlite(monkeypatch, data_root: Path) -> Path:
    monkeypatch.delenv("FINREP_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("FINREP_SQLITE_PATH", raising=False)
    monkeypatch.setattr(config, "DATA_PATH", str(data_root))
    return data_root / "finrep.sqlite3"


def _create_v7_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    old_accounts = """CREATE TABLE asset_accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK (trim(name) <> ''),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT"""
    old_view = """CREATE VIEW v_asset_snapshots AS
        SELECT s.id, s.period, s.account_id, a.name AS account_name,
          s.currency_code, s.amount_minor, s.row_version
        FROM asset_snapshots s JOIN asset_accounts a ON a.id = s.account_id"""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in sqlite_store._TABLES:
            if statement.startswith((
                    "CREATE TABLE asset_types",
                    "CREATE TABLE liquidity_classes",
                    "CREATE TABLE asset_type_liquidity_defaults",
                    "CREATE TABLE cpi_series",
                    "CREATE TABLE cpi_observations",
                    "CREATE TABLE cpi_observation_sources")):
                continue
            connection.execute(
                old_accounts if statement.startswith("CREATE TABLE asset_accounts")
                else statement)
        for statement in sqlite_store._INDEXES_AND_TRIGGERS:
            if ("ix_cpi_lookup" in statement or "uq_asset_account_name" in statement
                    or "asset_account_archive" in statement
                    or "archived_asset_snapshot" in statement):
                continue
            connection.execute(statement)
        for statement in sqlite_store._VIEWS:
            if statement.startswith("CREATE VIEW v_effective_cpi"):
                continue
            connection.execute(
                old_view if statement.startswith("CREATE VIEW v_asset_snapshots")
                else statement)
        connection.execute(
            "INSERT INTO app_metadata VALUES (1, 'epoch-v7', 'live', '2026-10-03T00:00:00Z')")
        connection.execute("INSERT INTO currencies VALUES ('RUB', 2)")
        connection.execute(
            "INSERT INTO schema_migrations VALUES (7, 'normalized_core', ?, '2026-10-03T00:00:00Z')",
            ("0" * 64,))
        connection.execute(
            "INSERT INTO asset_accounts VALUES ('account-1', 'Счёт', 1, 'now', 'now')")
        connection.execute("""INSERT INTO asset_snapshots
            (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
            VALUES ('snapshot-1', 'account-1', '2026-09', 'RUB', 12345, 'now', 'now')""")
        connection.execute("PRAGMA user_version = 7")


def _create_v8_database(path: Path) -> None:
    _create_v7_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v7_to_v8(connection)


def _create_v9_database(path: Path) -> None:
    _create_v8_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v8_to_v9(connection)


def _create_v10_database(path: Path) -> None:
    _create_v9_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v9_to_v10(connection)
        connection.execute("""INSERT INTO cpi_observations
            (id, currency_code, period, index_value_text, source_version,
             payload_sha256, fetched_at)
            VALUES ('legacy-rub', 'RUB', '2026-01', '100', 'rosstat-release', ?, 'now')""",
            ("a" * 64,))
        connection.execute("""INSERT INTO cpi_observations
            (id, currency_code, period, index_value_text, source_version,
             payload_sha256, fetched_at)
            VALUES ('legacy-kzt', 'KZT', '2026-01', '100', 'stat-kz-release', ?, 'now')""",
            ("b" * 64,))


def _create_v11_database(path: Path) -> None:
    _create_v10_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v10_to_v11(connection)


def _create_v13_database(path: Path) -> None:
    _create_v11_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v11_to_v12(connection)
        sqlite_store._migrate_v12_to_v13(connection)


def _create_v14_database(path: Path) -> None:
    _create_v13_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v13_to_v14(connection)


def _create_v15_database(path: Path) -> None:
    _create_v14_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v14_to_v15(connection)


def _create_v16_database(path: Path) -> None:
    _create_v15_database(path)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        sqlite_store._migrate_v15_to_v16(connection)


def test_sqlite_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("FINREP_STORAGE_BACKEND", raising=False)

    assert config.get_storage_backend() == "sqlite"


def test_v14_upgrade_infers_closed_period_for_inactive_accounts(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    _create_v14_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE asset_accounts SET active = 0 WHERE id = 'account-1'")

    initialize_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT active, closed_period FROM asset_accounts WHERE id = 'account-1'"
        ).fetchone() == (0, "2026-09")
        with pytest.raises(sqlite3.IntegrityError, match="closed for this period"):
            connection.execute("""INSERT INTO asset_snapshots
                (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
                VALUES ('snapshot-2', 'account-1', '2026-10', 'RUB', 1, 'now', 'now')""")


def test_v15_upgrade_archives_accounts_absent_from_latest_snapshot(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    _create_v15_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("""INSERT INTO asset_accounts
            (id, name, active, created_at, updated_at)
            VALUES ('latest-account', 'Latest', 1, 'now', 'now')""")
        connection.execute("""INSERT INTO asset_snapshots
            (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
            VALUES ('latest-snapshot', 'latest-account', '2026-10', 'RUB', 1, 'now', 'now')""")

    initialize_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT active, closed_period FROM asset_accounts WHERE id = 'account-1'"
        ).fetchone() == (0, "2026-09")
        assert connection.execute(
            "SELECT active, closed_period FROM asset_accounts WHERE id = 'latest-account'"
        ).fetchone() == (1, None)
        assert connection.execute(
            "SELECT action FROM audit_events WHERE entity_id = 'account-1'"
        ).fetchone()[0] == "archived"


def test_live_bootstrap_upgrades_existing_v16_database(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    database.parent.mkdir(parents=True)
    _create_v16_database(database)

    assert ensure_default_live_database() == "upgraded"

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "target_expense_months" in {
            row[1] for row in connection.execute("PRAGMA table_info(annual_goals)")
        }
    assert database.with_name(f"finrep.pre-v{SCHEMA_VERSION}.sqlite3").is_file()


def test_v17_upgrade_preserves_legacy_comments_and_adds_source_comment(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("""INSERT INTO cash_transactions
            (id, occurred_on, flow_direction, amount_minor, currency_code,
             category_id, comment, created_at, updated_at)
            VALUES ('legacy', '2026-10-01', 'income', 10000, 'RUB',
                    'income.salary', 'Edited user comment', 'now', 'now')""")
        for table in ("cash_transactions", "transaction_drafts"):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN source_comment")
        connection.execute("DELETE FROM schema_migrations WHERE version = 18")
        connection.execute("""INSERT INTO schema_migrations VALUES
            (17, 'annual_goal_expense_months', ?, 'now')""", ("0" * 64,))
        connection.execute("PRAGMA user_version = 17")

    initialize_database(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT comment, source_comment FROM cash_transactions WHERE id = 'legacy'"
        ).fetchone() == ("Edited user comment", "")
        assert "source_comment" in {
            row[1] for row in connection.execute("PRAGMA table_info(transaction_drafts)")
        }


def test_empty_install_creates_live_database_once(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")

    assert ensure_default_live_database() == "created"
    assert ensure_default_live_database() == "existing"

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT data_mode FROM app_metadata WHERE id = 1").fetchone()[0] == "live"
        assert connection.execute(
            "SELECT COUNT(*) FROM categories WHERE active = 1").fetchone()[0] == 14


def test_legacy_working_csv_requires_migration(monkeypatch, tmp_path):
    data_root = tmp_path / "data"
    database = _use_default_sqlite(monkeypatch, data_root)
    legacy = data_root / "transactions_info" / "2026" / "2026_01.csv"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("Дата;Пища\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="one-time SQLite migration"):
        ensure_default_live_database()

    assert not database.exists()


def test_existing_empty_database_is_not_overwritten(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    database.parent.mkdir(parents=True)
    database.touch()

    with pytest.raises(RuntimeError, match="empty or inaccessible"):
        ensure_default_live_database()

    assert database.stat().st_size == 0


def test_compatible_existing_database_is_preserved(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    database.parent.mkdir(parents=True)
    initialize_database(database, data_mode="migration")
    with sqlite3.connect(database) as connection:
        epoch = connection.execute(
            "SELECT storage_epoch FROM app_metadata WHERE id = 1").fetchone()[0]

    assert ensure_default_live_database() == "existing"

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT storage_epoch FROM app_metadata WHERE id = 1").fetchone()[0] == epoch


def test_v7_database_is_backed_up_and_upgraded_without_guessing_asset_types(
        monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    _create_v7_database(database)

    assert ensure_default_live_database() == "upgraded"
    assert ensure_default_live_database() == "existing"
    backup = database.with_name(f"{database.stem}.pre-v{SCHEMA_VERSION}{database.suffix}")

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT asset_type_id, include_in_capital FROM asset_accounts"
        ).fetchone() == (None, 1)
        assert connection.execute("SELECT COUNT(*) FROM asset_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM asset_types").fetchone()[0] == 9
        assert connection.execute("SELECT COUNT(*) FROM liquidity_classes").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert connection.execute("SELECT COUNT(*) FROM asset_snapshots").fetchone()[0] == 1


def test_v8_database_is_backed_up_and_upgraded_with_liquidity_defaults(
        monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    _create_v8_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE asset_accounts SET asset_type_id = 'cash_account' WHERE id = 'account-1'")

    assert ensure_default_live_database() == "upgraded"
    backup = database.with_name(f"{database.stem}.pre-v{SCHEMA_VERSION}{database.suffix}")

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT liquidity_class_override_id FROM asset_accounts").fetchone()[0] is None
        assert connection.execute(
            "SELECT liquidity_class_id, liquidity_source FROM v_asset_snapshots"
        ).fetchone() == ("A1", "suggested")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 8


def test_v9_database_is_backed_up_and_upgraded_with_official_cpi_registry(
        monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    _create_v9_database(database)

    assert ensure_default_live_database() == "upgraded"
    backup = database.with_name(f"{database.stem}.pre-v{SCHEMA_VERSION}{database.suffix}")

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM cpi_series").fetchone()[0] == 5
        assert connection.execute("SELECT COUNT(*) FROM cpi_observations").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'cpi_series'").fetchone() is None


def test_v10_database_replaces_unreachable_russia_cpi_source(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    _create_v10_database(database)

    assert ensure_default_live_database() == "upgraded"

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT provider_id, series_code, index_method FROM cpi_series "
            "WHERE currency_code = 'RUB'"
        ).fetchone() == ("world_bank_gem", "CPTOTNSXN", "published_index")
        assert connection.execute(
            "SELECT COUNT(*) FROM cpi_observations WHERE currency_code = 'RUB'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM cpi_observations WHERE currency_code = 'KZT'"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT provider_id FROM cpi_series WHERE currency_code = 'KZT'"
        ).fetchone()[0] == "world_bank_gem+stat_kz"


def test_v11_database_adds_hybrid_kzt_provenance(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    _create_v11_database(database)

    assert ensure_default_live_database() == "upgraded"

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT COUNT(*) FROM cpi_observation_sources"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT provider_id FROM cpi_series WHERE currency_code = 'KZT'"
        ).fetchone()[0] == "world_bank_gem+stat_kz"


def test_v13_database_merges_duplicate_asset_accounts(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    _create_v13_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE asset_accounts SET asset_type_id = 'cash' WHERE id = 'account-1'"
        )
        connection.execute(
            """INSERT INTO asset_accounts
            (id, name, active, created_at, updated_at, asset_type_id, include_in_capital)
            VALUES ('duplicate-account', 'Счёт', 1, 'now', 'now', NULL, 1)"""
        )
        connection.execute(
            """INSERT INTO asset_snapshots
            (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
            VALUES ('snapshot-2', 'duplicate-account', '2026-10', 'RUB', 23456, 'now', 'now')"""
        )

    assert ensure_default_live_database() == "upgraded"
    backup = database.with_name(f"{database.stem}.pre-v{SCHEMA_VERSION}{database.suffix}")

    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute(
            "SELECT id, asset_type_id FROM asset_accounts WHERE name = 'Счёт'"
        ).fetchall() == [("account-1", "cash")]
        assert connection.execute(
            "SELECT account_id, period FROM asset_snapshots ORDER BY period"
        ).fetchall() == [("account-1", "2026-09"), ("account-1", "2026-10")]
        assert connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE action = 'merged'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM pragma_index_list('asset_accounts') "
            "WHERE name = 'uq_asset_account_name' AND \"unique\" = 1"
        ).fetchone()[0] == 1
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 13
        assert connection.execute(
            "SELECT COUNT(*) FROM asset_accounts WHERE name = 'Счёт'"
        ).fetchone()[0] == 2


def test_concurrent_first_start_publishes_one_database(monkeypatch, tmp_path):
    database = _use_default_sqlite(monkeypatch, tmp_path / "data")

    with ThreadPoolExecutor(max_workers=2) as executor:
        states = sorted(executor.map(lambda _item: ensure_default_live_database(), range(2)))

    assert states == ["created", "existing"]
    assert database.is_file()


def test_demo_only_start_does_not_create_live_database(monkeypatch, tmp_path):
    from src.dashboard.app import create_app

    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    monkeypatch.delenv("FINREP_DASH_PASSWORD", raising=False)
    monkeypatch.delenv("FINREP_DASH_SECRET_KEY", raising=False)
    monkeypatch.chdir(tmp_path)

    app = create_app()

    assert app.server.config["FINREP_LIVE_AUTH_ENABLED"] is False
    assert not database.exists()


def test_live_app_start_creates_default_database(monkeypatch, tmp_path):
    from src.dashboard.app import create_app

    database = _use_default_sqlite(monkeypatch, tmp_path / "data")
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "secret")
    monkeypatch.chdir(tmp_path)

    app = create_app()

    assert app.server.config["FINREP_LIVE_AUTH_ENABLED"] is True
    assert database.is_file()
