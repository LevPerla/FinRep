import pandas as pd

from src.dashboard import main_data


def test_top_purchases_sorts_limits_and_preserves_identity():
    purchases = [
        {
            "Дата": pd.Timestamp("2025-01-01") + pd.Timedelta(days=index),
            "Категория": "Пища",
            "Валюта": "RUB",
            "Значение": float(index + 1),
            "Комментарий": f"Покупка {index + 1}",
            "Год": "2025",
            "transaction_id": str(index),
            "category_id": "expense.food",
            "row_version": 1,
        }
        for index in range(17)
    ]
    transactions = pd.DataFrame(
        purchases
        + [
            {
                "Дата": pd.Timestamp("2024-12-31"),
                "Категория": "Поездки",
                "Валюта": "RUB",
                "Значение": 1000.0,
                "Комментарий": "Покупка из другого года",
                "Год": "2024",
                "transaction_id": "older",
                "category_id": "expense.travel",
                "row_version": 2,
            },
        ]
    )
    history = main_data._rank_top_purchases(transactions)

    assert len(history) == 15
    assert history.iloc[0]["Комментарий"] == "Покупка из другого года"
    assert history.iloc[0]["transaction_id"] == "older"
    assert history.iloc[0]["row_version"] == 2
    assert history.iloc[-1]["Сумма"] == 4.0
