from pathlib import Path

import pytest

from src import config
from src.data.get import clear_data_cache, get_assets, get_investments, get_transactions
from src.data.sqlite_migration import migrate_core_csv
from src.data.sqlite_store import (
    add_category,
    add_cash_transaction,
    create_debt_record,
    initialize_database,
    publish_domain_drafts,
    save_month,
)
from src.data.assets_editor import ensure_asset_snapshot, read_asset_snapshot, write_asset_snapshot
from src.dashboard.planning_data import _load_goals, save_goal_targets
from src.dashboard.income_data import build_income_dashboard_data
from src.data.get_finance import _append_cache_rows, _read_cache
from src.data.investments import read_investment_transactions, read_price_cache, write_price_cache
from src.data.debts import create_debt, create_debt_payment_from_cash, read_debt_payments, read_debts
from src.data.crypto import (
    read_crypto_balances,
    read_crypto_refresh_status,
    refresh_crypto_balances,
)
from src.data.sqlite_store import upsert_crypto_wallet
from src.data import staging
from src.data.validation import validate_all_data
from src.model.create_tables import clear_table_cache, get_balance_by_month
import pandas as pd


def test_sqlite_backend_feeds_existing_analytics_shapes(tmp_path, monkeypatch):
    target = tmp_path / "target.sqlite3"
    summary = migrate_core_csv(
        Path(config.SAMPLE_DATA_PATH), target, tmp_path / "migration.sqlite3")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(target))
    clear_data_cache()
    try:
        transactions = get_transactions()
        assets = get_assets()
        investments = get_investments()
    finally:
        clear_data_cache()

    assert list(transactions.columns) == [
        "Дата", "Категория", "Валюта", "Значение", "Комментарий", "Год", "Квартал", "Месяц"]
    assert transactions.loc[transactions["Значение"].ne(0)].shape[0] == (
        summary.cash_imported + summary.domain_cash_events_imported)
    assert set(transactions["Категория"]).issubset({
        "Зарплата", "Проценты", "Инвест доход", "Прочие доходы",
        "Быт и товары для дома", "На себя", "Одежда", "Пища", "Поездки",
        "Прочее", "Связь", "Развлечения", "Соц жизнь", "Транспорт",
        "Доход без категории",
        "Инвестиции", "Дебиторская задолженность", "Погашение деб. зад.",
        "Кредиторская задолженность", "Погашение кред. зад.",
    })
    assert list(assets.columns) == ["Счет", "Валюта", "Значение", "Год", "Квартал", "Месяц"]
    assert len(assets) == summary.snapshots_imported
    assert len(investments) == summary.trades_imported
    assert {"Тип_транзакции", "Актив", "Тикер", "Количество", "Дата", "Цена", "Валюта"}.issubset(
        investments.columns)
    assert set(config.INCOME_CATEGORY_LABELS).issubset(config.NOT_COST_COLS)
    assert config.UNCLASSIFIED_INCOME_LABEL in config.NOT_COST_COLS


def test_test_mode_never_switches_to_live_sqlite(tmp_path, monkeypatch):
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(tmp_path / "missing.sqlite3"))
    monkeypatch.setattr(config, "is_test_mode", lambda: True)
    assert not config.use_sqlite_storage()


def test_sqlite_asset_and_goal_ui_adapters_round_trip(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))

    saved = write_asset_snapshot(
        [{"account": "Card", "amount": "123.45", "currency": "RUB"}],
        "2026", "5")
    assert saved["rows"] == 1
    loaded = read_asset_snapshot("2026", "5")
    assert loaded.to_dict("records") == [
        {"account": "Card", "amount": loaded.iloc[0]["amount"], "currency": "RUB"}]
    assert str(loaded.iloc[0]["amount"]) == "123.45"
    copied = ensure_asset_snapshot("2026", "6")
    assert copied["created"]
    assert read_asset_snapshot("2026", "6")["account"].tolist() == ["Card"]

    save_goal_targets(
        "2027", "RUB",
        [{"Показатель": "Капитал", "Цель": "1000000"},
         {"Показатель": "Средний расход/мес", "Цель": "50000"}],
    )
    goals = _load_goals()
    assert len(goals) == 1
    assert str(goals.iloc[0]["target_capital"]) == "1000000.00"
    assert str(goals.iloc[0]["target_monthly_expense"]) == "50000.00"


