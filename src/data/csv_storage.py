from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from shutil import copy2
from stat import S_IMODE
from tempfile import mkstemp

import pandas as pd


def atomic_write_csv(data: pd.DataFrame, path: str | Path, **to_csv_kwargs) -> None:
    target_path = Path(path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(target_path)
    try:
        data.to_csv(temporary_path, **to_csv_kwargs)
        _sync_file(temporary_path)
        _preserve_target_mode(temporary_path, target_path)
        os.replace(temporary_path, target_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_copy_file(source: str | Path, target: str | Path) -> None:
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = _temporary_path(target_path)
    try:
        copy2(source, temporary_path)
        _sync_file(temporary_path)
        os.replace(temporary_path, target_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def create_unique_backup(source: str | Path, backup_root: str | Path) -> Path:
    source_path = Path(source)
    backup_path = Path(backup_root)
    backup_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    temporary_path = _temporary_path(backup_path / source_path.name)
    try:
        copy2(source_path, temporary_path)
        _sync_file(temporary_path)
        counter = 0
        while True:
            suffix = "" if counter == 0 else f"_{counter}"
            candidate = backup_path / (
                f"{source_path.stem}.backup_{timestamp}{suffix}{source_path.suffix}"
            )
            try:
                os.link(temporary_path, candidate)
                return candidate
            except FileExistsError:
                counter += 1
    finally:
        temporary_path.unlink(missing_ok=True)


def _temporary_path(target_path: Path) -> Path:
    file_descriptor, name = mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent
    )
    os.close(file_descriptor)
    return Path(name)


def _sync_file(path: Path) -> None:
    with path.open("rb") as file:
        os.fsync(file.fileno())


def _preserve_target_mode(temporary_path: Path, target_path: Path) -> None:
    if target_path.exists():
        temporary_path.chmod(S_IMODE(target_path.stat().st_mode))
