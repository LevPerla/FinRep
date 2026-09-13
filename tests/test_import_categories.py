from unittest.mock import patch

import pandas as pd

from src.data.importers import common


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


def test_category_falls_back_to_import_rules_without_history_match(tmp_path, monkeypatch):
    rules_path = tmp_path / "import_rules" / "categories.csv"
    rules_path.parent.mkdir(parents=True)
    rules_path.write_text("pattern;category\ncafe;Пища\n", encoding="utf-8")
    monkeypatch.setattr(common.config, "DATA_PATH", str(tmp_path))

    category = common.categorize("Cafe near home", -500.0, {})

    assert category == "Пища"
