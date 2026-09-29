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
    "transaction_drafts",
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
    pending_adapter_files: int
    auxiliary_records_imported: int
    drafts_imported: int


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
        if target.execute("SELECT 1 FROM cash_transactions UNION ALL SELECT 1 FROM asset_snapshots LIMIT 1").fetchone():
            raise ValueError("target database must not contain migrated facts")

    audit = sqlite3.connect(Path(migration_db))
    try:
        cash_imported = snapshots_imported = auxiliary = drafts = unresolved = source_cash = source_snapshots = 0
        for item in entries:
            if item.status != "included":
                continue
            path = root / item.relative_path
            if item.family == "cash_transactions":
                imported, candidates, problems = _migrate_transaction_file(path, item, target_db, audit)
                cash_imported += imported
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
        metrics = {
            "manifest_files": len(entries),
            "included_files": sum(item.status == "included" for item in entries),
            "source_cash_candidates": source_cash,
            "imported_cash_transactions": cash_imported,
            "source_asset_snapshots": source_snapshots,
            "imported_asset_snapshots": snapshots_imported,
            "unresolved_financial_records": unresolved,
            "pending_adapter_files": sum(
                item.status == "included" and item.family not in _ADAPTED_FAMILIES
                for item in entries
            ),
            "auxiliary_records_imported": auxiliary,
            "drafts_imported": drafts,
        }
        audit.executemany("INSERT INTO reconciliation VALUES (?, ?)", metrics.items())
        audit.commit()
    finally:
        audit.close()
    return MigrationSummary(
        manifest_hash, len(entries), sum(item.status == "included" for item in entries),
        cash_imported, snapshots_imported, unresolved,
        sum(item.status == "included" and item.family not in _ADAPTED_FAMILIES for item in entries),
        auxiliary, drafts,
    )


def _migrate_transaction_file(path: Path, item: ManifestEntry, target_db: Path,
                              audit: sqlite3.Connection) -> tuple[int, int, int]:
    imported = candidates = problems = 0
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
                    if occurred_on is None or classification is None:
                        problems += 1
                        code = "invalid_date" if occurred_on is None else (
                            "pass_through_cash_event" if column in _PASS_THROUGH_TRANSACTION_COLUMNS
                            else "unmapped_category")
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
    return imported, candidates, problems


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
            category_id = _CATEGORY_BY_LABEL.get((row.get("category") or "").strip())
            pattern = (row.get("pattern") or "").strip()
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
                with connect_database(target_db, writable=True) as target:
                    target.execute("""INSERT INTO annual_goals
                        (year, currency_code, target_capital_minor, target_monthly_income_minor,
                         target_monthly_expense_minor, notes, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now'))""",
                        (year, currency, *values, row.get("notes") or ""))
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
                         origin_kind, origin_key, bank_status, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'))""",
                        (draft_id, occurred_on, draft_kind, domain_action, direction,
                         amount_minor, (row.get("currency") or "").strip().upper(), category_id,
                         sanitize_transaction_comment(row.get("comment") or ""), source_record_id,
                         origin_kind, origin_key, bank_status, status))
            except (ValueError, sqlite3.IntegrityError) as exc:
                _issue(audit, item.relative_path, str(row_number), "invalid_transaction_draft", str(exc), True)
                continue
            imported += 1
    return imported


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
