"""Deterministic inventory and core CSV migration for an isolated dry run."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3

from src.dashboard.income_sources import classify_income_comment
from src.data.money import parse_money_amount
from src.data.sqlite_store import (
    add_asset_account,
    add_asset_snapshot,
    add_cash_transaction,
    connect_database,
    initialize_database,
    link_entity_source,
    link_transaction_source,
    register_source_record,
    save_asset_month,
    save_fx_rate,
    save_month,
)
from src.data.staging import TRANSACTION_BOUNDARY_RE, sanitize_transaction_comment


PARSER_VERSION = "core-csv-v1"
_ARCHIVE_EXACT = {
    "balance/positions.csv",
    "balance/valuations.csv",
    "plans/fire_scenarios.csv",
}
_WORKING_EXACT = {
    "debts/debts.csv": "debts",
    "debts/debt_payments.csv": "debt_payments",
    "import_rules/categories.csv": "category_rules",
    "investments/instruments.csv": "investment_instruments",
    "investments/investments.csv": "investments_legacy",
    "investments/transactions.csv": "investment_transactions",
    "investments/price_cache.csv": "market_prices",
    "investments/crypto_wallets.csv": "crypto_wallets",
    "investments/crypto_balances.csv": "crypto_balances",
    "investments/crypto_transactions.csv": "crypto_transactions",
    "investments/crypto_refresh_status.csv": "crypto_refresh_status",
    "plans/goals.csv": "annual_goals",
    "rates/fx_rates.csv": "fx_rates",
    "staging/transaction_drafts.csv": "transaction_drafts",
}
_EXPENSE_CATEGORIES = {
    "Быт и товары для дома": "expense.home_goods",
    "На себя": "expense.personal",
    "Одежда": "expense.clothing",
    "Пища": "expense.food",
    "Поездки": "expense.travel",
    "Крупные покупки/ Поездки": "expense.travel",
    "Прочее": "expense.other",
    "Связь": "expense.communication",
    "Развлечения": "expense.entertainment_legacy",
    "Соц жизнь": "expense.social",
    "Соц.жизнь": "expense.social",
    "Транспорт": "expense.transport",
}
_INCOME_COLUMNS = {
    "Зарплата": "income.salary",
    "Проценты": "income.interest",
    "Инвест доход": "income.investment",
    "Прочие доходы": "income.other",
    "Сбережения": "income.other",
}
_PASS_THROUGH_TRANSACTION_COLUMNS = {
    "Инвестиции", "Дебиторская задолженность", "Погашение деб. зад.",
    "Кредиторская задолженность", "Погашение кред. зад.", "Долги (у меня)",
}
_DEBT_CASH_ACTIONS = {
    "Дебиторская задолженность": ("issue", "receivable"),
    "Долги (у меня)": ("issue", "receivable"),
    "Погашение деб. зад.": ("repayment", "receivable"),
    "Кредиторская задолженность": ("issue", "liability"),
    "Погашение кред. зад.": ("repayment", "liability"),
}
_CATEGORY_BY_LABEL = {
    **_EXPENSE_CATEGORIES,
    "Зарплата": "income.salary",
    "Проценты": "income.interest",
    "Инвест доход": "income.investment",
    "Прочие доходы": "income.other",
    "Сбережения": "income.other",
}
_ADAPTED_FAMILIES = {
    "cash_transactions", "asset_snapshots", "category_rules", "fx_rates", "annual_goals",
    "transaction_drafts", "debts", "debt_payments", "investment_instruments",
    "investment_transactions", "market_prices",
    "investments_legacy",
    "crypto_wallets", "crypto_balances", "crypto_transactions", "crypto_refresh_status",
}
_FAMILY_ORDER = {
    "cash_transactions": 10, "asset_snapshots": 20, "category_rules": 30,
    "fx_rates": 40, "annual_goals": 50, "transaction_drafts": 60,
    "debts": 70, "debt_payments": 80, "investments_legacy": 85, "investment_instruments": 90,
    "investment_transactions": 100, "crypto_wallets": 105, "market_prices": 110,
    "crypto_balances": 130, "crypto_transactions": 140, "crypto_refresh_status": 150,
}
_DRAFT_DOMAIN_ACTIONS = {
    "Дебиторская задолженность": ("debt", "receivable_opening"),
    "Погашение деб. зад.": ("debt", "receivable_payment"),
    "Кредиторская задолженность": ("debt", "liability_opening"),
    "Погашение кред. зад.": ("debt", "liability_payment"),
    "Долги (у меня)": ("debt", "receivable_opening"),
    "Инвестиции": ("investment", "cash_movement"),
}


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    size_bytes: int
    sha256: str
    headers: tuple[str, ...]
    row_count: int
    family: str
    status: str
    reason: str


@dataclass(frozen=True)
class MigrationSummary:
    manifest_hash: str
    files_total: int
    files_included: int
    cash_imported: int
    snapshots_imported: int
    unresolved_financial_records: int
    domain_cash_events_imported: int
    pending_adapter_files: int
    auxiliary_records_imported: int
    drafts_imported: int
    debts_imported: int
    debt_payments_imported: int
    debt_issues: int
    instruments_imported: int
    trades_imported: int
    market_prices_imported: int
    investment_issues: int
    crypto_wallets_imported: int
    crypto_balances_imported: int
    crypto_transactions_imported: int
    crypto_refresh_results_imported: int
    crypto_issues: int


def build_manifest(source_root: str | Path) -> tuple[list[ManifestEntry], str]:
    """Inventory every CSV as included, archived, shadowed, or unknown."""
    root = Path(source_root).resolve()
    entries = []
    has_new_investments = (root / "investments/transactions.csv").exists()
    for path in sorted(root.rglob("*.csv")):
        relative = path.relative_to(root).as_posix()
        family, status, reason = _classify_path(relative, has_new_investments)
        headers, row_count = _csv_shape(path)
        entries.append(ManifestEntry(
            relative, path.stat().st_size, _file_hash(path), headers, row_count,
            family, status, reason,
        ))
    canonical = json.dumps(
        [entry.__dict__ for entry in entries], ensure_ascii=False,
        sort_keys=True, separators=(",", ":"),
    )
    return entries, hashlib.sha256(canonical.encode()).hexdigest()


def _classify_path(relative: str, has_new_investments: bool) -> tuple[str, str, str]:
    if relative.startswith("backups/"):
        return "archive", "excluded", "backup archive"
    if relative in _ARCHIVE_EXACT:
        return "archive", "excluded", "owner-approved archive exclusion"
    if relative == "investments/investments.csv" and has_new_investments:
        return "investments_legacy", "excluded", "shadowed by investments/transactions.csv"
    if relative.startswith("transactions_info/"):
        return "cash_transactions", "included", ""
    if relative.startswith("assets_info/"):
        return "asset_snapshots", "included", ""
    if relative in _WORKING_EXACT:
        return _WORKING_EXACT[relative], "included", ""
    return "unknown", "unknown", "no schema-registry route"


def _csv_shape(path: Path) -> tuple[tuple[str, ...], int]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, delimiter=";")
        headers = tuple(next(reader, []))
        return headers, sum(1 for _ in reader)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_migration_database(path: str | Path, entries: list[ManifestEntry], manifest_hash: str) -> None:
    """Rebuild the disposable audit DB; raw legacy data never enters runtime tables."""
    database = sqlite3.connect(Path(path))
    try:
        database.executescript("""
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS migration_runs (
          manifest_hash TEXT PRIMARY KEY, parser_version TEXT NOT NULL, created_at TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS manifest_files (
          relative_path TEXT PRIMARY KEY, size_bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
          headers_json TEXT NOT NULL, row_count INTEGER NOT NULL, family TEXT NOT NULL,
          status TEXT NOT NULL, reason TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS raw_records (
          source_record_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL,
          row_number INTEGER NOT NULL, column_name TEXT NOT NULL, part_number INTEGER NOT NULL,
          raw_text TEXT NOT NULL, payload_hash TEXT NOT NULL
        ) STRICT;
        CREATE TABLE IF NOT EXISTS migration_issues (
          id INTEGER PRIMARY KEY AUTOINCREMENT, relative_path TEXT NOT NULL,
          coordinate TEXT NOT NULL, code TEXT NOT NULL, message TEXT NOT NULL,
          blocking INTEGER NOT NULL CHECK (blocking IN (0, 1))
        ) STRICT;
        CREATE TABLE IF NOT EXISTS reconciliation (
          metric TEXT PRIMARY KEY, value INTEGER NOT NULL
        ) STRICT;
        DELETE FROM raw_records;
        DELETE FROM migration_issues;
        DELETE FROM reconciliation;
        DELETE FROM manifest_files;
        DELETE FROM migration_runs;
        """)
        database.execute("INSERT INTO migration_runs VALUES (?, ?, ?)",
                         (manifest_hash, PARSER_VERSION, datetime.utcnow().isoformat() + "Z"))
        database.executemany(
            "INSERT INTO manifest_files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(item.relative_path, item.size_bytes, item.sha256,
              json.dumps(item.headers, ensure_ascii=False), item.row_count,
              item.family, item.status, item.reason) for item in entries],
        )
        for item in entries:
            if item.status == "unknown":
                _issue(database, item.relative_path, "file", "unknown_file", item.reason, True)
            elif item.status == "included" and item.family not in _ADAPTED_FAMILIES:
                _issue(database, item.relative_path, "file", "adapter_pending",
                       f"{item.family} adapter is required before cutover", True)
        database.commit()
    finally:
        database.close()


def migrate_core_csv(source_root: str | Path, target_db: str | Path,
                     migration_db: str | Path) -> MigrationSummary:
    """Import cash and asset facts into a new target DB and expose all unresolved facts."""
    root = Path(source_root).resolve()
    entries, manifest_hash = build_manifest(root)
    prepare_migration_database(migration_db, entries, manifest_hash)
    initialize_database(target_db, data_mode="migration")
    with connect_database(target_db) as target:
        if target.execute("SELECT 1 FROM source_batches LIMIT 1").fetchone():
            raise ValueError("target database must not contain migrated facts")

    audit = sqlite3.connect(Path(migration_db))
    try:
        cash_imported = domain_events = snapshots_imported = auxiliary = drafts = unresolved = source_cash = source_snapshots = 0
        debts = debt_payments = instruments = trades = market_prices = investment_adapter_issues = 0
        crypto_wallets = crypto_balances = crypto_transactions = crypto_refresh = crypto_issues = 0
        for item in sorted(entries, key=lambda entry: (_FAMILY_ORDER.get(entry.family, 999), entry.relative_path)):
            if item.status != "included":
                continue
            path = root / item.relative_path
            if item.family == "cash_transactions":
                imported, domain_imported, candidates, problems = _migrate_transaction_file(
                    path, item, target_db, audit)
                cash_imported += imported
                domain_events += domain_imported
                source_cash += candidates
                unresolved += problems
            elif item.family == "asset_snapshots":
                imported, candidates, problems = _migrate_asset_file(path, item, target_db, audit)
                snapshots_imported += imported
                source_snapshots += candidates
                unresolved += problems
            elif item.family == "category_rules":
                auxiliary += _migrate_category_rules(path, item, target_db, audit)
            elif item.family == "fx_rates":
                auxiliary += _migrate_fx_rates(path, item, target_db, audit)
            elif item.family == "annual_goals":
                auxiliary += _migrate_annual_goals(path, item, target_db, audit)
            elif item.family == "transaction_drafts":
                drafts += _migrate_transaction_drafts(path, item, target_db, audit)
            elif item.family == "debts":
                debts += _migrate_debts(path, item, target_db, audit)
            elif item.family == "debt_payments":
                debt_payments += _migrate_debt_payments(path, item, target_db, audit)
            elif item.family == "investment_instruments":
                instruments += _migrate_instruments(path, item, target_db, audit)
            elif item.family == "investments_legacy":
                added_instruments, added_trades, issues = _migrate_legacy_investments(
                    path, item, target_db, audit)
                instruments += added_instruments
                trades += added_trades
                investment_adapter_issues += issues
            elif item.family == "investment_transactions":
                trades += _migrate_investment_trades(path, item, target_db, audit)
            elif item.family == "market_prices":
                added_prices, added_instruments, issues = _migrate_market_prices(
                    path, item, target_db, audit)
                market_prices += added_prices
                instruments += added_instruments
                investment_adapter_issues += issues
            elif item.family == "crypto_wallets":
                imported, issues = _migrate_crypto_wallets(path, item, target_db, audit)
                crypto_wallets += imported
                crypto_issues += issues
            elif item.family == "crypto_balances":
                imported, issues = _migrate_crypto_balances(path, item, target_db, audit)
                crypto_balances += imported
                crypto_issues += issues
            elif item.family == "crypto_transactions":
                imported, issues = _migrate_crypto_transactions(path, item, target_db, audit)
                crypto_transactions += imported
                crypto_issues += issues
            elif item.family == "crypto_refresh_status":
                imported, issues = _migrate_crypto_refresh_results(path, item, target_db, audit)
                crypto_refresh += imported
                crypto_issues += issues
        debt_issues = _reconcile_debts(target_db, audit)
        investment_issues = investment_adapter_issues + _reconcile_investments(target_db, audit)
        metrics = {
            "manifest_files": len(entries),
            "included_files": sum(item.status == "included" for item in entries),
            "source_cash_candidates": source_cash,
            "imported_cash_transactions": cash_imported,
            "imported_domain_cash_events": domain_events,
            "source_asset_snapshots": source_snapshots,
            "imported_asset_snapshots": snapshots_imported,
            "unresolved_financial_records": unresolved,
            "pending_adapter_files": sum(
                item.status == "included" and item.family not in _ADAPTED_FAMILIES
                for item in entries
            ),
            "auxiliary_records_imported": auxiliary,
            "drafts_imported": drafts,
            "debts_imported": debts,
            "debt_payments_imported": debt_payments,
            "debt_issues": debt_issues,
            "instruments_imported": instruments,
            "trades_imported": trades,
            "market_prices_imported": market_prices,
            "investment_issues": investment_issues,
            "crypto_wallets_imported": crypto_wallets,
            "crypto_balances_imported": crypto_balances,
            "crypto_transactions_imported": crypto_transactions,
            "crypto_refresh_results_imported": crypto_refresh,
            "crypto_issues": crypto_issues,
        }
        audit.executemany("INSERT INTO reconciliation VALUES (?, ?)", metrics.items())
        audit.commit()
    finally:
        audit.close()
    return MigrationSummary(
        manifest_hash, len(entries), sum(item.status == "included" for item in entries),
        cash_imported, snapshots_imported, unresolved, domain_events,
        sum(item.status == "included" and item.family not in _ADAPTED_FAMILIES for item in entries),
        auxiliary, drafts, debts, debt_payments, debt_issues,
        instruments, trades, market_prices, investment_issues,
        crypto_wallets, crypto_balances, crypto_transactions, crypto_refresh, crypto_issues,
    )


def _migrate_transaction_file(path: Path, item: ManifestEntry, target_db: Path,
                              audit: sqlite3.Connection) -> tuple[int, int, int, int]:
    imported = domain_imported = candidates = problems = 0
    save_month(target_db, path.stem.rstrip("_").replace("_", "-"))
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            occurred_on = _legacy_date(row.get("Дата", ""))
            for column, cell in row.items():
                if column == "Дата":
                    continue
                for part_number, part in enumerate(_split_cell(cell), start=1):
                    parsed = _money_cell(part, include_comment=True)
                    if parsed is None or parsed[0] == 0:
                        continue
                    candidates += 1
                    coordinate = f"{row_number}:{column}:{part_number}"
                    record_key = _stable_id("source-coordinate", f"{item.relative_path}\0{coordinate}")
                    payload_hash = hashlib.sha256(part.encode()).hexdigest()
                    _, source_record_id = register_source_record(
                        target_db, source_kind=item.family, document_hash=item.sha256,
                        parser_version=PARSER_VERSION, record_key=record_key,
                        payload_hash=payload_hash,
                    )
                    _raw(audit, source_record_id, item.relative_path, row_number,
                         column, part_number, part, payload_hash)
                    amount, currency, comment = parsed
                    classification = _cash_classification(column, amount, comment)
                    if classification is None and column in _PASS_THROUGH_TRANSACTION_COLUMNS:
                        try:
                            if occurred_on is None or amount == 0:
                                raise ValueError("domain cash event needs a valid date and non-zero amount")
                            _add_domain_cash_event(
                                target_db, source_record_id, occurred_on, column,
                                amount, currency, comment)
                        except (ValueError, sqlite3.IntegrityError) as exc:
                            problems += 1
                            _issue(audit, item.relative_path, coordinate,
                                   "invalid_domain_cash_event", str(exc), True)
                        else:
                            domain_imported += 1
                        continue
                    if occurred_on is None or classification is None:
                        problems += 1
                        code = "invalid_date" if occurred_on is None else "unmapped_category"
                        _issue(audit, item.relative_path, coordinate, code,
                               f"requires manual route for column {column!r}", True)
                        continue
                    direction, category_id, method = classification
                    transaction_id = _stable_id("cash", source_record_id)
                    add_cash_transaction(
                        target_db, transaction_id=transaction_id, occurred_on=occurred_on,
                        flow_direction=direction, category_id=category_id, amount=abs(amount),
                        currency=currency, comment=comment, classification_method=method,
                    )
                    link_transaction_source(target_db, transaction_id, source_record_id)
                    imported += 1
    return imported, domain_imported, candidates, problems


def _add_domain_cash_event(target_db: Path, source_record_id: str, occurred_on: str,
                           column: str, amount: Decimal, currency: str, comment: str) -> None:
    amount_minor = _positive_minor(target_db, currency, abs(amount))
    entity_id = _stable_id("domain-cash-event", source_record_id)
    with connect_database(target_db, writable=True) as target:
        if column == "Инвестиции":
            flow_kind = "contribution" if amount > 0 else "withdrawal"
            target.execute("""INSERT INTO investment_cash_events
                (id, occurred_on, flow_kind, amount_minor, currency_code, comment, created_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                (entity_id, occurred_on, flow_kind, amount_minor, currency, comment))
            entity_type = "investment_cash_event"
        else:
            if amount < 0:
                raise ValueError("negative debt cash event requires manual review")
            event_kind, side = _DEBT_CASH_ACTIONS[column]
            target.execute("""INSERT INTO debt_cash_events
                (id, occurred_on, event_kind, side, amount_minor, currency_code, comment, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (entity_id, occurred_on, event_kind, side, amount_minor, currency, comment))
            entity_type = "debt_cash_event"
    link_entity_source(target_db, entity_type, entity_id, source_record_id)


def _migrate_asset_file(path: Path, item: ManifestEntry, target_db: Path,
                        audit: sqlite3.Connection) -> tuple[int, int, int]:
    imported = candidates = problems = 0
    period = path.stem.replace("_", "-")
    save_asset_month(target_db, period)
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            candidates += 1
            raw = row.get("Сумма", "")
            parsed = _money_cell(raw, include_comment=False)
            coordinate = f"{row_number}:Сумма:1"
            record_key = _stable_id("source-coordinate", f"{item.relative_path}\0{coordinate}")
            payload_hash = hashlib.sha256(raw.encode()).hexdigest()
            _, source_record_id = register_source_record(
                target_db, source_kind=item.family, document_hash=item.sha256,
                parser_version=PARSER_VERSION, record_key=record_key, payload_hash=payload_hash,
            )
            _raw(audit, source_record_id, item.relative_path, row_number,
                 "Сумма", 1, raw, payload_hash)
            account_name = (row.get("Счет") or "").strip()
            if parsed is None or not account_name:
                problems += 1
                _issue(audit, item.relative_path, coordinate, "invalid_asset",
                       "asset requires account and amount|currency", True)
                continue
            amount, currency, _ = parsed
            if amount < 0:
                problems += 1
                _issue(audit, item.relative_path, coordinate, "negative_asset",
                       "negative snapshot requires manual review", True)
                continue
            account_id = _stable_id("account", account_name.casefold())
            with connect_database(target_db) as target:
                exists = target.execute("SELECT 1 FROM asset_accounts WHERE id = ?", (account_id,)).fetchone()
            if not exists:
                add_asset_account(target_db, account_id, account_name)
            add_asset_snapshot(
                target_db, snapshot_id=_stable_id("snapshot", source_record_id),
                account_id=account_id, period=period, amount=amount, currency=currency,
            )
            imported += 1
    return imported, candidates, problems


def _migrate_category_rules(path: Path, item: ManifestEntry, target_db: Path,
                            audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            pattern = (row.get("pattern") or "").strip()
            source_category = (row.get("category") or "").strip()
            category_id = _CATEGORY_BY_LABEL.get(source_category)
            if source_category == "Доход":
                reason = classify_income_comment(pattern)
                category_id = {"salary": "income.salary", "deposit_interest": "income.interest"}.get(
                    reason)
            if not category_id or not pattern:
                _issue(audit, item.relative_path, str(row_number), "invalid_category_rule",
                       "rule needs a known category and non-empty pattern", True)
                continue
            entity_id = _stable_id("category-rule", source_record_id)
            direction = category_id.split(".", 1)[0]
            with connect_database(target_db, writable=True) as target:
                target.execute("""INSERT INTO categorization_rules
                    (id, priority, direction_scope, matcher_type, pattern, category_id,
                     active, created_at, updated_at)
                    VALUES (?, ?, ?, 'contains', ?, ?, 1, datetime('now'), datetime('now'))""",
                    (entity_id, row_number - 2, direction, pattern, category_id))
            link_entity_source(target_db, "categorization_rule", entity_id, source_record_id)
            imported += 1
    return imported


def _migrate_legacy_investments(path: Path, item: ManifestEntry, target_db: Path,
                                audit: sqlite3.Connection) -> tuple[int, int, int]:
    instruments_added = trades_added = issues = 0
    operation_map = {"Покупка": "buy", "Продажа": "sell"}
    asset_map = {"Акции": "stocks", "Фонды": "funds", "Крипто": "crypto"}
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                ticker = (row.get("Тикер") or "").strip().upper()
                operation = operation_map.get((row.get("Тип_транзакции") or "").strip())
                asset_type = asset_map.get((row.get("Актив") or "").strip())
                occurred_on = _legacy_date(row.get("Дата") or "")
                price_parts = (row.get("Цена") or "").split("|", 1)
                price = _decimal_text(price_parts[0], allow_zero=True)
                currency = (price_parts[1] if len(price_parts) == 2 else "RUB").strip().upper()
                quantity = _decimal_text(row.get("Количество"), allow_zero=False)
                if not ticker or operation is None or asset_type is None or occurred_on is None:
                    raise ValueError("legacy trade has unsupported identity, operation, type or date")
                instrument_id = _stable_id("instrument", ticker)
                with connect_database(target_db) as target:
                    instrument = target.execute(
                        "SELECT id, asset_type FROM instruments WHERE ticker = ?", (ticker,)
                    ).fetchone()
                if instrument is None:
                    with connect_database(target_db, writable=True) as target:
                        target.execute("""INSERT INTO instruments
                            (id, ticker, name, asset_type, quote_currency_code,
                             created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                            (instrument_id, ticker, ticker, asset_type, currency))
                    link_entity_source(target_db, "instrument", instrument_id, source_record_id)
                    instruments_added += 1
                elif instrument["asset_type"] != asset_type:
                    raise ValueError("legacy ticker maps to conflicting asset types")
                trade_id = _stable_id("investment-trade", source_record_id)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO investment_trades
                        (id, occurred_on, operation, instrument_id, quantity_text,
                         unit_price_text, price_currency_code, fee_minor, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'))""",
                        (trade_id, occurred_on, operation, instrument_id, quantity, price, currency))
            except (ValueError, sqlite3.IntegrityError) as exc:
                issues += 1
                _issue(audit, item.relative_path, str(row_number), "invalid_legacy_investment", str(exc), True)
                continue
            link_entity_source(target_db, "investment_trade", trade_id, source_record_id)
            trades_added += 1
    return instruments_added, trades_added, issues


def _migrate_fx_rates(path: Path, item: ManifestEntry, target_db: Path,
                      audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            observation_id = _stable_id("fx-observation", source_record_id)
            try:
                save_fx_rate(
                    target_db, observation_id=observation_id,
                    rate_date=(row.get("date") or "").strip(),
                    currency=(row.get("currency") or "").strip(),
                    usd_rate=row.get("usd_rate"), source=(row.get("source") or "migration").strip(),
                    fetched_at=(row.get("fetched_at") or "").strip(),
                    sequence=row_number - 2,
                )
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_fx_rate", str(exc), True)
                continue
            link_entity_source(target_db, "fx_rate_observation", observation_id, source_record_id)
            imported += 1
    return imported


def _migrate_annual_goals(path: Path, item: ManifestEntry, target_db: Path,
                          audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            currency = (row.get("currency") or "").strip().upper()
            try:
                year = int(row.get("year") or "")
                values = [
                    _optional_minor(target_db, currency, row.get(column, ""))
                    for column in ("target_capital", "target_monthly_income", "target_monthly_expense")
                ]
                raw_months = (row.get("target_expense_months") or "").strip()
                target_expense_months = None
                if raw_months:
                    parsed_months = parse_money_amount(
                        raw_months, field_name="target expense months")
                    if (parsed_months <= 0
                            or parsed_months != parsed_months.to_integral_value()):
                        raise ValueError(
                            "target expense months must be a positive integer")
                    target_expense_months = int(parsed_months)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO annual_goals
                        (year, currency_code, target_capital_minor, target_monthly_income_minor,
                         target_monthly_expense_minor, target_expense_months, notes, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                        (year, currency, *values, target_expense_months,
                         row.get("notes") or ""))
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_annual_goal", str(exc), True)
                continue
            entity_id = f"{year}:{currency}"
            link_entity_source(target_db, "annual_goal", entity_id, source_record_id)
            imported += 1
    return imported


def _migrate_transaction_drafts(path: Path, item: ManifestEntry, target_db: Path,
                                audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            category_label = (row.get("category") or "").strip()
            domain = _DRAFT_DOMAIN_ACTIONS.get(category_label)
            direction = category_id = domain_action = None
            draft_kind = "cash"
            if domain:
                draft_kind, domain_action = domain
            else:
                category_id, direction = _draft_cash_category(category_label, row.get("comment") or "")
                explicit = {"credit": "income", "debit": "expense", "": direction}.get(
                    (row.get("direction") or "").strip().lower()
                )
                if category_id is None or explicit is None or explicit != direction:
                    _issue(audit, item.relative_path, str(row_number), "unresolved_draft_category",
                           f"draft category/direction requires review: {category_label!r}", True)
                    continue
            try:
                occurred_on = _legacy_date(row.get("date") or "")
                if occurred_on is None:
                    raise ValueError("draft date must be ISO or DD.MM.YYYY")
                amount_minor = _positive_minor(
                    target_db, (row.get("currency") or "").strip().upper(), row.get("amount")
                )
                status = (row.get("status") or "draft").strip()
                if status not in {"draft", "ready", "exported", "archived", "ignored"}:
                    raise ValueError("unsupported draft status")
                bank_status = (row.get("bank_status") or "").strip().lower() or None
                if bank_status not in {None, "pending", "posted"}:
                    raise ValueError("unsupported bank status")
                origin_kind = (row.get("source") or "manual").strip() or "manual"
                origin_key = (row.get("source_id") or "").strip() or source_record_id
                draft_id = _stable_id("transaction-draft", source_record_id)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO transaction_drafts
                        (id, occurred_on, draft_kind, domain_action, flow_direction,
                         amount_minor, currency_code, category_id, comment, source_record_id,
                         origin_kind, origin_key, bank_status, bank_reference, bank_account_id,
                         status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                        (draft_id, occurred_on, draft_kind, domain_action, direction,
                         amount_minor, (row.get("currency") or "").strip().upper(), category_id,
                         sanitize_transaction_comment(row.get("comment") or ""), source_record_id,
                         origin_kind, origin_key, bank_status,
                         (row.get("bank_reference") or "").strip(),
                         (row.get("bank_account_id") or "").strip(), status))
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_transaction_draft", str(exc), True)
                continue
            imported += 1
    return imported


def _migrate_debts(path: Path, item: ManifestEntry, target_db: Path,
                   audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                debt_id = (row.get("debt_id") or "").strip()
                kind = (row.get("type") or "").strip().lower()
                counterparty = (row.get("counterparty") or "").strip()
                opened_on = _legacy_date(row.get("opened_date") or "")
                principal_currency = (row.get("principal_currency") or "").strip().upper()
                cash_currency = (row.get("cash_currency") or "").strip().upper()
                status = (row.get("status") or "active").strip().lower()
                if not debt_id or not counterparty or opened_on is None:
                    raise ValueError("debt needs ID, counterparty and valid opened date")
                if kind not in {"receivable", "liability"} or status not in {"active", "closed"}:
                    raise ValueError("unsupported debt type or status")
                principal_minor = _positive_minor(target_db, principal_currency, row.get("principal_amount"))
                cash_minor = _positive_minor(target_db, cash_currency, row.get("cash_amount"))
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO debts
                        (id, kind, counterparty, opened_on, principal_amount_minor,
                         principal_currency_code, cash_amount_minor, cash_currency_code,
                         comment, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                        (debt_id, kind, counterparty, opened_on, principal_minor,
                         principal_currency, cash_minor, cash_currency,
                         row.get("comment") or "", status))
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_debt", str(exc), True)
                continue
            link_entity_source(target_db, "debt", debt_id, source_record_id)
            imported += 1
    return imported


def _migrate_debt_payments(path: Path, item: ManifestEntry, target_db: Path,
                           audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                payment_id = (row.get("payment_id") or "").strip()
                debt_id = (row.get("debt_id") or "").strip()
                occurred_on = _legacy_date(row.get("date") or "")
                cash_currency = (row.get("cash_currency") or "").strip().upper()
                status = (row.get("status") or "posted").strip().lower()
                with connect_database(target_db) as target:
                    debt = target.execute(
                        "SELECT principal_currency_code FROM debts WHERE id = ?", (debt_id,)
                    ).fetchone()
                if not payment_id or occurred_on is None or debt is None or status != "posted":
                    raise ValueError("payment needs ID, known debt, valid date and posted status")
                principal_minor = _positive_minor(target_db, debt[0], row.get("amount"))
                cash_minor = _positive_minor(target_db, cash_currency, row.get("cash_amount"))
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO debt_payments
                        (id, debt_id, occurred_on, principal_amount_minor,
                         cash_amount_minor, cash_currency_code, comment, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                        (payment_id, debt_id, occurred_on, principal_minor, cash_minor,
                         cash_currency, row.get("comment") or "", status))
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_debt_payment", str(exc), True)
                continue
            link_entity_source(target_db, "debt_payment", payment_id, source_record_id)
            imported += 1
    return imported


def _reconcile_debts(target_db: Path, audit: sqlite3.Connection) -> int:
    issues = 0
    with connect_database(target_db) as target:
        rows = target.execute("""SELECT d.id, d.opened_on, d.principal_amount_minor, d.status,
            COALESCE(SUM(p.principal_amount_minor), 0) AS paid_minor,
            MIN(p.occurred_on) AS first_payment
            FROM debts d LEFT JOIN debt_payments p ON p.debt_id = d.id GROUP BY d.id""").fetchall()
    for row in rows:
        if row["paid_minor"] > row["principal_amount_minor"]:
            issues += 1
            _issue(audit, "debts/debt_payments.csv", row["id"], "debt_overpayment",
                   "payments exceed principal", True)
        if row["first_payment"] and row["first_payment"] < row["opened_on"]:
            issues += 1
            _issue(audit, "debts/debt_payments.csv", row["id"], "payment_before_opening",
                   "payment date precedes debt opening", True)
        expected_closed = row["paid_minor"] == row["principal_amount_minor"]
        if expected_closed != (row["status"] == "closed"):
            issues += 1
            _issue(audit, "debts/debts.csv", row["id"], "debt_status_mismatch",
                   "closed status does not match outstanding principal", True)
    return issues


def _migrate_instruments(path: Path, item: ManifestEntry, target_db: Path,
                         audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                ticker = (row.get("ticker") or "").strip().upper()
                name = (row.get("name") or "").strip() or ticker
                asset_type = (row.get("asset_type") or "").strip().lower()
                currency = (row.get("currency") or "").strip().upper()
                if not ticker or asset_type not in {"stocks", "funds", "crypto"}:
                    raise ValueError("instrument needs ticker and supported asset type")
                with connect_database(target_db) as target:
                    if target.execute("SELECT 1 FROM currencies WHERE code = ?", (currency,)).fetchone() is None:
                        raise ValueError("unsupported instrument currency")
                instrument_id = _stable_id("instrument", ticker)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO instruments
                        (id, ticker, name, asset_type, quote_currency_code, provider, exchange,
                         created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                        (instrument_id, ticker, name, asset_type, currency,
                         (row.get("provider") or "").strip(), (row.get("exchange") or "").strip()))
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_instrument", str(exc), True)
                continue
            link_entity_source(target_db, "instrument", instrument_id, source_record_id)
            imported += 1
    return imported


def _migrate_investment_trades(path: Path, item: ManifestEntry, target_db: Path,
                               audit: sqlite3.Connection) -> int:
    imported = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                ticker = (row.get("ticker") or "").strip().upper()
                operation = (row.get("operation") or "").strip().lower()
                asset_type = (row.get("asset_type") or "").strip().lower()
                occurred_on = _legacy_date(row.get("date") or "")
                currency = (row.get("currency") or "").strip().upper()
                with connect_database(target_db) as target:
                    instrument = target.execute(
                        "SELECT id, asset_type FROM instruments WHERE ticker = ?", (ticker,)
                    ).fetchone()
                if instrument is None or instrument["asset_type"] != asset_type:
                    raise ValueError("trade needs a known instrument with matching asset type")
                if operation not in {"buy", "sell"} or occurred_on is None:
                    raise ValueError("trade needs supported operation and valid date")
                quantity = _decimal_text(row.get("quantity"), allow_zero=False)
                price = _decimal_text(row.get("price"), allow_zero=True)
                fee_minor = _nonnegative_minor(target_db, currency, row.get("fee") or "0")
                trade_id = _stable_id("investment-trade", source_record_id)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO investment_trades
                        (id, occurred_on, operation, instrument_id, quantity_text,
                         unit_price_text, price_currency_code, fee_minor, account_label,
                         comment, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                        (trade_id, occurred_on, operation, instrument["id"], quantity,
                         price, currency, fee_minor, row.get("account") or "",
                         row.get("comment") or ""))
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_investment_trade", str(exc), True)
                continue
            link_entity_source(target_db, "investment_trade", trade_id, source_record_id)
            imported += 1
    return imported


def _migrate_market_prices(path: Path, item: ManifestEntry, target_db: Path,
                           audit: sqlite3.Connection) -> tuple[int, int, int]:
    imported = instruments_added = issues = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                ticker = (row.get("ticker") or "").strip().upper()
                price_date = _legacy_date(row.get("date") or "")
                currency = (row.get("currency") or "").strip().upper()
                source = (row.get("source") or "migration").strip()
                with connect_database(target_db) as target:
                    instrument = target.execute("SELECT id FROM instruments WHERE ticker = ?", (ticker,)).fetchone()
                    crypto_wallet = target.execute(
                        "SELECT 1 FROM crypto_wallets WHERE asset_code = ? LIMIT 1", (ticker,)
                    ).fetchone()
                if instrument is None and crypto_wallet is not None:
                    instrument_id = _stable_id("instrument", ticker)
                    with connect_database(target_db, writable=True) as target:
                        target.execute("""INSERT INTO instruments
                            (id, ticker, name, asset_type, quote_currency_code,
                             created_at, updated_at)
                            VALUES (?, ?, ?, 'crypto', ?, datetime('now'), datetime('now'))""",
                            (instrument_id, ticker, ticker, currency))
                    link_entity_source(target_db, "instrument", instrument_id, source_record_id)
                    instrument = {"id": instrument_id}
                    instruments_added += 1
                if instrument is None or price_date is None or not source:
                    raise ValueError("price needs known trade/wallet instrument, date and source")
                price = _decimal_text(row.get("price"), allow_zero=False)
                observation_id = _stable_id("market-price", source_record_id)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO market_price_observations
                        (id, instrument_id, price_date, price_text, currency_code,
                         source, fetched_at, sequence)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (observation_id, instrument["id"], price_date, price, currency,
                         source, (row.get("fetched_at") or "").strip(), row_number - 2))
            except (ValueError, sqlite3.IntegrityError) as exc:
                issues += 1
                _issue(audit, item.relative_path, str(row_number), "invalid_market_price", str(exc), True)
                continue
            link_entity_source(target_db, "market_price_observation", observation_id, source_record_id)
            imported += 1
    return imported, instruments_added, issues


def _reconcile_investments(target_db: Path, audit: sqlite3.Connection) -> int:
    issues = 0
    positions: dict[str, Decimal] = {}
    with connect_database(target_db) as target:
        rows = target.execute("""SELECT t.id, t.operation, t.quantity_text, i.ticker
            FROM investment_trades t JOIN instruments i ON i.id = t.instrument_id
            ORDER BY t.occurred_on, t.id""").fetchall()
    for row in rows:
        quantity = Decimal(row["quantity_text"])
        balance = positions.get(row["ticker"], Decimal("0"))
        balance += quantity if row["operation"] == "buy" else -quantity
        positions[row["ticker"]] = balance
        if balance < 0:
            issues += 1
            _issue(audit, "investments/transactions.csv", row["id"], "investment_oversell",
                   f"sell exceeds known quantity for {row['ticker']}", True)
    return issues


def _migrate_crypto_wallets(path: Path, item: ManifestEntry, target_db: Path,
                            audit: sqlite3.Connection) -> tuple[int, int]:
    imported = issues = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                account = (row.get("account") or "").strip()
                chain = (row.get("chain") or "").strip().lower()
                asset = (row.get("asset") or "").strip().upper()
                address = (row.get("address") or "").strip()
                token = (row.get("token_contract") or "").strip().lower()
                if not account or not chain or not asset or not address:
                    raise ValueError("wallet needs account, chain, asset and public address")
                enabled_text = (row.get("enabled") or "1").strip().lower()
                if enabled_text not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
                    raise ValueError("unsupported wallet enabled value")
                enabled = int(enabled_text not in {"0", "false", "no", "off"})
                wallet_id = _stable_id("crypto-wallet", f"{chain}\0{asset}\0{address}\0{token}")
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO crypto_wallets
                        (id, account_label, chain, asset_code, address, token_contract,
                         label, enabled, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                        (wallet_id, account, chain, asset, address, token,
                         row.get("label") or "", enabled))
            except (ValueError, sqlite3.IntegrityError) as exc:
                issues += 1
                _issue(audit, item.relative_path, str(row_number), "invalid_crypto_wallet", str(exc), True)
                continue
            link_entity_source(target_db, "crypto_wallet", wallet_id, source_record_id)
            imported += 1
    return imported, issues


def _migrate_crypto_balances(path: Path, item: ManifestEntry, target_db: Path,
                             audit: sqlite3.Connection) -> tuple[int, int]:
    imported = issues = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                wallet_id = _crypto_wallet_match(target_db, row)
                if wallet_id is None:
                    raise ValueError("balance does not match exactly one configured wallet")
                quantity = _decimal_text(row.get("balance"), allow_zero=True)
                fetched_at = (row.get("fetched_at") or "").strip()
                source = (row.get("source") or "").strip()
                if not fetched_at or not source:
                    raise ValueError("balance needs fetched_at and source")
                observation_id = _stable_id("crypto-balance", source_record_id)
                with connect_database(target_db, writable=True) as target:
                    target.execute("INSERT INTO crypto_balance_observations VALUES (?, ?, ?, ?, ?)",
                                   (observation_id, wallet_id, fetched_at, quantity, source))
            except (ValueError, sqlite3.IntegrityError) as exc:
                issues += 1
                _issue(audit, item.relative_path, str(row_number), "invalid_crypto_balance", str(exc), True)
                continue
            link_entity_source(target_db, "crypto_balance_observation", observation_id, source_record_id)
            imported += 1
    return imported, issues


def _migrate_crypto_transactions(path: Path, item: ManifestEntry, target_db: Path,
                                 audit: sqlite3.Connection) -> tuple[int, int]:
    imported = issues = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                wallet_id = _crypto_wallet_match(target_db, row)
                tx_id = (row.get("tx_id") or "").strip()
                occurred_on = _legacy_date(row.get("date") or "")
                operation = (row.get("operation") or "").strip()
                source = (row.get("source") or "").strip()
                if wallet_id is None or not tx_id or occurred_on is None or not operation or not source:
                    raise ValueError("crypto transaction needs wallet, tx ID, date, operation and source")
                quantity = _optional_decimal_text(row.get("quantity"))
                fee = _optional_decimal_text(row.get("fee"))
                entity_id = _stable_id("crypto-transaction", source_record_id)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO crypto_transactions
                        (id, wallet_id, chain_tx_id, occurred_on, operation, quantity_text,
                         fee_text, counterparty, source, comment)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (entity_id, wallet_id, tx_id, occurred_on, operation, quantity, fee,
                         row.get("counterparty") or "", source, row.get("comment") or ""))
            except (ValueError, sqlite3.IntegrityError) as exc:
                issues += 1
                _issue(audit, item.relative_path, str(row_number), "invalid_crypto_transaction", str(exc), True)
                continue
            link_entity_source(target_db, "crypto_transaction", entity_id, source_record_id)
            imported += 1
    return imported, issues


def _migrate_crypto_refresh_results(path: Path, item: ManifestEntry, target_db: Path,
                                    audit: sqlite3.Connection) -> tuple[int, int]:
    imported = issues = 0
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        for row_number, row in enumerate(csv.DictReader(stream, delimiter=";"), start=2):
            source_record_id = _source_row(item, row_number, row, target_db, audit)
            try:
                fetched_at = (row.get("fetched_at") or "").strip()
                status = (row.get("status") or "").strip()
                if not fetched_at or not status:
                    raise ValueError("refresh result needs fetched_at and status")
                source_row = int(row["row_number"]) if (row.get("row_number") or "").strip() else None
                wallet_id = _crypto_wallet_match(target_db, row)
                entity_id = _stable_id("crypto-refresh", source_record_id)
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO crypto_refresh_results
                        (id, fetched_at, wallet_id, source_row_number, observed_account,
                         observed_chain, observed_asset, observed_address, status, message)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (entity_id, fetched_at, wallet_id, source_row,
                         row.get("account") or "", (row.get("chain") or "").lower(),
                         (row.get("asset") or "").upper(), row.get("address") or "",
                         status, row.get("message") or ""))
            except (ValueError, sqlite3.IntegrityError) as exc:
                issues += 1
                _issue(audit, item.relative_path, str(row_number), "invalid_crypto_refresh", str(exc), True)
                continue
            link_entity_source(target_db, "crypto_refresh_result", entity_id, source_record_id)
            imported += 1
    return imported, issues


def _crypto_wallet_match(target_db: Path, row: dict) -> str | None:
    chain = (row.get("chain") or "").strip().lower()
    asset = (row.get("asset") or "").strip().upper()
    address = (row.get("address") or "").strip()
    with connect_database(target_db) as target:
        matches = target.execute("""SELECT id FROM crypto_wallets
            WHERE chain = ? AND asset_code = ? AND address = ?""", (chain, asset, address)).fetchall()
    return matches[0][0] if len(matches) == 1 else None


def _draft_cash_category(label: str, comment: str) -> tuple[str | None, str | None]:
    if label == "Доход":
        source = classify_income_comment(comment)
        category_id = {"salary": "income.salary", "deposit_interest": "income.interest"}.get(
            source, "income.unknown")
        return category_id, "income"
    category_id = _CATEGORY_BY_LABEL.get(label)
    return (category_id, category_id.split(".", 1)[0]) if category_id else (None, None)


def _positive_minor(target_db: Path, currency: str, value) -> int:
    result = _optional_minor(target_db, currency, value)
    if result is None or result <= 0:
        raise ValueError("draft amount must be positive")
    return result


def _source_row(item: ManifestEntry, row_number: int, row: dict, target_db: Path,
                audit: sqlite3.Connection) -> str:
    raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload_hash = hashlib.sha256(raw.encode()).hexdigest()
    coordinate = str(row_number)
    record_key = _stable_id("source-coordinate", f"{item.relative_path}\0{coordinate}")
    _, source_record_id = register_source_record(
        target_db, source_kind=item.family, document_hash=item.sha256,
        parser_version=PARSER_VERSION, record_key=record_key, payload_hash=payload_hash,
    )
    _raw(audit, source_record_id, item.relative_path, row_number, "*", 1, raw, payload_hash)
    return source_record_id


def _optional_minor(target_db: Path, currency: str, value: str) -> int | None:
    if value is None or not str(value).strip():
        return None
    amount = parse_money_amount(value)
    if amount < 0:
        raise ValueError("goal amount must be non-negative")
    with connect_database(target_db) as target:
        row = target.execute("SELECT minor_unit FROM currencies WHERE code = ?", (currency,)).fetchone()
    if row is None:
        raise ValueError("unsupported goal currency")
    scaled = amount * (10 ** row[0])
    if scaled != scaled.to_integral_value():
        raise ValueError("goal exceeds currency minor-unit precision")
    return int(scaled)


def _nonnegative_minor(target_db: Path, currency: str, value) -> int:
    result = _optional_minor(target_db, currency, value)
    if result is None:
        raise ValueError("money amount is required")
    return result


def _decimal_text(value, *, allow_zero: bool) -> str:
    parsed = parse_money_amount(value, field_name="decimal value")
    if parsed < 0 or (parsed == 0 and not allow_zero):
        raise ValueError("decimal value must be positive" if not allow_zero else "decimal value must be non-negative")
    return format(parsed, "f")


def _optional_decimal_text(value) -> str | None:
    if value is None or not str(value).strip():
        return None
    parsed = parse_money_amount(value, field_name="decimal value")
    return format(parsed, "f")


def _cash_classification(column: str, amount: Decimal, comment: str):
    if column == "Доход":
        source = classify_income_comment(comment)
        category = {"salary": "income.salary", "deposit_interest": "income.interest"}.get(
            source, "income.unknown")
        method = "migration_comment" if source in {"salary", "deposit_interest"} else "migration_unresolved"
        return ("income", category, method) if amount > 0 else ("expense", "expense.other", "migration_sign")
    if column in _INCOME_COLUMNS:
        return ("income", _INCOME_COLUMNS[column], "migration_column") if amount > 0 else (
            "expense", "expense.other", "migration_sign")
    if column in _EXPENSE_CATEGORIES:
        return ("expense", _EXPENSE_CATEGORIES[column], "migration_column") if amount > 0 else (
            "income", "income.other", "migration_sign")
    return None


def _split_cell(value: str | None) -> list[str]:
    text = (value or "").strip()
    return TRANSACTION_BOUNDARY_RE.split(text) if text else []


def _money_cell(value: str, *, include_comment: bool):
    normalized = value.strip().replace("\u00a0", "").replace("\\xa0", "").replace(" ₽", "")
    parts = normalized.split("|", 2 if include_comment else 1)
    try:
        amount = parse_money_amount(parts[0])
    except (ValueError, IndexError):
        return None
    currency = parts[1].strip().upper() if len(parts) >= 2 else "RUB"
    comment = sanitize_transaction_comment(parts[2]) if include_comment and len(parts) == 3 else ""
    return amount, currency, comment


def _legacy_date(value: str) -> str | None:
    for pattern in ("%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), pattern).date().isoformat()
        except ValueError:
            pass
    return None


def _stable_id(kind: str, value: str) -> str:
    return hashlib.sha256(f"{kind}\0{value}".encode()).hexdigest()[:32]


def _raw(database, record_id, path, row, column, part, raw_text, payload_hash):
    database.execute("INSERT INTO raw_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                     (record_id, path, row, column, part, raw_text, payload_hash))


def _issue(database, path, coordinate, code, message, blocking):
    database.execute("""INSERT INTO migration_issues
        (relative_path, coordinate, code, message, blocking) VALUES (?, ?, ?, ?, ?)""",
        (path, coordinate, code, message, int(blocking)))
