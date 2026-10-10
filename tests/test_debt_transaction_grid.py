from __future__ import annotations

import sqlite3

import pytest

from src.data.importers.common import new_manual_grid_row, save_input_grid_to_transactions
from src.data.sqlite_store import (
    connect_database, create_debt_record, initialize_database, publish_domain_drafts,
    record_debt_payment,
)


def _row(category, amount, source_id, **extra):
    return {
        **new_manual_grid_row(),
        "source_id": source_id,
        "date": "2026-10-10",
        "category": category,
        "currency": "RUB",
        "amount": amount,
        **extra,
    }


def test_reviewed_debt_categories_link_cash_events_and_registry(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))

    opening = _row("Возникновение дебиторской задолженности", "-100", "receivable-open",
                   counterparty="Synthetic A", comment="loan")
    first = save_input_grid_to_transactions([opening])
    assert first["published_rows"] == 1
    assert not first["remaining_rows"]
    assert save_input_grid_to_transactions([opening])["already_published_rows"] == 1

    with connect_database(database) as connection:
        debt_id = connection.execute("SELECT id FROM debts WHERE kind = 'receivable'").fetchone()[0]
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 0
        assert connection.execute("SELECT debt_id FROM debt_cash_events").fetchone()[0] == debt_id

    payment = _row("Погашение дебиторской задолженности", "40", "receivable-pay",
                   source="kaspi_pdf", direction="credit", debt_id=debt_id,
                   source_comment="Original bank description")
    assert save_input_grid_to_transactions([payment])["published_rows"] == 1
    assert save_input_grid_to_transactions([payment])["already_published_rows"] == 1
    with connect_database(database) as connection:
        assert connection.execute("SELECT SUM(principal_amount_minor) FROM debt_payments").fetchone()[0] == 4000
        assert connection.execute("SELECT count(*) FROM debt_cash_events WHERE debt_id = ?", (debt_id,)).fetchone()[0] == 2
        assert connection.execute("SELECT source_comment FROM transaction_drafts WHERE origin_key = 'receivable-pay'").fetchone()[0] == "Original bank description"

    overpayment = _row("Погашение дебиторской задолженности", "70", "overpay", debt_id=debt_id)
    result = save_input_grid_to_transactions([overpayment])
    assert result["invalid_rows"] == 1
    assert "exceeds" in result["remaining_rows"][0]["validation_error"]

    liability = _row("Возникновение кредиторской задолженности", "50", "liability-open",
                     counterparty="Synthetic B", source="bcc_pdf", direction="credit")
    assert save_input_grid_to_transactions([liability])["published_rows"] == 1
    with connect_database(database) as connection:
        liability_id = connection.execute("SELECT id FROM debts WHERE kind = 'liability'").fetchone()[0]
    repayment = _row("Погашение кредиторской задолженности", "-50", "liability-pay", debt_id=liability_id)
    assert save_input_grid_to_transactions([repayment])["published_rows"] == 1
    with connect_database(database) as connection:
        assert connection.execute("SELECT status FROM debts WHERE id = ?", (liability_id,)).fetchone()[0] == "closed"
        assert connection.execute("SELECT count(*) FROM cash_transactions").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM debt_cash_events WHERE debt_id IS NULL").fetchone()[0] == 0


def test_debt_grid_requires_explicit_target_and_matching_direction(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    missing = [
        _row("Возникновение дебиторской задолженности", "-10", "missing-receivable"),
        _row("Погашение дебиторской задолженности", "10", "missing-receivable-payment"),
        _row("Возникновение кредиторской задолженности", "10", "missing-liability"),
        _row("Погашение кредиторской задолженности", "-10", "missing-liability-payment"),
    ]
    wrong_sign = _row("Возникновение кредиторской задолженности", "-10", "wrong-sign",
                      counterparty="Synthetic")
    result = save_input_grid_to_transactions([*missing, wrong_sign])
    assert result["invalid_rows"] == 5
    assert all("укажи нового контрагента" in row["validation_error"] for row in result["remaining_rows"][::2][:2])
    assert all("выбери погашаемый долг" in row["validation_error"] for row in result["remaining_rows"][1::2][:2])
    assert "категория не соответствует знаку" in result["remaining_rows"][-1]["validation_error"]
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM debts").fetchone()[0] == 0


def test_debt_screen_drafts_publish_with_the_same_debt_link(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    opening = create_debt_record(
        database, kind="receivable", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )
    payment = record_debt_payment(
        database, debt_id=opening["debt_id"], occurred_on="2026-10-10",
        amount="25", operation_key="payment",
    )
    publish_domain_drafts(database, draft_ids=[opening["draft_id"], payment["draft_id"]],
                          operation_key="publish")
    with connect_database(database) as connection:
        assert {row[0] for row in connection.execute("SELECT debt_id FROM debt_cash_events")} == {opening["debt_id"]}


def test_unlinked_debt_draft_cannot_be_published(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    opening = create_debt_record(
        database, kind="liability", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )
    with connect_database(database, writable=True) as connection:
        connection.execute("""UPDATE transaction_drafts
            SET origin_kind = 'legacy', origin_key = 'unlinked'
            WHERE id = ?""", (opening["draft_id"],))
    with pytest.raises(ValueError, match="не привязана к долгу"):
        publish_domain_drafts(database, draft_ids=[opening["draft_id"]], operation_key="publish")
    with connect_database(database) as connection:
        assert connection.execute("SELECT count(*) FROM debt_cash_events").fetchone()[0] == 0
        assert connection.execute("SELECT status FROM transaction_drafts").fetchone()[0] == "ready"


def test_debt_draft_cannot_be_linked_to_wrong_side(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    opening = create_debt_record(
        database, kind="receivable", counterparty="Synthetic",
        opened_on="2026-10-01", principal_amount="100", currency="RUB",
        operation_key="open",
    )
    with connect_database(database, writable=True) as connection:
        connection.execute("""UPDATE transaction_drafts SET domain_action = 'liability_opening'
            WHERE id = ?""", (opening["draft_id"],))
    with pytest.raises(ValueError, match="Тип или валюта"):
        publish_domain_drafts(database, draft_ids=[opening["draft_id"]], operation_key="publish")


def test_v19_debt_events_gain_nullable_link_without_changing_history(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE debt_cash_events DROP COLUMN debt_id")
        connection.execute("DELETE FROM schema_migrations WHERE version = 20")
        connection.execute("PRAGMA user_version = 19")
    initialize_database(database)
    with connect_database(database) as connection:
        assert "debt_id" in {row[1] for row in connection.execute("PRAGMA table_info(debt_cash_events)")}
