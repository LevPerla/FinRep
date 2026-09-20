import pandas as pd
import pytest

from src.dashboard.income_sources import classify_income_comment, classify_income_transactions


@pytest.mark.parametrize("comment,expected", [
    ("  ЗАРПЛАТА   за июль ", "salary"),
    ("З/П", "salary"),
    ("Salary", "salary"),
    ("Зараплата", "salary"),
    ("Премия", "salary"),
    ("Отпускные", "salary"),
    ("Выплаты за отпуск", "salary"),
    ("Ретеншн бонус", "salary"),
    ("ПРОЦЕНТЫ по ДЕПОЗИТУ", "deposit_interest"),
    ("выплата процентов по вкладу", "deposit_interest"),
    ("Deposit interest", "deposit_interest"),
    ("Проценты на вклда", "deposit_interest"),
    ("Проценты", "deposit_interest"),
    ("Процент", "deposit_interest"),
    ("Депозит", "deposit_interest"),
    ("Вклад", "deposit_interest"),
    ("С депозита", "deposit_interest"),
    ("Зарплата и проценты по депозиту", "conflict"),
    ("не зарплата", "unknown_text"),
    ("не проценты по депозиту", "unknown_text"),
    ("Проценты за задержку", "unknown_text"),
    ("Перевод на депозит", "unknown_text"),
    ("Командировочные", "unknown_text"),
    ("Кэшбек", "unknown_text"),
    ("", "unknown_empty"),
    (None, "unknown_empty"),
])
def test_historical_income_comment_source(comment, expected):
    assert classify_income_comment(comment) == expected


def test_income_rows_are_classified_once_without_changing_amounts_or_comments():
    rows = pd.DataFrame([
        ("Доход", 100, "зарплата", "RUB"),
        ("Доход", -5, "Премия", "RUB"),
        ("Доход", 3, "проценты по вкладу", "USD"),
        ("Доход", 7, "зарплата и проценты по депозиту", "RUB"),
        ("Доход", 2, None, "RUB"),
        ("Доход", 0, "зарплата", "RUB"),
        ("Сбережения", 40, "зарплата", "RUB"),
    ], columns=["Категория", "Значение", "Комментарий", "Валюта"])
    original = rows.copy(deep=True)

    result = classify_income_transactions(rows)

    assert result["income_source"].tolist() == ["salary", "salary", "deposit_interest", "unknown", "unknown"]
    assert result["source_reason"].tolist() == ["salary", "salary", "deposit_interest", "conflict", "unknown_empty"]
    assert result.groupby("Валюта")["Значение"].sum().to_dict() == {"RUB": 104, "USD": 3}
    pd.testing.assert_frame_equal(rows, original)
