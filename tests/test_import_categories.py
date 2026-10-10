from unittest.mock import patch

import pandas as pd

from src.data.importers import common
from src.data.sqlite_store import add_cash_transaction, connect_database, initialize_database


def test_category_comes_from_latest_transaction_with_same_comment():
    history = pd.DataFrame(
        [
            {"Дата": "2025-01-10", "Категория": "Пища", "Комментарий": "Coffee shop"},
            {"Дата": "2026-03-20", "Категория": "Досуг", "Комментарий": "  COFFEE   SHOP "},
        ]
    )

    with patch.object(common, "get_transactions", return_value=history):
        data = common.import_frame_from_rows(
            [
                {
                    "date": "2026-07-19",
                    "signed_amount": -500.0,
                    "currency": "RUB",
                    "details": "Coffee shop",
                }
            ]
        )

    assert data.iloc[0]["category"] == "Досуг"


def test_category_comes_from_latest_transaction_with_same_comment_and_direction():
    history = pd.DataFrame(
        [
            {"Дата": "2026-01-10", "Категория": "Транспорт", "Комментарий": "Transfer Ivan"},
            {"Дата": "2026-03-20", "Категория": "Прочие доходы", "Комментарий": "Transfer Ivan"},
        ]
    )

    with patch.object(common, "get_transactions", return_value=history):
        expense = common.import_frame_from_rows(
            [{
                "date": "2026-07-19",
                "signed_amount": -500.0,
                "currency": "RUB",
                "details": "Transfer Ivan",
            }]
        )
        income = common.import_frame_from_rows(
            [{
                "date": "2026-07-20",
                "signed_amount": 500.0,
                "currency": "RUB",
                "details": "Transfer Ivan",
            }]
        )

    assert expense.iloc[0]["category"] == "Транспорт"
    assert income.iloc[0]["category"] == "Прочие доходы"


def test_category_falls_back_to_import_rules_without_history_match(tmp_path, monkeypatch):
    rules_path = tmp_path / "import_rules" / "categories.csv"
    rules_path.parent.mkdir(parents=True)
    rules_path.write_text("pattern;category\ncafe;Пища\n", encoding="utf-8")
    monkeypatch.setattr(common.config, "DATA_PATH", str(tmp_path))

    category = common.categorize("Cafe near home", -500.0, {})

    assert category == "Пища"


def test_category_rules_are_read_from_sqlite_backend(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    with connect_database(database, writable=True) as connection:
        connection.execute("""INSERT INTO categorization_rules
            (id, priority, direction_scope, matcher_type, pattern, category_id,
             active, created_at, updated_at)
            VALUES ('rule-cafe', 1, 'expense', 'contains', 'cafe', 'expense.food',
             1, datetime('now'), datetime('now'))""")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))

    assert common.categorize("Cafe near home", -500.0, {}) == "Пища"
    assert common.categorize("Cafe refund", 500.0, {}) == "Прочие доходы"


def test_positive_transactions_receive_concrete_income_categories(
        tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))

    data = common.import_frame_from_rows([
        {"date": "2026-10-01", "signed_amount": 100, "currency": "RUB",
         "details": "Salary"},
        {"date": "2026-10-02", "signed_amount": 10, "currency": "RUB",
         "details": "Проценты"},
        {"date": "2026-10-03", "signed_amount": 5, "currency": "RUB",
         "details": "Cashback"},
    ])

    categories = dict(zip(data["comment"], data["category"]))
    assert categories == {
        "Salary": "Зарплата",
        "Проценты": "Проценты",
        "Cashback": "Прочие доходы",
    }


def test_income_category_is_reused_from_sqlite_history(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    add_cash_transaction(
        database, transaction_id="previous-income", occurred_on="2026-09-01",
        flow_direction="income", category_id="income.investment", amount="100",
        currency="RUB", comment="Partner distribution",
    )

    preview = common.import_frame_from_rows([{
        "date": "2026-10-01", "signed_amount": 50, "currency": "RUB",
        "details": "Partner distribution",
    }])

    assert preview.iloc[0]["category"] == "Инвест доход"
