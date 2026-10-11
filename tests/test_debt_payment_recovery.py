from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from src import config
from src.data import debts, file_commit, staging


@pytest.fixture
def payment_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    debt = debts.create_debt(
        "receivable",
        "Synthetic",
        "2026-09-01",
        100,
        "RUB",
        create_draft=False,
        operation_id="debt-setup",
    )
    return debt["debt_id"]


def _pay(debt_id: str, operation_id: str = "payment-request-A", amount=25) -> dict:
    return debts.create_debt_payment(
        debt_id,
        "2026-09-02",
        amount,
        comment="synthetic",
        operation_id=operation_id,
    )


@pytest.mark.parametrize("failed_write", [1, 2, 3, 4, 5])
def test_retry_after_each_payment_write_is_idempotent(
    payment_data, monkeypatch, failed_write
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
        _pay(payment_data, amount=100)
    monkeypatch.setattr(file_commit, "_atomic_write_bytes", original)

    result = _pay(payment_data, amount=100)

    payments = debts.read_debt_payments()
    drafts = staging.read_transaction_drafts()
    assert payments["payment_id"].tolist() == [result["payment_id"]]
    assert drafts["source_id"].tolist() == [f"{payment_data}:{result['payment_id']}"]
    assert debts.active_debt_balances("receivable", "RUB").empty
    assert debts.read_debts().iloc[0]["status"] == "closed"


def test_partial_payment_keeps_debt_active_and_full_payment_closes_it(payment_data):
    first = _pay(payment_data, "partial", 25)
    remaining = debts.active_debt_balances("receivable", "RUB")
    assert remaining.iloc[0]["outstanding_amount"] == 75
    assert debts.read_debts().iloc[0]["status"] == "active"

    second = _pay(payment_data, "final", 75)

    assert first["payment_id"] != second["payment_id"]
    assert debts.active_debt_balances("receivable", "RUB").empty
    assert debts.read_debts().iloc[0]["status"] == "closed"
    assert len(debts.read_debt_payments()) == 2
    assert len(staging.read_transaction_drafts()) == 2


def test_concurrent_payments_cannot_overpay(payment_data):
    def pay(operation_id):
        try:
            return _pay(payment_data, operation_id, 60)
        except ValueError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(pay, ["payment-A", "payment-B"]))

    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum("больше остатка" in str(result) for result in results if isinstance(result, ValueError)) == 1
    assert len(debts.read_debt_payments()) == 1
    assert len(staging.read_transaction_drafts()) == 1
    assert debts.active_debt_balances("receivable", "RUB").iloc[0]["outstanding_amount"] == 40


def test_payment_without_cash_draft_updates_debt_and_payment_only(payment_data):
    result = debts.create_debt_payment(
        payment_data,
        "2026-09-02",
        25,
        create_draft=False,
        operation_id="no-draft",
    )

    assert result["draft_created"] is False
    assert len(debts.read_debt_payments()) == 1
    assert staging.read_transaction_drafts().empty
    assert debts.active_debt_balances("receivable", "RUB").iloc[0]["outstanding_amount"] == 75


def test_dashboard_retry_with_same_request_id_does_not_duplicate_payment(
    payment_data, monkeypatch
):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    from src.dashboard.app import create_app

    app = create_app()
    client = app.server.test_client()
    with client.session_transaction() as session:
        session["authenticated"] = True
        session["data_mode"] = "live"
    key = next(key for key in app.callback_map if "debt-input-message.children" in key)
    callback = app.callback_map[key]
    values = {
        "debt-add-button": 0,
        "debt-payment-button": 1,
        "dashboard-currency": "RUB",
        "debt-new-grid": [],
        "debt-payment-grid": [{
            "operation_id": "browser-payment-A",
            "debt": f"Мне должны | Synthetic | 100 RUB | {payment_data}",
            "date": "2026-09-02", "amount": 25,
            "cash_currency": "RUB", "comment": "synthetic",
        }, {
            "operation_id": "browser-payment-invalid", "debt": "",
            "date": "2026-09-02", "amount": 1,
            "cash_currency": "RUB", "comment": "synthetic",
        }],
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
        "changedPropIds": ["debt-payment-button.n_clicks"],
    }

    first = client.post("/_dash-update-component", json=payload)
    second = client.post("/_dash-update-component", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    remaining = first.get_json()["response"]["debt-payment-grid"]["rowData"]
    assert len(remaining) == 1
    assert "Выбери долг" in remaining[0]["validation_error"]
    assert len(debts.read_debt_payments()) == 1
    assert len(staging.read_transaction_drafts()) == 1
    assert debts.active_debt_balances("receivable", "RUB").iloc[0]["outstanding_amount"] == 75

    payload["state"][2]["value"] = [{
        "operation_id": "browser-overpayment", "debt": f"Мне должны | Synthetic | 75 RUB | {payment_data}",
        "date": "2026-09-03", "amount": 100, "cash_currency": "RUB", "comment": "too much",
    }]
    overpayment = client.post("/_dash-update-component", json=payload)
    assert overpayment.status_code == 200
    result = overpayment.get_json()["response"]
    assert "Погашение больше остатка" in result["debt-input-message"]["children"]
    assert result["debt-input-message"]["color"] == "danger"
    assert debts.active_debt_balances("receivable", "RUB").iloc[0]["outstanding_amount"] == 75
