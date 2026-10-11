import pandas as pd

from src import config
from src.data import get as data_get
from src.dashboard.main_data import DashboardDataset
from src.dashboard.month_data import _format_integer_table
from src.dashboard.report_tables import _grid_row_data, _report_cell_style, _report_table_style_maps
from src.model import create_tables


def test_month_transactions_show_only_used_categories_in_direction_order(monkeypatch):
    rows = [
        ("2026-10-01", "Кэшбек", 15),
        ("2026-10-01", "Зарплата", 100),
        ("2026-10-01", "Пища", 20),
        ("2026-10-01", "На себя", 5),
        ("2026-10-01", "Сбережения", 3),
        ("2026-10-01", "Прочее", 0),
        ("2026-10-01", "Одежда", 10),
        ("2026-10-01", "Одежда", -10),
        ("2026-10-02", "Кэшбек", -4),
        ("2026-10-02", "Пища", -6),
        ("2026-10-02", "Инвестиции", 2),
        ("2026-09-30", "Транспорт", 99),
    ]
    transactions = pd.DataFrame(rows, columns=["Дата", "Категория", "Значение"])
    transactions["Дата"] = pd.to_datetime(transactions["Дата"])
    transactions["Год"] = transactions["Дата"].dt.year.astype(str)
    transactions["Месяц"] = transactions["Дата"].dt.month.astype(str)
    monkeypatch.setattr(create_tables, "get_transactions", lambda: transactions)
    monkeypatch.setattr(data_get, "get_income_categories", lambda: pd.DataFrame({
        "Категория": ["Зарплата", "Кэшбек"],
    }))
    monkeypatch.setattr(config, "DEBUG", True)

    raw = create_tables._get_month_transactions_cached.__wrapped__("synthetic", "RUB", "2026", "10")

    assert raw.columns.tolist() == [
        "Дата", "Зарплата", "Кэшбек", "На себя", "Одежда", "Пища",
        "Инвестиции", "Сбережения",
    ]
    assert raw["Дата"].tolist() == ["2026-10-01", "2026-10-02"]
    assert raw.loc[0, "Одежда"] == 0

    display = _format_integer_table(raw, not_num_cols=["Дата"])
    assert display.loc[0, ["Зарплата", "Кэшбек", "На себя", "Пища", "Сбережения"]].tolist() == [
        "+100", "+15", "-5", "-20", "+3",
    ]
    assert display.loc[1, ["Кэшбек", "Пища", "Одежда"]].tolist() == ["-4", "+6", "0"]
    assert display.loc[1, "Инвестиции"] == "-2"

    dataset = DashboardDataset("month_transactions", "Транзакции", raw, display)
    records = _grid_row_data(dataset, display)
    styles = _report_table_style_maps(dataset, display)
    for category in ("Зарплата", "Кэшбек"):
        assert styles["levels"][category][1] == "green"
        assert _report_cell_style(category, records[0], styles, "dark")["backgroundColor"]
    for category in ("На себя", "Пища"):
        assert styles["levels"][category][1] == "red"
        assert _report_cell_style(category, records[0], styles, "dark")["backgroundColor"]
    assert _report_cell_style("Кэшбек", records[1], styles, "dark")["backgroundColor"]
    assert _report_cell_style("Пища", records[1], styles, "dark")["backgroundColor"]
    assert "Инвестиции" not in styles["levels"]
    assert "Сбережения" not in styles["levels"]


def test_neutral_debt_columns_show_cash_direction():
    raw = pd.DataFrame([{
        "Дата": "2026-10-01",
        "Дебиторская задолженность": 10,
        "Погашение деб. зад.": 4,
        "Кредиторская задолженность": 8,
        "Погашение кред. зад.": 3,
    }])

    display = _format_integer_table(raw, not_num_cols=["Дата"])

    assert display.iloc[0, 1:].tolist() == ["-10", "+4", "+8", "-3"]
