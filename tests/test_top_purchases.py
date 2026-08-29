import pandas as pd

from src import config
from src.dashboard import main_data


def test_top_purchases_filters_sorts_and_limits(monkeypatch):
    purchases = [
        {
            "Дата": pd.Timestamp("2025-01-01") + pd.Timedelta(days=index),
            "Категория": "Пища",
            "Валюта": "RUB",
            "Значение": float(index + 1),
            "Комментарий": f"Покупка {index + 1}",
            "Год": "2025",
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
            },
            {
                "Дата": pd.Timestamp("2025-02-01"),
                "Категория": "Доход",
                "Валюта": "RUB",
                "Значение": 5000.0,
                "Комментарий": "Зарплата",
                "Год": "2025",
            },
        ]
    )
    monkeypatch.setattr(main_data, "get_transactions", lambda: transactions)
    monkeypatch.setattr(config, "DEBUG", True)

    history = main_data._top_purchases_data("RUB")
    year = main_data._top_purchases_data("RUB", year="2025")

    assert len(history) == 15
    assert history.iloc[0]["Комментарий"] == "Покупка из другого года"
    assert len(year) == 15
    assert year.iloc[0]["Сумма"] == 17.0
    assert year.iloc[-1]["Сумма"] == 3.0
    assert "Зарплата" not in set(year["Комментарий"])
