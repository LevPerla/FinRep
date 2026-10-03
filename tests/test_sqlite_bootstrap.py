from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

import pytest

from src import config
from src.data.sqlite_bootstrap import ensure_default_live_database
from src.data.sqlite_store import SCHEMA_VERSION, initialize_database


def _use_default_sqlite(monkeypatch, data_root: Path) -> Path:
    monkeypatch.delenv("FINREP_STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("FINREP_SQLITE_PATH", raising=False)
    monkeypatch.setattr(config, "DATA_PATH", str(data_root))
    return data_root / "finrep.sqlite3"


def test_sqlite_is_the_default_backend(monkeypatch):
    monkeypatch.delenv("FINREP_STORAGE_BACKEND", raising=False)

    assert config.get_storage_backend() == "sqlite"


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
