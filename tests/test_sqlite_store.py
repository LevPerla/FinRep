import csv
from decimal import Decimal
from pathlib import Path
import sqlite3

from flask import Flask, session
import pytest

from src import config
from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    add_cash_transaction,
    add_category,
    asset_snapshots,
    cash_transactions,
    connect_database,
    fx_rates,
    initialize_database,
    save_asset_month,
    save_month,
    saved_asset_months,
    saved_months,
    save_fx_rate,
)


def test_initial_categories_match_the_agreed_income_and_expense_lists(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    with connect_database(database) as connection:
        rows = connection.execute(
            "SELECT direction, name_ru FROM categories WHERE active = 1 ORDER BY name_ru"
        ).fetchall()
    names = {
        direction: {row["name_ru"] for row in rows if row["direction"] == direction}
        for direction in ("income", "expense")
    }
    assert names["income"] == {"Зарплата", "Проценты", "Инвест доход", "Прочие доходы"}
    assert names["expense"] == {
        "Быт и товары для дома", "На себя", "Одежда", "Пища", "Поездки",
        "Прочее", "Связь", "Развлечения", "Соц жизнь", "Транспорт",
    }


def test_sample_values_round_trip_without_touching_csv(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    sample = Path(config.SAMPLE_DATA_PATH)
    with (sample / "transactions_info/2025/2025_01_.csv").open(newline="") as source:
        income_cell = next(csv.DictReader(source, delimiter=";"))["Доход"]
    amount, currency, comment = income_cell.split("|", 2)
    with (sample / "assets_info/2025/2025_01.csv").open(newline="") as source:
        account_row = next(csv.DictReader(source, delimiter=";"))
    asset_amount, asset_currency = account_row["Сумма"].split("|", 1)

    initialize_database(database)
    initialize_database(database)
    save_month(database, "2025-01")
    save_month(database, "2025-02")  # A saved month may have no nonzero operation.
    add_cash_transaction(
        database, transaction_id="one", period="2025-01", occurred_on="2025-01-03",
        category_id="income.salary", amount=amount, currency=currency,
        comment=comment,
    )
    add_cash_transaction(
        database, transaction_id="two", period="2025-01", occurred_on="2025-01-03",
        category_id="income.salary", amount=amount, currency=currency,
        comment=comment,
    )
    add_asset_account(database, "account-1", account_row["Счет"], asset_currency)
    save_asset_month(database, "2025-01")
    save_asset_month(database, "2025-02")
    add_asset_snapshot(
        database, snapshot_id="snapshot-1", account_id="account-1",
        period="2025-01", amount=asset_amount,
    )

    assert saved_months(database) == ["2025-01", "2025-02"]
    assert saved_asset_months(database) == ["2025-01", "2025-02"]
    assert [(row["id"], row["amount"]) for row in cash_transactions(database)] == [
        ("one", Decimal(amount)), ("two", Decimal(amount)),
    ]
    assert asset_snapshots(database)[0]["amount"] == Decimal(asset_amount)
    with connect_database(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_transaction_direction_comes_from_category_and_amount_is_positive(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    add_category(database, "income.custom", "Новый доход", direction="income", activity_class="passive")
    common = dict(
        path=database, transaction_id="tx", period="2026-01",
        occurred_on="2026-01-01", currency="RUB",
    )

    with pytest.raises(ValueError, match="active category"):
        add_cash_transaction(**common, category_id="income.unknown", amount="0.001")
    with pytest.raises(ValueError, match="amount must be positive"):
        add_cash_transaction(**common, category_id="income.investment", amount="-0.001")
    with pytest.raises(ValueError, match="amount must be positive"):
        add_cash_transaction(**common, category_id="expense.other", amount="0")
    add_cash_transaction(**common, category_id="income.custom", amount="0.001")
    add_cash_transaction(**{**common, "transaction_id": "loss"},
                         category_id="expense.other", amount="0.001")
    rows = {row["id"]: row for row in cash_transactions(database)}
    assert rows["tx"]["amount"] == Decimal("0.001")
    assert rows["tx"]["activity_class"] == "passive"
    assert rows["loss"]["amount"] == Decimal("0.001")
    assert rows["loss"]["direction"] == "expense"
    assert saved_months(database) == ["2026-01"]
    with connect_database(database) as connection:
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(cash_transactions)")}
    assert "legacy_category" not in columns
    assert "legacy_coordinate" in columns
    with pytest.raises(sqlite3.IntegrityError):
        add_cash_transaction(**common, category_id="income.custom", amount="0.001")


def test_fx_rates_preserve_precision_and_source(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    save_fx_rate(
        database, rate_date="2026-01-31", currency="RUB", usd_rate="0.012345678901",
        source="official", fetched_at="2026-02-01T12:00:00",
    )
    assert fx_rates(database) == [{
        "date": "2026-01-31", "currency": "RUB",
        "usd_rate_text": "0.012345678901", "usd_rate": Decimal("0.012345678901"),
        "source": "official", "fetched_at": "2026-02-01T12:00:00",
    }]
    save_fx_rate(database, rate_date="2026-01-31", currency="RUB", usd_rate="0.0124")
    assert len(fx_rates(database)) == 1
    assert fx_rates(database)[0]["usd_rate"] == Decimal("0.0124")
    with pytest.raises(ValueError, match="usd_rate must be positive"):
        save_fx_rate(database, rate_date="2026-02-01", currency="RUB", usd_rate="0")
    with pytest.raises(ValueError, match="ISO date"):
        save_fx_rate(database, rate_date="20260201", currency="RUB", usd_rate="0.01")


def test_foreign_keys_are_enforced_on_every_connection(tmp_path):
    database = tmp_path / "synthetic.sqlite3"
    initialize_database(database)
    with pytest.raises(sqlite3.IntegrityError):
        with connect_database(database, writable=True) as connection:
            connection.execute(
                "INSERT INTO cash_transactions "
                "(id, period, occurred_on, category_id, amount_text, currency) "
                "VALUES ('invalid', '2026-01', '2026-01-01', 'missing', '1', 'RUB')"
            )
    assert cash_transactions(database) == []


def test_test_mode_cannot_open_live_or_write_database(tmp_path, monkeypatch):
    sample_dir = tmp_path / "sample"
    sample_dir.mkdir()
    monkeypatch.setattr(config, "SAMPLE_DATA_PATH", str(sample_dir))
    database = sample_dir / "synthetic.sqlite3"
    live_database = tmp_path / "live.sqlite3"
    initialize_database(database)
    initialize_database(live_database)
    app = Flask(__name__)
    app.secret_key = "test-only"

    with app.test_request_context("/"):
        session["authenticated"] = True
        session["data_mode"] = "test"
        assert saved_months(database) == []
        with pytest.raises(PermissionError, match="sample database"):
            saved_months(live_database)
        with pytest.raises(PermissionError, match="только на чтение"):
            save_month(database, "2026-01")
        with pytest.raises(PermissionError, match="только на чтение"):
            initialize_database(sample_dir / "must-not-exist.sqlite3")

    assert not (sample_dir / "must-not-exist.sqlite3").exists()
