"""Validate or create the default LIVE SQLite database at process startup."""

from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import tempfile

from src import config
from src.data.sqlite_store import (
    SCHEMA_VERSION,
    _schema_checksum,
    backup_database,
    initialize_database,
)


_LEGACY_WORKING_DATA_GLOBS = (
    "transactions_info/*/*.csv",
    "assets_info/*/*.csv",
    "staging/transaction_drafts.csv",
    "rates/fx_rates.csv",
    "plans/goals.csv",
    "import_rules/categories.csv",
    "debts/*.csv",
    "investments/*.csv",
)


def _legacy_working_files(data_root: Path) -> list[Path]:
    return sorted({path for pattern in _LEGACY_WORKING_DATA_GLOBS
                   for path in data_root.glob(pattern) if path.is_file()})


def _verify_database(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"SQLite database is empty or inaccessible: {path}")
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported SQLite schema version {version}; expected {SCHEMA_VERSION}")
            migration = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if migration is None or migration[0] != _schema_checksum():
                raise RuntimeError("SQLite schema checksum mismatch")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("SQLite integrity check failed")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("SQLite foreign key check failed")
            if connection.execute(
                    "SELECT storage_epoch FROM app_metadata WHERE id = 1").fetchone() is None:
                raise RuntimeError("SQLite app metadata is missing")
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Cannot open SQLite database {path}: {exc}") from exc


def _schema_version(path: Path) -> int:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()


def _upgrade_existing_database(path: Path) -> bool:
    if path.stat().st_size == 0:
        return False
    version = _schema_version(path)
    if version == SCHEMA_VERSION:
        return False
    if version not in {7, 8, 9}:
        raise RuntimeError(
            f"Unsupported SQLite schema version {version}; expected {SCHEMA_VERSION}")
    backup = path.with_name(f"{path.stem}.pre-v{SCHEMA_VERSION}{path.suffix}")
    if not backup.exists():
        backup_database(path, backup)
    initialize_database(path, data_mode="live")
    return True


def ensure_default_live_database() -> str:
    """Return backend state after safely preparing the configured LIVE database."""
    if config.get_storage_backend() != "sqlite":
        return "csv"

    database = config.active_database_path()
    if database.exists():
        upgraded = _upgrade_existing_database(database)
        _verify_database(database)
        return "upgraded" if upgraded else "existing"

    legacy_files = _legacy_working_files(Path(config.DATA_PATH))
    if legacy_files:
        raise RuntimeError(
            f"SQLite database does not exist, but legacy CSV data was found under "
            f"{config.DATA_PATH}. Run the one-time SQLite migration before starting "
            "LIVE mode, or set FINREP_SQLITE_PATH to an existing database."
        )

    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{database.name}.", suffix=".tmp", dir=database.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        initialize_database(temporary, data_mode="live")
        _verify_database(temporary)
        try:
            os.link(temporary, database)
            state = "created"
        except FileExistsError:
            _verify_database(database)
            state = "existing"
        temporary.unlink()
        directory_descriptor = os.open(database.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return state
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
