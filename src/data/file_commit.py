from __future__ import annotations

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
from hashlib import sha256
import json
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import mkstemp
from typing import Any


class FileCommitRecoveryError(RuntimeError):
    pass


def commit_file_images(
    journal_path: str | Path,
    images: dict[str | Path, bytes],
    *,
    receipt_path: str | Path | None = None,
    receipt: dict[str, Any] | None = None,
) -> None:
    journal = Path(journal_path)
    recover_file_commit(journal)
    entries = []
    for path, content in images.items():
        target = Path(path).resolve()
        entries.append(
            {
                "path": str(target),
                "before_sha256": _file_digest(target),
                "after_sha256": _bytes_digest(content),
                "content_base64": b64encode(content).decode("ascii"),
            }
        )
    manifest = {
        "version": 1,
        "entries": entries,
        "receipt_path": None if receipt_path is None else str(Path(receipt_path).resolve()),
        "receipt": receipt,
    }
    _atomic_write_json(journal, manifest)
    recover_file_commit(journal)


def recover_file_commit(journal_path: str | Path) -> dict[str, Any] | None:
    journal = Path(journal_path)
    if not journal.exists():
        return None
    try:
        manifest = json.loads(journal.read_text(encoding="utf-8"))
        entries = _validated_entries(manifest)
    except (
        OSError,
        AttributeError,
        Base64Error,
        ValueError,
        TypeError,
        KeyError,
        json.JSONDecodeError,
    ) as error:
        raise FileCommitRecoveryError(
            f"Не удалось прочитать журнал сохранения {journal}."
        ) from error

    receipt_path = manifest.get("receipt_path")
    receipt = manifest.get("receipt")
    if receipt_path is not None and not isinstance(receipt, dict):
        raise FileCommitRecoveryError("Журнал сохранения содержит некорректный receipt.")

    states = []
    for entry in entries:
        current_digest = _file_digest(entry["path"])
        if current_digest not in {entry["before_sha256"], entry["after_sha256"]}:
            raise FileCommitRecoveryError(
                f"Файл {entry['path']} изменён после сбоя сохранения. "
                "Автоматическое восстановление остановлено, чтобы не потерять правки."
            )
        states.append(current_digest)

    for entry, current_digest in zip(entries, states):
        if current_digest != entry["after_sha256"]:
            _atomic_write_bytes(entry["path"], entry["content"])

    if receipt_path is not None:
        _atomic_write_json(Path(receipt_path), receipt)

    _remove_journal(journal)
    return manifest


def read_commit_receipt(path: str | Path) -> dict[str, Any] | None:
    receipt_path = Path(path)
    if not receipt_path.exists():
        return None
    try:
        value = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FileCommitRecoveryError(
            f"Не удалось прочитать результат сохранения {receipt_path}."
        ) from error
    if not isinstance(value, dict):
        raise FileCommitRecoveryError(f"Некорректный результат сохранения {receipt_path}.")
    return value


def _validated_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(manifest, dict):
        raise ValueError("journal must be an object")
    if manifest.get("version") != 1 or not isinstance(manifest.get("entries"), list):
        raise ValueError("unsupported journal format")
    result = []
    for raw in manifest["entries"]:
        path = Path(raw["path"])
        before_digest = raw["before_sha256"]
        after_digest = raw["after_sha256"]
        if before_digest is not None and not _is_digest(before_digest):
            raise ValueError("invalid before digest")
        if not _is_digest(after_digest):
            raise ValueError("invalid after digest")
        content = b64decode(raw["content_base64"], validate=True)
        if _bytes_digest(content) != after_digest:
            raise ValueError("journal payload digest mismatch")
        result.append(
            {
                "path": path,
                "before_sha256": before_digest,
                "after_sha256": after_digest,
                "content": content,
            }
        )
    if not result:
        raise ValueError("journal has no entries")
    return result


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    _atomic_write_bytes(path, content)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if path.exists():
            temporary.chmod(S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _remove_journal(path: Path) -> None:
    path.unlink()
    _sync_directory(path.parent)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return _bytes_digest(path.read_bytes())


def _bytes_digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )
