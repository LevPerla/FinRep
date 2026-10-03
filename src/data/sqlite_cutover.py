"""Build and verify a new SQLite database from the legacy CSV data tree."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile

from src.data.sqlite_migration import build_manifest, migrate_core_csv
from src.data.sqlite_reconciliation import reconcile_migration
from src.data.sqlite_store import SCHEMA_VERSION, connect_database


def run_cutover_preflight(
    source_root: str | Path,
    target_db: str | Path,
    migration_db: str | Path,
    result_path: str | Path,
) -> dict:
    """Create fresh migration artifacts and return the complete cutover verdict."""
    source = Path(source_root).resolve()
    target = Path(target_db).resolve()
    audit = Path(migration_db).resolve()
    result = Path(result_path).resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"source data directory does not exist: {source}")
    outputs = (target, audit, result)
    if len(set(outputs)) != len(outputs):
        raise ValueError("target, migration audit and result paths must be different")
    existing = [path for path in outputs if path.exists()]
    if existing:
        raise FileExistsError(f"cutover output already exists: {existing[0]}")
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)

    entries, manifest_hash = build_manifest(source)
    summary = migrate_core_csv(source, target, audit)
    report = reconcile_migration(source, target, audit)
    with connect_database(target) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_violations = len(
            connection.execute("PRAGMA foreign_key_check").fetchall())
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        metadata = connection.execute(
            "SELECT data_mode FROM app_metadata WHERE id = 1").fetchone()

    validation_issues = []
    if schema_version != SCHEMA_VERSION:
        validation_issues.append(
            f"unsupported schema version {schema_version}; expected {SCHEMA_VERSION}")
    if metadata is None:
        validation_issues.append("app metadata is missing")
    elif metadata[0] != "migration":
        validation_issues.append(
            f"unexpected data mode {metadata[0]!r}; expected 'migration'")

    database_valid = (
        integrity == "ok"
        and foreign_key_violations == 0
        and not validation_issues
    )
    payload = {
        "manifest_hash": manifest_hash,
        "manifest": {
            "files_total": len(entries),
            "included": sum(item.status == "included" for item in entries),
            "excluded": sum(item.status == "excluded" for item in entries),
            "unknown": sum(item.status == "unknown" for item in entries),
            "families": sorted({item.family for item in entries}),
        },
        "migration": asdict(summary),
        "reconciliation": {
            "checks": [
                {"name": check.name, "passed": check.passed}
                for check in report.checks
            ],
            "passed": report.passed,
            "blocking_review_items": report.blocking_review_items,
            "ready_for_cutover": report.ready_for_cutover,
        },
        "database": {
            "integrity_check": integrity,
            "foreign_key_violations": foreign_key_violations,
            "schema_version": schema_version,
            "validation_issues": validation_issues,
            "size_bytes": target.stat().st_size,
        },
        "ready_for_cutover": report.ready_for_cutover and database_valid,
    }
    _write_json_once(result, payload)
    return payload


def _write_json_once(path: Path, payload: dict) -> None:
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


def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--migration-audit", required=True)
    parser.add_argument("--result", required=True)
    args = parser.parse_args()
    payload = run_cutover_preflight(
        args.source, args.target, args.migration_audit, args.result)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    if not payload["ready_for_cutover"]:
        raise SystemExit(2)


if __name__ == "__main__":
    _main()
