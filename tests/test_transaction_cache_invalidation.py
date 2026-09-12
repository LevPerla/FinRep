from __future__ import annotations

import pandas as pd
import pytest

from src import config
from src.data import staging
from src.data.get import clear_data_cache, get_transactions
from src.model.create_tables import clear_table_cache, get_balance_by_month, get_month_transactions


@pytest.fixture
def cached_month(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    monkeypatch.setattr(config, "DEBUG", True)
    (tmp_path / "assets_info").mkdir()
    target = staging.monthly_transaction_csv_path("2026", "09")
    target.parent.mkdir(parents=True)
    pd.DataFrame(
        [
            {
                "Дата": "01.09.2026",
                "Доход": "0",
                "Сбережения": "0",
                "Инвестиции": "0",
                "Дебиторская задолженность": "0",
                "Погашение деб. зад.": "0",
                "Кредиторская задолженность": "0",
                "Погашение кред. зад.": "0",
                "Прочее": "0",
            }
        ]
    ).to_csv(target, sep=";", index=False, encoding="utf-8-sig")
    clear_data_cache()
    clear_table_cache()
    yield target
    clear_data_cache()
    clear_table_cache()


def _expense_total() -> float:
    return float(get_transactions().loc[lambda data: data["Категория"] == "Прочее", "Значение"].sum())


def test_month_export_invalidates_transaction_and_report_caches(cached_month):
    assert _expense_total() == 0
    assert get_balance_by_month("RUB").iloc[0]["Расход"] == 0
    assert get_month_transactions("RUB", "2026", "09").iloc[0]["Прочее"] == 0

    staging.append_transaction_draft(
        "2026-09-01",
        "Прочее",
        "RUB",
        100,
        "cached transaction",
        source_id="cache-A",
    )
    preview, state = staging.prepare_monthly_transaction_export("2026", "09")
    staging.export_monthly_transaction_drafts(
        "2026",
        "09",
        preview_rows=preview.to_dict("records"),
        preview_state=state,
    )

    assert _expense_total() == 100
    assert get_balance_by_month("RUB").iloc[0]["Расход"] == 100
    assert get_month_transactions("RUB", "2026", "09").iloc[0]["Прочее"] == 100

    clear_data_cache()
    clear_table_cache()
    assert _expense_total() == 100
    assert get_balance_by_month("RUB").iloc[0]["Расход"] == 100


def test_idempotent_export_retry_keeps_fresh_caches(cached_month):
    staging.append_transaction_draft(
        "2026-09-01", "Прочее", "RUB", 100, source_id="cache-A"
    )
    preview, state = staging.prepare_monthly_transaction_export("2026", "09")
    rows = preview.to_dict("records")
    staging.export_monthly_transaction_drafts(
        "2026", "09", preview_rows=rows, preview_state=state
    )
    assert _expense_total() == 100

    staging.export_monthly_transaction_drafts(
        "2026", "09", preview_rows=rows, preview_state=state
    )

    assert _expense_total() == 100
    assert get_balance_by_month("RUB").iloc[0]["Расход"] == 100
