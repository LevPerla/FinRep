from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from src import config
from src.data import file_commit, staging


@pytest.fixture
def export_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    drafts = tmp_path / "staging" / "transaction_drafts.csv"
    transactions = tmp_path / "transactions_info"
    staging.append_transaction_draft(
        "2026-09-01",
        "Прочее",
        "RUB",
        100,
        "synthetic",
        source="test",
        source_id="A",
        path=drafts,
    )
    preview, state = staging.prepare_monthly_transaction_export(
        "2026", "09", path=drafts, transactions_root=transactions
    )
    return drafts, transactions, preview.to_dict("records"), state


@pytest.mark.parametrize("failed_write", [1, 2, 3, 4])
def test_retry_after_each_commit_write_exports_once(export_data, monkeypatch, failed_write):
    drafts, transactions, rows, state = export_data
    original = file_commit._atomic_write_bytes
    calls = 0

    def fail_selected_write(path, content):
        nonlocal calls
        calls += 1
        if calls == failed_write:
            raise OSError(f"injected write failure {failed_write}")
        return original(path, content)

    monkeypatch.setattr(file_commit, "_atomic_write_bytes", fail_selected_write)
    with pytest.raises(OSError, match="injected"):
        staging.export_monthly_transaction_drafts(
            "2026",
            "09",
            path=drafts,
            transactions_root=transactions,
            preview_rows=rows,
            preview_state=state,
        )

    monkeypatch.setattr(file_commit, "_atomic_write_bytes", original)
    result = staging.export_monthly_transaction_drafts(
        "2026",
        "09",
        path=drafts,
        transactions_root=transactions,
        preview_rows=rows,
        preview_state=state,
    )

    saved = pd.read_csv(result["target_path"], sep=";", dtype=str, encoding="utf-8-sig")
    assert saved.loc[0, "Прочее"] == "100|RUB|synthetic"
    assert staging.read_transaction_drafts(drafts).iloc[0]["status"] == "exported"
    assert not staging._transaction_export_journal_path(drafts).exists()


def test_pending_commit_is_recovered_before_next_staging_read(export_data, monkeypatch):
    drafts, transactions, rows, state = export_data
    original = file_commit._atomic_write_bytes
    calls = 0

    def fail_draft_write(path, content):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected draft failure")
        return original(path, content)

    monkeypatch.setattr(file_commit, "_atomic_write_bytes", fail_draft_write)
    with pytest.raises(OSError):
        staging.export_monthly_transaction_drafts(
            "2026", "09", path=drafts, transactions_root=transactions,
            preview_rows=rows, preview_state=state,
        )
    monkeypatch.setattr(file_commit, "_atomic_write_bytes", original)

    recovered = staging.read_transaction_drafts(drafts)

    target = staging.monthly_transaction_csv_path("2026", "09", transactions)
    saved = pd.read_csv(target, sep=";", dtype=str, encoding="utf-8-sig")
    assert saved.loc[0, "Прочее"] == "100|RUB|synthetic"
    assert recovered.iloc[0]["status"] == "exported"


def test_pending_commit_is_recovered_in_new_process(export_data, monkeypatch):
    drafts, transactions, rows, state = export_data
    original = file_commit._atomic_write_bytes
    calls = 0

    def fail_draft_write(path, content):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected draft failure")
        return original(path, content)

    monkeypatch.setattr(file_commit, "_atomic_write_bytes", fail_draft_write)
    with pytest.raises(OSError):
        staging.export_monthly_transaction_drafts(
            "2026", "09", path=drafts, transactions_root=transactions,
            preview_rows=rows, preview_state=state,
        )
    monkeypatch.setattr(file_commit, "_atomic_write_bytes", original)

    environment = os.environ.copy()
    environment["FINREP_DATA_DIR"] = str(drafts.parents[1])
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPATH"] = str(Path(__file__).parents[1])
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src.data.staging import read_transaction_drafts; "
            "print(read_transaction_drafts().iloc[0]['status'])",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    target = staging.monthly_transaction_csv_path("2026", "09", transactions)
    saved = pd.read_csv(target, sep=";", dtype=str, encoding="utf-8-sig")
    assert completed.stdout.strip() == "exported"
    assert saved.loc[0, "Прочее"] == "100|RUB|synthetic"
    assert not staging._transaction_export_journal_path(drafts).exists()


