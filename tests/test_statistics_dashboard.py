from io import BytesIO
from datetime import date

from openpyxl import load_workbook

from src.dashboard.app import _dataframe_to_xlsx_bytes, _statistics_report_layout
from src.dashboard.i18n import localize_report_datasets
from src.dashboard.statistics_data import build_statistics_dashboard_data
from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    add_cash_transaction,
    connect_database,
    create_transaction_draft,
    initialize_database,
    record_investment_trade,
    void_cash_transaction,
)


def _metrics(database):
    frame = build_statistics_dashboard_data(database)["data_statistics"].dataframe
    return frame.set_index("Показатель").to_dict("index")


def _component_text(component):
    if component is None:
        return []
    if isinstance(component, (str, int, float)):
        return [str(component)]
    if isinstance(component, (list, tuple)):
        return [text for child in component for text in _component_text(child)]
    return _component_text(getattr(component, "children", None))


def test_empty_database_statistics_are_explicit(tmp_path):
    database = tmp_path / "empty.sqlite3"
    initialize_database(database)

    metrics = _metrics(database)
    assert metrics["Всего транзакций"]["Значение"] == 0
    assert metrics["Первая операция"]["Значение"] == "—"
    assert metrics["Последняя операция"]["Значение"] == "—"
    assert metrics["Календарный охват, дней"]["Значение"] == "—"
    assert metrics["Календарный охват, дней"]["Детали"] == "Нет данных"
    assert "Доходных категорий" not in metrics
    assert "Расходных категорий" not in metrics
    assert metrics["Инвестиционных сделок"]["Значение"] == 0
    assert metrics["Снимков активов"]["Значение"] == 0
    assert metrics["Валют в финансовых фактах"]["Значение"] == 0


def test_statistics_count_only_business_facts_in_one_all_time_profile(tmp_path):
    database = tmp_path / "populated.sqlite3"
    initialize_database(database)
    add_cash_transaction(
        database, transaction_id="income", occurred_on="2024-01-15",
        flow_direction="income", category_id="income.salary", amount="100",
        currency="RUB",
    )
    add_cash_transaction(
        database, transaction_id="expense", occurred_on="2025-03-20",
        flow_direction="expense", category_id="expense.food", amount="20",
        currency="USD",
    )
    add_cash_transaction(
        database, transaction_id="void", occurred_on="2026-01-01",
        flow_direction="income", category_id="income.interest", amount="1",
        currency="RUB",
    )
    void_cash_transaction(database, "void", reason="synthetic")
    create_transaction_draft(
        database, occurred_on="2026-02-01", flow_direction="income",
        category_id="income.other", amount="10", currency="RUB",
        origin_kind="manual", origin_key="open-draft",
    )
    record_investment_trade(
        database, occurred_on="2025-01-01", operation="buy", ticker="ABC",
        asset_type="stocks", quantity="2", unit_price="10", currency="USD",
        operation_key="buy-abc",
    )
    record_investment_trade(
        database, occurred_on="2025-02-01", operation="sell", ticker="ABC",
        asset_type="stocks", quantity="1", unit_price="12", currency="USD",
        operation_key="sell-abc",
    )
    record_investment_trade(
        database, occurred_on="2025-03-01", operation="buy", ticker="BTC",
        asset_type="crypto", quantity="0.1", unit_price="100", currency="KZT",
        operation_key="buy-btc",
    )
    add_asset_account(database, "cash", "Cash")
    add_asset_account(database, "deposit", "Deposit")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="cash", period="2024-02",
        amount="100", currency="RUB",
    )
    add_asset_snapshot(
        database, snapshot_id="snapshot-2", account_id="cash", period="2025-03",
        amount="50", currency="USD",
    )
    add_asset_snapshot(
        database, snapshot_id="snapshot-3", account_id="deposit", period="2025-03",
        amount="200", currency="KZT",
    )
    with connect_database(database, writable=True) as connection:
        connection.execute(
            """INSERT INTO audit_events
               (entity_type, entity_id, action, reason, occurred_at)
               VALUES ('synthetic', 'technical', 'created', 'must not count',
                       '2026-01-01T00:00:00Z')"""
        )

    metrics = _metrics(database)
    assert metrics["Всего транзакций"]["Значение"] == 2
    assert metrics["Доходных транзакций"]["Значение"] == 1
    assert metrics["Расходных транзакций"]["Значение"] == 1
    assert metrics["Открытых черновиков"]["Значение"] == 1
    assert metrics["Первая операция"]["Значение"] == "2024-01-15"
    assert metrics["Последняя операция"]["Значение"] == "2025-03-20"
    assert metrics["Календарный охват, дней"]["Значение"] == (
        date(2025, 3, 20) - date(2024, 1, 15)
    ).days + 1
    assert metrics["Календарный охват, дней"]["Детали"] == "1 г. 2 мес."
    assert metrics["Месяцев с операциями"]["Значение"] == 2
    assert "Доходных категорий" not in metrics
    assert "Расходных категорий" not in metrics
    assert metrics["Инвестиционных сделок"]["Значение"] == 3
    assert metrics["Покупок"]["Значение"] == 2
    assert metrics["Продаж"]["Значение"] == 1
    assert metrics["Инструментов в сделках"]["Значение"] == 2
    assert metrics["Счетов активов"]["Значение"] == 2
    assert metrics["Снимков активов"]["Значение"] == 3
    assert metrics["Первый снимок"]["Значение"] == "2024-02"
    assert metrics["Последний снимок"]["Значение"] == "2025-03"
    assert metrics["Валют в финансовых фактах"]["Значение"] == 3


def test_statistics_ui_and_xlsx_share_the_same_dataset(tmp_path):
    database = tmp_path / "statistics.sqlite3"
    initialize_database(database)
    add_cash_transaction(
        database, transaction_id="income", occurred_on="2026-01-03",
        flow_direction="income", category_id="income.salary", amount="100",
        currency="RUB",
    )
    dataset = build_statistics_dashboard_data(database)["data_statistics"]

    layout = _statistics_report_layout(dataset, theme="light")
    visible_text = _component_text(layout)
    assert "Статистика" in visible_text
    assert "Всего транзакций" in visible_text
    assert "1" in visible_text

    workbook = load_workbook(
        BytesIO(_dataframe_to_xlsx_bytes(dataset.dataframe, dataset.title)),
        data_only=False,
    )
    worksheet = workbook["Статистика"]
    exported = list(worksheet.values)
    assert exported[0] == tuple(dataset.dataframe.columns)
    expected_rows = [
        tuple(None if value == "" else value for value in row)
        for row in dataset.dataframe.itertuples(index=False, name=None)
    ]
    assert exported[1:] == expected_rows


def test_statistics_layout_localizes_labels_without_changing_raw_data(tmp_path):
    database = tmp_path / "statistics.sqlite3"
    initialize_database(database)
    raw = build_statistics_dashboard_data(database)

    localized = localize_report_datasets(raw, "en")["data_statistics"]
    layout = _statistics_report_layout(localized, theme="dark", locale="en")
    visible_text = _component_text(layout)

    assert localized.title == "Statistics"
    assert "Transactions" in visible_text
    assert "Total transactions" in visible_text
    assert "History coverage" in visible_text
    assert raw["data_statistics"].dataframe.loc[0, "Показатель"] == "Всего транзакций"