def test_sqlite_fx_and_price_adapters_append_observations(tmp_path, monkeypatch):
    target = tmp_path / "target.sqlite3"
    migrate_core_csv(Path(config.SAMPLE_DATA_PATH), target, tmp_path / "migration.sqlite3")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(target))
    _append_cache_rows(
        "EUR", pd.Series(["1.25"], index=pd.to_datetime(["2027-01-01"])), "test-provider")
    fx = _read_cache()
    assert ((fx["currency"] == "EUR") & (fx["date"] == pd.Timestamp("2027-01-01"))).any()

    trades = read_investment_transactions()
    assert not trades.empty
    prices = read_price_cache()
    ticker = trades.iloc[0]["ticker"]
    currency = trades.iloc[0]["currency"]
    updated = pd.concat([prices, pd.DataFrame([{
        "date": "2027-01-01", "ticker": ticker, "price": "123.456",
        "currency": currency, "source": "test-provider", "fetched_at": "2027-01-01T10:00:00Z",
    }])], ignore_index=True)
    write_price_cache(updated)
    stored = read_price_cache()
    assert ((stored["ticker"] == ticker) & (stored["date"] == "2027-01-01")
            & (stored["price"] == "123.456")).any()


def test_sqlite_debt_ui_adapter_uses_atomic_commands(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    created = create_debt(
        debt_type="liability", counterparty="Bank", opened_date="2026-05-01",
        principal_amount="1000", principal_currency="RUB",
        operation_id="ui-create-debt")
    payment = create_debt_payment_from_cash(
        debt_id=created["debt_id"], date="2026-05-10", cash_amount="250",
        cash_currency="RUB", operation_id="ui-pay-debt")
    assert payment["remaining_minor"] == 75000
    assert read_debts()["debt_id"].tolist() == [created["debt_id"]]
    assert read_debt_payments()["payment_id"].tolist() == [payment["payment_id"]]


def test_sqlite_crypto_balance_refresh_keeps_success_after_failure(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    upsert_crypto_wallet(
        database, account_label="Cold", chain="bitcoin", asset_code="BTC",
        address="public-address")
    monkeypatch.setattr("src.data.crypto._fetch_wallet_balance", lambda *_args, **_kwargs: "0.25")
    first = refresh_crypto_balances()
    assert first["balance"].tolist() == ["0.25"]

    def fail(*_args, **_kwargs):
        raise RuntimeError("timeout")

    monkeypatch.setattr("src.data.crypto._fetch_wallet_balance", fail)
    second = refresh_crypto_balances()
    assert second["balance"].tolist() == ["0.25"]
    assert second.attrs["errors"]
    assert read_crypto_balances()["balance"].tolist() == ["0.25"]
    assert read_crypto_refresh_status()["status"].tolist() == ["error"]


def test_sqlite_staging_adapter_supports_batch_edit_and_remove(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    _, revision = staging.read_transaction_drafts_snapshot()
    result = staging.append_transaction_draft_rows(
        pd.DataFrame([{
            "date": "2026-06-01", "category": "Пища", "currency": "RUB",
            "amount": "100.25", "comment": "Lunch", "source": "manual",
            "source_id": "manual:1", "status": "draft",
        }]), expected_revision=revision)
    assert result["accepted_rows"] == 1
    rows, revision = staging.read_transaction_drafts_snapshot()
    assert rows.iloc[0]["category"] == "Пища"
    assert rows.iloc[0]["amount"] == "100.25"
    edited = rows.to_dict("records")
    edited[0]["amount"] = "110.50"
    staging.merge_transaction_draft_rows(edited, expected_revision=revision)
    rows, revision = staging.read_transaction_drafts_snapshot()
    assert rows.iloc[0]["amount"] == "110.5"
    staging.delete_transaction_drafts(rows.to_dict("records"), expected_revision=revision)
    hidden, _ = staging.read_transaction_drafts_snapshot()
    assert hidden.empty


def test_sqlite_month_preview_publishes_edited_cash_and_domain_rows(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    _, revision = staging.read_transaction_drafts_snapshot()
    staging.append_transaction_draft_rows(pd.DataFrame([{
        "date": "2026-06-01", "category": "Пища", "currency": "RUB",
        "amount": "100.25", "comment": "Lunch", "source": "manual",
        "source_id": "manual:preview", "status": "draft",
    }]), expected_revision=revision)
    create_debt_record(
        database, kind="receivable", counterparty="Friend", opened_on="2026-06-02",
        principal_amount="500", currency="RUB", operation_key="preview-debt")

    preview, state = staging.prepare_monthly_transaction_export("2026", "6")
    assert preview["category"].tolist() == ["Пища", "Дебиторская задолженность"]
    edited = preview.to_dict("records")
    edited[0]["amount"] = "110.50"
    edited[0]["comment"] = "Edited lunch"
    first = staging.export_monthly_transaction_drafts(
        "2026", "6", preview_rows=edited, preview_state=state)
    second = staging.export_monthly_transaction_drafts(
        "2026", "6", preview_rows=edited, preview_state=state)
    assert first == second
    assert first["exported_rows"] == 2
    assert first["target_path"] == str(database)
    ledger = staging.read_monthly_transaction_csv("2026", "6")
    assert ledger["category"].tolist() == ["Пища", "Дебиторская задолженность"]
    assert ledger.iloc[0]["amount"] == "110.5"
    assert ledger.iloc[0]["comment"] == "Edited lunch"


def test_sqlite_month_preview_rejects_stale_revision(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    _, revision = staging.read_transaction_drafts_snapshot()
    staging.append_transaction_draft_rows(pd.DataFrame([{
        "date": "2026-07-01", "category": "Пища", "currency": "RUB",
        "amount": "10", "source": "manual", "source_id": "stale:1",
        "status": "draft",
    }]), expected_revision=revision)
    preview, state = staging.prepare_monthly_transaction_export("2026", "7")
    _, revision = staging.read_transaction_drafts_snapshot()
    staging.append_transaction_draft_rows(pd.DataFrame([{
        "date": "2026-07-02", "category": "Пища", "currency": "RUB",
        "amount": "20", "source": "manual", "source_id": "stale:2",
        "status": "draft",
    }]), expected_revision=revision)
    with pytest.raises(staging.DraftRevisionConflict, match="Preview устарел"):
        staging.export_monthly_transaction_drafts(
            "2026", "7", preview_rows=preview.to_dict("records"), preview_state=state)
    assert staging.read_monthly_transaction_csv("2026", "7").empty


def test_sqlite_validation_does_not_fall_back_to_csv(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path / "missing-csv-root"))

    assert validate_all_data(False) == []


def test_sqlite_balance_uses_new_income_categories_and_domain_cash_events(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    add_cash_transaction(
        database, transaction_id="salary", occurred_on="2026-08-01",
        flow_direction="income", category_id="income.salary",
        amount="100", currency="RUB")
    add_cash_transaction(
        database, transaction_id="interest", occurred_on="2026-08-02",
        flow_direction="income", category_id="income.interest",
        amount="20", currency="RUB")
    add_cash_transaction(
        database, transaction_id="food", occurred_on="2026-08-03",
        flow_direction="expense", category_id="expense.food",
        amount="30", currency="RUB")
    debt = create_debt_record(
        database, kind="receivable", counterparty="Friend", opened_on="2026-08-04",
        principal_amount="50", currency="RUB", operation_key="balance-debt")
    publish_domain_drafts(
        database, draft_ids=[debt["draft_id"]], operation_key="balance-debt-publish")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setattr(config, "DEBUG", True)
    clear_data_cache()
    clear_table_cache()
    try:
        balance = get_balance_by_month("RUB")
    finally:
        clear_data_cache()
        clear_table_cache()

    assert balance.iloc[0]["Доход"] == 120
    assert balance.iloc[0]["Расход"] == 30
    assert balance.iloc[0]["Дебиторская задолженность"] == 50
    assert balance.iloc[0]["Баланс"] == 40


def test_sqlite_reader_preserves_saved_month_without_transactions(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    save_month(database, "2026-09")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    clear_data_cache()
    try:
        transactions = get_transactions()
    finally:
        clear_data_cache()

    assert len(transactions) == 1
    assert transactions.iloc[0]["Дата"] == pd.Timestamp("2026-09-30")
    assert transactions.iloc[0]["Категория"] == config.UNCLASSIFIED_INCOME_LABEL
    assert transactions.iloc[0]["Значение"] == 0


def test_sqlite_income_dashboard_uses_extensible_category_registry(tmp_path, monkeypatch):
    database = tmp_path / "target.sqlite3"
    initialize_database(database)
    add_category(
        database, "income.rent", "Аренда", direction="income", income_class="passive")
    add_cash_transaction(
        database, transaction_id="rent", occurred_on="2026-09-15",
        flow_direction="income", category_id="income.rent",
        amount="75", currency="RUB")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setattr(config, "DEBUG", True)
    clear_data_cache()
    try:
        datasets = build_income_dashboard_data("RUB")
    finally:
        clear_data_cache()

    sources = datasets["income_sources_monthly"].dataframe.set_index("Источник")["Доход"]
    totals = datasets["income_receipts_monthly"].dataframe.iloc[0]
    assert sources["Аренда"] == 75
    assert totals["Пассивный доход"] == 75
    assert totals["Доля пассивного дохода, %"] == 100


def test_sqlite_cutover_smoke_restarts_after_preview_without_csv_readers(tmp_path, monkeypatch):
    from src.dashboard.app import create_app

    database = tmp_path / "target.sqlite3"
    migrate_core_csv(
        Path(config.SAMPLE_DATA_PATH), database, tmp_path / "migration.sqlite3")
    monkeypatch.setenv("FINREP_STORAGE_BACKEND", "sqlite")
    monkeypatch.setenv("FINREP_SQLITE_PATH", str(database))
    monkeypatch.setattr("src.data.proccess.get_actual_fx_rate", lambda *_args: 1.0)

    def no_csv_fallback(*_args, **_kwargs):
        raise AssertionError("SQLite runtime attempted to read a source CSV dataset")

    no_csv_fallback.cache_clear = lambda: None
    monkeypatch.setattr("src.data.get._get_transactions_cached", no_csv_fallback)
    monkeypatch.setattr("src.data.get._get_assets_cached", no_csv_fallback)
    monkeypatch.setattr("src.data.get._get_investments_cached", no_csv_fallback)
    clear_data_cache()
    clear_table_cache()
    first = create_app()
    assert first.layout is not None

    _, revision = staging.read_transaction_drafts_snapshot()
    staging.append_transaction_draft_rows(pd.DataFrame([{
        "date": "2027-01-03", "category": "Пища", "currency": "RUB",
        "amount": "125.50", "comment": "Cutover smoke", "source": "manual",
        "source_id": "cutover-smoke", "status": "ready",
    }]), expected_revision=revision)
    preview, state = staging.prepare_monthly_transaction_export("2027", "1")
    staging.export_monthly_transaction_drafts(
        "2027", "1", preview_rows=preview.to_dict("records"), preview_state=state)

    clear_data_cache()
    clear_table_cache()
    restarted = create_app()
    assert restarted.layout is not None
    assert staging.read_monthly_transaction_csv("2027", "1")["comment"].tolist() == [
        "Cutover smoke"]
