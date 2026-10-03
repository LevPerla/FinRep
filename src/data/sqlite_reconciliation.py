"""Financial comparison between current CSV readers and a migrated SQLite copy."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from src.dashboard.income_sources import classify_income_comment
from src.data.get import _get_assets_cached, _get_transactions_cached
from src.data.sqlite_store import connect_database


_EXPENSES = {
    "Быт и товары для дома": "expense.home_goods", "На себя": "expense.personal",
    "Одежда": "expense.clothing", "Пища": "expense.food", "Поездки": "expense.travel",
    "Крупные покупки/ Поездки": "expense.travel", "Прочее": "expense.other",
    "Связь": "expense.communication", "Развлечения": "expense.entertainment_legacy",
    "Соц жизнь": "expense.social", "Соц.жизнь": "expense.social", "Транспорт": "expense.transport",
}
_INCOMES = {
    "Сбережения": "income.other", "Зарплата": "income.salary",
    "Проценты": "income.interest", "Инвест доход": "income.investment",
    "Прочие доходы": "income.other",
}
_DEBT_CASH_ACTIONS = {
    "Дебиторская задолженность": ("issue", "receivable"),
    "Долги (у меня)": ("issue", "receivable"),
    "Погашение деб. зад.": ("repayment", "receivable"),
    "Кредиторская задолженность": ("issue", "liability"),
    "Погашение кред. зад.": ("repayment", "liability"),
}


@dataclass(frozen=True)
class ReconciliationCheck:
    name: str
    passed: bool
    expected: object
    actual: object


@dataclass(frozen=True)
class ReconciliationReport:
    checks: tuple[ReconciliationCheck, ...]
    blocking_review_items: int

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def ready_for_cutover(self) -> bool:
        return self.passed and self.blocking_review_items == 0


def reconcile_migration(source_root: str | Path, target_db: str | Path,
                        migration_db: str | Path) -> ReconciliationReport:
    root = Path(source_root).resolve()
    checks = [
        _check("cash_by_month_currency_direction_category",
               _legacy_cash(root), _target_cash(target_db)),
        _check("asset_snapshots_by_period_account_currency",
               _legacy_assets(root), _target_assets(target_db)),
        _check("debt_principal_by_kind_currency",
               _legacy_debts(root), _target_debts(target_db)),
        _check("debt_payments_by_currency",
               _legacy_debt_payments(root), _target_debt_payments(target_db)),
        _check("debt_cash_events_by_month_currency_kind_side",
               _legacy_debt_cash_events(root), _target_debt_cash_events(target_db)),
        _check("investment_cash_events_by_month_currency_kind",
               _legacy_investment_cash_events(root),
               _target_investment_cash_events(target_db)),
        _check("investment_position_quantity_by_ticker",
               _legacy_positions(root), _target_positions(target_db)),
        _check("market_price_row_count",
               _csv_row_count(root / "investments/price_cache.csv"),
               _table_count(target_db, "market_price_observations")),
        _check("fx_observation_row_count",
               _csv_row_count(root / "rates/fx_rates.csv"),
               _table_count(target_db, "fx_rate_observations")),
        _check("annual_goal_row_count",
               _csv_row_count(root / "plans/goals.csv"),
               _table_count(target_db, "annual_goals")),
    ]
    migration = sqlite3.connect(Path(migration_db))
    try:
        blocking = migration.execute(
            "SELECT count(*) FROM migration_issues WHERE blocking = 1"
        ).fetchone()[0]
        migration.execute("""CREATE TABLE IF NOT EXISTS comparison_results (
            check_name TEXT PRIMARY KEY, status TEXT NOT NULL,
            expected_json TEXT NOT NULL, actual_json TEXT NOT NULL
        ) STRICT""")
        migration.execute("DELETE FROM comparison_results")
        migration.executemany(
            "INSERT INTO comparison_results VALUES (?, ?, ?, ?)",
            [(item.name, "pass" if item.passed else "fail", _json(item.expected), _json(item.actual))
             for item in checks],
        )
        migration.commit()
    finally:
        migration.close()
    return ReconciliationReport(tuple(checks), blocking)


def _check(name: str, expected, actual) -> ReconciliationCheck:
    return ReconciliationCheck(name, expected == actual, expected, actual)


def _legacy_cash(root: Path) -> dict[str, int]:
    data = _get_transactions_cached(str(root / "transactions_info"))
    result: dict[str, int] = {}
    for row in data.to_dict("records"):
        amount = Decimal(str(row["Значение"]))
        if not amount.is_finite() or amount == 0:
            continue
        category = str(row["Категория"])
        target = None
        base_direction = None
        if category == "Доход":
            reason = classify_income_comment(row.get("Комментарий"))
            target = {"salary": "income.salary", "deposit_interest": "income.interest"}.get(
                reason, "income.unknown")
            base_direction = "income"
        elif category in _INCOMES:
            target, base_direction = _INCOMES[category], "income"
        elif category in _EXPENSES:
            target, base_direction = _EXPENSES[category], "expense"
        if target is None:
            continue
        direction = base_direction if amount > 0 else ("expense" if base_direction == "income" else "income")
        if direction != base_direction:
            target = "expense.other" if direction == "expense" else "income.other"
        minor = _minor(abs(amount))
        period = row["Дата"].strftime("%Y-%m")
        key = "|".join((period, str(row["Валюта"]), direction, target))
        result[key] = result.get(key, 0) + minor
    return dict(sorted(result.items()))


def _target_cash(path: str | Path) -> dict[str, int]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT period, currency_code, flow_direction, category_id,
            SUM(amount_minor) AS amount_minor FROM v_cash_transactions
            GROUP BY period, currency_code, flow_direction, category_id""").fetchall()
    return dict(sorted(("|".join((r[0], r[1], r[2], r[3])), r[4]) for r in rows))


