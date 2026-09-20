"""Isolated SQLite foundation for the CSV-to-SQLite dry run.

No application reader or writer uses this module before the cutover.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
import sqlite3
from uuid import uuid4

from src import config
from src.data.money import parse_money_amount


SCHEMA_VERSION = 3

_SCHEMA = (
    """CREATE TABLE schema_meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL,
        storage_epoch TEXT NOT NULL
    )""",
    """CREATE TABLE categories (
        id TEXT PRIMARY KEY,
        direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
        name_ru TEXT NOT NULL CHECK (name_ru <> ''),
        name_en TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        activity_class TEXT CHECK (activity_class IN ('active', 'passive', 'unclassified')),
        CHECK ((direction = 'income' AND activity_class IS NOT NULL)
            OR (direction = 'expense' AND activity_class IS NULL))
    )""",
    """CREATE TABLE category_aliases (
        legacy_name TEXT PRIMARY KEY,
        category_id TEXT NOT NULL REFERENCES categories(id)
    )""",
    """CREATE TABLE months (
        period TEXT PRIMARY KEY,
        transactions_saved INTEGER NOT NULL DEFAULT 0 CHECK (transactions_saved IN (0, 1)),
        assets_saved INTEGER NOT NULL DEFAULT 0 CHECK (assets_saved IN (0, 1))
    )""",
    """CREATE TABLE cash_transactions (
        id TEXT PRIMARY KEY,
        period TEXT NOT NULL REFERENCES months(period),
        occurred_on TEXT NOT NULL,
        category_id TEXT NOT NULL REFERENCES categories(id),
        amount_text TEXT NOT NULL CHECK (
            amount_text <> '' AND substr(amount_text, 1, 1) NOT IN ('-', '+')
        ),
        currency TEXT NOT NULL CHECK (currency <> ''),
        comment TEXT NOT NULL DEFAULT '',
        classification_method TEXT,
        source TEXT,
        source_id TEXT,
        legacy_coordinate TEXT
    )""",
    """CREATE TABLE fx_rates (
        date TEXT NOT NULL,
        currency TEXT NOT NULL,
        usd_rate_text TEXT NOT NULL CHECK (
            usd_rate_text <> '' AND substr(usd_rate_text, 1, 1) NOT IN ('-', '+')
        ),
        source TEXT NOT NULL DEFAULT '',
        fetched_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (date, currency)
    )""",
    """CREATE TABLE asset_accounts (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL CHECK (name <> ''),
        currency TEXT NOT NULL CHECK (currency <> '')
    )""",
    """CREATE TABLE asset_snapshots (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES asset_accounts(id),
        period TEXT NOT NULL REFERENCES months(period),
        amount_text TEXT NOT NULL CHECK (amount_text <> ''),
        currency TEXT NOT NULL CHECK (currency <> ''),
        legacy_coordinate TEXT
    )""",
)

_SYSTEM_CATEGORIES = (
    ("income.salary", "income", "Зарплата", 1, "active"),
    ("income.interest", "income", "Проценты", 1, "passive"),
    ("income.investment", "income", "Инвест доход", 1, "passive"),
    ("income.other", "income", "Прочие доходы", 1, "active"),
    ("income.unknown", "income", "Доход без категории", 0, "unclassified"),
    ("expense.home_goods", "expense", "Быт и товары для дома", 1, None),
    ("expense.personal", "expense", "На себя", 1, None),
    ("expense.clothing", "expense", "Одежда", 1, None),
    ("expense.food", "expense", "Пища", 1, None),
    ("expense.travel", "expense", "Поездки", 1, None),
    ("expense.other", "expense", "Прочее", 1, None),
    ("expense.communication", "expense", "Связь", 1, None),
    ("expense.entertainment_legacy", "expense", "Развлечения", 1, None),
    ("expense.social", "expense", "Соц жизнь", 1, None),
    ("expense.transport", "expense", "Транспорт", 1, None),
)


@contextmanager
def connect_database(path: str | Path, *, writable: bool = False):
    """Open an explicit database path; reads never create a database."""
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
        if not writable:
            connection.execute("PRAGMA query_only = ON")
        yield connection
        if writable:
            connection.commit()
    except Exception:
        if writable:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(path: str | Path) -> None:
    """Create the first schema only in an empty, explicitly named database."""
    with connect_database(path, writable=True) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            meta = connection.execute("SELECT version FROM schema_meta WHERE id = 1").fetchone()
            if meta is None or meta[0] != version:
                raise ValueError("SQLite schema version mismatch")
            return
        if version != 0 or connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone():
            raise ValueError("SQLite database is not empty or has an unsupported schema")
        for statement in _SCHEMA:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_meta VALUES (1, ?, ?)", (SCHEMA_VERSION, uuid4().hex)
        )
        connection.executemany(
            "INSERT INTO categories (id, direction, name_ru, active, activity_class) "
            "VALUES (?, ?, ?, ?, ?)",
            _SYSTEM_CATEGORIES,
        )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _period(value: str) -> str:
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except ValueError as exc:
        raise ValueError("period must be YYYY-MM") from exc
    if parsed.strftime("%Y-%m") != value:
        raise ValueError("period must be YYYY-MM")
    return value


def _amount_text(value) -> str:
    return format(parse_money_amount(value), "f")


def save_month(path: str | Path, period: str) -> None:
    """Record a saved transaction month even when it contains no operations."""
    period = _period(period)
    with connect_database(path, writable=True) as connection:
        connection.execute(
            "INSERT INTO months (period, transactions_saved) VALUES (?, 1) "
            "ON CONFLICT(period) DO UPDATE SET transactions_saved = 1",
            (period,),
        )


def saved_months(path: str | Path) -> list[str]:
    with connect_database(path) as connection:
        return [row[0] for row in connection.execute(
            "SELECT period FROM months WHERE transactions_saved = 1 ORDER BY period"
        )]


def add_category(
    path: str | Path, category_id: str, name_ru: str, *,
    direction: str = "expense", activity_class: str | None = None,
) -> None:
    """Add a user category without deriving its ID from its label."""
    if not category_id or not name_ru.strip():
        raise ValueError("category ID and name are required")
    if direction not in {"income", "expense"}:
        raise ValueError("category direction must be income or expense")
    if (direction == "income" and activity_class not in {"active", "passive"}) or (
        direction == "expense" and activity_class is not None
    ):
        raise ValueError("activity class belongs only to income categories")
    with connect_database(path, writable=True) as connection:
        connection.execute(
            "INSERT INTO categories (id, direction, name_ru, activity_class) VALUES (?, ?, ?, ?)",
            (category_id, direction, name_ru.strip(), activity_class),
        )


def add_cash_transaction(
    path: str | Path, *, transaction_id: str, period: str, occurred_on: str,
    category_id: str, amount, currency: str, comment: str = "",
) -> None:
    if not transaction_id:
        raise ValueError("transaction ID is required")
    period = _period(period)
    try:
        transaction_date = date.fromisoformat(occurred_on)
    except ValueError as exc:
        raise ValueError("occurred_on must be an ISO date") from exc
    if transaction_date.strftime("%Y-%m") != period:
        raise ValueError("transaction date must belong to period")
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError("unsupported currency")
    amount_text = _amount_text(amount)
    if Decimal(amount_text) <= 0:
        raise ValueError("transaction amount must be positive")
    with connect_database(path, writable=True) as connection:
        category = connection.execute(
            "SELECT active FROM categories WHERE id = ?", (category_id,)
        ).fetchone()
        if category is None or not category[0]:
            raise ValueError("new transaction needs an active category")
        connection.execute(
            "INSERT INTO months (period, transactions_saved) VALUES (?, 1) "
            "ON CONFLICT(period) DO UPDATE SET transactions_saved = 1",
            (period,),
        )
        connection.execute(
            """INSERT INTO cash_transactions
               (id, period, occurred_on, category_id, amount_text, currency, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (transaction_id, period, occurred_on, category_id, amount_text, currency, comment),
        )


