from concurrent.futures import ThreadPoolExecutor
import multiprocessing
from pathlib import Path
from threading import Event

import pandas as pd
import pytest

from src import config
from src.data import staging


def _append_in_process(path: str, source_id: str) -> None:
    staging.append_transaction_draft(
        "2026-01-01", "Прочее", "RUB", 100, source="process", source_id=source_id, path=path
    )


@pytest.fixture
def drafts_path(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path / "staging" / "transaction_drafts.csv"


def _append(path: Path, source_id: str) -> None:
    staging.append_transaction_draft(
        "2026-01-01", "Прочее", "RUB", 100, source="test", source_id=source_id, path=path
    )


def test_concurrent_thread_appends_preserve_every_row(drafts_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: _append(drafts_path, f"T{index}"), range(24)))

    saved = staging.read_transaction_drafts(drafts_path)
    assert set(saved["source_id"]) == {f"T{index}" for index in range(24)}


def test_concurrent_batch_appends_preserve_unique_import_rows(drafts_path):
    def batch(first: int, last: int):
        rows = pd.DataFrame(
            [
                {
                    "date": "2026-01-01",
                    "category": "Прочее",
                    "currency": "RUB",
                    "amount": "100",
                    "comment": source_id,
                    "source": "bank",
                    "source_id": source_id,
                    "status": "draft",
                }
                for source_id in (f"B{index}" for index in range(first, last))
            ]
        )
        return staging.append_transaction_draft_rows(rows, drafts_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda bounds: batch(*bounds), [(0, 10), (5, 15)]))

    saved = staging.read_transaction_drafts(drafts_path)
    assert set(saved["source_id"]) == {f"B{index}" for index in range(15)}
    assert sum(result["accepted_rows"] for result in results) == 15
    assert sum(result["skipped_rows"] for result in results) == 5


def test_concurrent_process_appends_preserve_every_row(drafts_path):
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_append_in_process, args=(str(drafts_path), f"P{index}"))
        for index in range(6)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    saved = staging.read_transaction_drafts(drafts_path)
    assert set(saved["source_id"]) == {f"P{index}" for index in range(6)}


def test_stale_grid_revision_rejects_save_and_preserves_server_data(drafts_path):
    _append(drafts_path, "A")
    rows, revision = staging.read_transaction_drafts_snapshot(drafts_path)
    edited_rows = rows.to_dict("records")
    edited_rows[0]["amount"] = "150"
    _append(drafts_path, "B")
    before = drafts_path.read_bytes()

    with pytest.raises(staging.DraftRevisionConflict, match="изменились"):
        staging.merge_transaction_draft_rows(
            edited_rows, path=drafts_path, expected_revision=revision
        )

    assert drafts_path.read_bytes() == before
    assert set(staging.read_transaction_drafts(drafts_path)["source_id"]) == {"A", "B"}


def test_current_grid_revision_allows_save(drafts_path):
    _append(drafts_path, "A")
    rows, revision = staging.read_transaction_drafts_snapshot(drafts_path)
    edited_rows = rows.to_dict("records")
    edited_rows[0]["amount"] = "150"

    staging.merge_transaction_draft_rows(
        edited_rows, path=drafts_path, expected_revision=revision
    )

    assert staging.read_transaction_drafts(drafts_path).iloc[0]["amount"] == "150"


def test_lock_timeout_leaves_file_unchanged(drafts_path, monkeypatch):
    _append(drafts_path, "A")
    before = drafts_path.read_bytes()
    monkeypatch.setattr(staging, "DRAFT_LOCK_TIMEOUT_SECONDS", 0.05)
    started = Event()

    def blocked_append():
        started.set()
        _append(drafts_path, "B")

    with staging._transaction_drafts_lock(drafts_path):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(blocked_append)
            assert started.wait(1)
            with pytest.raises(staging.DraftWriteBusyError, match="сохраняются"):
                future.result(timeout=1)

    assert drafts_path.read_bytes() == before


def test_lock_uses_stable_sidecar_file(drafts_path):
    _append(drafts_path, "A")

    lock_path = drafts_path.with_name(f".{drafts_path.name}.lock")
    assert lock_path.exists()
    assert lock_path.read_bytes() == b""
    assert staging.read_transaction_drafts(drafts_path).iloc[0]["source_id"] == "A"


def test_test_mode_read_does_not_create_sidecar(drafts_path, monkeypatch):
    _append(drafts_path, "A")
    lock_path = drafts_path.with_name(f".{drafts_path.name}.lock")
    lock_path.unlink()
    monkeypatch.setattr(config, "is_test_mode", lambda: True)

    data, _ = staging.read_transaction_drafts_snapshot(drafts_path)

    assert data.iloc[0]["source_id"] == "A"
    assert not lock_path.exists()


def _manual_callback_request(app, client, *, add_request_id="browser-add-request", locale="ru"):
    key = next(key for key in app.callback_map if "transaction-input-message.children" in key)
    callback = app.callback_map[key]
    values = {
        "transaction-add-button": 1,
        "transaction-input-date": "2026-01-01",
        "transaction-input-category": "Прочее",
        "transaction-input-currency": "RUB",
        "transaction-input-amount": 100,
        "transaction-input-comment": "",
        "transaction-add-request-id": add_request_id,
        "dashboard-locale": locale,
    }
    payload = {
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [{**item, "value": values[item["id"]]} for item in callback["inputs"]],
        "state": [{**item, "value": values[item["id"]]} for item in callback["state"]],
        "changedPropIds": ["transaction-add-button.n_clicks"],
    }
    return client.post("/_dash-update-component", json=payload)


def test_manual_submit_clears_sent_fields_and_retry_does_not_duplicate(
    drafts_path, monkeypatch
):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"

    first = _manual_callback_request(app, client, add_request_id="manual-submit-A")
    retry = _manual_callback_request(app, client, add_request_id="manual-submit-A")

    assert first.status_code == 200
    first_result = first.get_json()["response"]
    assert first_result["transaction-input-message"]["children"] == "Черновик добавлен."
    assert first_result["transaction-input-amount"]["value"] is None
    assert first_result["transaction-input-comment"]["value"] == ""
    assert retry.status_code == 200
    retry_result = retry.get_json()["response"]
    assert "повтор не создан" in retry_result["transaction-input-message"]["children"]
    saved = staging.read_transaction_drafts(drafts_path)
    assert len(saved) == 1
    assert saved.iloc[0]["source_id"] == "manual:manual-submit-A"


def test_manual_submit_feedback_uses_selected_locale(drafts_path, monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"

    response = _manual_callback_request(
        app, client, add_request_id="manual-submit-en", locale="en"
    )

    assert response.status_code == 200
    result = response.get_json()["response"]
    assert result["transaction-input-message"]["children"] == "Draft added."
    saved = staging.read_transaction_drafts(drafts_path)
    assert saved.iloc[0]["category"] == "Прочее"
    assert saved.iloc[0]["source_id"] == "manual:manual-submit-en"
