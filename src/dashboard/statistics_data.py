"""All-time profile of business data stored in SQLite."""

from datetime import date
from pathlib import Path

import pandas as pd

from src import config
from src.dashboard.main_data import DashboardDataset
from src.data.sqlite_store import connect_database


STATISTICS_COLUMNS = ["Раздел", "Показатель", "Значение", "Детали"]


def build_statistics_dashboard_data(
    database_path: str | Path | None = None,
) -> dict[str, DashboardDataset]:
    """Build an all-time profile from business tables in one SQLite snapshot."""
    if database_path is None:
        if not config.use_sqlite_storage():
            raise RuntimeError("Статистика данных доступна в режиме SQLite.")
        database_path = config.active_database_path()

    facts = _read_statistics(database_path)
    data = _statistics_frame(facts)
    return {
        "data_statistics": DashboardDataset(
            id="data_statistics",
            title="Статистика",
            dataframe=data,
            display_dataframe=data.copy(deep=True),
        )
    }


def _read_statistics(database_path: str | Path) -> dict:
    with connect_database(database_path) as connection:
        row = connection.execute(
            """SELECT
                (SELECT COUNT(*) FROM cash_transactions WHERE status = 'posted')
                    AS transaction_count,
                (SELECT COUNT(*) FROM cash_transactions
                 WHERE status = 'posted' AND flow_direction = 'income')
                    AS income_count,
                (SELECT COUNT(*) FROM cash_transactions
                 WHERE status = 'posted' AND flow_direction = 'expense')
                    AS expense_count,
                (SELECT COUNT(*) FROM transaction_drafts
                 WHERE status IN ('draft', 'ready')) AS open_draft_count,
                (SELECT MIN(occurred_on) FROM cash_transactions WHERE status = 'posted')
                    AS first_transaction_date,
                (SELECT MAX(occurred_on) FROM cash_transactions WHERE status = 'posted')
                    AS last_transaction_date,
                (SELECT COUNT(DISTINCT substr(occurred_on, 1, 7))
                 FROM cash_transactions WHERE status = 'posted')
                    AS transaction_month_count,
                (SELECT COUNT(DISTINCT t.category_id)
                 FROM cash_transactions t JOIN categories c ON c.id = t.category_id
                 WHERE t.status = 'posted' AND c.direction = 'income'
                   AND c.id <> 'income.unknown') AS used_income_category_count,
                (SELECT COUNT(*) FROM categories
                 WHERE direction = 'income' AND active = 1
                   AND id <> 'income.unknown') AS active_income_category_count,
                (SELECT COUNT(DISTINCT t.category_id)
                 FROM cash_transactions t JOIN categories c ON c.id = t.category_id
                 WHERE t.status = 'posted' AND c.direction = 'expense')
                    AS used_expense_category_count,
                (SELECT COUNT(*) FROM categories
                 WHERE direction = 'expense' AND active = 1)
                    AS active_expense_category_count,
                (SELECT COUNT(*) FROM investment_trades) AS trade_count,
                (SELECT COUNT(*) FROM investment_trades WHERE operation = 'buy')
                    AS buy_count,
                (SELECT COUNT(*) FROM investment_trades WHERE operation = 'sell')
                    AS sell_count,
                (SELECT COUNT(DISTINCT instrument_id) FROM investment_trades)
                    AS traded_instrument_count,
                (SELECT COUNT(*) FROM asset_accounts) AS asset_account_count,
                (SELECT COUNT(*) FROM asset_snapshots) AS asset_snapshot_count,
                (SELECT MIN(period) FROM asset_snapshots) AS first_asset_period,
                (SELECT MAX(period) FROM asset_snapshots) AS last_asset_period,
                (SELECT COUNT(*) FROM (
                    SELECT currency_code FROM cash_transactions WHERE status = 'posted'
                    UNION SELECT currency_code FROM asset_snapshots
                    UNION SELECT price_currency_code FROM investment_trades
                    UNION SELECT currency_code FROM investment_cash_events
                    UNION SELECT currency_code FROM debt_cash_events
                )) AS fact_currency_count"""
        ).fetchone()
    return dict(row)


def _statistics_frame(facts: dict) -> pd.DataFrame:
    first_date = _parse_date(facts["first_transaction_date"])
    last_date = _parse_date(facts["last_transaction_date"])
    coverage_days = (last_date - first_date).days + 1 if first_date and last_date else None
    coverage_detail = _calendar_coverage(first_date, last_date)
    missing = "—"
    rows = [
        ("Транзакции", "Всего транзакций", facts["transaction_count"], ""),
        ("Транзакции", "Доходных транзакций", facts["income_count"], ""),
        ("Транзакции", "Расходных транзакций", facts["expense_count"], ""),
        ("Транзакции", "Открытых черновиков", facts["open_draft_count"], "Не входят в итог транзакций"),
        ("Охват истории", "Первая операция", facts["first_transaction_date"] or missing, ""),
        ("Охват истории", "Последняя операция", facts["last_transaction_date"] or missing, ""),
        ("Охват истории", "Календарный охват, дней", coverage_days if coverage_days is not None else missing, coverage_detail),
        ("Охват истории", "Месяцев с операциями", facts["transaction_month_count"], ""),
        (
            "Категории",
            "Доходных категорий",
            f"{facts['used_income_category_count']} / {facts['active_income_category_count']}",
            "Использовано / активно",
        ),
        (
            "Категории",
            "Расходных категорий",
            f"{facts['used_expense_category_count']} / {facts['active_expense_category_count']}",
            "Использовано / активно",
        ),
        ("Инвестиции", "Инвестиционных сделок", facts["trade_count"], ""),
        ("Инвестиции", "Покупок", facts["buy_count"], ""),
        ("Инвестиции", "Продаж", facts["sell_count"], ""),
        ("Инвестиции", "Инструментов в сделках", facts["traded_instrument_count"], ""),
        ("Активы", "Счетов активов", facts["asset_account_count"], ""),
        ("Активы", "Снимков активов", facts["asset_snapshot_count"], ""),
        ("Активы", "Первый снимок", facts["first_asset_period"] or missing, ""),
        ("Активы", "Последний снимок", facts["last_asset_period"] or missing, ""),
        ("Активы", "Валют в финансовых фактах", facts["fact_currency_count"], ""),
    ]
    return pd.DataFrame(rows, columns=STATISTICS_COLUMNS)


def _parse_date(value) -> date | None:
    if not value:
        return None
    return date.fromisoformat(str(value))


def _calendar_coverage(first_date: date | None, last_date: date | None) -> str:
    if first_date is None or last_date is None:
        return "Нет данных"
    months = (last_date.year - first_date.year) * 12 + last_date.month - first_date.month
    if last_date.day < first_date.day:
        months -= 1
    years, remaining_months = divmod(max(months, 0), 12)
    return f"{years} г. {remaining_months} мес."
