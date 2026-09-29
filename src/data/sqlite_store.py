"""Isolated target SQLite store; the live application still uses CSV."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from src import config
from src.data.money import parse_money_amount


SCHEMA_VERSION = 6
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
    """CREATE TABLE entity_source_links (
        entity_type TEXT NOT NULL CHECK (trim(entity_type) <> ''),
        entity_id TEXT NOT NULL CHECK (trim(entity_id) <> ''),
        source_record_id TEXT NOT NULL REFERENCES source_records(id) ON DELETE RESTRICT,
        link_role TEXT NOT NULL DEFAULT 'original' CHECK (link_role IN ('original', 'replacement')),
        PRIMARY KEY (entity_type, entity_id, source_record_id, link_role)
    ) STRICT""",
    """CREATE TABLE transaction_drafts (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        draft_kind TEXT NOT NULL CHECK (draft_kind IN ('cash', 'debt', 'investment')),
        domain_action TEXT,
        flow_direction TEXT CHECK (flow_direction IN ('income', 'expense')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        category_id TEXT REFERENCES categories(id) ON DELETE RESTRICT, comment TEXT NOT NULL DEFAULT '',
        source_record_id TEXT UNIQUE REFERENCES source_records(id) ON DELETE RESTRICT,
        origin_kind TEXT NOT NULL CHECK (trim(origin_kind) <> ''),
        origin_key TEXT NOT NULL CHECK (trim(origin_key) <> ''),
        bank_status TEXT CHECK (bank_status IN ('pending', 'posted')),
        bank_reference TEXT NOT NULL DEFAULT '', bank_account_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('draft', 'ready', 'exported', 'archived', 'ignored')),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (origin_kind, origin_key),
        CHECK ((draft_kind = 'cash' AND domain_action IS NULL
                AND flow_direction IS NOT NULL AND category_id IS NOT NULL)
            OR (draft_kind <> 'cash' AND domain_action IS NOT NULL
                AND flow_direction IS NULL AND category_id IS NULL))
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
    """CREATE TABLE debts (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('receivable', 'liability')),
        counterparty TEXT NOT NULL CHECK (trim(counterparty) <> ''),
        opened_on TEXT NOT NULL CHECK (length(opened_on) = 10),
        principal_amount_minor INTEGER NOT NULL CHECK (principal_amount_minor > 0),
        principal_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        cash_amount_minor INTEGER NOT NULL CHECK (cash_amount_minor > 0),
        cash_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('active', 'closed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE debt_payments (
        id TEXT PRIMARY KEY,
        debt_id TEXT NOT NULL REFERENCES debts(id) ON DELETE RESTRICT,
        occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        principal_amount_minor INTEGER NOT NULL CHECK (principal_amount_minor > 0),
        cash_amount_minor INTEGER NOT NULL CHECK (cash_amount_minor > 0),
        cash_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status = 'posted'),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE instruments (
        id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL UNIQUE CHECK (trim(ticker) <> ''),
        name TEXT NOT NULL CHECK (trim(name) <> ''),
        asset_type TEXT NOT NULL CHECK (asset_type IN ('stocks', 'funds', 'crypto')),
        quote_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        provider TEXT NOT NULL DEFAULT '', exchange TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE investment_trades (
        id TEXT PRIMARY KEY,
        occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        operation TEXT NOT NULL CHECK (operation IN ('buy', 'sell')),
        instrument_id TEXT NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
        quantity_text TEXT NOT NULL CHECK (trim(quantity_text) <> ''),
        unit_price_text TEXT NOT NULL CHECK (trim(unit_price_text) <> ''),
        price_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        fee_minor INTEGER NOT NULL DEFAULT 0 CHECK (fee_minor >= 0),
        account_label TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE market_price_observations (
        id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
        price_date TEXT NOT NULL CHECK (length(price_date) = 10),
        price_text TEXT NOT NULL CHECK (trim(price_text) <> ''),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        source TEXT NOT NULL CHECK (trim(source) <> ''), fetched_at TEXT NOT NULL,
        sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),
        UNIQUE (instrument_id, price_date, source, fetched_at, sequence)
    ) STRICT""",
    """CREATE TABLE crypto_wallets (
        id TEXT PRIMARY KEY,
        account_label TEXT NOT NULL CHECK (trim(account_label) <> ''),
        chain TEXT NOT NULL CHECK (trim(chain) <> ''),
        asset_code TEXT NOT NULL CHECK (trim(asset_code) <> ''),
        address TEXT NOT NULL CHECK (trim(address) <> ''),
        token_contract TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (chain, asset_code, address, token_contract)
    ) STRICT""",
    """CREATE TABLE crypto_balance_observations (
        id TEXT PRIMARY KEY,
        wallet_id TEXT NOT NULL REFERENCES crypto_wallets(id) ON DELETE RESTRICT,
        fetched_at TEXT NOT NULL, quantity_text TEXT NOT NULL CHECK (trim(quantity_text) <> ''),
        source TEXT NOT NULL CHECK (trim(source) <> '')
    ) STRICT""",
    """CREATE TABLE crypto_transactions (
        id TEXT PRIMARY KEY,
        wallet_id TEXT NOT NULL REFERENCES crypto_wallets(id) ON DELETE RESTRICT,
        chain_tx_id TEXT NOT NULL CHECK (trim(chain_tx_id) <> ''),
        occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        operation TEXT NOT NULL CHECK (trim(operation) <> ''),
        quantity_text TEXT, fee_text TEXT, counterparty TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL CHECK (trim(source) <> ''), comment TEXT NOT NULL DEFAULT '',
        UNIQUE (wallet_id, chain_tx_id, operation)
    ) STRICT""",
    """CREATE TABLE crypto_refresh_results (
        id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL,
        wallet_id TEXT REFERENCES crypto_wallets(id) ON DELETE RESTRICT,
        source_row_number INTEGER,
        observed_account TEXT NOT NULL DEFAULT '', observed_chain TEXT NOT NULL DEFAULT '',
        observed_asset TEXT NOT NULL DEFAULT '', observed_address TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (trim(status) <> ''), message TEXT NOT NULL DEFAULT ''
    ) STRICT""",
    """CREATE TABLE investment_cash_events (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        flow_kind TEXT NOT NULL CHECK (flow_kind IN ('contribution', 'withdrawal')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE debt_cash_events (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        event_kind TEXT NOT NULL CHECK (event_kind IN ('issue', 'repayment')),
        side TEXT NOT NULL CHECK (side IN ('receivable', 'liability')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
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
    "CREATE INDEX ix_debt_payments_debt_date ON debt_payments(debt_id, occurred_on, id)",
    "CREATE INDEX ix_investment_trades_instrument_date ON investment_trades(instrument_id, occurred_on, id)",
    "CREATE INDEX ix_market_prices_lookup ON market_price_observations(instrument_id, price_date, fetched_at, sequence)",
    "CREATE INDEX ix_crypto_balances_wallet_time ON crypto_balance_observations(wallet_id, fetched_at)",
    "CREATE INDEX ix_crypto_transactions_wallet_date ON crypto_transactions(wallet_id, occurred_on)",
    "CREATE INDEX ix_investment_cash_events_date ON investment_cash_events(occurred_on, currency_code)",
    "CREATE INDEX ix_debt_cash_events_date ON debt_cash_events(occurred_on, side, event_kind)",
    """CREATE TRIGGER draft_category_insert BEFORE INSERT ON transaction_drafts
    WHEN NEW.draft_kind = 'cash' BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction AND c.active = 1)
      THEN RAISE(ABORT, 'cash draft needs an active category with matching direction') END;
    END""",
    """CREATE TRIGGER draft_category_update BEFORE UPDATE OF category_id, flow_direction ON transaction_drafts
    WHEN NEW.draft_kind = 'cash' BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction AND c.active = 1)
      THEN RAISE(ABORT, 'cash draft needs an active category with matching direction') END;
    END""",
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


def _decimal_text(value, name: str) -> str:
    normalized = str(value).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        parsed = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return format(parsed, "f")


def _positive_decimal_text(value, name: str) -> str:
    parsed = _decimal_text(value, name)
    if Decimal(parsed) <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _non_negative_decimal_text(value, name: str) -> str:
    parsed = _decimal_text(value, name)
    if Decimal(parsed) < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


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


def create_transaction_draft(
    path: str | Path,
    *,
    occurred_on: str,
    flow_direction: str,
    category_id: str,
    amount,
    currency: str,
    origin_kind: str,
    origin_key: str,
    comment: str = "",
    bank_status: str | None = None,
    bank_reference: str = "",
    bank_account_id: str = "",
    status: str = "draft",
    source_record_id: str | None = None,
    draft_id: str | None = None,
) -> str:
    """Create one idempotent cash draft using its external origin identity."""
    occurred_on = _iso_date(occurred_on, "occurred_on")
    if flow_direction not in _DIRECTIONS:
        raise ValueError("flow direction must be income or expense")
    if not origin_kind.strip() or not origin_key.strip():
        raise ValueError("draft origin kind and key are required")
    if bank_status not in {None, "pending", "posted"}:
        raise ValueError("unsupported bank status")
    if status not in {"draft", "ready"}:
        raise ValueError("a new draft must have draft or ready status")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        amount_minor = _minor_units(connection, currency, amount, allow_zero=False)
        values = (
            occurred_on, flow_direction, amount_minor, currency.upper(), category_id,
            comment, source_record_id, origin_kind.strip(), origin_key.strip(), bank_status,
            bank_reference, bank_account_id, status,
        )
        existing = connection.execute("""SELECT id, occurred_on, flow_direction,
            amount_minor, currency_code, category_id, comment, source_record_id,
            origin_kind, origin_key, bank_status, bank_reference, bank_account_id, status
            FROM transaction_drafts WHERE origin_kind = ? AND origin_key = ?""",
            (origin_kind.strip(), origin_key.strip())).fetchone()
        if existing is not None:
            if tuple(existing)[1:] != values:
                raise ValueError("draft origin key was reused with another payload")
            return existing["id"]
        draft_id = draft_id or uuid4().hex
        connection.execute("""INSERT INTO transaction_drafts
            (id, occurred_on, draft_kind, domain_action, flow_direction, amount_minor,
             currency_code, category_id, comment, source_record_id, origin_kind, origin_key,
             bank_status, bank_reference, bank_account_id, status, created_at, updated_at)
            VALUES (?, ?, 'cash', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (draft_id, *values, now, now))
    return draft_id


