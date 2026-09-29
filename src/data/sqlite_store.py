"""Isolated target SQLite store; the live application still uses CSV."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from src import config
from src.data.money import parse_money_amount


SCHEMA_VERSION = 4
_DIRECTIONS = {"income", "expense"}
_DATASETS = {"cash_transactions", "asset_snapshots"}
_SYSTEM_CATEGORIES = (
    ("income.salary", None, "income", "Зарплата", 1, "active", 10),
    ("income.interest", None, "income", "Проценты", 1, "passive", 20),
    ("income.investment", None, "income", "Инвест доход", 1, "passive", 30),
    ("income.other", None, "income", "Прочие доходы", 1, "active", 40),
    ("income.unknown", None, "income", "Доход без категории", 0, "unclassified", 90),
    ("expense.home_goods", None, "expense", "Быт и товары для дома", 1, None, 10),
    ("expense.personal", None, "expense", "На себя", 1, None, 20),
    ("expense.clothing", None, "expense", "Одежда", 1, None, 30),
    ("expense.food", None, "expense", "Пища", 1, None, 40),
    ("expense.travel", None, "expense", "Поездки", 1, None, 50),
    ("expense.other", None, "expense", "Прочее", 1, None, 60),
    ("expense.communication", None, "expense", "Связь", 1, None, 70),
    ("expense.entertainment_legacy", None, "expense", "Развлечения", 1, None, 80),
    ("expense.social", None, "expense", "Соц жизнь", 1, None, 90),
    ("expense.transport", None, "expense", "Транспорт", 1, None, 100),
)

_TABLES = (
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        checksum TEXT NOT NULL CHECK (length(checksum) = 64), applied_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE app_metadata (
        id INTEGER PRIMARY KEY CHECK (id = 1), storage_epoch TEXT NOT NULL UNIQUE,
        data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'migration', 'synthetic')),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE currencies (
        code TEXT PRIMARY KEY CHECK (length(code) BETWEEN 3 AND 8),
        minor_unit INTEGER NOT NULL CHECK (minor_unit BETWEEN 0 AND 6)
    ) STRICT""",
    """CREATE TABLE categories (
        id TEXT PRIMARY KEY, parent_id TEXT REFERENCES categories(id) ON DELETE RESTRICT,
        direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
        name_ru TEXT NOT NULL CHECK (trim(name_ru) <> ''), name_en TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        income_class TEXT CHECK (income_class IN ('active', 'passive', 'unclassified')),
        sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK (id <> parent_id),
        CHECK ((direction = 'income' AND income_class IS NOT NULL)
            OR (direction = 'expense' AND income_class IS NULL))
    ) STRICT""",
    """CREATE TABLE category_aliases (
        alias TEXT NOT NULL CHECK (trim(alias) <> ''),
        direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
        category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        PRIMARY KEY (alias, direction)
    ) STRICT""",
    """CREATE TABLE period_states (
        period TEXT NOT NULL CHECK (length(period) = 7),
        dataset TEXT NOT NULL CHECK (dataset IN ('cash_transactions', 'asset_snapshots')),
        status TEXT NOT NULL CHECK (status IN ('draft', 'saved')),
        revision INTEGER NOT NULL CHECK (revision >= 1), updated_at TEXT NOT NULL,
        PRIMARY KEY (period, dataset)
    ) STRICT""",
    """CREATE TABLE cash_transactions (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        flow_direction TEXT NOT NULL CHECK (flow_direction IN ('income', 'expense')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '', classification_method TEXT,
        status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'void')),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        voided_at TEXT, void_reason TEXT,
        superseded_by_id TEXT REFERENCES cash_transactions(id) ON DELETE RESTRICT,
        CHECK ((status = 'posted' AND voided_at IS NULL AND void_reason IS NULL)
            OR (status = 'void' AND voided_at IS NOT NULL AND trim(void_reason) <> ''))
    ) STRICT""",
    """CREATE TABLE asset_accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK (trim(name) <> ''),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE asset_snapshots (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES asset_accounts(id) ON DELETE RESTRICT,
        period TEXT NOT NULL CHECK (length(period) = 7),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (account_id, period, currency_code)
    ) STRICT""",
    """CREATE TABLE source_batches (
        id TEXT PRIMARY KEY, source_kind TEXT NOT NULL CHECK (trim(source_kind) <> ''),
        document_hash TEXT NOT NULL CHECK (length(document_hash) = 64),
        parser_version TEXT NOT NULL CHECK (trim(parser_version) <> ''),
        status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted', 'rejected')),
        created_at TEXT NOT NULL, UNIQUE (source_kind, document_hash, parser_version)
    ) STRICT""",
    """CREATE TABLE source_records (
        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES source_batches(id) ON DELETE RESTRICT,
        record_key TEXT NOT NULL CHECK (trim(record_key) <> ''),
        payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64), created_at TEXT NOT NULL,
        UNIQUE (batch_id, record_key)
    ) STRICT""",
    """CREATE TABLE transaction_source_links (
        transaction_id TEXT NOT NULL REFERENCES cash_transactions(id) ON DELETE RESTRICT,
        source_record_id TEXT NOT NULL REFERENCES source_records(id) ON DELETE RESTRICT,
        link_role TEXT NOT NULL CHECK (link_role IN ('original', 'split', 'merged', 'replacement')),
        allocated_amount_minor INTEGER CHECK (allocated_amount_minor > 0),
        PRIMARY KEY (transaction_id, source_record_id, link_role)
    ) STRICT""",
    """CREATE TABLE transaction_drafts (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        flow_direction TEXT NOT NULL CHECK (flow_direction IN ('income', 'expense')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        category_id TEXT REFERENCES categories(id) ON DELETE RESTRICT, comment TEXT NOT NULL DEFAULT '',
        source_record_id TEXT UNIQUE REFERENCES source_records(id) ON DELETE RESTRICT,
        status TEXT NOT NULL CHECK (status IN ('draft', 'ready', 'posted', 'error')),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE operation_receipts (
        operation_key TEXT PRIMARY KEY, operation_kind TEXT NOT NULL CHECK (trim(operation_kind) <> ''),
        result_entity_type TEXT NOT NULL CHECK (trim(result_entity_type) <> ''),
        result_entity_id TEXT NOT NULL CHECK (trim(result_entity_id) <> ''),
        result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL CHECK (trim(entity_type) <> ''),
        entity_id TEXT NOT NULL CHECK (trim(entity_id) <> ''),
        action TEXT NOT NULL CHECK (trim(action) <> ''), before_json TEXT, after_json TEXT,
        reason TEXT NOT NULL CHECK (trim(reason) <> ''), occurred_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE categorization_rules (
        id TEXT PRIMARY KEY, priority INTEGER NOT NULL UNIQUE CHECK (priority >= 0),
        direction_scope TEXT NOT NULL CHECK (direction_scope IN ('income', 'expense', 'any')),
        matcher_type TEXT NOT NULL CHECK (matcher_type IN ('contains', 'exact', 'regex')),
        pattern TEXT NOT NULL CHECK (trim(pattern) <> ''),
        category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE fx_rate_observations (
        id TEXT PRIMARY KEY, rate_date TEXT NOT NULL CHECK (length(rate_date) = 10),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        usd_per_unit_text TEXT NOT NULL CHECK (
            trim(usd_per_unit_text) <> '' AND substr(usd_per_unit_text, 1, 1) NOT IN ('-', '+')),
        source TEXT NOT NULL CHECK (trim(source) <> ''), fetched_at TEXT NOT NULL,
        sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),
        UNIQUE (rate_date, currency_code, source, fetched_at, sequence)
    ) STRICT""",
    """CREATE TABLE annual_goals (
        year INTEGER NOT NULL CHECK (year BETWEEN 1900 AND 9999),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        target_capital_minor INTEGER CHECK (target_capital_minor >= 0),
        target_monthly_income_minor INTEGER CHECK (target_monthly_income_minor >= 0),
        target_monthly_expense_minor INTEGER CHECK (target_monthly_expense_minor >= 0),
        notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY (year, currency_code)
    ) STRICT""",
)

