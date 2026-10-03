"""Create and verify restic-ready SQLite snapshots."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from src.data.sqlite_store import SCHEMA_VERSION, backup_database


MANIFEST_VERSION = 1


def create_snapshot(
    source_path: str | Path,
    destination_path: str | Path,
    manifest_path: str | Path | None = None,
) -> dict:
    """Create a consistent SQLite snapshot and an immutable verification manifest."""
    source = Path(source_path).resolve()
    destination = Path(destination_path).resolve()
    manifest = _manifest_path(destination, manifest_path)
    if manifest.exists():
        raise FileExistsError("snapshot manifest already exists")
    try:
        backup_database(source, destination)
        payload = _snapshot_fingerprint(destination)
        payload.update({
            "manifest_version": MANIFEST_VERSION,
            "created_at": _utc_now(),
            "snapshot_file": destination.name,
            "source_file": source.name,
        })
        _write_manifest(manifest, payload)
        return payload
    except Exception:
        destination.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        raise


def verify_snapshot(
    snapshot_path: str | Path,
    manifest_path: str | Path | None = None,
) -> dict:
    """Verify file identity, SQLite integrity, schema metadata and table counts."""
    snapshot = Path(snapshot_path).resolve()
    manifest = _manifest_path(snapshot, manifest_path)
    expected = json.loads(manifest.read_text(encoding="utf-8"))
    if expected.get("manifest_version") != MANIFEST_VERSION:
        raise ValueError("unsupported snapshot manifest version")
    if expected.get("snapshot_file") != snapshot.name:
        raise ValueError("snapshot filename does not match manifest")
    actual = _snapshot_fingerprint(snapshot)
    for field in (
        "sha256", "size_bytes", "schema_version", "storage_epoch", "data_mode",
        "table_counts",
    ):
        if actual.get(field) != expected.get(field):
            raise ValueError(f"snapshot verification failed for {field}")
    return {
        "verified": True,
        "snapshot_file": snapshot.name,
        "sha256": actual["sha256"],
        "size_bytes": actual["size_bytes"],
        "schema_version": actual["schema_version"],
        "data_mode": actual["data_mode"],
        "table_counts": actual["table_counts"],
    }


def _snapshot_fingerprint(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only = ON")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"snapshot integrity_check failed: {integrity}")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("snapshot foreign_key_check failed")
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if schema_version != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported snapshot schema version {schema_version}; expected {SCHEMA_VERSION}")
        metadata = connection.execute(
            "SELECT storage_epoch, data_mode FROM app_metadata WHERE id = 1").fetchone()
        if metadata is None:
            raise ValueError("snapshot app metadata is missing")
        tables = [row[0] for row in connection.execute("""SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name""")]
        counts = {
            table: connection.execute(
                f"SELECT count(*) FROM {_quote_identifier(table)}").fetchone()[0]
            for table in tables
        }
    finally:
        connection.close()
    return {
        "sha256": digest.hexdigest(),
        "size_bytes": path.stat().st_size,
        "schema_version": schema_version,
        "storage_epoch": metadata[0],
        "data_mode": metadata[1],
        "table_counts": counts,
    }


def _manifest_path(snapshot: Path, manifest_path: str | Path | None) -> Path:
    return (Path(manifest_path).resolve() if manifest_path is not None
            else snapshot.with_suffix(f"{snapshot.suffix}.manifest.json"))


def _write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        temporary.unlink()
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _quote_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--source", required=True)
    create.add_argument("--destination", required=True)
    create.add_argument("--manifest")
    verify = commands.add_parser("verify")
    verify.add_argument("--snapshot", required=True)
    verify.add_argument("--manifest")
    args = parser.parse_args()
    if args.command == "create":
        result = create_snapshot(args.source, args.destination, args.manifest)
    else:
        result = verify_snapshot(args.snapshot, args.manifest)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    _main()
