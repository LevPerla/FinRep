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