_INDEXES_AND_TRIGGERS = (
    "CREATE UNIQUE INDEX uq_active_category_name ON categories(direction, COALESCE(parent_id, ''), name_ru) WHERE active = 1",
    "CREATE INDEX ix_cash_date_category ON cash_transactions(occurred_on, category_id)",
    "CREATE INDEX ix_cash_category_date ON cash_transactions(category_id, occurred_on)",
    "CREATE INDEX ix_snapshots_period_currency ON asset_snapshots(period, currency_code)",
    "CREATE INDEX ix_fx_lookup ON fx_rate_observations(currency_code, rate_date, fetched_at, sequence)",
    "CREATE INDEX ix_source_records_batch ON source_records(batch_id)",
    "CREATE INDEX ix_audit_entity ON audit_events(entity_type, entity_id, id)",
    """CREATE TRIGGER categories_parent_insert BEFORE INSERT ON categories
    WHEN NEW.parent_id IS NOT NULL BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories p WHERE p.id = NEW.parent_id
        AND p.parent_id IS NULL AND p.direction = NEW.direction)
      THEN RAISE(ABORT, 'category parent must be a root with the same direction') END;
    END""",
    """CREATE TRIGGER categories_parent_update BEFORE UPDATE OF parent_id, direction ON categories
    WHEN NEW.parent_id IS NOT NULL BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories p WHERE p.id = NEW.parent_id
        AND p.parent_id IS NULL AND p.direction = NEW.direction)
      THEN RAISE(ABORT, 'category parent must be a root with the same direction') END;
    END""",
    """CREATE TRIGGER categories_direction_used BEFORE UPDATE OF direction ON categories
    WHEN NEW.direction <> OLD.direction AND (
      EXISTS (SELECT 1 FROM cash_transactions t WHERE t.category_id = OLD.id)
      OR EXISTS (SELECT 1 FROM categories c WHERE c.parent_id = OLD.id))
    BEGIN SELECT RAISE(ABORT, 'used category direction is immutable'); END""",
    """CREATE TRIGGER cash_category_insert BEFORE INSERT ON cash_transactions BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction AND (c.active = 1 OR
          (c.id = 'income.unknown' AND NEW.classification_method = 'migration_unresolved')))
      THEN RAISE(ABORT, 'transaction needs an active category with matching direction') END;
    END""",
    """CREATE TRIGGER cash_category_update BEFORE UPDATE OF category_id, flow_direction ON cash_transactions BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction)
      THEN RAISE(ABORT, 'transaction category direction mismatch') END;
    END""",
    """CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
    """CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
)