def cash_transactions(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute(
            "SELECT t.id, t.period, t.occurred_on, t.category_id, c.direction, "
            "c.activity_class, t.amount_text, t.currency, t.comment "
            "FROM cash_transactions AS t JOIN categories AS c ON c.id = t.category_id "
            "ORDER BY t.occurred_on, t.id"
        ).fetchall()
    return [{**dict(row), "amount": Decimal(row["amount_text"])} for row in rows]


def save_fx_rate(
    path: str | Path, *, rate_date: str, currency: str, usd_rate,
    source: str = "", fetched_at: str = "",
) -> None:
    try:
        parsed_date = date.fromisoformat(rate_date)
    except ValueError as exc:
        raise ValueError("rate_date must be an ISO date") from exc
    if parsed_date.isoformat() != rate_date:
        raise ValueError("rate_date must be an ISO date")
    currency = currency.upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError("unsupported currency")
    rate_text = _amount_text(usd_rate)
    if Decimal(rate_text) <= 0:
        raise ValueError("usd_rate must be positive")
    with connect_database(path, writable=True) as connection:
        connection.execute(
            """INSERT INTO fx_rates (date, currency, usd_rate_text, source, fetched_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(date, currency) DO UPDATE SET
                   usd_rate_text = excluded.usd_rate_text,
                   source = excluded.source,
                   fetched_at = excluded.fetched_at""",
            (rate_date, currency, rate_text, source, fetched_at),
        )


def fx_rates(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute(
            "SELECT date, currency, usd_rate_text, source, fetched_at "
            "FROM fx_rates ORDER BY date, currency"
        ).fetchall()
    return [{**dict(row), "usd_rate": Decimal(row["usd_rate_text"])} for row in rows]


def save_asset_month(path: str | Path, period: str) -> None:
    period = _period(period)
    with connect_database(path, writable=True) as connection:
        connection.execute(
            "INSERT INTO months (period, assets_saved) VALUES (?, 1) "
            "ON CONFLICT(period) DO UPDATE SET assets_saved = 1",
            (period,),
        )


def saved_asset_months(path: str | Path) -> list[str]:
    with connect_database(path) as connection:
        return [row[0] for row in connection.execute(
            "SELECT period FROM months WHERE assets_saved = 1 ORDER BY period"
        )]


def add_asset_account(
    path: str | Path, account_id: str, name: str, currency: str,
) -> None:
    currency = currency.upper()
    if not account_id or not name.strip() or currency not in config.UNIQUE_TICKERS:
        raise ValueError("account ID, name and supported currency are required")
    with connect_database(path, writable=True) as connection:
        connection.execute(
            "INSERT INTO asset_accounts (id, name, currency) VALUES (?, ?, ?)",
            (account_id, name.strip(), currency),
        )


def add_asset_snapshot(
    path: str | Path, *, snapshot_id: str, account_id: str, period: str, amount,
) -> None:
    if not snapshot_id:
        raise ValueError("snapshot ID is required")
    period = _period(period)
    amount_text = _amount_text(amount)
    with connect_database(path, writable=True) as connection:
        account = connection.execute(
            "SELECT currency FROM asset_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None:
            raise ValueError("unknown asset account")
        connection.execute(
            "INSERT INTO months (period, assets_saved) VALUES (?, 1) "
            "ON CONFLICT(period) DO UPDATE SET assets_saved = 1",
            (period,),
        )
        connection.execute(
            """INSERT INTO asset_snapshots
               (id, account_id, period, amount_text, currency) VALUES (?, ?, ?, ?, ?)""",
            (snapshot_id, account_id, period, amount_text, account[0]),
        )


def asset_snapshots(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute(
            "SELECT id, account_id, period, amount_text, currency FROM asset_snapshots "
            "ORDER BY period, id"
        ).fetchall()
    return [{**dict(row), "amount": Decimal(row["amount_text"])} for row in rows]
