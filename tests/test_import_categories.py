from unittest.mock import patch

import pandas as pd

from src.data.importers import kaspi_pdf


def test_category_comes_from_latest_transaction_with_same_comment():
    history = pd.DataFrame(
        [
            {"Дата": "2025-01-10", "Категория": "Пища", "Комментарий": "Coffee shop"},
            {"Дата": "2026-03-20", "Категория": "Досуг", "Комментарий": "  COFFEE   SHOP "},
        ]
    )

    with patch.object(kaspi_pdf, "get_transactions", return_value=history):
        data = kaspi_pdf._import_frame_from_rows(
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


def test_category_falls_back_to_import_rules_without_history_match():
    with patch.object(kaspi_pdf, "get_transactions", return_value=pd.DataFrame()):
        category = kaspi_pdf._categorize("Cafe near home", -500.0, {})

    assert category == "Пища"