_VIEWS = (
    """CREATE VIEW v_cash_transactions AS
    SELECT t.id, t.occurred_on, substr(t.occurred_on, 1, 7) AS period,
      t.flow_direction, t.amount_minor,
      CASE t.flow_direction WHEN 'income' THEN t.amount_minor ELSE -t.amount_minor END AS signed_amount_minor,
      t.currency_code, t.category_id, c.parent_id AS parent_category_id,
      c.name_ru AS category_name_ru, c.income_class, t.comment,
      t.classification_method, t.row_version
    FROM cash_transactions t JOIN categories c ON c.id = t.category_id
    WHERE t.status = 'posted'""",
    """CREATE VIEW v_monthly_cashflow AS
    SELECT period, currency_code,
      SUM(CASE WHEN flow_direction = 'income' THEN amount_minor ELSE 0 END) AS income_minor,
      SUM(CASE WHEN flow_direction = 'expense' THEN amount_minor ELSE 0 END) AS expense_minor,
      SUM(signed_amount_minor) AS balance_minor
    FROM v_cash_transactions GROUP BY period, currency_code""",
    """CREATE VIEW v_monthly_category_allocation AS
    SELECT period, currency_code, flow_direction, category_id, category_name_ru,
      SUM(amount_minor) AS category_amount_minor,
      SUM(SUM(amount_minor)) OVER (PARTITION BY period, currency_code, flow_direction) AS direction_amount_minor,
      CAST(SUM(amount_minor) AS REAL) /
        NULLIF(SUM(SUM(amount_minor)) OVER (PARTITION BY period, currency_code, flow_direction), 0) AS share
    FROM v_cash_transactions
    GROUP BY period, currency_code, flow_direction, category_id, category_name_ru""",
    """CREATE VIEW v_asset_snapshots AS
    SELECT s.id, s.period, s.account_id, a.name AS account_name,
      s.currency_code, s.amount_minor, s.row_version
    FROM asset_snapshots s JOIN asset_accounts a ON a.id = s.account_id""",
    """CREATE VIEW v_effective_fx_rates AS
    SELECT id, rate_date, currency_code, usd_per_unit_text, source, fetched_at, sequence
    FROM (SELECT f.*, ROW_NUMBER() OVER (PARTITION BY rate_date, currency_code
      ORDER BY fetched_at DESC, sequence DESC, id DESC) AS selection_rank
      FROM fx_rate_observations f) WHERE selection_rank = 1""",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _schema_checksum() -> str:
    return hashlib.sha256("\n".join((*_TABLES, *_INDEXES_AND_TRIGGERS, *_VIEWS)).encode()).hexdigest()


@contextmanager
def connect_database(path: str | Path, *, writable: bool = False):
    database_path = Path(path).resolve()
    if config.is_test_mode() and not database_path.is_relative_to(Path(config.SAMPLE_DATA_PATH).resolve()):
        raise PermissionError("Test mode can only read the sample database")
    if writable:
        config.require_writable_mode()
        connection = sqlite3.connect(database_path)
    else:
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL" if writable else "PRAGMA query_only = ON")
        yield connection
        if writable:
            connection.commit()
    except Exception:
        if writable:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(path: str | Path, *, data_mode: str = "synthetic") -> None:
    if data_mode not in {"live", "migration", "synthetic"}:
        raise ValueError("unsupported data mode")
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise RuntimeError("SQLite 3.37 or newer is required for STRICT tables")
    with connect_database(path, writable=True) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            row = connection.execute("SELECT checksum FROM schema_migrations WHERE version = ?", (version,)).fetchone()
            if row is None or row[0] != _schema_checksum():
                raise ValueError("SQLite schema version or checksum mismatch")
            return
        has_tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if version != 0 or has_tables:
            raise ValueError("SQLite database is not empty or has an unsupported schema")
        for statement in (*_TABLES, *_INDEXES_AND_TRIGGERS, *_VIEWS):
            connection.execute(statement)
        now = _utc_now()
        connection.execute("INSERT INTO app_metadata VALUES (1, ?, ?, ?)", (uuid4().hex, data_mode, now))
        connection.executemany(
            """INSERT INTO categories
              (id, parent_id, direction, name_ru, active, income_class, sort_order, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(*category, now, now) for category in _SYSTEM_CATEGORIES],
        )
        connection.executemany("INSERT INTO currencies VALUES (?, 2)", [(code,) for code in sorted(config.UNIQUE_TICKERS)])
        connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                           (SCHEMA_VERSION, "normalized_core", _schema_checksum(), now))
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _period(value: str) -> str:
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError) as exc:
        raise ValueError("period must be YYYY-MM") from exc
    if parsed.strftime("%Y-%m") != value:
        raise ValueError("period must be YYYY-MM")
    return value


def _iso_date(value: str, name: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be an ISO date")
    return value


def _minor_units(connection: sqlite3.Connection, currency: str, value, *, allow_zero: bool) -> int:
    currency = currency.upper()
    row = connection.execute("SELECT minor_unit FROM currencies WHERE code = ?", (currency,)).fetchone()
    if row is None:
        raise ValueError("unsupported currency")
    amount = parse_money_amount(value)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError(f"amount must be {'non-negative' if allow_zero else 'positive'}")
    scaled = amount * (10 ** row[0])
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount exceeds {currency} minor-unit precision")
    result = int(scaled)
    if not -(2**63) < result < 2**63:
        raise ValueError("amount is outside SQLite INTEGER range")
    return result


def _amount(minor: int, minor_unit: int) -> Decimal:
    return Decimal(minor).scaleb(-minor_unit)


def _mark_period(connection: sqlite3.Connection, period: str, dataset: str) -> None:
    if dataset not in _DATASETS:
        raise ValueError("unsupported period dataset")
    connection.execute(
        """INSERT INTO period_states VALUES (?, ?, 'saved', 1, ?)
        ON CONFLICT(period, dataset) DO UPDATE SET status = 'saved',
        revision = period_states.revision + 1, updated_at = excluded.updated_at""",
        (_period(period), dataset, _utc_now()),
    )


def save_month(path: str | Path, period: str) -> None:
    with connect_database(path, writable=True) as connection:
        _mark_period(connection, period, "cash_transactions")


def save_asset_month(path: str | Path, period: str) -> None:
    with connect_database(path, writable=True) as connection:
        _mark_period(connection, period, "asset_snapshots")


def _saved_periods(path: str | Path, dataset: str) -> list[str]:
    with connect_database(path) as connection:
        return [row[0] for row in connection.execute(
            "SELECT period FROM period_states WHERE dataset = ? AND status = 'saved' ORDER BY period", (dataset,))]


def saved_months(path: str | Path) -> list[str]:
    return _saved_periods(path, "cash_transactions")


def saved_asset_months(path: str | Path) -> list[str]:
    return _saved_periods(path, "asset_snapshots")


def add_category(path: str | Path, category_id: str, name_ru: str, *,
                 direction: str = "expense", income_class: str | None = None,
                 parent_id: str | None = None) -> None:
    if not category_id or not name_ru.strip():
        raise ValueError("category ID and name are required")
    if direction not in _DIRECTIONS:
        raise ValueError("category direction must be income or expense")
    if (direction == "income" and income_class not in {"active", "passive"}) or (
        direction == "expense" and income_class is not None):
        raise ValueError("income class belongs only to income categories")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        connection.execute(
            """INSERT INTO categories
              (id, parent_id, direction, name_ru, income_class, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (category_id, parent_id, direction, name_ru.strip(), income_class, now, now),
        )


def add_cash_transaction(path: str | Path, *, transaction_id: str, occurred_on: str,
                         flow_direction: str, category_id: str, amount, currency: str,
                         comment: str = "", classification_method: str | None = None) -> None:
    if not transaction_id:
        raise ValueError("transaction ID is required")
    occurred_on = _iso_date(occurred_on, "occurred_on")
    if flow_direction not in _DIRECTIONS:
        raise ValueError("flow direction must be income or expense")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        amount_minor = _minor_units(connection, currency, amount, allow_zero=False)
        try:
            connection.execute(
                """INSERT INTO cash_transactions
                  (id, occurred_on, flow_direction, amount_minor, currency_code,
                   category_id, comment, classification_method, created_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (transaction_id, occurred_on, flow_direction, amount_minor,
                 currency.upper(), category_id, comment, classification_method, now, now),
            )
        except sqlite3.IntegrityError as exc:
            if "active category with matching direction" in str(exc):
                raise ValueError("transaction needs an active category with matching direction") from exc
            raise
        _mark_period(connection, occurred_on[:7], "cash_transactions")


def cash_transactions(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute(
            """SELECT v.*, c.minor_unit FROM v_cash_transactions v
            JOIN currencies c ON c.code = v.currency_code ORDER BY v.occurred_on, v.id""").fetchall()
    return [{**dict(row), "amount": _amount(row["amount_minor"], row["minor_unit"])} for row in rows]


def _audit(connection, entity_id, action, before, after, reason) -> None:
    connection.execute(
        """INSERT INTO audit_events
          (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
          VALUES ('cash_transaction', ?, ?, ?, ?, ?, ?)""",
        (entity_id, action, json.dumps(dict(before), ensure_ascii=False, sort_keys=True),
         json.dumps(dict(after), ensure_ascii=False, sort_keys=True), reason.strip(), _utc_now()),
    )


def change_transaction_category(path: str | Path, transaction_id: str,
                                category_id: str, *, reason: str) -> None:
    if not reason.strip():
        raise ValueError("change reason is required")
    with connect_database(path, writable=True) as connection:
        before = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        if before is None:
            raise ValueError("unknown transaction")
        connection.execute("""UPDATE cash_transactions SET category_id = ?, row_version = row_version + 1,
                           updated_at = ? WHERE id = ?""", (category_id, _utc_now(), transaction_id))
        after = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        _audit(connection, transaction_id, "category_changed", before, after, reason)


def void_cash_transaction(path: str | Path, transaction_id: str, *, reason: str) -> None:
    if not reason.strip():
        raise ValueError("void reason is required")
    with connect_database(path, writable=True) as connection:
        before = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        if before is None or before["status"] != "posted":
            raise ValueError("only a posted transaction can be voided")
        now = _utc_now()
        connection.execute("""UPDATE cash_transactions SET status = 'void', voided_at = ?, void_reason = ?,
                           row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                           (now, reason.strip(), now, transaction_id))
        after = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        _audit(connection, transaction_id, "voided", before, after, reason)


def add_asset_account(path: str | Path, account_id: str, name: str) -> None:
    if not account_id or not name.strip():
        raise ValueError("account ID and name are required")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        connection.execute("INSERT INTO asset_accounts VALUES (?, ?, 1, ?, ?)",
                           (account_id, name.strip(), now, now))


def add_asset_snapshot(path: str | Path, *, snapshot_id: str, account_id: str,
                       period: str, amount, currency: str) -> None:
    if not snapshot_id:
        raise ValueError("snapshot ID is required")
    period = _period(period)
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        amount_minor = _minor_units(connection, currency, amount, allow_zero=True)
        connection.execute(
            """INSERT INTO asset_snapshots
              (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, account_id, period, currency.upper(), amount_minor, now, now),
        )
        _mark_period(connection, period, "asset_snapshots")


def asset_snapshots(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT v.*, c.minor_unit FROM v_asset_snapshots v
            JOIN currencies c ON c.code = v.currency_code ORDER BY v.period, v.id""").fetchall()
    return [{**dict(row), "amount": _amount(row["amount_minor"], row["minor_unit"])} for row in rows]


def save_fx_rate(path: str | Path, *, rate_date: str, currency: str, usd_rate,
                 source: str = "manual", fetched_at: str = "", sequence: int = 0) -> None:
    rate_date = _iso_date(rate_date, "rate_date")
    if not source.strip():
        raise ValueError("FX source is required")
    rate = parse_money_amount(usd_rate, field_name="usd_rate")
    if rate <= 0:
        raise ValueError("usd_rate must be positive")
    with connect_database(path, writable=True) as connection:
        if connection.execute("SELECT 1 FROM currencies WHERE code = ?", (currency.upper(),)).fetchone() is None:
            raise ValueError("unsupported currency")
        connection.execute(
            """INSERT INTO fx_rate_observations VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uuid4().hex, rate_date, currency.upper(), format(rate, "f"), source.strip(),
             fetched_at or _utc_now(), sequence),
        )


def fx_rates(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT rate_date AS date, currency_code AS currency,
            usd_per_unit_text AS usd_rate_text, source, fetched_at, sequence
            FROM v_effective_fx_rates ORDER BY rate_date, currency_code""").fetchall()
    return [{**dict(row), "usd_rate": Decimal(row["usd_rate_text"])} for row in rows]


def register_source_record(path: str | Path, *, source_kind: str, document_hash: str,
                           parser_version: str, record_key: str,
                           payload_hash: str) -> tuple[str, str]:
    if any(len(value) != 64 for value in (document_hash, payload_hash)):
        raise ValueError("source hashes must be SHA-256 hex digests")
    with connect_database(path, writable=True) as connection:
        batch = connection.execute("""SELECT id FROM source_batches WHERE source_kind = ?
            AND document_hash = ? AND parser_version = ?""",
            (source_kind, document_hash, parser_version)).fetchone()
        batch_id = batch[0] if batch else hashlib.sha256(
            f"batch\0{source_kind}\0{document_hash}\0{parser_version}".encode()
        ).hexdigest()[:32]
        if batch is None:
            connection.execute("INSERT INTO source_batches VALUES (?, ?, ?, ?, 'accepted', ?)",
                               (batch_id, source_kind, document_hash, parser_version, _utc_now()))
        record = connection.execute("SELECT id, payload_hash FROM source_records WHERE batch_id = ? AND record_key = ?",
                                    (batch_id, record_key)).fetchone()
        if record and record["payload_hash"] != payload_hash:
            raise ValueError("source record key was reused with another payload")
        record_id = record["id"] if record else hashlib.sha256(
            f"record\0{batch_id}\0{record_key}".encode()
        ).hexdigest()[:32]
        if record is None:
            connection.execute("INSERT INTO source_records VALUES (?, ?, ?, ?, ?)",
                               (record_id, batch_id, record_key, payload_hash, _utc_now()))
        return batch_id, record_id


def link_transaction_source(path: str | Path, transaction_id: str, source_record_id: str,
                            *, role: str = "original",
                            allocated_amount_minor: int | None = None) -> None:
    with connect_database(path, writable=True) as connection:
        connection.execute("INSERT INTO transaction_source_links VALUES (?, ?, ?, ?)",
                           (transaction_id, source_record_id, role, allocated_amount_minor))


def backup_database(source_path: str | Path, destination_path: str | Path) -> None:
    """Create and validate a consistent snapshot through SQLite Backup API."""
    destination = Path(destination_path).resolve()
    if destination.exists():
        raise FileExistsError("backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    config.require_writable_mode()
    try:
        with connect_database(source_path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
            target.row_factory = sqlite3.Row
            target.execute("PRAGMA foreign_keys = ON")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup failed SQLite integrity_check")
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("backup failed SQLite foreign_key_check")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
