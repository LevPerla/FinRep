import json
from pathlib import Path

import pytest

from src.data.sqlite_cutover import run_cutover_preflight


def _write_minimal_source(root: Path) -> None:
    transactions = root / "transactions_info" / "2026"
    transactions.mkdir(parents=True)
    (transactions / "2026_01.csv").write_text(
        "Дата;Пища\n01.01.2026;12.34|RUB|Lunch\n", encoding="utf-8")
    assets = root / "assets_info" / "2026"
    assets.mkdir(parents=True)
    (assets / "2026_01.csv").write_text(
        "Счет;Сумма\nCash;1000|RUB\n", encoding="utf-8")
    debts = root / "debts"
    debts.mkdir()
    (debts / "debts.csv").write_text(
        "debt_id;type;counterparty;opened_date;principal_amount;principal_currency;"
        "cash_amount;cash_currency;comment;status\n",
        encoding="utf-8",
    )
    (debts / "debt_payments.csv").write_text(
        "payment_id;debt_id;date;amount;cash_amount;cash_currency;comment;status\n",
        encoding="utf-8",
    )


def test_cutover_preflight_builds_fresh_verified_artifacts(tmp_path):
    source = tmp_path / "source"
    _write_minimal_source(source)
    target = tmp_path / "output" / "finrep.sqlite3"
    audit = tmp_path / "output" / "migration.sqlite3"
    result = tmp_path / "output" / "result.json"

    payload = run_cutover_preflight(source, target, audit, result)

    assert payload["ready_for_cutover"] is True
    assert payload["manifest"]["unknown"] == 0
    assert payload["reconciliation"]["blocking_review_items"] == 0
    assert len(payload["reconciliation"]["checks"]) == 10
    assert all(check["passed"] for check in payload["reconciliation"]["checks"])
    assert payload["database"]["integrity_check"] == "ok"
    assert payload["database"]["foreign_key_violations"] == 0
    assert json.loads(result.read_text(encoding="utf-8")) == payload


def test_cutover_preflight_never_overwrites_an_output(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "finrep.sqlite3"
    target.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError, match="output already exists"):
        run_cutover_preflight(
            source,
            target,
            tmp_path / "migration.sqlite3",
            tmp_path / "result.json",
        )

    assert target.read_text(encoding="utf-8") == "keep"
