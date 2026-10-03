import json
from pathlib import Path
import shutil

import pytest

from src.data.sqlite_snapshot import create_snapshot, verify_snapshot
from src.data.sqlite_store import add_cash_transaction, initialize_database


def test_snapshot_manifest_verifies_restored_copy(tmp_path):
    database = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "published" / "finrep.sqlite3"
    initialize_database(database)
    add_cash_transaction(
        database, transaction_id="salary", occurred_on="2026-10-01",
        flow_direction="income", category_id="income.salary",
        amount="123.45", currency="RUB")

    created = create_snapshot(database, snapshot)
    manifest = snapshot.with_suffix(".sqlite3.manifest.json")
    restored = tmp_path / "restored" / snapshot.name
    restored.parent.mkdir()
    shutil.copy2(snapshot, restored)
    shutil.copy2(manifest, restored.with_suffix(".sqlite3.manifest.json"))
    verified = verify_snapshot(restored)

    assert created["sha256"] == verified["sha256"]
    assert created["table_counts"]["cash_transactions"] == 1
    assert verified["verified"]
    assert json.loads(manifest.read_text(encoding="utf-8"))["storage_epoch"]


def test_snapshot_verification_does_not_write_to_restored_directory(tmp_path):
    database = tmp_path / "source.sqlite3"
    published = tmp_path / "published" / "finrep.sqlite3"
    initialize_database(database)
    create_snapshot(database, published)

    restored_dir = tmp_path / "restored-read-only"
    restored_dir.mkdir()
    restored = restored_dir / published.name
    manifest = published.with_suffix(".sqlite3.manifest.json")
    restored_manifest = restored.with_suffix(".sqlite3.manifest.json")
    shutil.copy2(published, restored)
    shutil.copy2(manifest, restored_manifest)
    restored.chmod(0o444)
    restored_manifest.chmod(0o444)
    restored_dir.chmod(0o555)
    try:
        assert verify_snapshot(restored)["verified"] is True
        assert not restored.with_name(f"{restored.name}-wal").exists()
        assert not restored.with_name(f"{restored.name}-shm").exists()
    finally:
        restored_dir.chmod(0o755)


def test_snapshot_verification_rejects_changed_file(tmp_path):
    database = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    create_snapshot(database, snapshot)
    with snapshot.open("ab") as stream:
        stream.write(b"changed-after-publication")

    with pytest.raises(ValueError, match="sha256"):
        verify_snapshot(snapshot)


def test_snapshot_creation_never_overwrites_snapshot_or_manifest(tmp_path):
    database = tmp_path / "source.sqlite3"
    snapshot = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    first = create_snapshot(database, snapshot)

    with pytest.raises(FileExistsError):
        create_snapshot(database, snapshot)
    assert verify_snapshot(snapshot)["sha256"] == first["sha256"]
