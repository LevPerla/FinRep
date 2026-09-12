import json
from decimal import Decimal

import pandas as pd
import pytest

from src import config
from src.data import debts, staging
from src.data.get_finance import set_fx_network_enabled


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    set_fx_network_enabled(False)
    staging.ensure_transaction_drafts_file()
    debts.ensure_debt_files()
    return tmp_path


def _csv_rows(path):
    return pd.read_csv(path, sep=";", dtype=str, encoding="utf-8-sig").fillna("")


def test_debt_create_and_payment_use_half_up_storage(data_root):
    created = debts.create_debt(
        "receivable",
        "Synthetic",
        "2026-01-01",
        "100.005",
        "RUB",
        cash_amount="1 234,565",
        cash_currency="KZT",
        operation_id="money-create",
    )
    paid = debts.create_debt_payment(
        created["debt_id"],
        "2026-01-02",
        "1.005",
        cash_amount="2.675",
        cash_currency="USD",
        operation_id="money-payment",
    )

    debt_row = _csv_rows(data_root / "debts" / "debts.csv").iloc[0]
    payment_row = _csv_rows(data_root / "debts" / "debt_payments.csv").iloc[0]
    drafts = staging.read_transaction_drafts().set_index("source_id")
    assert debt_row["principal_amount"] == "100.01"
    assert debt_row["cash_amount"] == "1234.57"
    assert payment_row["amount"] == "1.01"
    assert payment_row["cash_amount"] == "2.68"
    assert drafts.loc[f"{created['debt_id']}:open", "amount"] == "1234.57"
    assert drafts.loc[f"{created['debt_id']}:{paid['payment_id']}", "amount"] == "2.68"
    assert debts.active_debt_balances("receivable").iloc[0]["outstanding_amount"] == Decimal("99.00")


def test_large_debt_and_payment_keep_exact_cents(data_root):
    created = debts.create_debt(
        "receivable",
        "Large",
        "2026-01-01",
        "99999999999999.99",
        "RUB",
        operation_id="large-create",
    )
    debts.create_debt_payment(
        created["debt_id"],
        "2026-01-02",
        "0.01",
        operation_id="large-payment",
    )

    balance = debts.active_debt_balances("receivable").iloc[0]

    assert balance["principal_amount"] == Decimal("99999999999999.99")
    assert balance["outstanding_amount"] == Decimal("99999999999999.98")


def test_cross_currency_payment_rounds_once_with_half_up(data_root, monkeypatch):
    from src.data import proccess

    monkeypatch.setattr(
        proccess,
        "get_rates",
        lambda **_: pd.DataFrame(
            {"USDRUB=X": [1.005]}, index=pd.DatetimeIndex([pd.Timestamp("2026-01-01")])
        ),
    )

    result = debts._cash_to_debt_amount(Decimal("1.00"), "USD", "RUB", "2026-01-01")

    assert result == Decimal("1.01")


def test_new_debt_does_not_quantize_existing_ledger_row(data_root):
    path = data_root / "debts" / "debts.csv"
    existing = {column: "" for column in debts.DEBT_COLUMNS}
    existing.update(
        debt_id="legacy",
        type="receivable",
        counterparty="Legacy",
        opened_date="2025-01-01",
        principal_amount="100.005",
        principal_currency="RUB",
        cash_amount="100.005",
        cash_currency="RUB",
        status="active",
    )
    pd.DataFrame([existing]).to_csv(path, sep=";", index=False, encoding="utf-8-sig")

    debts.create_debt(
        "receivable",
        "New",
        "2026-01-01",
        "10",
        "RUB",
        operation_id="preserve-existing",
    )

    saved = _csv_rows(path).set_index("debt_id")
    assert saved.loc["legacy", "principal_amount"] == "100.005"
    assert saved.loc["legacy", "cash_amount"] == "100.005"


def test_debt_display_is_exact_and_json_safe(data_root, monkeypatch):
    monkeypatch.setenv("FINREP_DASH_PASSWORD", "synthetic-password")
    monkeypatch.setenv("FINREP_DASH_SECRET_KEY", "synthetic-key")
    created = debts.create_debt(
        "receivable",
        "Large",
        "2026-01-01",
        "99999999999999.99",
        "RUB",
        operation_id="display-create",
    )
    from src.dashboard.app import _active_debt_records, _debt_select_options

    records = _active_debt_records("RUB", "receivable")
    options = _debt_select_options("RUB")

    assert records[0]["Сумма долга"] == "99 999 999 999 999.99"
    assert any("99 999 999 999 999.99 RUB" in option["label"] for option in options)
    assert any(option["value"] == created["debt_id"] for option in options)
    json.dumps(records)


def test_ambiguous_debt_amount_preserves_all_files(data_root):
    before = {str(path.relative_to(data_root)): path.read_bytes() for path in data_root.rglob("*") if path.is_file()}

    with pytest.raises(ValueError):
        debts.create_debt(
            "receivable",
            "Synthetic",
            "2026-01-01",
            "1,234.56",
            "RUB",
            operation_id="ambiguous-create",
        )

    after = {str(path.relative_to(data_root)): path.read_bytes() for path in data_root.rglob("*") if path.is_file()}
    assert after == before