def _legacy_assets(root: Path) -> dict[str, int]:
    data = _get_assets_cached(str(root / "assets_info"))
    result: dict[str, int] = {}
    for row in data.to_dict("records"):
        key = "|".join((f"{row['Год']}-{int(row['Месяц']):02d}",
                        str(row["Счет"]).strip(), str(row["Валюта"])))
        result[key] = result.get(key, 0) + _minor(Decimal(str(row["Значение"])))
    return dict(sorted(result.items()))


def _target_assets(path: str | Path) -> dict[str, int]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT period, account_name, currency_code, SUM(amount_minor)
            FROM v_asset_snapshots GROUP BY period, account_name, currency_code""").fetchall()
    return dict(sorted(("|".join((r[0], r[1], r[2])), r[3]) for r in rows))


def _legacy_debts(root: Path) -> dict[str, int]:
    return _csv_money_group(root / "debts/debts.csv", ("type", "principal_currency"), "principal_amount")


def _target_debts(path: str | Path) -> dict[str, int]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT kind, principal_currency_code, SUM(principal_amount_minor)
            FROM debts GROUP BY kind, principal_currency_code""").fetchall()
    return dict(sorted((f"{r[0]}|{r[1]}", r[2]) for r in rows))


def _legacy_debt_payments(root: Path) -> dict[str, int]:
    import csv
    debt_currency = {}
    with (root / "debts/debts.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, delimiter=";"):
            debt_currency[row["debt_id"]] = row["principal_currency"]
    result = {}
    with (root / "debts/debt_payments.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, delimiter=";"):
            currency = debt_currency[row["debt_id"]]
            result[currency] = result.get(currency, 0) + _minor(Decimal(row["amount"]))
    return dict(sorted(result.items()))


def _target_debt_payments(path: str | Path) -> dict[str, int]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT d.principal_currency_code, SUM(p.principal_amount_minor)
            FROM debt_payments p JOIN debts d ON d.id = p.debt_id
            GROUP BY d.principal_currency_code""").fetchall()
    return dict(sorted((r[0], r[1]) for r in rows))


def _legacy_debt_cash_events(root: Path) -> dict[str, int]:
    data = _get_transactions_cached(str(root / "transactions_info"))
    result: dict[str, int] = {}
    for row in data.to_dict("records"):
        category = str(row["Категория"])
        action = _DEBT_CASH_ACTIONS.get(category)
        if action is None:
            continue
        amount = Decimal(str(row["Значение"]))
        if not amount.is_finite() or amount <= 0:
            continue
        event_kind, side = action
        key = "|".join((row["Дата"].strftime("%Y-%m"), str(row["Валюта"]),
                        event_kind, side))
        result[key] = result.get(key, 0) + _minor(amount)
    return dict(sorted(result.items()))


def _target_debt_cash_events(path: str | Path) -> dict[str, int]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT substr(occurred_on, 1, 7), currency_code,
            event_kind, side, SUM(amount_minor) FROM debt_cash_events
            GROUP BY substr(occurred_on, 1, 7), currency_code, event_kind, side""").fetchall()
    return dict(sorted(("|".join((r[0], r[1], r[2], r[3])), r[4]) for r in rows))


def _legacy_investment_cash_events(root: Path) -> dict[str, int]:
    data = _get_transactions_cached(str(root / "transactions_info"))
    result: dict[str, int] = {}
    for row in data.to_dict("records"):
        if str(row["Категория"]) != "Инвестиции":
            continue
        amount = Decimal(str(row["Значение"]))
        if not amount.is_finite() or amount == 0:
            continue
        flow_kind = "contribution" if amount > 0 else "withdrawal"
        key = "|".join((row["Дата"].strftime("%Y-%m"), str(row["Валюта"]), flow_kind))
        result[key] = result.get(key, 0) + _minor(abs(amount))
    return dict(sorted(result.items()))


def _target_investment_cash_events(path: str | Path) -> dict[str, int]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT substr(occurred_on, 1, 7), currency_code,
            flow_kind, SUM(amount_minor) FROM investment_cash_events
            GROUP BY substr(occurred_on, 1, 7), currency_code, flow_kind""").fetchall()
    return dict(sorted(("|".join((r[0], r[1], r[2])), r[3]) for r in rows))


def _legacy_positions(root: Path) -> dict[str, str]:
    import csv
    result: dict[str, Decimal] = {}
    path = root / "investments/transactions.csv"
    if not path.exists():
        legacy = root / "investments/investments.csv"
        if not legacy.exists():
            return {}
        with legacy.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream, delimiter=";"):
                quantity = Decimal(row["Количество"].replace(",", "."))
                if row["Тип_транзакции"] == "Продажа":
                    quantity = -quantity
                ticker = row["Тикер"].upper()
                result[ticker] = result.get(ticker, Decimal("0")) + quantity
        return {key: format(value, "f") for key, value in sorted(result.items())}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, delimiter=";"):
            quantity = Decimal(row["quantity"].replace(",", "."))
            if row["operation"].lower() == "sell":
                quantity = -quantity
            ticker = row["ticker"].upper()
            result[ticker] = result.get(ticker, Decimal("0")) + quantity
    return {key: format(value, "f") for key, value in sorted(result.items())}


def _target_positions(path: str | Path) -> dict[str, str]:
    result: dict[str, Decimal] = {}
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT i.ticker, t.operation, t.quantity_text
            FROM investment_trades t JOIN instruments i ON i.id = t.instrument_id""").fetchall()
    for ticker, operation, quantity_text in rows:
        quantity = Decimal(quantity_text) * (-1 if operation == "sell" else 1)
        result[ticker] = result.get(ticker, Decimal("0")) + quantity
    return {key: format(value, "f") for key, value in sorted(result.items())}


def _csv_money_group(path: Path, keys: tuple[str, ...], amount_name: str) -> dict[str, int]:
    import csv
    result = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, delimiter=";"):
            key = "|".join(row[name] for name in keys)
            result[key] = result.get(key, 0) + _minor(Decimal(row[amount_name]))
    return dict(sorted(result.items()))


def _csv_row_count(path: Path) -> int:
    if not path.exists():
        return 0
    import csv
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return sum(1 for _ in csv.DictReader(stream, delimiter=";"))


def _table_count(path: str | Path, table: str) -> int:
    allowed = {"market_price_observations", "fx_rate_observations", "annual_goals"}
    if table not in allowed:
        raise ValueError("unsupported reconciliation table")
    with connect_database(path) as connection:
        return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _minor(value: Decimal) -> int:
    scaled = value * 100
    if scaled != scaled.to_integral_value():
        raise ValueError("legacy money exceeds two decimal places")
    return int(scaled)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
