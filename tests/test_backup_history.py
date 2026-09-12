from concurrent.futures import ThreadPoolExecutor
from datetime import datetime as RealDatetime
import multiprocessing
from pathlib import Path

import pandas as pd
import pytest

from src import config
from src.data import assets_editor, csv_storage, staging


class FrozenDatetime:
    @classmethod
    def now(cls):
        return RealDatetime(2026, 9, 12, 12, 0, 0, 123456)


def _backup_worker(source: str, backup_root: str, results) -> None:
    results.put(str(csv_storage.create_unique_backup(source, backup_root)))


def test_two_backups_at_same_time_preserve_both_source_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_storage, "datetime", FrozenDatetime)
    source = tmp_path / "2026_09.csv"
    backup_root = tmp_path / "backups"
    source.write_text("value\nV1\n", encoding="utf-8")

    first = csv_storage.create_unique_backup(source, backup_root)
    source.write_text("value\nV2\n", encoding="utf-8")
    second = csv_storage.create_unique_backup(source, backup_root)

    assert first != second
    assert first.name == "2026_09.backup_20260912_120000_123456.csv"
    assert second.name == "2026_09.backup_20260912_120000_123456_1.csv"
    assert first.read_text(encoding="utf-8") == "value\nV1\n"
    assert second.read_text(encoding="utf-8") == "value\nV2\n"


def test_parallel_backup_creation_allocates_unique_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(csv_storage, "datetime", FrozenDatetime)
    source = tmp_path / "data.csv"
    source.write_text("value\ncomplete\n", encoding="utf-8")
    backup_root = tmp_path / "backups"

    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(
            pool.map(lambda _: csv_storage.create_unique_backup(source, backup_root), range(12))
        )

    assert len(set(paths)) == 12
    assert all(path.read_text(encoding="utf-8") == "value\ncomplete\n" for path in paths)


def test_backup_creation_does_not_overwrite_between_processes(tmp_path, monkeypatch):
    source = tmp_path / "source.csv"
    backup_root = tmp_path / "backups"
    source.write_text("value\ncomplete\n", encoding="utf-8")
    monkeypatch.setattr(csv_storage, "datetime", FrozenDatetime)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    processes = [
        context.Process(target=_backup_worker, args=(str(source), str(backup_root), results))
        for _ in range(2)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    paths = [Path(results.get(timeout=1)) for _ in range(2)]
    assert len(set(paths)) == 2
    assert all(path.read_text(encoding="utf-8") == "value\ncomplete\n" for path in paths)


def test_asset_saves_return_distinct_restorable_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(csv_storage, "datetime", FrozenDatetime)
    assets_root = tmp_path / "assets_info"
    target = assets_editor.asset_snapshot_path("2026", "09", assets_root)
    csv_storage.atomic_write_csv(
        pd.DataFrame([{"Счет": "Synthetic", "Сумма": "100|RUB"}]),
        target,
        sep=";",
        index=False,
        encoding="utf-8-sig",
    )

    first = assets_editor.write_asset_snapshot(
        [{"account": "Synthetic", "amount": 200, "currency": "RUB"}],
        "2026",
        "09",
        assets_root,
    )
    second = assets_editor.write_asset_snapshot(
        [{"account": "Synthetic", "amount": 300, "currency": "RUB"}],
        "2026",
        "09",
        assets_root,
    )

    first_value = pd.read_csv(first["backup_path"], sep=";").iloc[0]["Сумма"]
    second_value = pd.read_csv(second["backup_path"], sep=";").iloc[0]["Сумма"]
    assert first["backup_path"] != second["backup_path"]
    assert [first_value, second_value] == ["100|RUB", "200|RUB"]


def test_month_exports_return_distinct_restorable_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(csv_storage, "datetime", FrozenDatetime)
    drafts = tmp_path / "staging" / "transaction_drafts.csv"
    transactions_root = tmp_path / "transactions_info"
    target = staging.monthly_transaction_csv_path("2026", "09", transactions_root)
    csv_storage.atomic_write_csv(
        pd.DataFrame([{"Дата": "01.09.2026", "Прочее": "50|RUB|V1"}]),
        target,
        sep=";",
        index=False,
        encoding="utf-8-sig",
    )
    staging.append_transaction_draft(
        "2026-09-01", "Прочее", "RUB", 100, "A", source_id="A", path=drafts
    )
    preview, state = staging.prepare_monthly_transaction_export(
        "2026", "09", drafts, transactions_root
    )
    first = staging.export_monthly_transaction_drafts(
        "2026", "09", drafts, transactions_root, preview.to_dict("records"), state
    )
    staging.append_transaction_draft(
        "2026-09-01", "Прочее", "RUB", 200, "B", source_id="B", path=drafts
    )
    preview, state = staging.prepare_monthly_transaction_export(
        "2026", "09", drafts, transactions_root
    )
    second = staging.export_monthly_transaction_drafts(
        "2026", "09", drafts, transactions_root, preview.to_dict("records"), state
    )

    first_value = pd.read_csv(first["backup_path"], sep=";").iloc[0]["Прочее"]
    second_value = pd.read_csv(second["backup_path"], sep=";").iloc[0]["Прочее"]
    assert first["backup_path"] != second["backup_path"]
    assert first_value == "50|RUB|V1"
    assert second_value == "50|RUB|V1#100|RUB|A"


def test_failed_unique_backup_leaves_no_final_or_temporary_file(tmp_path, monkeypatch):
    source = tmp_path / "source.csv"
    source.write_text("value\ncomplete\n", encoding="utf-8")
    backup_root = tmp_path / "backups"

    def interrupted_copy(source_path, temporary_path):
        Path(temporary_path).write_text("value\npartial", encoding="utf-8")
        raise OSError("synthetic copy interruption")

    monkeypatch.setattr(csv_storage, "copy2", interrupted_copy)

    with pytest.raises(OSError, match="copy interruption"):
        csv_storage.create_unique_backup(source, backup_root)

    assert list(backup_root.iterdir()) == []