def publish_cash_drafts(path: str | Path, *, draft_ids: list[str], operation_key: str) -> dict:
    """Atomically publish drafts, their source links, statuses and retry receipt."""
    requested_ids = sorted(set(draft_ids))
    if not requested_ids or len(requested_ids) != len(draft_ids):
        raise ValueError("draft IDs must be non-empty and unique")
    if not operation_key.strip():
        raise ValueError("operation key is required")
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            if receipt["operation_kind"] != "publish_cash_drafts":
                raise ValueError("operation key was already used for another command")
            result = json.loads(receipt["result_json"])
            if result.get("draft_ids") != requested_ids:
                raise ValueError("operation key was reused with another draft set")
            return result

        placeholders = ",".join("?" for _ in requested_ids)
        drafts = connection.execute(f"""SELECT * FROM transaction_drafts
            WHERE id IN ({placeholders}) ORDER BY id""", requested_ids).fetchall()
        if len(drafts) != len(requested_ids):
            raise ValueError("one or more drafts do not exist")
        if any(row["draft_kind"] != "cash" for row in drafts):
            raise ValueError("only cash drafts can be published as cash transactions")
        if any(row["status"] not in {"draft", "ready"} for row in drafts):
            raise ValueError("only draft or ready rows can be published")
        if any(row["bank_status"] == "pending" for row in drafts):
            raise ValueError("pending bank rows cannot be published")

        now = _utc_now()
        transaction_ids = []
        for draft in drafts:
            transaction_id = hashlib.sha256(
                f"cash-transaction\0{draft['id']}".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO cash_transactions
                (id, occurred_on, flow_direction, amount_minor, currency_code, category_id,
                 comment, classification_method, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'user', ?, ?)""",
                (transaction_id, draft["occurred_on"], draft["flow_direction"],
                 draft["amount_minor"], draft["currency_code"], draft["category_id"],
                 draft["comment"], now, now))
            if draft["source_record_id"]:
                connection.execute("INSERT INTO transaction_source_links VALUES (?, ?, 'original', NULL)",
                                   (transaction_id, draft["source_record_id"]))
            connection.execute("""UPDATE transaction_drafts SET status = 'exported',
                row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                (now, draft["id"]))
            _mark_period(connection, draft["occurred_on"][:7], "cash_transactions")
            transaction_ids.append(transaction_id)

        result = {
            "draft_ids": requested_ids,
            "transaction_ids": transaction_ids,
            "published_rows": len(transaction_ids),
        }
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'publish_cash_drafts',
             'cash_transaction_batch', ?, ?, ?)""",
            (operation_key, operation_key, json.dumps(result, sort_keys=True), now))
        return result


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


def replace_asset_snapshot_month(path: str | Path, *, period: str,
                                 rows: list[dict]) -> dict:
    """Atomically make one period equal to the supplied account/currency rows."""
    period = _period(period)
    normalized = []
    seen = set()
    for row in rows:
        account_name = str(row.get("account", "")).strip()
        currency = str(row.get("currency", "")).strip().upper()
        if not account_name:
            raise ValueError("asset account name is required")
        key = (account_name, currency)
        if key in seen:
            raise ValueError("asset snapshot contains a duplicate account/currency")
        seen.add(key)
        normalized.append((account_name, currency, row.get("amount")))

    now = _utc_now()
    inserted = updated = deleted = 0
    with connect_database(path, writable=True) as connection:
        existing_rows = connection.execute(
            "SELECT * FROM asset_snapshots WHERE period = ?", (period,)).fetchall()
        existing = {(row["account_id"], row["currency_code"]): row for row in existing_rows}
        wanted = set()
        for account_name, currency, amount in normalized:
            account_id = hashlib.sha256(
                f"asset-account\0{account_name}".encode()).hexdigest()[:32]
            account = connection.execute(
                "SELECT name FROM asset_accounts WHERE id = ?", (account_id,)).fetchone()
            if account is None:
                connection.execute("INSERT INTO asset_accounts VALUES (?, ?, 1, ?, ?)",
                                   (account_id, account_name, now, now))
            elif account["name"] != account_name:
                raise ValueError("asset account identity collision")
            key = (account_id, currency)
            wanted.add(key)
            amount_minor = _minor_units(connection, currency, amount, allow_zero=True)
            current = existing.get(key)
            if current is None:
                snapshot_id = hashlib.sha256(
                    f"asset-snapshot\0{account_id}\0{period}\0{currency}".encode()
                ).hexdigest()[:32]
                connection.execute("""INSERT INTO asset_snapshots
                    (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, account_id, period, currency, amount_minor, now, now))
                inserted += 1
            elif current["amount_minor"] != amount_minor:
                connection.execute("""UPDATE asset_snapshots SET amount_minor = ?,
                    row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                    (amount_minor, now, current["id"]))
                connection.execute("""INSERT INTO audit_events
                    (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                    VALUES ('asset_snapshot', ?, 'amount_changed', ?, ?,
                    'monthly snapshot replacement', ?)""",
                    (current["id"], json.dumps(dict(current), sort_keys=True),
                     json.dumps({**dict(current), "amount_minor": amount_minor}, sort_keys=True), now))
                updated += 1

        for key, current in existing.items():
            if key in wanted:
                continue
            connection.execute("""DELETE FROM entity_source_links
                WHERE entity_type = 'asset_snapshot' AND entity_id = ?""", (current["id"],))
            connection.execute("DELETE FROM asset_snapshots WHERE id = ?", (current["id"],))
            connection.execute("""INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_snapshot', ?, 'removed', ?, NULL,
                'monthly snapshot replacement', ?)""",
                (current["id"], json.dumps(dict(current), sort_keys=True), now))
            deleted += 1
        _mark_period(connection, period, "asset_snapshots")
    return {"inserted": inserted, "updated": updated, "deleted": deleted,
            "rows": len(normalized)}


def asset_snapshot_month(path: str | Path, period: str) -> list[dict]:
    period = _period(period)
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT v.*, c.minor_unit FROM v_asset_snapshots v
            JOIN currencies c ON c.code = v.currency_code WHERE v.period = ?
            ORDER BY v.account_name, v.currency_code""", (period,)).fetchall()
    return [{**dict(row), "amount": _amount(row["amount_minor"], row["minor_unit"])}
            for row in rows]


