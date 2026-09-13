from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from src import config
from src.data import get, staging
from src.data.importers import common


@pytest.fixture
def import_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    (tmp_path / "transactions_info").mkdir(parents=True)
    get.clear_data_cache()
    yield tmp_path
    get.clear_data_cache()


def _row(signed_amount: float, details: str = "Shop A") -> list[dict]:
    return [
        {
            "date": "2026-09-01",
            "signed_amount": signed_amount,
            "currency": "RUB",
            "details": details,
        }
    ]


def _preview(signed_amount: float, statement_id: str) -> pd.DataFrame:
    return common.import_frame_from_rows(
        _row(signed_amount), statement_id=statement_id
    )


def test_refund_for_known_expense_defaults_to_savings(import_data):
    history = pd.DataFrame(
        [
            {
                "Дата": pd.Timestamp("2026-08-01"),
                "Категория": "Пища",
                "Комментарий": "Shop A",
            }
        ]
    )
    with patch.object(common, "get_transactions", return_value=history):
        preview = _preview(40, "refund")

    assert preview.iloc[0]["category"] == "Сбережения"
    assert preview.iloc[0]["direction"] == "credit"
    assert preview.iloc[0]["amount"] == 40


def test_direct_income_history_is_preserved(import_data):
    history = pd.DataFrame(
        [
            {
                "Дата": pd.Timestamp("2026-08-01"),
                "Категория": "Доход",
                "Комментарий": "Employer",
            }
        ]
    )
    with patch.object(common, "get_transactions", return_value=history):
        preview = common.import_frame_from_rows(
            _row(1000, "Employer"), statement_id="salary"
        )

    assert preview.iloc[0]["category"] == "Доход"


def test_unknown_credit_keeps_existing_income_default(import_data):
    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        preview = _preview(40, "unknown-credit")

    assert preview.iloc[0]["category"] == "Доход"


def test_credit_cannot_be_saved_in_expense_category(import_data):
    with patch.object(common, "get_transactions", return_value=pd.DataFrame()):
        preview = _preview(40, "credit")
    rows = preview.to_dict("records")
    rows[0]["category"] = "Прочее"

    with pytest.raises(ValueError, match="нельзя сохранить как расход"):
        common.save_import_to_staging(rows)

    assert staging.read_transaction_drafts().empty


def test_expense_and_refund_have_net_cash_effect_of_minus_sixty(import_data):
    expense = _preview(-100, "expense")
    common.save_import_to_staging(expense.to_dict("records"))
    staging.export_monthly_transaction_drafts("2026", "09")
    get.clear_data_cache()

    refund = _preview(40, "refund")
    assert refund.iloc[0]["category"] == "Сбережения"
    common.save_import_to_staging(refund.to_dict("records"))
    staging.export_monthly_transaction_drafts("2026", "09")
    get.clear_data_cache()

    transactions = get.get_transactions()
    totals = transactions.groupby("Категория")["Значение"].sum().to_dict()

    assert totals["Прочее"] == 100
    assert totals["Сбережения"] == 40
    assert totals["Сбережения"] - totals["Прочее"] == -60
