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


SCHEMA_VERSION = 1

_SCHEMA = (
    """CREATE TABLE schema_meta (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        version INTEGER NOT NULL,
        storage_epoch TEXT NOT NULL
    )""",
    """CREATE TABLE categories (
        id TEXT PRIMARY KEY,
        financial_kind TEXT NOT NULL CHECK (financial_kind IN (
            'income', 'other_inflow', 'expense', 'receivable_open', 'receivable_repay',
            'liability_open', 'liability_repay', 'investment_legacy'
        )),
        name_ru TEXT NOT NULL CHECK (name_ru <> ''),
        name_en TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
    )""",
    """CREATE TABLE category_aliases (
        legacy_name TEXT PRIMARY KEY,
        category_id TEXT NOT NULL REFERENCES categories(id)
    )""",
    """CREATE TABLE income_types (
        id TEXT PRIMARY KEY,
        name_ru TEXT NOT NULL CHECK (name_ru <> ''),
        name_en TEXT,
        activity_class TEXT NOT NULL CHECK (activity_class IN ('active', 'passive', 'unclassified')),
        archived INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
        CHECK ((id = 'unknown' AND activity_class = 'unclassified' AND archived = 1)
            OR (id <> 'unknown' AND activity_class IN ('active', 'passive')))
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
        amount_text TEXT NOT NULL CHECK (amount_text <> ''),
        currency TEXT NOT NULL CHECK (currency <> ''),
        comment TEXT NOT NULL DEFAULT '',
        income_type_id TEXT REFERENCES income_types(id),
        classification_method TEXT,
        source TEXT,
        source_id TEXT,
        legacy_category TEXT,
        legacy_coordinate TEXT
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
    ("flow.income", "income", "Доход"),
    ("flow.other_inflow", "other_inflow", "Сбережения"),
    ("flow.receivable_open", "receivable_open", "Дебиторская задолженность"),
    ("flow.receivable_repay", "receivable_repay", "Погашение деб. зад."),
    ("flow.liability_open", "liability_open", "Кредиторская задолженность"),
    ("flow.liability_repay", "liability_repay", "Погашение кред. зад."),
    ("flow.investment_legacy", "investment_legacy", "Инвестиции"),
)

_INITIAL_INCOME_TYPES = (
    ("salary", "Зарплата", "active", 0),
    ("deposit_interest", "Проценты по депозиту", "passive", 0),
    ("other_income", "Прочий доход", "active", 0),
    ("unknown", "Не определено", "unclassified", 1),
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
            "INSERT INTO categories (id, financial_kind, name_ru) VALUES (?, ?, ?)",
            _SYSTEM_CATEGORIES,
        )
        connection.executemany(
            "INSERT INTO income_types (id, name_ru, activity_class, archived) VALUES (?, ?, ?, ?)",
            _INITIAL_INCOME_TYPES,
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


def add_category(path: str | Path, category_id: str, name_ru: str) -> None:
    """Add a user expense category without deriving its ID from its label."""
    if not category_id or not name_ru.strip():
        raise ValueError("category ID and name are required")
    with connect_database(path, writable=True) as connection:
        connection.execute(
            "INSERT INTO categories (id, financial_kind, name_ru) VALUES (?, 'expense', ?)",
            (category_id, name_ru.strip()),
        )


def add_income_type(
    path: str | Path, income_type_id: str, name_ru: str, activity_class: str,
) -> None:
    if not income_type_id or income_type_id == "unknown" or not name_ru.strip():
        raise ValueError("selectable income type needs an ID and name")
    if activity_class not in {"active", "passive"}:
        raise ValueError("selectable income type must be active or passive")
    with connect_database(path, writable=True) as connection:
        connection.execute(
            "INSERT INTO income_types (id, name_ru, activity_class) VALUES (?, ?, ?)",
            (income_type_id, name_ru.strip(), activity_class),
        )


def add_cash_transaction(
    path: str | Path, *, transaction_id: str, period: str, occurred_on: str,
    category_id: str, amount, currency: str, comment: str = "",
    income_type_id: str | None = None,
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
    if Decimal(amount_text) == 0:
        raise ValueError("zero cells are represented by the saved month, not transactions")
    with connect_database(path, writable=True) as connection:
        category = connection.execute(
            "SELECT financial_kind FROM categories WHERE id = ?", (category_id,)
        ).fetchone()
        if category is None:
            raise ValueError("unknown category")
        if category[0] == "income":
            income_type = connection.execute(
                "SELECT archived FROM income_types WHERE id = ?", (income_type_id,)
            ).fetchone()
            if income_type is None or income_type[0]:
                raise ValueError("new income needs an active income type")
        elif income_type_id is not None:
            raise ValueError("income type is only valid for income")
        connection.execute(
            "INSERT INTO months (period, transactions_saved) VALUES (?, 1) "
            "ON CONFLICT(period) DO UPDATE SET transactions_saved = 1",
            (period,),
        )
        connection.execute(
            """INSERT INTO cash_transactions
               (id, period, occurred_on, category_id, amount_text, currency, comment, income_type_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (transaction_id, period, occurred_on, category_id, amount_text, currency, comment, income_type_id),
        )


def cash_transactions(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute(
            "SELECT id, period, occurred_on, category_id, amount_text, currency, comment, income_type_id "
            "FROM cash_transactions ORDER BY occurred_on, id"
        ).fetchall()
    return [{**dict(row), "amount": Decimal(row["amount_text"])} for row in rows]


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