def upsert_annual_goal(path: str | Path, *, year: int, currency: str,
                       target_capital=None, target_monthly_income=None,
                       target_monthly_expense=None, notes: str = "") -> None:
    if not 1900 <= int(year) <= 9999:
        raise ValueError("goal year is out of range")
    currency = currency.upper()
    with connect_database(path, writable=True) as connection:
        values = [
            None if value is None or str(value).strip() == "" else
            _minor_units(connection, currency, value, allow_zero=True)
            for value in (target_capital, target_monthly_income, target_monthly_expense)
        ]
        connection.execute("""INSERT INTO annual_goals
            (year, currency_code, target_capital_minor, target_monthly_income_minor,
             target_monthly_expense_minor, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, currency_code) DO UPDATE SET
              target_capital_minor = excluded.target_capital_minor,
              target_monthly_income_minor = excluded.target_monthly_income_minor,
              target_monthly_expense_minor = excluded.target_monthly_expense_minor,
              notes = excluded.notes, updated_at = excluded.updated_at""",
            (int(year), currency, *values, notes, _utc_now()))


def annual_goals(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT g.*, c.minor_unit FROM annual_goals g
            JOIN currencies c ON c.code = g.currency_code ORDER BY g.year, g.currency_code""").fetchall()
    amount_fields = ("target_capital", "target_monthly_income", "target_monthly_expense")
    result = []
    for row in rows:
        item = dict(row)
        for field in amount_fields:
            minor = item[f"{field}_minor"]
            item[field] = None if minor is None else _amount(minor, item["minor_unit"])
        result.append(item)
    return result


def create_debt_record(path: str | Path, *, kind: str, counterparty: str,
                       opened_on: str, principal_amount, currency: str,
                       operation_key: str, comment: str = "",
                       create_draft: bool = True) -> dict:
    """Atomically create a new same-currency personal debt and optional cash draft."""
    if kind not in {"receivable", "liability"}:
        raise ValueError("debt kind must be receivable or liability")
    if not counterparty.strip() or not operation_key.strip():
        raise ValueError("counterparty and operation key are required")
    opened_on = _iso_date(opened_on, "opened_on")
    currency = currency.upper()
    request = {
        "kind": kind, "counterparty": counterparty.strip(), "opened_on": opened_on,
        "principal_amount": str(parse_money_amount(principal_amount)), "currency": currency,
        "comment": comment, "create_draft": bool(create_draft),
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "create_debt" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        principal_minor = _minor_units(
            connection, currency, principal_amount, allow_zero=False)
        debt_id = f"debt-{uuid4().hex[:12]}"
        now = _utc_now()
        connection.execute("""INSERT INTO debts
            (id, kind, counterparty, opened_on, principal_amount_minor,
             principal_currency_code, cash_amount_minor, cash_currency_code,
             comment, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (debt_id, kind, counterparty.strip(), opened_on, principal_minor,
             currency, principal_minor, currency, comment, now, now))
        draft_id = None
        if create_draft:
            draft_id = hashlib.sha256(f"debt-draft\0{debt_id}\0open".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO transaction_drafts
                (id, occurred_on, draft_kind, domain_action, flow_direction,
                 amount_minor, currency_code, category_id, comment, source_record_id,
                 origin_kind, origin_key, status, created_at, updated_at)
                VALUES (?, ?, 'debt', ?, NULL, ?, ?, NULL, ?, NULL,
                 'debt', ?, 'ready', ?, ?)""",
                (draft_id, opened_on, f"{kind}_opening", principal_minor, currency,
                 comment, f"{debt_id}:open", now, now))
        result = {"debt_id": debt_id, "draft_id": draft_id,
                  "draft_created": bool(create_draft)}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'create_debt', 'debt', ?, ?, ?)""",
            (operation_key, debt_id, json.dumps(payload, sort_keys=True), now))
        return result


def record_debt_payment(path: str | Path, *, debt_id: str, occurred_on: str,
                        amount, operation_key: str, comment: str = "",
                        create_draft: bool = True) -> dict:
    """Atomically record a same-currency payment, forbidding chronological overpayment."""
    if not operation_key.strip():
        raise ValueError("operation key is required")
    occurred_on = _iso_date(occurred_on, "occurred_on")
    normalized_amount = str(parse_money_amount(amount))
    request = {
        "debt_id": debt_id, "occurred_on": occurred_on, "amount": normalized_amount,
        "comment": comment, "create_draft": bool(create_draft),
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "record_debt_payment" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        debt = connection.execute("SELECT * FROM debts WHERE id = ?", (debt_id,)).fetchone()
        if debt is None:
            raise ValueError("unknown debt")
        if occurred_on < debt["opened_on"]:
            raise ValueError("debt payment cannot precede debt opening")
        currency = debt["principal_currency_code"]
        amount_minor = _minor_units(connection, currency, amount, allow_zero=False)
        paid_minor = connection.execute(
            "SELECT COALESCE(SUM(principal_amount_minor), 0) FROM debt_payments WHERE debt_id = ?",
            (debt_id,)).fetchone()[0]
        outstanding_minor = debt["principal_amount_minor"] - paid_minor
        if amount_minor > outstanding_minor:
            raise ValueError("debt payment exceeds outstanding principal")
        if debt["status"] == "closed" or outstanding_minor <= 0:
            raise ValueError("closed debt cannot receive another payment")

        payment_id = f"payment-{uuid4().hex[:12]}"
        now = _utc_now()
        connection.execute("""INSERT INTO debt_payments
            (id, debt_id, occurred_on, principal_amount_minor, cash_amount_minor,
             cash_currency_code, comment, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'posted', ?)""",
            (payment_id, debt_id, occurred_on, amount_minor, amount_minor,
             currency, comment, now))
        remaining_minor = outstanding_minor - amount_minor
        if remaining_minor == 0:
            connection.execute(
                "UPDATE debts SET status = 'closed', updated_at = ? WHERE id = ?",
                (now, debt_id))
        draft_id = None
        if create_draft:
            draft_id = hashlib.sha256(
                f"debt-draft\0{payment_id}\0payment".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO transaction_drafts
                (id, occurred_on, draft_kind, domain_action, flow_direction,
                 amount_minor, currency_code, category_id, comment, source_record_id,
                 origin_kind, origin_key, status, created_at, updated_at)
                VALUES (?, ?, 'debt', ?, NULL, ?, ?, NULL, ?, NULL,
                 'debt', ?, 'ready', ?, ?)""",
                (draft_id, occurred_on, f"{debt['kind']}_payment", amount_minor,
                 currency, comment, f"{payment_id}:payment", now, now))
        result = {"payment_id": payment_id, "debt_id": debt_id,
                  "draft_id": draft_id, "remaining_minor": remaining_minor,
                  "closed": remaining_minor == 0}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'record_debt_payment',
             'debt_payment', ?, ?, ?)""",
            (operation_key, payment_id, json.dumps(payload, sort_keys=True), now))
        return result


def record_investment_trade(
    path: str | Path,
    *,
    occurred_on: str,
    operation: str,
    ticker: str,
    asset_type: str,
    quantity,
    unit_price,
    currency: str,
    fee=0,
    operation_key: str,
    instrument_name: str = "",
    provider: str = "",
    exchange: str = "",
    account_label: str = "",
    comment: str = "",
) -> dict:
    """Atomically create an instrument if needed and record one idempotent trade."""
    occurred_on = _iso_date(occurred_on, "occurred_on")
    operation = operation.lower()
    ticker = ticker.strip().upper()
    asset_type = asset_type.lower()
    currency = currency.upper()
    if operation not in {"buy", "sell"}:
        raise ValueError("investment operation must be buy or sell")
    if asset_type not in {"stocks", "funds", "crypto"}:
        raise ValueError("unsupported investment asset type")
    if not ticker or not operation_key.strip():
        raise ValueError("ticker and operation key are required")
    quantity_text = _positive_decimal_text(quantity, "quantity")
    price_text = _non_negative_decimal_text(unit_price, "unit price")
    fee_value = parse_money_amount(fee, field_name="fee")
    if fee_value < 0:
        raise ValueError("fee must be non-negative")
    request = {
        "occurred_on": occurred_on, "operation": operation, "ticker": ticker,
        "asset_type": asset_type, "quantity": quantity_text, "unit_price": price_text,
        "currency": currency, "fee": format(fee_value, "f"),
        "instrument_name": instrument_name.strip(), "provider": provider.strip(),
        "exchange": exchange.strip(), "account_label": account_label.strip(),
        "comment": comment,
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "record_investment_trade" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        if connection.execute("SELECT 1 FROM currencies WHERE code = ?", (currency,)).fetchone() is None:
            raise ValueError("unsupported currency")
        instrument = connection.execute(
            "SELECT * FROM instruments WHERE ticker = ?", (ticker,)).fetchone()
        instrument_id = hashlib.sha256(f"instrument\0{ticker}".encode()).hexdigest()[:32]
        now = _utc_now()
        if instrument is None:
            connection.execute("""INSERT INTO instruments
                (id, ticker, name, asset_type, quote_currency_code, provider, exchange,
                 active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (instrument_id, ticker, instrument_name.strip() or ticker, asset_type,
                 currency, provider.strip(), exchange.strip(), now, now))
        else:
            instrument_id = instrument["id"]
            if (instrument["asset_type"], instrument["quote_currency_code"]) != (asset_type, currency):
                raise ValueError("ticker is already registered with another type or currency")
        quantity_value = Decimal(quantity_text)
        if operation == "sell":
            rows = connection.execute("""SELECT operation, quantity_text, occurred_on
                FROM investment_trades WHERE instrument_id = ?""", (instrument_id,)).fetchall()
            position_before = sum(
                (Decimal(row["quantity_text"]) * (-1 if row["operation"] == "sell" else 1)
                 for row in rows if row["occurred_on"] <= occurred_on),
                Decimal("0"),
            )
            current_position = sum(
                (Decimal(row["quantity_text"]) * (-1 if row["operation"] == "sell" else 1)
                 for row in rows),
                Decimal("0"),
            )
            if quantity_value > position_before or quantity_value > current_position:
                raise ValueError("sale exceeds the available instrument position")
        fee_minor = _minor_units(connection, currency, fee_value, allow_zero=True)
        trade_id = f"trade-{uuid4().hex[:12]}"
        connection.execute("""INSERT INTO investment_trades
            (id, occurred_on, operation, instrument_id, quantity_text, unit_price_text,
             price_currency_code, fee_minor, account_label, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, occurred_on, operation, instrument_id, quantity_text, price_text,
             currency, fee_minor, account_label.strip(), comment, now))
        result = {"trade_id": trade_id, "instrument_id": instrument_id}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'record_investment_trade',
             'investment_trade', ?, ?, ?)""",
            (operation_key, trade_id, json.dumps(payload, sort_keys=True), now))
        return result


def upsert_crypto_wallet(path: str | Path, *, account_label: str, chain: str,
                         asset_code: str, address: str, token_contract: str = "",
                         label: str = "", enabled: bool = True) -> str:
    """Register public wallet identity; private credentials have no storage field."""
    account_label = account_label.strip()
    chain = chain.strip().lower()
    asset_code = asset_code.strip().upper()
    address = address.strip()
    token_contract = token_contract.strip().lower()
    if not all((account_label, chain, asset_code, address)):
        raise ValueError("wallet account, chain, asset and public address are required")
    wallet_id = hashlib.sha256(
        f"crypto-wallet\0{chain}\0{asset_code}\0{address}\0{token_contract}".encode()
    ).hexdigest()[:32]
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        connection.execute("""INSERT INTO crypto_wallets
            (id, account_label, chain, asset_code, address, token_contract, label,
             enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain, asset_code, address, token_contract) DO UPDATE SET
              account_label = excluded.account_label, label = excluded.label,
              enabled = excluded.enabled, updated_at = excluded.updated_at""",
            (wallet_id, account_label, chain, asset_code, address, token_contract,
             label.strip(), int(bool(enabled)), now, now))
        stored = connection.execute("""SELECT id FROM crypto_wallets
            WHERE chain = ? AND asset_code = ? AND address = ? AND token_contract = ?""",
            (chain, asset_code, address, token_contract)).fetchone()
        return stored["id"]


def record_crypto_refresh(
    path: str | Path,
    *,
    wallet_id: str,
    fetched_at: str,
    status: str,
    operation_key: str,
    source: str,
    balance=None,
    transactions: list[dict] | None = None,
    message: str = "",
) -> dict:
    """Atomically persist a wallet refresh, observations, transactions and receipt."""
    status = status.strip().lower()
    if status not in {"ok", "error"}:
        raise ValueError("crypto refresh status must be ok or error")
    if not fetched_at.strip() or not source.strip() or not operation_key.strip():
        raise ValueError("refresh time, source and operation key are required")
    balance_text = None if balance is None else _non_negative_decimal_text(balance, "balance")
    if status == "ok" and balance_text is None:
        raise ValueError("successful crypto refresh requires a balance")
    if status == "error" and (balance_text is not None or transactions):
        raise ValueError("failed crypto refresh cannot contain new observations")
    normalized_transactions = []
    for item in transactions or []:
        occurred_on = _iso_date(str(item.get("occurred_on", "")), "occurred_on")
        chain_tx_id = str(item.get("chain_tx_id", "")).strip()
        operation = str(item.get("operation", "")).strip()
        if not chain_tx_id or not operation:
            raise ValueError("crypto transaction ID and operation are required")
        quantity = item.get("quantity")
        fee = item.get("fee")
        normalized_transactions.append({
            "occurred_on": occurred_on,
            "chain_tx_id": chain_tx_id,
            "operation": operation,
            "quantity": None if quantity is None or str(quantity).strip() == "" else
                _positive_decimal_text(quantity, "crypto quantity"),
            "fee": None if fee is None or str(fee).strip() == "" else
                _non_negative_decimal_text(fee, "crypto fee"),
            "counterparty": str(item.get("counterparty", "")),
            "comment": str(item.get("comment", "")),
        })
    normalized_transactions.sort(key=lambda item: (
        item["occurred_on"], item["chain_tx_id"], item["operation"]))
    request = {
        "wallet_id": wallet_id, "fetched_at": fetched_at.strip(), "status": status,
        "source": source.strip(), "balance": balance_text,
        "transactions": normalized_transactions, "message": message,
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "record_crypto_refresh" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        wallet = connection.execute(
            "SELECT * FROM crypto_wallets WHERE id = ?", (wallet_id,)).fetchone()
        if wallet is None:
            raise ValueError("unknown crypto wallet")
        observation_id = None
        if balance_text is not None:
            observation_id = hashlib.sha256(
                f"crypto-balance\0{wallet_id}\0{operation_key}".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO crypto_balance_observations
                (id, wallet_id, fetched_at, quantity_text, source)
                VALUES (?, ?, ?, ?, ?)""",
                (observation_id, wallet_id, fetched_at.strip(), balance_text, source.strip()))
        transaction_ids = []
        for item in normalized_transactions:
            transaction_id = hashlib.sha256(
                f"crypto-transaction\0{wallet_id}\0{item['chain_tx_id']}\0{item['operation']}".encode()
            ).hexdigest()[:32]
            existing = connection.execute("""SELECT occurred_on, quantity_text, fee_text,
                counterparty, source, comment FROM crypto_transactions
                WHERE wallet_id = ? AND chain_tx_id = ? AND operation = ?""",
                (wallet_id, item["chain_tx_id"], item["operation"])).fetchone()
            values = (item["occurred_on"], item["quantity"], item["fee"],
                      item["counterparty"], source.strip(), item["comment"])
            if existing is None:
                connection.execute("""INSERT INTO crypto_transactions
                    (id, wallet_id, chain_tx_id, occurred_on, operation, quantity_text,
                     fee_text, counterparty, source, comment)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (transaction_id, wallet_id, item["chain_tx_id"], item["occurred_on"],
                     item["operation"], item["quantity"], item["fee"], item["counterparty"],
                     source.strip(), item["comment"]))
            elif tuple(existing) != values:
                raise ValueError("network transaction identity has conflicting payload")
            transaction_ids.append(transaction_id)
        refresh_id = hashlib.sha256(
            f"crypto-refresh\0{wallet_id}\0{operation_key}".encode()).hexdigest()[:32]
        connection.execute("""INSERT INTO crypto_refresh_results
            (id, fetched_at, wallet_id, observed_account, observed_chain,
             observed_asset, observed_address, status, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (refresh_id, fetched_at.strip(), wallet_id, wallet["account_label"],
             wallet["chain"], wallet["asset_code"], wallet["address"], status, message))
        result = {"refresh_id": refresh_id, "observation_id": observation_id,
                  "transaction_ids": transaction_ids, "status": status}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'record_crypto_refresh',
             'crypto_refresh_result', ?, ?, ?)""",
            (operation_key, refresh_id, json.dumps(payload, sort_keys=True), _utc_now()))
        return result


def save_fx_rate(path: str | Path, *, rate_date: str, currency: str, usd_rate,
                 source: str = "manual", fetched_at: str = "", sequence: int = 0,
                 observation_id: str | None = None) -> str:
    rate_date = _iso_date(rate_date, "rate_date")
    if not source.strip():
        raise ValueError("FX source is required")
    rate = parse_money_amount(usd_rate, field_name="usd_rate")
    if rate <= 0:
        raise ValueError("usd_rate must be positive")
    with connect_database(path, writable=True) as connection:
        if connection.execute("SELECT 1 FROM currencies WHERE code = ?", (currency.upper(),)).fetchone() is None:
            raise ValueError("unsupported currency")
        observation_id = observation_id or uuid4().hex
        connection.execute(
            """INSERT INTO fx_rate_observations VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (observation_id, rate_date, currency.upper(), format(rate, "f"), source.strip(),
             fetched_at or _utc_now(), sequence),
        )
    return observation_id


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


def link_entity_source(path: str | Path, entity_type: str, entity_id: str,
                       source_record_id: str, *, role: str = "original") -> None:
    with connect_database(path, writable=True) as connection:
        connection.execute("INSERT INTO entity_source_links VALUES (?, ?, ?, ?)",
                           (entity_type, entity_id, source_record_id, role))


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
