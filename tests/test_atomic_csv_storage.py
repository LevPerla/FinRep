from pathlib import Path

import pandas as pd
import pytest

from src import config
from src.data import csv_storage, staging


def _temporary_files(target: Path) -> list[Path]:
    return list(target.parent.glob(f".{target.name}.*.tmp"))


def test_atomic_csv_write_replaces_target_with_complete_file(tmp_path):
    target = tmp_path / "nested" / "data.csv"
    target.parent.mkdir()
    target.write_text("value\nold\n", encoding="utf-8")

    csv_storage.atomic_write_csv(pd.DataFrame({"value": ["new"]}), target, index=False)

    assert target.read_text(encoding="utf-8") == "value\nnew\n"
    assert _temporary_files(target) == []


def test_serialization_failure_preserves_old_target_and_removes_temporary_file(
    tmp_path, monkeypatch
):
    target = tmp_path / "data.csv"
    target.write_text("value\nold\n", encoding="utf-8")

    def interrupted_to_csv(self, path_or_buf=None, *args, **kwargs):
        Path(path_or_buf).write_text("value\npartial", encoding="utf-8")
        raise OSError("synthetic serialization interruption")

    monkeypatch.setattr(pd.DataFrame, "to_csv", interrupted_to_csv)

    with pytest.raises(OSError, match="serialization interruption"):
        csv_storage.atomic_write_csv(pd.DataFrame({"value": ["new"]}), target, index=False)

    assert target.read_text(encoding="utf-8") == "value\nold\n"
    assert _temporary_files(target) == []


def test_replace_failure_preserves_old_target_and_removes_complete_temporary_file(
    tmp_path, monkeypatch
):
    target = tmp_path / "data.csv"
    target.write_text("value\nold\n", encoding="utf-8")

    def interrupted_replace(source, destination):
        raise OSError("synthetic replace interruption")

    monkeypatch.setattr(csv_storage.os, "replace", interrupted_replace)

    with pytest.raises(OSError, match="replace interruption"):
        csv_storage.atomic_write_csv(pd.DataFrame({"value": ["new"]}), target, index=False)

    assert target.read_text(encoding="utf-8") == "value\nold\n"
    assert _temporary_files(target) == []


def test_atomic_copy_preserves_old_target_on_replace_failure(tmp_path, monkeypatch):
    source = tmp_path / "source.csv"
    target = tmp_path / "target.csv"
    source.write_text("value\nnew\n", encoding="utf-8")
    target.write_text("value\nold\n", encoding="utf-8")

    monkeypatch.setattr(
        csv_storage.os,
        "replace",
        lambda source_path, target_path: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        csv_storage.atomic_copy_file(source, target)

    assert target.read_text(encoding="utf-8") == "value\nold\n"
    assert _temporary_files(target) == []


def test_staging_writer_uses_atomic_replace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    target = tmp_path / "staging" / "transaction_drafts.csv"
    staging.append_transaction_draft(
        "2026-01-01", "Прочее", "RUB", 100, source_id="A", path=target
    )
    before = target.read_bytes()
    monkeypatch.setattr(
        csv_storage.os,
        "replace",
        lambda source_path, target_path: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        staging.append_transaction_draft(
            "2026-01-02", "Прочее", "RUB", 200, source_id="B", path=target
        )

    assert target.read_bytes() == before
    assert _temporary_files(target) == []