def test_retry_after_receipt_before_journal_removal_exports_once(export_data, monkeypatch):
    drafts, transactions, rows, state = export_data
    original_remove = file_commit._remove_journal
    failed = False

    def fail_remove(path):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected journal removal failure")
        return original_remove(path)

    monkeypatch.setattr(file_commit, "_remove_journal", fail_remove)
    with pytest.raises(OSError, match="removal"):
        staging.export_monthly_transaction_drafts(
            "2026", "09", path=drafts, transactions_root=transactions,
            preview_rows=rows, preview_state=state,
        )
    monkeypatch.setattr(file_commit, "_remove_journal", original_remove)

    result = staging.export_monthly_transaction_drafts(
        "2026", "09", path=drafts, transactions_root=transactions,
        preview_rows=rows, preview_state=state,
    )
    saved = pd.read_csv(result["target_path"], sep=";", dtype=str, encoding="utf-8-sig")
    assert saved.loc[0, "Прочее"] == "100|RUB|synthetic"
    assert staging.read_transaction_drafts(drafts).iloc[0]["status"] == "exported"


def test_completed_receipt_does_not_accept_another_requested_period(export_data):
    drafts, transactions, rows, state = export_data
    staging.export_monthly_transaction_drafts(
        "2026", "09", path=drafts, transactions_root=transactions,
        preview_rows=rows, preview_state=state,
    )

    with pytest.raises(ValueError, match="другого периода"):
        staging.export_monthly_transaction_drafts(
            "2026", "10", path=drafts, transactions_root=transactions,
            preview_rows=rows, preview_state=state,
        )


def test_completed_receipt_does_not_accept_another_transactions_root(export_data):
    drafts, transactions, rows, state = export_data
    staging.export_monthly_transaction_drafts(
        "2026", "09", path=drafts, transactions_root=transactions,
        preview_rows=rows, preview_state=state,
    )
    other_transactions = transactions.parent / "other-transactions"

    with pytest.raises(ValueError, match="Preview устарел"):
        staging.export_monthly_transaction_drafts(
            "2026", "09", path=drafts, transactions_root=other_transactions,
            preview_rows=rows, preview_state=state,
        )


def test_external_edit_blocks_recovery_without_overwriting_either_file(export_data, monkeypatch):
    drafts, transactions, rows, state = export_data
    original = file_commit._atomic_write_bytes
    calls = 0

    def fail_first_target(path, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected target failure")
        return original(path, content)

    monkeypatch.setattr(file_commit, "_atomic_write_bytes", fail_first_target)
    with pytest.raises(OSError):
        staging.export_monthly_transaction_drafts(
            "2026", "09", path=drafts, transactions_root=transactions,
            preview_rows=rows, preview_state=state,
        )
    monkeypatch.setattr(file_commit, "_atomic_write_bytes", original)
    drafts.write_text("manual external edit", encoding="utf-8")
    target = staging.monthly_transaction_csv_path("2026", "09", transactions)
    before_month = target.read_bytes() if target.exists() else None

    with pytest.raises(file_commit.FileCommitRecoveryError, match="не потерять правки"):
        staging.read_transaction_drafts(drafts)

    assert drafts.read_text(encoding="utf-8") == "manual external edit"
    assert (target.read_bytes() if target.exists() else None) == before_month


def test_corrupt_journal_is_not_ignored(export_data):
    drafts, _, _, _ = export_data
    journal = staging._transaction_export_journal_path(drafts)
    journal.write_text(json.dumps({"version": 1, "entries": []}), encoding="utf-8")

    with pytest.raises(file_commit.FileCommitRecoveryError, match="журнал"):
        staging.read_transaction_drafts(drafts)
