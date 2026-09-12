from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from src import config
from src.data import debts, file_commit, staging


@pytest.fixture
def debt_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    return tmp_path


def _create(operation_id: str = "request-A") -> dict:
    return debts.create_debt(
        debt_type="receivable",
        counterparty="Synthetic",
        opened_date="2026-09-01",
        principal_amount=100,
        principal_currency="RUB",
        comment="test",
        operation_id=operation_id,
    )


@pytest.mark.parametrize("failed_write", [1, 2, 3, 4])
def test_retry_after_each_debt_create_write_is_idempotent(
    debt_data, monkeypatch, failed_write
):
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
        _create()
    monkeypatch.setattr(file_commit, "_atomic_write_bytes", original)

    result = _create()

    saved_debts = debts.read_debts()
    saved_drafts = staging.read_transaction_drafts()
    assert saved_debts["debt_id"].tolist() == [result["debt_id"]]
    assert saved_drafts["source_id"].tolist() == [f"{result['debt_id']}:open"]
    assert saved_drafts.iloc[0]["amount"] == "100"


def test_new_process_style_read_recovers_debt_and_draft_together(
    debt_data, monkeypatch
):
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
        _create()
    monkeypatch.setattr(file_commit, "_atomic_write_bytes", original)

    saved_debts = debts.read_debts()
    saved_drafts = staging.read_transaction_drafts()
    assert len(saved_debts) == 1
    assert saved_drafts["source_id"].tolist() == [f"{saved_debts.iloc[0]['debt_id']}:open"]


def test_identical_debts_with_distinct_operation_ids_are_both_created(debt_data):
    first = _create("request-A")
    second = _create("request-B")

    assert first["debt_id"] != second["debt_id"]
    assert len(debts.read_debts()) == 2
    assert len(staging.read_transaction_drafts()) == 2


def test_concurrent_debt_creates_preserve_both_debts_and_drafts(debt_data):
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(_create, ["request-A", "request-B"]))

    debt_ids = {result["debt_id"] for result in results}
    assert set(debts.read_debts()["debt_id"]) == debt_ids
    assert set(staging.read_transaction_drafts()["source_id"]) == {
        f"{debt_id}:open" for debt_id in debt_ids
    }


def test_debt_create_without_cash_draft_commits_only_debt(debt_data):
    result = debts.create_debt(
        "liability",
        "Synthetic",
        "2026-09-01",
        100,
        "RUB",
        create_draft=False,
        operation_id="no-draft",
    )

    assert result["draft_created"] is False
    assert len(debts.read_debts()) == 1
    assert staging.read_transaction_drafts().empty


def test_dashboard_retry_with_same_request_id_does_not_duplicate_debt(
    debt_data, monkeypatch
):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"
    key = next(key for key in app.callback_map if "debt-create-request-id.data" in key)
    callback = app.callback_map[key]
    values = {
        "debt-add-button": 1,
        "debt-payment-button": 0,
        "debt-migrate-button": 0,
        "dashboard-currency": "RUB",
        "debt-opened-date": "2026-09-01",
        "debt-type": "receivable",
        "debt-counterparty": "Synthetic",
        "debt-principal-amount": 100,
        "debt-principal-currency": "RUB",
        "debt-cash-amount": None,
        "debt-cash-currency": "RUB",
        "debt-comment": "test",
        "debt-payment-id": "",
        "debt-payment-date": "2026-09-01",
        "debt-payment-amount": None,
        "debt-payment-cash-currency": "RUB",
        "debt-payment-comment": "",
        "debt-create-request-id": "browser-request-A",
        "dashboard-refresh-token": 0,
    }
    payload = {
        "output": key,
        "outputs": [
            {"id": item.component_id, "property": item.component_property}
            for item in callback["output"]
        ],
        "inputs": [{**item, "value": values[item["id"]]} for item in callback["inputs"]],
        "state": [{**item, "value": values[item["id"]]} for item in callback["state"]],
        "changedPropIds": ["debt-add-button.n_clicks"],
    }

    first = client.post("/_dash-update-component", json=payload)
    second = client.post("/_dash-update-component", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(debts.read_debts()) == 1
    assert len(staging.read_transaction_drafts()) == 1
