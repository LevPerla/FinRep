"""Isolated target SQLite store; the live application still uses CSV."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from src import config
from src.data.money import parse_money_amount


SCHEMA_VERSION = 18
_DIRECTIONS = {"income", "expense"}
_DATASETS = {"cash_transactions", "asset_snapshots"}
_ASSET_TYPES = (
    ("cash", "Наличные", "Cash", 5),
    ("cash_account", "Расчётный счёт", "Cash account", 10),
    ("deposit", "Депозит", "Deposit", 20),
    ("bond", "Облигации", "Bonds", 30),
    ("equity", "Акции", "Equities", 40),
    ("fund", "Фонд", "Fund", 50),
    ("crypto", "Крипто", "Crypto", 60),
    ("real_estate", "Недвижимость", "Real estate", 70),
    ("other", "Другое", "Other", 80),
)
_LIQUIDITY_CLASSES = (
    ("A1", "Наиболее ликвидные активы", "Most liquid assets", "До 1–3 дней", "Up to 1–3 days", 10),
    ("A2", "Быстрореализуемые активы", "Quickly realizable assets", "До 12 месяцев", "Up to 12 months", 20),
    ("A3", "Медленно реализуемые активы", "Slowly realizable assets", "Свыше 12 месяцев", "Over 12 months", 30),
    ("A4", "Труднореализуемые активы", "Hard-to-realize assets", "Обычно не менее 12 месяцев", "Usually at least 12 months", 40),
)
_ASSET_TYPE_LIQUIDITY_DEFAULTS = (
    ("cash", "A1"),
    ("cash_account", "A1"),
    ("deposit", "A1"),
    ("bond", "A2"),
    ("equity", "A2"),
    ("fund", "A2"),
    ("crypto", "A2"),
    ("real_estate", "A4"),
    ("other", "A3"),
)
_CPI_SERIES = (
    ("RUB", "RU", "world_bank_gem", "CPTOTNSXN", "World Bank Global Economic Monitor",
     "https://datacatalog.worldbank.org/search/dataset/0037798/global-economic-monitor",
     "published_index"),
    ("KZT", "KZ", "world_bank_gem+stat_kz", "CPTOTNSXN+cpi_all_items_monthly",
     "World Bank GEM + Бюро национальной статистики Казахстана",
     "https://datacatalog.worldbank.org/search/dataset/0037798/global-economic-monitor",
     "chained_monthly_rate"),
    ("USD", "US", "bls", "CUUR0000SA0", "U.S. Bureau of Labor Statistics",
     "https://www.bls.gov/cpi/data.htm", "published_index"),
    ("GBP", "GB", "ons", "D7BT", "Office for National Statistics",
     "https://www.ons.gov.uk/economy/inflationandpriceindices/timeseries/d7bt/mm23",
     "published_index"),
    ("EUR", "EA", "eurostat", "prc_hicp_minr.I25.TOTAL.EA", "Eurostat",
     "https://ec.europa.eu/eurostat/databrowser/view/prc_hicp_minr/default/table",
     "published_index"),
)
_SYSTEM_CATEGORIES = (
    ("income.salary", None, "income", "Зарплата", 1, "active", 10),
    ("income.interest", None, "income", "Проценты", 1, "passive", 20),
    ("income.investment", None, "income", "Инвест доход", 1, "passive", 30),
    ("income.other", None, "income", "Прочие доходы", 1, "active", 40),
    ("income.unknown", None, "income", "Доход без категории", 0, "unclassified", 90),
    ("expense.home_goods", None, "expense", "Быт и товары для дома", 1, None, 10),
    ("expense.personal", None, "expense", "На себя", 1, None, 20),
    ("expense.clothing", None, "expense", "Одежда", 1, None, 30),
    ("expense.food", None, "expense", "Пища", 1, None, 40),
    ("expense.travel", None, "expense", "Поездки", 1, None, 50),
    ("expense.other", None, "expense", "Прочее", 1, None, 60),
    ("expense.communication", None, "expense", "Связь", 1, None, 70),
    ("expense.entertainment_legacy", None, "expense", "Развлечения", 1, None, 80),
    ("expense.social", None, "expense", "Соц жизнь", 1, None, 90),
    ("expense.transport", None, "expense", "Транспорт", 1, None, 100),
)


class StorageRevisionConflict(ValueError):
    pass

_TABLES = (
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        checksum TEXT NOT NULL CHECK (length(checksum) = 64), applied_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE app_metadata (
        id INTEGER PRIMARY KEY CHECK (id = 1), storage_epoch TEXT NOT NULL UNIQUE,
        data_mode TEXT NOT NULL CHECK (data_mode IN ('live', 'migration', 'synthetic')),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE currencies (
        code TEXT PRIMARY KEY CHECK (length(code) BETWEEN 3 AND 8),
        minor_unit INTEGER NOT NULL CHECK (minor_unit BETWEEN 0 AND 6)
    ) STRICT""",
    """CREATE TABLE asset_types (
        id TEXT PRIMARY KEY CHECK (trim(id) <> ''),
        name_ru TEXT NOT NULL CHECK (trim(name_ru) <> ''),
        name_en TEXT NOT NULL CHECK (trim(name_en) <> ''),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        sort_order INTEGER NOT NULL DEFAULT 0
    ) STRICT""",
    """CREATE TABLE liquidity_classes (
        id TEXT PRIMARY KEY CHECK (id IN ('A1', 'A2', 'A3', 'A4')),
        name_ru TEXT NOT NULL CHECK (trim(name_ru) <> ''),
        name_en TEXT NOT NULL CHECK (trim(name_en) <> ''),
        horizon_ru TEXT NOT NULL CHECK (trim(horizon_ru) <> ''),
        horizon_en TEXT NOT NULL CHECK (trim(horizon_en) <> ''),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        sort_order INTEGER NOT NULL DEFAULT 0
    ) STRICT""",
    """CREATE TABLE asset_type_liquidity_defaults (
        asset_type_id TEXT PRIMARY KEY REFERENCES asset_types(id) ON DELETE CASCADE,
        liquidity_class_id TEXT NOT NULL REFERENCES liquidity_classes(id) ON DELETE RESTRICT
    ) STRICT""",
    """CREATE TABLE categories (
        id TEXT PRIMARY KEY, parent_id TEXT REFERENCES categories(id) ON DELETE RESTRICT,
        direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
        name_ru TEXT NOT NULL CHECK (trim(name_ru) <> ''), name_en TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        income_class TEXT CHECK (income_class IN ('active', 'passive', 'unclassified')),
        sort_order INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK (id <> parent_id),
        CHECK ((direction = 'income' AND income_class IS NOT NULL)
            OR (direction = 'expense' AND income_class IS NULL))
    ) STRICT""",
    """CREATE TABLE category_aliases (
        alias TEXT NOT NULL CHECK (trim(alias) <> ''),
        direction TEXT NOT NULL CHECK (direction IN ('income', 'expense')),
        category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        PRIMARY KEY (alias, direction)
    ) STRICT""",
    """CREATE TABLE period_states (
        period TEXT NOT NULL CHECK (length(period) = 7),
        dataset TEXT NOT NULL CHECK (dataset IN ('cash_transactions', 'asset_snapshots')),
        status TEXT NOT NULL CHECK (status IN ('draft', 'saved')),
        revision INTEGER NOT NULL CHECK (revision >= 1), updated_at TEXT NOT NULL,
        PRIMARY KEY (period, dataset)
    ) STRICT""",
    """CREATE TABLE cash_transactions (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        flow_direction TEXT NOT NULL CHECK (flow_direction IN ('income', 'expense')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '', source_comment TEXT NOT NULL DEFAULT '',
        classification_method TEXT,
        status TEXT NOT NULL DEFAULT 'posted' CHECK (status IN ('posted', 'void')),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        voided_at TEXT, void_reason TEXT,
        superseded_by_id TEXT REFERENCES cash_transactions(id) ON DELETE RESTRICT,
        CHECK ((status = 'posted' AND voided_at IS NULL AND void_reason IS NULL)
            OR (status = 'void' AND voided_at IS NOT NULL AND trim(void_reason) <> ''))
    ) STRICT""",
    """CREATE TABLE asset_accounts (
        id TEXT PRIMARY KEY, name TEXT NOT NULL CHECK (trim(name) <> ''),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        asset_type_id TEXT REFERENCES asset_types(id) ON DELETE RESTRICT,
        liquidity_class_override_id TEXT REFERENCES liquidity_classes(id) ON DELETE RESTRICT,
        include_in_capital INTEGER NOT NULL DEFAULT 1 CHECK (include_in_capital IN (0, 1)),
        closed_period TEXT CHECK (closed_period IS NULL OR
          (length(closed_period) = 7 AND closed_period GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]')),
        CHECK ((active = 1 AND closed_period IS NULL)
          OR (active = 0 AND closed_period IS NOT NULL))
    ) STRICT""",
    """CREATE TABLE asset_snapshots (
        id TEXT PRIMARY KEY,
        account_id TEXT NOT NULL REFERENCES asset_accounts(id) ON DELETE RESTRICT,
        period TEXT NOT NULL CHECK (length(period) = 7),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        amount_minor INTEGER NOT NULL CHECK (amount_minor >= 0),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (account_id, period, currency_code)
    ) STRICT""",
    """CREATE TABLE source_batches (
        id TEXT PRIMARY KEY, source_kind TEXT NOT NULL CHECK (trim(source_kind) <> ''),
        document_hash TEXT NOT NULL CHECK (length(document_hash) = 64),
        parser_version TEXT NOT NULL CHECK (trim(parser_version) <> ''),
        status TEXT NOT NULL DEFAULT 'accepted' CHECK (status IN ('accepted', 'rejected')),
        created_at TEXT NOT NULL, UNIQUE (source_kind, document_hash, parser_version)
    ) STRICT""",
    """CREATE TABLE source_records (
        id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES source_batches(id) ON DELETE RESTRICT,
        record_key TEXT NOT NULL CHECK (trim(record_key) <> ''),
        payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64), created_at TEXT NOT NULL,
        UNIQUE (batch_id, record_key)
    ) STRICT""",
    """CREATE TABLE transaction_source_links (
        transaction_id TEXT NOT NULL REFERENCES cash_transactions(id) ON DELETE RESTRICT,
        source_record_id TEXT NOT NULL REFERENCES source_records(id) ON DELETE RESTRICT,
        link_role TEXT NOT NULL CHECK (link_role IN ('original', 'split', 'merged', 'replacement')),
        allocated_amount_minor INTEGER CHECK (allocated_amount_minor > 0),
        PRIMARY KEY (transaction_id, source_record_id, link_role)
    ) STRICT""",
    """CREATE TABLE entity_source_links (
        entity_type TEXT NOT NULL CHECK (trim(entity_type) <> ''),
        entity_id TEXT NOT NULL CHECK (trim(entity_id) <> ''),
        source_record_id TEXT NOT NULL REFERENCES source_records(id) ON DELETE RESTRICT,
        link_role TEXT NOT NULL DEFAULT 'original' CHECK (link_role IN ('original', 'replacement')),
        PRIMARY KEY (entity_type, entity_id, source_record_id, link_role)
    ) STRICT""",
    """CREATE TABLE transaction_drafts (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        draft_kind TEXT NOT NULL CHECK (draft_kind IN ('cash', 'debt', 'investment')),
        domain_action TEXT,
        flow_direction TEXT CHECK (flow_direction IN ('income', 'expense')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        category_id TEXT REFERENCES categories(id) ON DELETE RESTRICT, comment TEXT NOT NULL DEFAULT '',
        source_comment TEXT NOT NULL DEFAULT '',
        source_record_id TEXT UNIQUE REFERENCES source_records(id) ON DELETE RESTRICT,
        origin_kind TEXT NOT NULL CHECK (trim(origin_kind) <> ''),
        origin_key TEXT NOT NULL CHECK (trim(origin_key) <> ''),
        bank_status TEXT CHECK (bank_status IN ('pending', 'posted')),
        bank_reference TEXT NOT NULL DEFAULT '', bank_account_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('draft', 'ready', 'exported', 'archived', 'ignored')),
        row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (origin_kind, origin_key),
        CHECK ((draft_kind = 'cash' AND domain_action IS NULL
                AND flow_direction IS NOT NULL AND category_id IS NOT NULL)
            OR (draft_kind <> 'cash' AND domain_action IS NOT NULL
                AND flow_direction IS NULL AND category_id IS NULL))
    ) STRICT""",
    """CREATE TABLE operation_receipts (
        operation_key TEXT PRIMARY KEY, operation_kind TEXT NOT NULL CHECK (trim(operation_kind) <> ''),
        result_entity_type TEXT NOT NULL CHECK (trim(result_entity_type) <> ''),
        result_entity_id TEXT NOT NULL CHECK (trim(result_entity_id) <> ''),
        result_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE audit_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL CHECK (trim(entity_type) <> ''),
        entity_id TEXT NOT NULL CHECK (trim(entity_id) <> ''),
        action TEXT NOT NULL CHECK (trim(action) <> ''), before_json TEXT, after_json TEXT,
        reason TEXT NOT NULL CHECK (trim(reason) <> ''), occurred_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE categorization_rules (
        id TEXT PRIMARY KEY, priority INTEGER NOT NULL UNIQUE CHECK (priority >= 0),
        direction_scope TEXT NOT NULL CHECK (direction_scope IN ('income', 'expense', 'any')),
        matcher_type TEXT NOT NULL CHECK (matcher_type IN ('contains', 'exact', 'regex')),
        pattern TEXT NOT NULL CHECK (trim(pattern) <> ''),
        category_id TEXT NOT NULL REFERENCES categories(id) ON DELETE RESTRICT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE fx_rate_observations (
        id TEXT PRIMARY KEY, rate_date TEXT NOT NULL CHECK (length(rate_date) = 10),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        usd_per_unit_text TEXT NOT NULL CHECK (
            trim(usd_per_unit_text) <> '' AND substr(usd_per_unit_text, 1, 1) NOT IN ('-', '+')),
        source TEXT NOT NULL CHECK (trim(source) <> ''), fetched_at TEXT NOT NULL,
        sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),
        UNIQUE (rate_date, currency_code, source, fetched_at, sequence)
    ) STRICT""",
    """CREATE TABLE cpi_series (
        currency_code TEXT PRIMARY KEY REFERENCES currencies(code) ON DELETE RESTRICT,
        territory_code TEXT NOT NULL CHECK (trim(territory_code) <> ''),
        provider_id TEXT NOT NULL CHECK (trim(provider_id) <> ''),
        series_code TEXT NOT NULL CHECK (trim(series_code) <> ''),
        source_name TEXT NOT NULL CHECK (trim(source_name) <> ''),
        source_url TEXT NOT NULL CHECK (source_url LIKE 'https://%'),
        index_method TEXT NOT NULL CHECK (
            index_method IN ('published_index', 'chained_monthly_rate')),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE cpi_observations (
        id TEXT PRIMARY KEY,
        currency_code TEXT NOT NULL REFERENCES cpi_series(currency_code) ON DELETE RESTRICT,
        period TEXT NOT NULL CHECK (
            length(period) = 7 AND substr(period, 5, 1) = '-'),
        index_value_text TEXT NOT NULL CHECK (
            trim(index_value_text) <> '' AND substr(index_value_text, 1, 1) NOT IN ('-', '+')),
        published_on TEXT CHECK (published_on IS NULL OR length(published_on) = 10),
        source_version TEXT NOT NULL CHECK (trim(source_version) <> ''),
        payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
        fetched_at TEXT NOT NULL,
        UNIQUE (currency_code, period, source_version)
    ) STRICT""",
    """CREATE TABLE annual_goals (
        year INTEGER NOT NULL CHECK (year BETWEEN 1900 AND 9999),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        target_capital_minor INTEGER CHECK (target_capital_minor >= 0),
        target_monthly_income_minor INTEGER CHECK (target_monthly_income_minor >= 0),
        target_monthly_expense_minor INTEGER CHECK (target_monthly_expense_minor >= 0),
        target_expense_months INTEGER CHECK (target_expense_months > 0),
        notes TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL,
        PRIMARY KEY (year, currency_code)
    ) STRICT""",
    """CREATE TABLE debts (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK (kind IN ('receivable', 'liability')),
        counterparty TEXT NOT NULL CHECK (trim(counterparty) <> ''),
        opened_on TEXT NOT NULL CHECK (length(opened_on) = 10),
        principal_amount_minor INTEGER NOT NULL CHECK (principal_amount_minor > 0),
        principal_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        cash_amount_minor INTEGER NOT NULL CHECK (cash_amount_minor > 0),
        cash_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status IN ('active', 'closed')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE debt_payments (
        id TEXT PRIMARY KEY,
        debt_id TEXT NOT NULL REFERENCES debts(id) ON DELETE RESTRICT,
        occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        principal_amount_minor INTEGER NOT NULL CHECK (principal_amount_minor > 0),
        cash_amount_minor INTEGER NOT NULL CHECK (cash_amount_minor > 0),
        cash_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (status = 'posted'),
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE instruments (
        id TEXT PRIMARY KEY,
        ticker TEXT NOT NULL UNIQUE CHECK (trim(ticker) <> ''),
        name TEXT NOT NULL CHECK (trim(name) <> ''),
        asset_type TEXT NOT NULL CHECK (asset_type IN ('stocks', 'funds', 'crypto')),
        quote_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        provider TEXT NOT NULL DEFAULT '', exchange TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE investment_trades (
        id TEXT PRIMARY KEY,
        occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        operation TEXT NOT NULL CHECK (operation IN ('buy', 'sell')),
        instrument_id TEXT NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
        quantity_text TEXT NOT NULL CHECK (trim(quantity_text) <> ''),
        unit_price_text TEXT NOT NULL CHECK (trim(unit_price_text) <> ''),
        price_currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        fee_minor INTEGER NOT NULL DEFAULT 0 CHECK (fee_minor >= 0),
        account_label TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE market_price_observations (
        id TEXT PRIMARY KEY,
        instrument_id TEXT NOT NULL REFERENCES instruments(id) ON DELETE RESTRICT,
        price_date TEXT NOT NULL CHECK (length(price_date) = 10),
        price_text TEXT NOT NULL CHECK (trim(price_text) <> ''),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        source TEXT NOT NULL CHECK (trim(source) <> ''), fetched_at TEXT NOT NULL,
        sequence INTEGER NOT NULL DEFAULT 0 CHECK (sequence >= 0),
        UNIQUE (instrument_id, price_date, source, fetched_at, sequence)
    ) STRICT""",
    """CREATE TABLE crypto_wallets (
        id TEXT PRIMARY KEY,
        account_label TEXT NOT NULL CHECK (trim(account_label) <> ''),
        chain TEXT NOT NULL CHECK (trim(chain) <> ''),
        asset_code TEXT NOT NULL CHECK (trim(asset_code) <> ''),
        address TEXT NOT NULL CHECK (trim(address) <> ''),
        token_contract TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (chain, asset_code, address, token_contract)
    ) STRICT""",
    """CREATE TABLE crypto_balance_observations (
        id TEXT PRIMARY KEY,
        wallet_id TEXT NOT NULL REFERENCES crypto_wallets(id) ON DELETE RESTRICT,
        fetched_at TEXT NOT NULL, quantity_text TEXT NOT NULL CHECK (trim(quantity_text) <> ''),
        source TEXT NOT NULL CHECK (trim(source) <> '')
    ) STRICT""",
    """CREATE TABLE crypto_transactions (
        id TEXT PRIMARY KEY,
        wallet_id TEXT NOT NULL REFERENCES crypto_wallets(id) ON DELETE RESTRICT,
        chain_tx_id TEXT NOT NULL CHECK (trim(chain_tx_id) <> ''),
        occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        operation TEXT NOT NULL CHECK (trim(operation) <> ''),
        quantity_text TEXT, fee_text TEXT, counterparty TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL CHECK (trim(source) <> ''), comment TEXT NOT NULL DEFAULT '',
        UNIQUE (wallet_id, chain_tx_id, operation)
    ) STRICT""",
    """CREATE TABLE crypto_refresh_results (
        id TEXT PRIMARY KEY, fetched_at TEXT NOT NULL,
        wallet_id TEXT REFERENCES crypto_wallets(id) ON DELETE RESTRICT,
        source_row_number INTEGER,
        observed_account TEXT NOT NULL DEFAULT '', observed_chain TEXT NOT NULL DEFAULT '',
        observed_asset TEXT NOT NULL DEFAULT '', observed_address TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL CHECK (trim(status) <> ''), message TEXT NOT NULL DEFAULT ''
    ) STRICT""",
    """CREATE TABLE investment_cash_events (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        flow_kind TEXT NOT NULL CHECK (flow_kind IN ('contribution', 'withdrawal')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE debt_cash_events (
        id TEXT PRIMARY KEY, occurred_on TEXT NOT NULL CHECK (length(occurred_on) = 10),
        event_kind TEXT NOT NULL CHECK (event_kind IN ('issue', 'repayment')),
        side TEXT NOT NULL CHECK (side IN ('receivable', 'liability')),
        amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
        currency_code TEXT NOT NULL REFERENCES currencies(code) ON DELETE RESTRICT,
        comment TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
    ) STRICT""",
    """CREATE TABLE cpi_observation_sources (
        observation_id TEXT PRIMARY KEY
            REFERENCES cpi_observations(id) ON DELETE CASCADE,
        provider_id TEXT NOT NULL CHECK (trim(provider_id) <> ''),
        source_name TEXT NOT NULL CHECK (trim(source_name) <> ''),
        source_url TEXT NOT NULL CHECK (source_url LIKE 'https://%')
    ) STRICT""",
)

_INDEXES_AND_TRIGGERS = (
    "CREATE UNIQUE INDEX uq_active_category_name ON categories(direction, COALESCE(parent_id, ''), name_ru) WHERE active = 1",
    "CREATE INDEX ix_cash_date_category ON cash_transactions(occurred_on, category_id)",
    "CREATE INDEX ix_cash_category_date ON cash_transactions(category_id, occurred_on)",
    "CREATE INDEX ix_snapshots_period_currency ON asset_snapshots(period, currency_code)",
    "CREATE INDEX ix_fx_lookup ON fx_rate_observations(currency_code, rate_date, fetched_at, sequence)",
    "CREATE INDEX ix_cpi_lookup ON cpi_observations(currency_code, period, fetched_at)",
    "CREATE INDEX ix_source_records_batch ON source_records(batch_id)",
    "CREATE INDEX ix_audit_entity ON audit_events(entity_type, entity_id, id)",
    "CREATE INDEX ix_debt_payments_debt_date ON debt_payments(debt_id, occurred_on, id)",
    "CREATE INDEX ix_investment_trades_instrument_date ON investment_trades(instrument_id, occurred_on, id)",
    "CREATE INDEX ix_market_prices_lookup ON market_price_observations(instrument_id, price_date, fetched_at, sequence)",
    "CREATE INDEX ix_crypto_balances_wallet_time ON crypto_balance_observations(wallet_id, fetched_at)",
    "CREATE INDEX ix_crypto_transactions_wallet_date ON crypto_transactions(wallet_id, occurred_on)",
    "CREATE INDEX ix_investment_cash_events_date ON investment_cash_events(occurred_on, currency_code)",
    "CREATE INDEX ix_debt_cash_events_date ON debt_cash_events(occurred_on, side, event_kind)",
    """CREATE TRIGGER draft_category_insert BEFORE INSERT ON transaction_drafts
    WHEN NEW.draft_kind = 'cash' BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction AND (c.active = 1 OR c.id = 'income.unknown'))
      THEN RAISE(ABORT, 'cash draft needs an active category with matching direction') END;
    END""",
    """CREATE TRIGGER draft_category_update BEFORE UPDATE OF category_id, flow_direction ON transaction_drafts
    WHEN NEW.draft_kind = 'cash' BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction AND c.active = 1)
      THEN RAISE(ABORT, 'cash draft needs an active category with matching direction') END;
    END""",
    """CREATE TRIGGER categories_parent_insert BEFORE INSERT ON categories
    WHEN NEW.parent_id IS NOT NULL BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories p WHERE p.id = NEW.parent_id
        AND p.parent_id IS NULL AND p.direction = NEW.direction)
      THEN RAISE(ABORT, 'category parent must be a root with the same direction') END;
    END""",
    """CREATE TRIGGER categories_parent_update BEFORE UPDATE OF parent_id, direction ON categories
    WHEN NEW.parent_id IS NOT NULL BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories p WHERE p.id = NEW.parent_id
        AND p.parent_id IS NULL AND p.direction = NEW.direction)
      THEN RAISE(ABORT, 'category parent must be a root with the same direction') END;
    END""",
    """CREATE TRIGGER categories_direction_used BEFORE UPDATE OF direction ON categories
    WHEN NEW.direction <> OLD.direction AND (
      EXISTS (SELECT 1 FROM cash_transactions t WHERE t.category_id = OLD.id)
      OR EXISTS (SELECT 1 FROM categories c WHERE c.parent_id = OLD.id))
    BEGIN SELECT RAISE(ABORT, 'used category direction is immutable'); END""",
    """CREATE TRIGGER cash_category_insert BEFORE INSERT ON cash_transactions BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction AND (c.active = 1 OR
          (c.id = 'income.unknown' AND NEW.classification_method = 'migration_unresolved')))
      THEN RAISE(ABORT, 'transaction needs an active category with matching direction') END;
    END""",
    """CREATE TRIGGER cash_category_update BEFORE UPDATE OF category_id, flow_direction ON cash_transactions BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM categories c WHERE c.id = NEW.category_id
        AND c.direction = NEW.flow_direction)
      THEN RAISE(ABORT, 'transaction category direction mismatch') END;
    END""",
    """CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
    """CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS asset_account_archive_insert BEFORE INSERT ON asset_accounts
    WHEN NOT ((NEW.active = 1 AND NEW.closed_period IS NULL)
      OR (NEW.active = 0 AND NEW.closed_period IS NOT NULL))
    BEGIN SELECT RAISE(ABORT, 'asset account archive state is inconsistent'); END""",
    """CREATE TRIGGER IF NOT EXISTS asset_account_archive_update
    BEFORE UPDATE OF active, closed_period ON asset_accounts
    WHEN NOT ((NEW.active = 1 AND NEW.closed_period IS NULL)
      OR (NEW.active = 0 AND NEW.closed_period IS NOT NULL))
      OR (NEW.closed_period IS NOT NULL AND EXISTS (
        SELECT 1 FROM asset_snapshots s
        WHERE s.account_id = NEW.id AND s.period > NEW.closed_period))
    BEGIN SELECT RAISE(ABORT, 'asset account closure precedes an existing snapshot'); END""",
    """CREATE TRIGGER IF NOT EXISTS archived_asset_snapshot_insert BEFORE INSERT ON asset_snapshots
    WHEN EXISTS (SELECT 1 FROM asset_accounts a WHERE a.id = NEW.account_id
      AND a.closed_period IS NOT NULL AND NEW.period > a.closed_period)
    BEGIN SELECT RAISE(ABORT, 'asset account is closed for this period'); END""",
    """CREATE TRIGGER IF NOT EXISTS archived_asset_snapshot_update
    BEFORE UPDATE OF account_id, period ON asset_snapshots
    WHEN EXISTS (SELECT 1 FROM asset_accounts a WHERE a.id = NEW.account_id
      AND a.closed_period IS NOT NULL AND NEW.period > a.closed_period)
    BEGIN SELECT RAISE(ABORT, 'asset account is closed for this period'); END""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_asset_account_name ON asset_accounts(name)",
)

_VIEWS = (
    """CREATE VIEW v_cash_transactions AS
    SELECT t.id, t.occurred_on, substr(t.occurred_on, 1, 7) AS period,
      t.flow_direction, t.amount_minor,
      CASE t.flow_direction WHEN 'income' THEN t.amount_minor ELSE -t.amount_minor END AS signed_amount_minor,
      t.currency_code, t.category_id, c.parent_id AS parent_category_id,
      c.name_ru AS category_name_ru, c.income_class, t.comment,
      t.classification_method, t.row_version
    FROM cash_transactions t JOIN categories c ON c.id = t.category_id
    WHERE t.status = 'posted'""",
    """CREATE VIEW v_monthly_cashflow AS
    SELECT period, currency_code,
      SUM(CASE WHEN flow_direction = 'income' THEN amount_minor ELSE 0 END) AS income_minor,
      SUM(CASE WHEN flow_direction = 'expense' THEN amount_minor ELSE 0 END) AS expense_minor,
      SUM(signed_amount_minor) AS balance_minor
    FROM v_cash_transactions GROUP BY period, currency_code""",
    """CREATE VIEW v_monthly_category_allocation AS
    SELECT period, currency_code, flow_direction, category_id, category_name_ru,
      SUM(amount_minor) AS category_amount_minor,
      SUM(SUM(amount_minor)) OVER (PARTITION BY period, currency_code, flow_direction) AS direction_amount_minor,
      CAST(SUM(amount_minor) AS REAL) /
        NULLIF(SUM(SUM(amount_minor)) OVER (PARTITION BY period, currency_code, flow_direction), 0) AS share
    FROM v_cash_transactions
    GROUP BY period, currency_code, flow_direction, category_id, category_name_ru""",
    """CREATE VIEW v_asset_snapshots AS
    SELECT s.id, s.period, s.account_id, a.name AS account_name,
      a.asset_type_id, a.liquidity_class_override_id,
      COALESCE(a.liquidity_class_override_id, d.liquidity_class_id) AS liquidity_class_id,
      CASE WHEN a.liquidity_class_override_id IS NOT NULL THEN 'manual'
        WHEN d.liquidity_class_id IS NOT NULL THEN 'suggested' ELSE 'unclassified' END AS liquidity_source,
      a.include_in_capital, a.active, a.closed_period,
      s.currency_code, s.amount_minor, s.row_version
    FROM asset_snapshots s JOIN asset_accounts a ON a.id = s.account_id
    LEFT JOIN asset_type_liquidity_defaults d ON d.asset_type_id = a.asset_type_id""",
    """CREATE VIEW v_effective_fx_rates AS
    SELECT id, rate_date, currency_code, usd_per_unit_text, source, fetched_at, sequence
    FROM (SELECT f.*, ROW_NUMBER() OVER (PARTITION BY rate_date, currency_code
      ORDER BY fetched_at DESC, sequence DESC, id DESC) AS selection_rank
      FROM fx_rate_observations f) WHERE selection_rank = 1""",
    """CREATE VIEW v_effective_cpi AS
    SELECT id, currency_code, period, index_value_text, published_on,
      source_version, payload_sha256, fetched_at
    FROM (SELECT c.*, ROW_NUMBER() OVER (PARTITION BY currency_code, period
      ORDER BY fetched_at DESC, id DESC) AS selection_rank
      FROM cpi_observations c) WHERE selection_rank = 1""",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _schema_checksum() -> str:
    return hashlib.sha256("\n".join((*_TABLES, *_INDEXES_AND_TRIGGERS, *_VIEWS)).encode()).hexdigest()


def _migrate_v7_to_v8(connection: sqlite3.Connection) -> None:
    account_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(asset_accounts)")
    }
    if account_columns != {"id", "name", "active", "created_at", "updated_at"}:
        raise ValueError("SQLite v7 asset_accounts schema does not match the migration contract")

    connection.execute("""CREATE TABLE asset_types (
        id TEXT PRIMARY KEY CHECK (trim(id) <> ''),
        name_ru TEXT NOT NULL CHECK (trim(name_ru) <> ''),
        name_en TEXT NOT NULL CHECK (trim(name_en) <> ''),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        sort_order INTEGER NOT NULL DEFAULT 0
    ) STRICT""")
    connection.executemany(
        "INSERT INTO asset_types (id, name_ru, name_en, sort_order) VALUES (?, ?, ?, ?)",
        _ASSET_TYPES,
    )
    connection.execute(
        "ALTER TABLE asset_accounts ADD COLUMN asset_type_id TEXT REFERENCES asset_types(id) ON DELETE RESTRICT"
    )
    connection.execute(
        "ALTER TABLE asset_accounts ADD COLUMN include_in_capital INTEGER NOT NULL DEFAULT 1 "
        "CHECK (include_in_capital IN (0, 1))"
    )
    connection.execute("DROP VIEW v_asset_snapshots")
    connection.execute("""CREATE VIEW v_asset_snapshots AS
        SELECT s.id, s.period, s.account_id, a.name AS account_name,
          a.asset_type_id, a.include_in_capital,
          s.currency_code, s.amount_minor, s.row_version
        FROM asset_snapshots s JOIN asset_accounts a ON a.id = s.account_id""")
    now = _utc_now()
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (8, "asset_classification", hashlib.sha256(b"asset_classification_v8").hexdigest(), now),
    )
    connection.execute("PRAGMA user_version = 8")


def _migrate_v8_to_v9(connection: sqlite3.Connection) -> None:
    account_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(asset_accounts)")
    }
    expected_columns = {
        "id", "name", "active", "created_at", "updated_at", "asset_type_id",
        "include_in_capital",
    }
    if account_columns != expected_columns:
        raise ValueError("SQLite v8 asset_accounts schema does not match the migration contract")

    connection.execute("""CREATE TABLE liquidity_classes (
        id TEXT PRIMARY KEY CHECK (id IN ('A1', 'A2', 'A3', 'A4')),
        name_ru TEXT NOT NULL CHECK (trim(name_ru) <> ''),
        name_en TEXT NOT NULL CHECK (trim(name_en) <> ''),
        horizon_ru TEXT NOT NULL CHECK (trim(horizon_ru) <> ''),
        horizon_en TEXT NOT NULL CHECK (trim(horizon_en) <> ''),
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        sort_order INTEGER NOT NULL DEFAULT 0
    ) STRICT""")
    connection.execute("""CREATE TABLE asset_type_liquidity_defaults (
        asset_type_id TEXT PRIMARY KEY REFERENCES asset_types(id) ON DELETE CASCADE,
        liquidity_class_id TEXT NOT NULL REFERENCES liquidity_classes(id) ON DELETE RESTRICT
    ) STRICT""")
    connection.executemany(
        """INSERT INTO liquidity_classes
          (id, name_ru, name_en, horizon_ru, horizon_en, sort_order)
          VALUES (?, ?, ?, ?, ?, ?)""",
        _LIQUIDITY_CLASSES,
    )
    connection.executemany(
        "INSERT INTO asset_type_liquidity_defaults VALUES (?, ?)",
        _ASSET_TYPE_LIQUIDITY_DEFAULTS,
    )
    connection.execute("DROP VIEW v_asset_snapshots")
    connection.execute(
        "ALTER TABLE asset_accounts ADD COLUMN liquidity_class_override_id TEXT "
        "REFERENCES liquidity_classes(id) ON DELETE RESTRICT"
    )
    connection.execute(_VIEWS[3])
    now = _utc_now()
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (9, "asset_liquidity", "102e211473262760aa72800aa6afe19d295ad5997e0baa500b1cb644cf237ba5", now),
    )
    connection.execute("PRAGMA user_version = 9")


def _migrate_v9_to_v10(connection: sqlite3.Connection) -> None:
    if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cpi_series'").fetchone():
        raise ValueError("SQLite v9 unexpectedly contains CPI tables")
    connection.execute(_TABLES[21])
    connection.execute(_TABLES[22])
    connection.execute(_INDEXES_AND_TRIGGERS[5])
    connection.execute(_VIEWS[5])
    now = _utc_now()
    connection.executemany(
        "INSERT OR IGNORE INTO currencies VALUES (?, 2)",
        [(code,) for code in sorted(config.UNIQUE_TICKERS)],
    )
    connection.executemany(
        """INSERT INTO cpi_series
          (currency_code, territory_code, provider_id, series_code, source_name,
           source_url, index_method, created_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(*series, now, now) for series in _CPI_SERIES],
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (10, "official_cpi", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 10")


def _migrate_v10_to_v11(connection: sqlite3.Connection) -> None:
    now = _utc_now()
    connection.execute("DELETE FROM cpi_observations WHERE currency_code = 'RUB'")
    connection.execute(
        """UPDATE cpi_series
          SET provider_id = ?, series_code = ?, source_name = ?, source_url = ?,
              index_method = ?, updated_at = ?
          WHERE currency_code = 'RUB'""",
        (*_CPI_SERIES[0][2:], now),
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (11, "russia_cpi_world_bank_gem", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 11")


def _migrate_v11_to_v12(connection: sqlite3.Connection) -> None:
    now = _utc_now()
    connection.execute(_TABLES[-1])
    connection.execute(
        """INSERT INTO cpi_observation_sources
          (observation_id, provider_id, source_name, source_url)
          SELECT o.id, s.provider_id, s.source_name, s.source_url
          FROM cpi_observations o
          JOIN cpi_series s ON s.currency_code = o.currency_code"""
    )
    connection.execute("DELETE FROM cpi_observations WHERE currency_code = 'KZT'")
    connection.execute(
        """UPDATE cpi_series
          SET provider_id = ?, series_code = ?, source_name = ?, source_url = ?,
              index_method = ?, updated_at = ?
          WHERE currency_code = 'KZT'""",
        (*_CPI_SERIES[1][2:], now),
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (12, "kazakhstan_cpi_hybrid", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 12")


def _migrate_v12_to_v13(connection: sqlite3.Connection) -> None:
    now = _utc_now()
    connection.executemany(
        """INSERT INTO asset_types (id, name_ru, name_en, sort_order)
          VALUES (?, ?, ?, ?)
          ON CONFLICT(id) DO UPDATE SET name_ru = excluded.name_ru,
            name_en = excluded.name_en, active = 1, sort_order = excluded.sort_order""",
        _ASSET_TYPES,
    )
    connection.executemany(
        """INSERT INTO asset_type_liquidity_defaults
          (asset_type_id, liquidity_class_id) VALUES (?, ?)
          ON CONFLICT(asset_type_id) DO UPDATE
          SET liquidity_class_id = excluded.liquidity_class_id""",
        _ASSET_TYPE_LIQUIDITY_DEFAULTS,
    )
    connection.execute(
        "UPDATE asset_accounts SET liquidity_class_override_id = NULL "
        "WHERE liquidity_class_override_id IS NOT NULL"
    )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (13, "asset_liquidity_by_type", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 13")


def _migrate_v13_to_v14(connection: sqlite3.Connection) -> None:
    now = _utc_now()
    duplicate_names = connection.execute(
        """SELECT name FROM asset_accounts
        GROUP BY name HAVING COUNT(*) > 1 ORDER BY name"""
    ).fetchall()
    for duplicate_name in duplicate_names:
        name = duplicate_name["name"]
        accounts = connection.execute(
            """SELECT a.*, COUNT(s.id) AS snapshot_count
            FROM asset_accounts a
            LEFT JOIN asset_snapshots s ON s.account_id = a.id
            WHERE a.name = ?
            GROUP BY a.id
            ORDER BY snapshot_count DESC, a.asset_type_id IS NULL,
              a.created_at, a.id""",
            (name,),
        ).fetchall()
        asset_type_ids = {row["asset_type_id"] for row in accounts
                          if row["asset_type_id"] is not None}
        liquidity_ids = {row["liquidity_class_override_id"] for row in accounts
                         if row["liquidity_class_override_id"] is not None}
        if len(asset_type_ids) > 1 or len(liquidity_ids) > 1:
            raise ValueError(f"conflicting classifications for asset account: {name}")

        canonical = accounts[0]
        canonical_id = canonical["id"]
        if canonical["asset_type_id"] is None and asset_type_ids:
            connection.execute(
                "UPDATE asset_accounts SET asset_type_id = ?, updated_at = ? WHERE id = ?",
                (next(iter(asset_type_ids)), now, canonical_id),
            )
        if canonical["liquidity_class_override_id"] is None and liquidity_ids:
            connection.execute(
                """UPDATE asset_accounts
                SET liquidity_class_override_id = ?, updated_at = ? WHERE id = ?""",
                (next(iter(liquidity_ids)), now, canonical_id),
            )

        for duplicate in accounts[1:]:
            duplicate_id = duplicate["id"]
            conflict = connection.execute(
                """SELECT 1 FROM asset_snapshots duplicate
                JOIN asset_snapshots canonical
                  ON canonical.account_id = ?
                 AND canonical.period = duplicate.period
                 AND canonical.currency_code = duplicate.currency_code
                WHERE duplicate.account_id = ? LIMIT 1""",
                (canonical_id, duplicate_id),
            ).fetchone()
            if conflict is not None:
                raise ValueError(f"conflicting snapshots for asset account: {name}")
            connection.execute(
                "UPDATE asset_snapshots SET account_id = ?, updated_at = ? WHERE account_id = ?",
                (canonical_id, now, duplicate_id),
            )
            connection.execute(
                """INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_account', ?, 'merged', ?, ?,
                  'merge duplicate account names during schema migration', ?)""",
                (duplicate_id, json.dumps(dict(duplicate), sort_keys=True),
                 json.dumps({"merged_into": canonical_id}, sort_keys=True), now),
            )
            connection.execute("DELETE FROM asset_accounts WHERE id = ?", (duplicate_id,))

    connection.execute(_INDEXES_AND_TRIGGERS[-1])
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (14, "unique_asset_account_names", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 14")


def _migrate_v14_to_v15(connection: sqlite3.Connection) -> None:
    now = _utc_now()
    connection.execute("DROP VIEW v_asset_snapshots")
    account_columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(asset_accounts)")
    }
    if "closed_period" not in account_columns:
        connection.execute(
            "ALTER TABLE asset_accounts ADD COLUMN closed_period TEXT "
            "CHECK (closed_period IS NULL OR (length(closed_period) = 7 AND "
            "closed_period GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'))"
        )
    connection.execute(
        """UPDATE asset_accounts SET closed_period = COALESCE(
          (SELECT MAX(s.period) FROM asset_snapshots s WHERE s.account_id = asset_accounts.id),
          substr(created_at, 1, 7)) WHERE active = 0"""
    )
    connection.execute(_VIEWS[3])
    for statement in _INDEXES_AND_TRIGGERS[-5:-1]:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (15, "asset_account_archive", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 15")


def _migrate_v15_to_v16(connection: sqlite3.Connection) -> None:
    """Archive historical accounts that disappeared before the latest snapshot."""
    now = _utc_now()
    latest_period = connection.execute(
        "SELECT MAX(period) FROM asset_snapshots"
    ).fetchone()[0]
    if latest_period is not None:
        accounts = connection.execute(
            """SELECT a.*, MAX(s.period) AS last_period
            FROM asset_accounts a
            JOIN asset_snapshots s ON s.account_id = a.id
            WHERE a.active = 1
            GROUP BY a.id
            HAVING MAX(s.period) < ?""",
            (latest_period,),
        ).fetchall()
        for account in accounts:
            before = dict(account)
            before.pop("last_period", None)
            connection.execute(
                """UPDATE asset_accounts SET active = 0, closed_period = ?,
                updated_at = ? WHERE id = ?""",
                (account["last_period"], now, account["id"]),
            )
            after = connection.execute(
                "SELECT * FROM asset_accounts WHERE id = ?", (account["id"],)
            ).fetchone()
            connection.execute(
                """INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_account', ?, 'archived', ?, ?,
                'infer archive from absence in latest historical snapshot', ?)""",
                (
                    account["id"],
                    json.dumps(before, ensure_ascii=False, sort_keys=True),
                    json.dumps(dict(after), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (16, "infer_historical_asset_archives", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 16")


def _migrate_v16_to_v17(connection: sqlite3.Connection) -> None:
    now = _utc_now()
    columns = {
        row["name"] for row in connection.execute("PRAGMA table_info(annual_goals)")
    }
    if "target_expense_months" not in columns:
        connection.execute(
            "ALTER TABLE annual_goals ADD COLUMN target_expense_months INTEGER "
            "CHECK (target_expense_months > 0)"
        )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (17, "annual_goal_expense_months", _schema_checksum(), now),
    )
    connection.execute("PRAGMA user_version = 17")


def _migrate_v17_to_v18(connection: sqlite3.Connection) -> None:
    for table in ("transaction_drafts", "cash_transactions"):
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "source_comment" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN source_comment TEXT NOT NULL DEFAULT ''"
            )
    connection.execute(
        "INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
        (18, "preserve_statement_comment", _schema_checksum(), _utc_now()),
    )
    connection.execute("PRAGMA user_version = 18")


@contextmanager
def connect_database(path: str | Path, *, writable: bool = False):
    database_path = Path(path).resolve()
    if config.is_test_mode() and not database_path.is_relative_to(Path(config.SAMPLE_DATA_PATH).resolve()):
        raise PermissionError("Test mode can only read the sample database")
    if writable:
        config.require_writable_mode()
        connection = sqlite3.connect(database_path)
    else:
        connection = sqlite3.connect(f"{database_path.as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL" if writable else "PRAGMA query_only = ON")
        yield connection
        if writable:
            connection.commit()
    except Exception:
        if writable:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize_database(path: str | Path, *, data_mode: str = "synthetic") -> None:
    if data_mode not in {"live", "migration", "synthetic"}:
        raise ValueError("unsupported data mode")
    if sqlite3.sqlite_version_info < (3, 37, 0):
        raise RuntimeError("SQLite 3.37 or newer is required for STRICT tables")
    with connect_database(path, writable=True) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            row = connection.execute("SELECT checksum FROM schema_migrations WHERE version = ?", (version,)).fetchone()
            if row is None or row[0] != _schema_checksum():
                raise ValueError("SQLite schema version or checksum mismatch")
            return
        if version == 7:
            _migrate_v7_to_v8(connection)
            _migrate_v8_to_v9(connection)
            _migrate_v9_to_v10(connection)
            _migrate_v10_to_v11(connection)
            _migrate_v11_to_v12(connection)
            _migrate_v12_to_v13(connection)
            _migrate_v13_to_v14(connection)
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 8:
            _migrate_v8_to_v9(connection)
            _migrate_v9_to_v10(connection)
            _migrate_v10_to_v11(connection)
            _migrate_v11_to_v12(connection)
            _migrate_v12_to_v13(connection)
            _migrate_v13_to_v14(connection)
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 9:
            _migrate_v9_to_v10(connection)
            _migrate_v10_to_v11(connection)
            _migrate_v11_to_v12(connection)
            _migrate_v12_to_v13(connection)
            _migrate_v13_to_v14(connection)
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 10:
            _migrate_v10_to_v11(connection)
            _migrate_v11_to_v12(connection)
            _migrate_v12_to_v13(connection)
            _migrate_v13_to_v14(connection)
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 11:
            _migrate_v11_to_v12(connection)
            _migrate_v12_to_v13(connection)
            _migrate_v13_to_v14(connection)
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 12:
            _migrate_v12_to_v13(connection)
            _migrate_v13_to_v14(connection)
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 13:
            _migrate_v13_to_v14(connection)
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 14:
            _migrate_v14_to_v15(connection)
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 15:
            _migrate_v15_to_v16(connection)
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 16:
            _migrate_v16_to_v17(connection)
            _migrate_v17_to_v18(connection)
            return
        if version == 17:
            _migrate_v17_to_v18(connection)
            return
        has_tables = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if version != 0 or has_tables:
            raise ValueError("SQLite database is not empty or has an unsupported schema")
        for statement in (*_TABLES, *_INDEXES_AND_TRIGGERS, *_VIEWS):
            connection.execute(statement)
        now = _utc_now()
        connection.execute("INSERT INTO app_metadata VALUES (1, ?, ?, ?)", (uuid4().hex, data_mode, now))
        connection.executemany(
            """INSERT INTO categories
              (id, parent_id, direction, name_ru, active, income_class, sort_order, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(*category, now, now) for category in _SYSTEM_CATEGORIES],
        )
        connection.executemany(
            "INSERT INTO asset_types (id, name_ru, name_en, sort_order) VALUES (?, ?, ?, ?)",
            _ASSET_TYPES,
        )
        connection.executemany(
            """INSERT INTO liquidity_classes
              (id, name_ru, name_en, horizon_ru, horizon_en, sort_order)
              VALUES (?, ?, ?, ?, ?, ?)""",
            _LIQUIDITY_CLASSES,
        )
        connection.executemany(
            "INSERT INTO asset_type_liquidity_defaults VALUES (?, ?)",
            _ASSET_TYPE_LIQUIDITY_DEFAULTS,
        )
        connection.executemany("INSERT INTO currencies VALUES (?, 2)", [(code,) for code in sorted(config.UNIQUE_TICKERS)])
        connection.executemany(
            """INSERT INTO cpi_series
              (currency_code, territory_code, provider_id, series_code, source_name,
               source_url, index_method, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(*series, now, now) for series in _CPI_SERIES],
        )
        connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?, ?)",
                           (SCHEMA_VERSION, "normalized_core", _schema_checksum(), now))
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _period(value: str) -> str:
    try:
        parsed = date.fromisoformat(f"{value}-01")
    except (TypeError, ValueError) as exc:
        raise ValueError("period must be YYYY-MM") from exc
    if parsed.strftime("%Y-%m") != value:
        raise ValueError("period must be YYYY-MM")
    return value


def _iso_date(value: str, name: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{name} must be an ISO date")
    return value


def _minor_units(connection: sqlite3.Connection, currency: str, value, *, allow_zero: bool) -> int:
    currency = currency.upper()
    row = connection.execute("SELECT minor_unit FROM currencies WHERE code = ?", (currency,)).fetchone()
    if row is None:
        raise ValueError("unsupported currency")
    amount = parse_money_amount(value)
    if amount < 0 or (amount == 0 and not allow_zero):
        raise ValueError(f"amount must be {'non-negative' if allow_zero else 'positive'}")
    scaled = amount * (10 ** row[0])
    if scaled != scaled.to_integral_value():
        raise ValueError(f"amount exceeds {currency} minor-unit precision")
    result = int(scaled)
    if not -(2**63) < result < 2**63:
        raise ValueError("amount is outside SQLite INTEGER range")
    return result


def _require_active_category(connection: sqlite3.Connection, category_id: str,
                             direction: str) -> None:
    category = connection.execute(
        "SELECT direction FROM categories WHERE id = ? AND active = 1",
        (category_id,),
    ).fetchone()
    if category is None or category["direction"] != direction:
        raise ValueError("transaction category direction mismatch")


def _decimal_text(value, name: str) -> str:
    normalized = str(value).strip().replace(" ", "").replace("\u00a0", "").replace(",", ".")
    try:
        parsed = Decimal(normalized)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return format(parsed, "f")


def _positive_decimal_text(value, name: str) -> str:
    parsed = _decimal_text(value, name)
    if Decimal(parsed) <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


def _non_negative_decimal_text(value, name: str) -> str:
    parsed = _decimal_text(value, name)
    if Decimal(parsed) < 0:
        raise ValueError(f"{name} must be non-negative")
    return parsed


def _amount(minor: int, minor_unit: int) -> Decimal:
    return Decimal(minor).scaleb(-minor_unit)


def _mark_period(connection: sqlite3.Connection, period: str, dataset: str) -> None:
    if dataset not in _DATASETS:
        raise ValueError("unsupported period dataset")
    connection.execute(
        """INSERT INTO period_states VALUES (?, ?, 'saved', 1, ?)
        ON CONFLICT(period, dataset) DO UPDATE SET status = 'saved',
        revision = period_states.revision + 1, updated_at = excluded.updated_at""",
        (_period(period), dataset, _utc_now()),
    )


def save_month(path: str | Path, period: str) -> None:
    with connect_database(path, writable=True) as connection:
        _mark_period(connection, period, "cash_transactions")


def save_asset_month(path: str | Path, period: str) -> None:
    with connect_database(path, writable=True) as connection:
        _mark_period(connection, period, "asset_snapshots")


def _saved_periods(path: str | Path, dataset: str) -> list[str]:
    with connect_database(path) as connection:
        return [row[0] for row in connection.execute(
            "SELECT period FROM period_states WHERE dataset = ? AND status = 'saved' ORDER BY period", (dataset,))]


def saved_months(path: str | Path) -> list[str]:
    return _saved_periods(path, "cash_transactions")


def saved_asset_months(path: str | Path) -> list[str]:
    return _saved_periods(path, "asset_snapshots")


def add_category(path: str | Path, category_id: str, name_ru: str, *,
                 direction: str = "expense", income_class: str | None = None,
                 parent_id: str | None = None) -> None:
    if not category_id or not name_ru.strip():
        raise ValueError("category ID and name are required")
    if direction not in _DIRECTIONS:
        raise ValueError("category direction must be income or expense")
    if (direction == "income" and income_class not in {"active", "passive"}) or (
        direction == "expense" and income_class is not None):
        raise ValueError("income class belongs only to income categories")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        connection.execute(
            """INSERT INTO categories
              (id, parent_id, direction, name_ru, income_class, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (category_id, parent_id, direction, name_ru.strip(), income_class, now, now),
        )


def categories(path: str | Path, *, include_internal: bool = False) -> list[dict]:
    """Return the category registry with usage counts for the management UI."""
    with connect_database(path) as connection:
        rows = connection.execute(
            """SELECT c.id, c.parent_id, c.direction, c.name_ru, c.name_en,
                      c.active, c.income_class, c.sort_order,
                      p.name_ru AS parent_name_ru,
                      (SELECT COUNT(*) FROM cash_transactions t
                       WHERE t.category_id = c.id) AS transaction_count,
                      (SELECT COUNT(*) FROM transaction_drafts d
                       WHERE d.category_id = c.id
                         AND d.status IN ('draft', 'ready')) AS open_draft_count
               FROM categories c
               LEFT JOIN categories p ON p.id = c.parent_id
               ORDER BY CASE c.direction WHEN 'income' THEN 0 ELSE 1 END,
                        c.sort_order, c.name_ru, c.id"""
        ).fetchall()
    result = [dict(row) for row in rows]
    if not include_internal:
        result = [row for row in result if row["id"] != "income.unknown"]
    return result


def _ensure_unique_category_name(connection: sqlite3.Connection, *, direction: str,
                                 name_ru: str, exclude_id: str | None = None) -> None:
    del direction
    normalized = name_ru.strip().casefold()
    rows = connection.execute(
        "SELECT id, name_ru FROM categories"
    ).fetchall()
    if any(row["id"] != exclude_id and row["name_ru"].strip().casefold() == normalized
           for row in rows):
        raise ValueError("category name already exists for this direction")


def create_category(path: str | Path, name_ru: str, *, direction: str,
                    income_class: str | None = None) -> str:
    """Create a user-managed root category and return its stable opaque ID."""
    name_ru = name_ru.strip()
    if not name_ru:
        raise ValueError("category name is required")
    if direction not in _DIRECTIONS:
        raise ValueError("category direction must be income or expense")
    if (direction == "income" and income_class not in {"active", "passive"}) or (
        direction == "expense" and income_class is not None):
        raise ValueError("income class belongs only to income categories")
    category_id = f"user.{direction}.{uuid4().hex}"
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        _ensure_unique_category_name(
            connection, direction=direction, name_ru=name_ru)
        sort_order = connection.execute(
            """SELECT COALESCE(MAX(sort_order), 0) + 10 FROM categories
               WHERE direction = ? AND parent_id IS NULL""",
            (direction,),
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO categories
              (id, parent_id, direction, name_ru, income_class, sort_order,
               created_at, updated_at)
              VALUES (?, NULL, ?, ?, ?, ?, ?, ?)""",
            (category_id, direction, name_ru, income_class, sort_order, now, now),
        )
    return category_id


def rename_category(path: str | Path, category_id: str, name_ru: str) -> None:
    """Rename a category without changing its ID or historical assignments."""
    name_ru = name_ru.strip()
    if not name_ru:
        raise ValueError("category name is required")
    with connect_database(path, writable=True) as connection:
        category = connection.execute(
            "SELECT direction FROM categories WHERE id = ?", (category_id,)
        ).fetchone()
        if category is None or category_id == "income.unknown":
            raise ValueError("unknown category")
        _ensure_unique_category_name(
            connection, direction=category["direction"], name_ru=name_ru,
            exclude_id=category_id)
        connection.execute(
            "UPDATE categories SET name_ru = ?, updated_at = ? WHERE id = ?",
            (name_ru, _utc_now(), category_id),
        )


def set_category_active(path: str | Path, category_id: str, active: bool) -> None:
    """Change availability for new records while preserving historical rows."""
    if category_id in {"income.unknown", "income.other", "expense.other"}:
        raise ValueError("this fallback category cannot change activity")
    with connect_database(path, writable=True) as connection:
        category = connection.execute(
            "SELECT direction, name_ru, active FROM categories WHERE id = ?",
            (category_id,),
        ).fetchone()
        if category is None:
            raise ValueError("unknown category")
        target = int(bool(active))
        if category["active"] == target:
            return
        if not target:
            open_drafts = connection.execute(
                """SELECT COUNT(*) FROM transaction_drafts
                   WHERE category_id = ? AND status IN ('draft', 'ready')""",
                (category_id,),
            ).fetchone()[0]
            active_children = connection.execute(
                "SELECT COUNT(*) FROM categories WHERE parent_id = ? AND active = 1",
                (category_id,),
            ).fetchone()[0]
            if open_drafts:
                raise ValueError("category is used by open transaction drafts")
            if active_children:
                raise ValueError("category has active subcategories")
        else:
            _ensure_unique_category_name(
                connection, direction=category["direction"],
                name_ru=category["name_ru"], exclude_id=category_id)
        connection.execute(
            "UPDATE categories SET active = ?, updated_at = ? WHERE id = ?",
            (target, _utc_now(), category_id),
        )


def add_cash_transaction(path: str | Path, *, transaction_id: str, occurred_on: str,
                         flow_direction: str, category_id: str, amount, currency: str,
                         comment: str = "", classification_method: str | None = None) -> None:
    if not transaction_id:
        raise ValueError("transaction ID is required")
    occurred_on = _iso_date(occurred_on, "occurred_on")
    if flow_direction not in _DIRECTIONS:
        raise ValueError("flow direction must be income or expense")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        amount_minor = _minor_units(connection, currency, amount, allow_zero=False)
        try:
            connection.execute(
                """INSERT INTO cash_transactions
                  (id, occurred_on, flow_direction, amount_minor, currency_code,
                   category_id, comment, classification_method, created_at, updated_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (transaction_id, occurred_on, flow_direction, amount_minor,
                 currency.upper(), category_id, comment, classification_method, now, now),
            )
        except sqlite3.IntegrityError as exc:
            if "active category with matching direction" in str(exc):
                raise ValueError("transaction needs an active category with matching direction") from exc
            raise
        _mark_period(connection, occurred_on[:7], "cash_transactions")


def create_transaction_draft(
    path: str | Path,
    *,
    occurred_on: str,
    flow_direction: str,
    category_id: str,
    amount,
    currency: str,
    origin_kind: str,
    origin_key: str,
    comment: str = "",
    bank_status: str | None = None,
    bank_reference: str = "",
    bank_account_id: str = "",
    status: str = "draft",
    source_record_id: str | None = None,
    draft_id: str | None = None,
) -> str:
    """Create one idempotent cash draft using its external origin identity."""
    occurred_on = _iso_date(occurred_on, "occurred_on")
    if flow_direction not in _DIRECTIONS:
        raise ValueError("flow direction must be income or expense")
    if not origin_kind.strip() or not origin_key.strip():
        raise ValueError("draft origin kind and key are required")
    if bank_status not in {None, "pending", "posted"}:
        raise ValueError("unsupported bank status")
    if status not in {"draft", "ready"}:
        raise ValueError("a new draft must have draft or ready status")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        amount_minor = _minor_units(connection, currency, amount, allow_zero=False)
        _require_active_category(connection, category_id, flow_direction)
        values = (
            occurred_on, flow_direction, amount_minor, currency.upper(), category_id,
            comment, source_record_id, origin_kind.strip(), origin_key.strip(), bank_status,
            bank_reference, bank_account_id, status,
        )
        existing = connection.execute("""SELECT id, occurred_on, flow_direction,
            amount_minor, currency_code, category_id, comment, source_record_id,
            origin_kind, origin_key, bank_status, bank_reference, bank_account_id, status
            FROM transaction_drafts WHERE origin_kind = ? AND origin_key = ?""",
            (origin_kind.strip(), origin_key.strip())).fetchone()
        if existing is not None:
            if tuple(existing)[1:-1] != values[:-1]:
                raise ValueError("draft origin key was reused with another payload")
            return existing["id"]
        draft_id = draft_id or uuid4().hex
        connection.execute("""INSERT INTO transaction_drafts
            (id, occurred_on, draft_kind, domain_action, flow_direction, amount_minor,
             currency_code, category_id, comment, source_record_id, origin_kind, origin_key,
             bank_status, bank_reference, bank_account_id, status, created_at, updated_at)
            VALUES (?, ?, 'cash', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (draft_id, *values, now, now))
    return draft_id


def transaction_drafts_snapshot(path: str | Path) -> tuple[list[dict], str]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT d.*, c.minor_unit FROM transaction_drafts d
            JOIN currencies c ON c.code = d.currency_code ORDER BY d.id""").fetchall()
        revision = _draft_revision(connection)
    return ([{**dict(row), "currency": row["currency_code"],
              "amount": _amount(row["amount_minor"], row["minor_unit"])}
             for row in rows], revision)


def append_cash_drafts(path: str | Path, *, rows: list[dict],
                       expected_revision: str | None = None,
                       pending_replacements: dict[tuple[str, str], tuple[str, str]] | None = None) -> dict:
    """Atomically append an import batch while preserving origin-level idempotence."""
    if not rows:
        with connect_database(path) as connection:
            revision = _draft_revision(connection)
        return {"accepted_rows": 0, "skipped_rows": 0,
                "replaced_pending_rows": 0, "revision": revision}
    with connect_database(path, writable=True) as connection:
        _require_draft_revision(connection, expected_revision)
        accepted = skipped = replaced = 0
        now = _utc_now()
        for new_key, old_key in (pending_replacements or {}).items():
            if not any(
                str(row.get("origin_kind", "")).strip() == new_key[0]
                and str(row.get("origin_key", "")).strip() == new_key[1]
                for row in rows
            ):
                raise StorageRevisionConflict("replacement row is missing from the import batch")
            old = connection.execute("""SELECT id, bank_status, status FROM transaction_drafts
                WHERE origin_kind = ? AND origin_key = ?""", old_key).fetchone()
            if old is None or old["bank_status"] != "pending" or old["status"] not in {"draft", "ready"}:
                raise StorageRevisionConflict("pending draft changed after Preview")
            connection.execute("""UPDATE transaction_drafts SET status = 'ignored',
                row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                (now, old["id"]))
            replaced += 1
        for row in rows:
            occurred_on = _iso_date(str(row.get("occurred_on", "")), "occurred_on")
            flow_direction = str(row.get("flow_direction", "")).lower()
            if flow_direction not in _DIRECTIONS:
                raise ValueError("flow direction must be income or expense")
            category_id = str(row.get("category_id", ""))
            currency = str(row.get("currency", "")).upper()
            amount_minor = _minor_units(
                connection, currency, row.get("amount"), allow_zero=False)
            origin_kind = str(row.get("origin_kind", "")).strip()
            origin_key = str(row.get("origin_key", "")).strip()
            bank_status = row.get("bank_status") or None
            status = str(row.get("status") or "draft")
            if not origin_kind or not origin_key:
                raise ValueError("draft origin kind and key are required")
            if bank_status not in {None, "pending", "posted"}:
                raise ValueError("unsupported bank status")
            if status not in {"draft", "ready"}:
                raise ValueError("a new draft must have draft or ready status")
            _require_active_category(connection, category_id, flow_direction)
            values = (
                occurred_on, flow_direction, amount_minor, currency, category_id,
                str(row.get("comment", "")), str(row.get("source_comment", "")),
                row.get("source_record_id"), origin_kind,
                origin_key, bank_status, str(row.get("bank_reference", "")),
                str(row.get("bank_account_id", "")), status,
            )
            existing = connection.execute("""SELECT id, occurred_on, flow_direction,
                amount_minor, currency_code, category_id, comment, source_comment, source_record_id,
                origin_kind, origin_key, bank_status, bank_reference, bank_account_id, status
                FROM transaction_drafts WHERE origin_kind = ? AND origin_key = ?""",
                (origin_kind, origin_key)).fetchone()
            if existing is not None:
                if tuple(existing)[1:-1] != values[:-1]:
                    raise ValueError("draft origin key was reused with another payload")
                skipped += 1
                continue
            draft_id = str(row.get("draft_id") or uuid4().hex)
            connection.execute("""INSERT INTO transaction_drafts
                (id, occurred_on, draft_kind, domain_action, flow_direction, amount_minor,
                 currency_code, category_id, comment, source_comment, source_record_id,
                 origin_kind, origin_key,
                 bank_status, bank_reference, bank_account_id, status, created_at, updated_at)
                VALUES (?, ?, 'cash', NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (draft_id, *values, now, now))
            accepted += 1
        revision = _draft_revision(connection)
        return {"accepted_rows": accepted, "skipped_rows": skipped,
                "replaced_pending_rows": replaced,
                "revision": revision}


def update_cash_drafts(path: str | Path, *, rows: list[dict],
                       expected_revision: str) -> str:
    """Apply editable Preview fields with snapshot and row-version checks."""
    if len({str(row.get("id", "")) for row in rows}) != len(rows):
        raise ValueError("draft update contains duplicate IDs")
    with connect_database(path, writable=True) as connection:
        _require_draft_revision(connection, expected_revision)
        now = _utc_now()
        for row in rows:
            draft_id = str(row.get("id", ""))
            current = connection.execute(
                "SELECT * FROM transaction_drafts WHERE id = ?", (draft_id,)).fetchone()
            if current is None:
                raise ValueError("unknown transaction draft")
            if current["draft_kind"] != "cash":
                raise ValueError("domain draft cannot be edited as cash")
            if current["status"] in {"exported", "archived", "ignored"}:
                raise ValueError("published or removed draft cannot be edited")
            if int(row.get("row_version", -1)) != current["row_version"]:
                raise StorageRevisionConflict("transaction draft row changed after Preview")
            occurred_on = _iso_date(str(row.get("occurred_on", "")), "occurred_on")
            flow_direction = str(row.get("flow_direction", "")).lower()
            if flow_direction not in _DIRECTIONS:
                raise ValueError("flow direction must be income or expense")
            currency = str(row.get("currency", "")).upper()
            amount_minor = _minor_units(
                connection, currency, row.get("amount"), allow_zero=False)
            status = str(row.get("status") or "draft")
            if status not in {"draft", "ready"}:
                raise ValueError("editable draft status must be draft or ready")
            connection.execute("""UPDATE transaction_drafts SET occurred_on = ?,
                flow_direction = ?, amount_minor = ?, currency_code = ?, category_id = ?,
                comment = ?, status = ?, row_version = row_version + 1, updated_at = ?
                WHERE id = ?""",
                (occurred_on, flow_direction, amount_minor, currency,
                 str(row.get("category_id", "")), str(row.get("comment", "")),
                 status, now, draft_id))
        return _draft_revision(connection)


def remove_transaction_drafts(path: str | Path, *, draft_ids: list[str],
                              expected_revision: str) -> str:
    """Hide drafts without deleting their source identity or retry history."""
    requested = sorted(set(draft_ids))
    if len(requested) != len(draft_ids):
        raise ValueError("draft IDs must be unique")
    with connect_database(path, writable=True) as connection:
        _require_draft_revision(connection, expected_revision)
        now = _utc_now()
        for draft_id in requested:
            current = connection.execute(
                "SELECT status FROM transaction_drafts WHERE id = ?", (draft_id,)).fetchone()
            if current is None:
                raise ValueError("unknown transaction draft")
            status = "archived" if current["status"] in {"exported", "archived"} else "ignored"
            connection.execute("""UPDATE transaction_drafts SET status = ?,
                row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                (status, now, draft_id))
        return _draft_revision(connection)


def _draft_revision(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT id, row_version, status FROM transaction_drafts ORDER BY id").fetchall()
    payload = [tuple(row) for row in rows]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _require_draft_revision(connection: sqlite3.Connection,
                            expected_revision: str | None) -> None:
    if expected_revision is not None and _draft_revision(connection) != expected_revision:
        raise StorageRevisionConflict("transaction drafts changed after Preview")


def publish_transaction_draft_preview(
    path: str | Path,
    *,
    rows: list[dict],
    draft_ids: list[str],
    expected_revision: str,
    operation_key: str,
) -> dict:
    """Atomically apply Preview edits and publish cash and domain drafts."""
    requested_ids = sorted(set(draft_ids))
    if not requested_ids or len(requested_ids) != len(draft_ids):
        raise ValueError("draft IDs must be non-empty and unique")
    if not operation_key.strip():
        raise ValueError("operation key is required")
    if sorted(str(row.get("id", "")) for row in rows) != requested_ids:
        raise ValueError("Preview rows must match the requested draft set")
    request_rows = sorted(
        ({key: str(value) for key, value in row.items()} for row in rows),
        key=lambda row: row["id"],
    )
    request_hash = hashlib.sha256(json.dumps(
        {"draft_ids": requested_ids, "rows": request_rows},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    debt_actions = {
        "receivable_opening": ("issue", "receivable"),
        "receivable_payment": ("repayment", "receivable"),
        "liability_opening": ("issue", "liability"),
        "liability_payment": ("repayment", "liability"),
    }
    investment_actions = {
        "investment_contribution": "contribution",
        "investment_withdrawal": "withdrawal",
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if (receipt["operation_kind"] != "publish_transaction_draft_preview"
                    or payload.get("request_hash") != request_hash):
                raise ValueError("operation key was reused with another command or Preview")
            return payload["result"]

        _require_draft_revision(connection, expected_revision)
        placeholders = ",".join("?" for _ in requested_ids)
        stored_rows = connection.execute(f"""SELECT * FROM transaction_drafts
            WHERE id IN ({placeholders}) ORDER BY id""", requested_ids).fetchall()
        if len(stored_rows) != len(requested_ids):
            raise ValueError("one or more drafts do not exist")
        stored_by_id = {row["id"]: row for row in stored_rows}
        now = _utc_now()
        for row in rows:
            draft_id = str(row["id"])
            stored = stored_by_id[draft_id]
            if stored["status"] not in {"draft", "ready"}:
                raise ValueError("only draft or ready rows can be published")
            if int(row.get("row_version", -1)) != stored["row_version"]:
                raise StorageRevisionConflict("transaction draft row changed after Preview")
            occurred_on = _iso_date(str(row.get("occurred_on", "")), "occurred_on")
            currency = str(row.get("currency", "")).upper()
            amount_minor = _minor_units(
                connection, currency, row.get("amount"), allow_zero=False)
            status = str(row.get("status") or "draft")
            if status not in {"draft", "ready"}:
                raise ValueError("editable draft status must be draft or ready")
            bank_status = row.get("bank_status") or None
            if bank_status not in {None, "pending", "posted"}:
                raise ValueError("unsupported bank status")
            common = (
                occurred_on, amount_minor, currency, str(row.get("comment", "")),
                bank_status, str(row.get("bank_reference", "")),
                str(row.get("bank_account_id", "")), status, now, draft_id,
            )
            if stored["draft_kind"] == "cash":
                direction = str(row.get("flow_direction", "")).lower()
                category_id = str(row.get("category_id", ""))
                if direction not in _DIRECTIONS:
                    raise ValueError("flow direction must be income or expense")
                _require_active_category(connection, category_id, direction)
                connection.execute("""UPDATE transaction_drafts SET occurred_on = ?,
                    amount_minor = ?, currency_code = ?, comment = ?, bank_status = ?,
                    bank_reference = ?, bank_account_id = ?, status = ?, updated_at = ?,
                    flow_direction = ?, category_id = ?, row_version = row_version + 1
                    WHERE id = ?""", (*common[:-1], direction, category_id, draft_id))
            else:
                action = str(row.get("domain_action", ""))
                allowed = debt_actions if stored["draft_kind"] == "debt" else investment_actions
                if action not in allowed:
                    raise ValueError(f"unsupported {stored['draft_kind']} draft action")
                if (occurred_on != stored["occurred_on"]
                        or amount_minor != stored["amount_minor"]
                        or currency != stored["currency_code"]):
                    raise ValueError("domain draft amount, currency and date are immutable")
                if (stored["domain_action"] != "cash_movement"
                        and action != stored["domain_action"]):
                    raise ValueError("domain draft action is immutable")
                connection.execute("""UPDATE transaction_drafts SET occurred_on = ?,
                    amount_minor = ?, currency_code = ?, comment = ?, bank_status = ?,
                    bank_reference = ?, bank_account_id = ?, status = ?, updated_at = ?,
                    domain_action = ?, row_version = row_version + 1 WHERE id = ?""",
                    (*common[:-1], action, draft_id))

        drafts = connection.execute(f"""SELECT * FROM transaction_drafts
            WHERE id IN ({placeholders}) ORDER BY id""", requested_ids).fetchall()
        if any(row["bank_status"] == "pending" for row in drafts):
            raise ValueError("pending bank rows cannot be published")
        transaction_ids = []
        entity_ids = []
        for draft in drafts:
            if draft["draft_kind"] == "cash":
                entity_id = hashlib.sha256(
                    f"cash-transaction\0{draft['id']}".encode()).hexdigest()[:32]
                connection.execute("""INSERT INTO cash_transactions
                    (id, occurred_on, flow_direction, amount_minor, currency_code, category_id,
                     comment, source_comment, classification_method, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user', ?, ?)""",
                    (entity_id, draft["occurred_on"], draft["flow_direction"],
                     draft["amount_minor"], draft["currency_code"], draft["category_id"],
                     draft["comment"], draft["source_comment"], now, now))
                if draft["source_record_id"]:
                    connection.execute(
                        "INSERT INTO transaction_source_links VALUES (?, ?, 'original', NULL)",
                        (entity_id, draft["source_record_id"]))
                _mark_period(connection, draft["occurred_on"][:7], "cash_transactions")
                transaction_ids.append(entity_id)
            else:
                entity_id = hashlib.sha256(
                    f"domain-cash-event\0{draft['id']}".encode()).hexdigest()[:32]
                if draft["draft_kind"] == "debt":
                    event_kind, side = debt_actions[draft["domain_action"]]
                    connection.execute("""INSERT INTO debt_cash_events
                        (id, occurred_on, event_kind, side, amount_minor,
                         currency_code, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (entity_id, draft["occurred_on"], event_kind, side,
                         draft["amount_minor"], draft["currency_code"], draft["comment"], now))
                    entity_type = "debt_cash_event"
                else:
                    connection.execute("""INSERT INTO investment_cash_events
                        (id, occurred_on, flow_kind, amount_minor,
                         currency_code, comment, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (entity_id, draft["occurred_on"],
                         investment_actions[draft["domain_action"]], draft["amount_minor"],
                         draft["currency_code"], draft["comment"], now))
                    entity_type = "investment_cash_event"
                if draft["source_record_id"]:
                    connection.execute(
                        "INSERT INTO entity_source_links VALUES (?, ?, ?, 'original')",
                        (entity_type, entity_id, draft["source_record_id"]))
                entity_ids.append(entity_id)
            connection.execute("""UPDATE transaction_drafts SET status = 'exported',
                row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                (now, draft["id"]))

        result = {
            "draft_ids": requested_ids,
            "transaction_ids": transaction_ids,
            "entity_ids": entity_ids,
            "published_rows": len(requested_ids),
        }
        payload = {"request_hash": request_hash, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'publish_transaction_draft_preview',
             'transaction_draft_batch', ?, ?, ?)""",
            (operation_key, operation_key, json.dumps(payload, sort_keys=True), now))
        return result


def publish_cash_drafts(path: str | Path, *, draft_ids: list[str], operation_key: str) -> dict:
    """Atomically publish drafts, their source links, statuses and retry receipt."""
    requested_ids = sorted(set(draft_ids))
    if not requested_ids or len(requested_ids) != len(draft_ids):
        raise ValueError("draft IDs must be non-empty and unique")
    if not operation_key.strip():
        raise ValueError("operation key is required")
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            if receipt["operation_kind"] != "publish_cash_drafts":
                raise ValueError("operation key was already used for another command")
            result = json.loads(receipt["result_json"])
            if result.get("draft_ids") != requested_ids:
                raise ValueError("operation key was reused with another draft set")
            return result

        placeholders = ",".join("?" for _ in requested_ids)
        drafts = connection.execute(f"""SELECT * FROM transaction_drafts
            WHERE id IN ({placeholders}) ORDER BY id""", requested_ids).fetchall()
        if len(drafts) != len(requested_ids):
            raise ValueError("one or more drafts do not exist")
        if any(row["draft_kind"] != "cash" for row in drafts):
            raise ValueError("only cash drafts can be published as cash transactions")
        if any(row["status"] not in {"draft", "ready"} for row in drafts):
            raise ValueError("only draft or ready rows can be published")
        if any(row["bank_status"] == "pending" for row in drafts):
            raise ValueError("pending bank rows cannot be published")

        now = _utc_now()
        transaction_ids = []
        for draft in drafts:
            transaction_id = hashlib.sha256(
                f"cash-transaction\0{draft['id']}".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO cash_transactions
                (id, occurred_on, flow_direction, amount_minor, currency_code, category_id,
                 comment, source_comment, classification_method, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'user', ?, ?)""",
                (transaction_id, draft["occurred_on"], draft["flow_direction"],
                 draft["amount_minor"], draft["currency_code"], draft["category_id"],
                 draft["comment"], draft["source_comment"], now, now))
            if draft["source_record_id"]:
                connection.execute("INSERT INTO transaction_source_links VALUES (?, ?, 'original', NULL)",
                                   (transaction_id, draft["source_record_id"]))
            connection.execute("""UPDATE transaction_drafts SET status = 'exported',
                row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                (now, draft["id"]))
            _mark_period(connection, draft["occurred_on"][:7], "cash_transactions")
            transaction_ids.append(transaction_id)

        result = {
            "draft_ids": requested_ids,
            "transaction_ids": transaction_ids,
            "published_rows": len(transaction_ids),
        }
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'publish_cash_drafts',
             'cash_transaction_batch', ?, ?, ?)""",
            (operation_key, operation_key, json.dumps(result, sort_keys=True), now))
        return result


def publish_domain_drafts(path: str | Path, *, draft_ids: list[str],
                          operation_key: str) -> dict:
    """Publish debt/investment cash movements without treating them as income or expense."""
    requested_ids = sorted(set(draft_ids))
    if not requested_ids or len(requested_ids) != len(draft_ids):
        raise ValueError("draft IDs must be non-empty and unique")
    if not operation_key.strip():
        raise ValueError("operation key is required")
    debt_actions = {
        "receivable_opening": ("issue", "receivable"),
        "receivable_payment": ("repayment", "receivable"),
        "liability_opening": ("issue", "liability"),
        "liability_payment": ("repayment", "liability"),
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            if receipt["operation_kind"] != "publish_domain_drafts":
                raise ValueError("operation key was already used for another command")
            result = json.loads(receipt["result_json"])
            if result.get("draft_ids") != requested_ids:
                raise ValueError("operation key was reused with another draft set")
            return result
        placeholders = ",".join("?" for _ in requested_ids)
        drafts = connection.execute(f"""SELECT * FROM transaction_drafts
            WHERE id IN ({placeholders}) ORDER BY id""", requested_ids).fetchall()
        if len(drafts) != len(requested_ids):
            raise ValueError("one or more drafts do not exist")
        if any(row["draft_kind"] == "cash" for row in drafts):
            raise ValueError("cash drafts must use the cash publisher")
        if any(row["status"] not in {"draft", "ready"} for row in drafts):
            raise ValueError("only draft or ready rows can be published")
        if any(row["bank_status"] == "pending" for row in drafts):
            raise ValueError("pending bank rows cannot be published")
        now = _utc_now()
        entity_ids = []
        for draft in drafts:
            entity_id = hashlib.sha256(
                f"domain-cash-event\0{draft['id']}".encode()).hexdigest()[:32]
            if draft["draft_kind"] == "debt":
                action = debt_actions.get(draft["domain_action"])
                if action is None:
                    raise ValueError("unsupported debt draft action")
                event_kind, side = action
                connection.execute("""INSERT INTO debt_cash_events
                    (id, occurred_on, event_kind, side, amount_minor,
                     currency_code, comment, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (entity_id, draft["occurred_on"], event_kind, side,
                     draft["amount_minor"], draft["currency_code"], draft["comment"], now))
                entity_type = "debt_cash_event"
            elif draft["draft_kind"] == "investment":
                flow_kind = {
                    "investment_contribution": "contribution",
                    "investment_withdrawal": "withdrawal",
                }.get(draft["domain_action"])
                if flow_kind is None:
                    raise ValueError("investment cash movement needs contribution or withdrawal")
                connection.execute("""INSERT INTO investment_cash_events
                    (id, occurred_on, flow_kind, amount_minor, currency_code, comment, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (entity_id, draft["occurred_on"], flow_kind, draft["amount_minor"],
                     draft["currency_code"], draft["comment"], now))
                entity_type = "investment_cash_event"
            else:
                raise ValueError("unsupported domain draft kind")
            if draft["source_record_id"]:
                connection.execute("INSERT INTO entity_source_links VALUES (?, ?, ?, 'original')",
                                   (entity_type, entity_id, draft["source_record_id"]))
            connection.execute("""UPDATE transaction_drafts SET status = 'exported',
                row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                (now, draft["id"]))
            entity_ids.append(entity_id)
        result = {"draft_ids": requested_ids, "entity_ids": entity_ids,
                  "published_rows": len(entity_ids)}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'publish_domain_drafts',
             'domain_cash_event_batch', ?, ?, ?)""",
            (operation_key, operation_key, json.dumps(result, sort_keys=True), now))
        return result


def cash_transactions(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute(
            """SELECT v.*, c.minor_unit FROM v_cash_transactions v
            JOIN currencies c ON c.code = v.currency_code ORDER BY v.occurred_on, v.id""").fetchall()
    return [{**dict(row), "amount": _amount(row["amount_minor"], row["minor_unit"])} for row in rows]


def _audit(connection, entity_id, action, before, after, reason) -> None:
    connection.execute(
        """INSERT INTO audit_events
          (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
          VALUES ('cash_transaction', ?, ?, ?, ?, ?, ?)""",
        (entity_id, action, json.dumps(dict(before), ensure_ascii=False, sort_keys=True),
         json.dumps(dict(after), ensure_ascii=False, sort_keys=True), reason.strip(), _utc_now()),
    )


def change_transaction_category(path: str | Path, transaction_id: str,
                                category_id: str, *, reason: str) -> None:
    if not reason.strip():
        raise ValueError("change reason is required")
    with connect_database(path, writable=True) as connection:
        before = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        if before is None:
            raise ValueError("unknown transaction")
        connection.execute("""UPDATE cash_transactions SET category_id = ?, row_version = row_version + 1,
                           updated_at = ? WHERE id = ?""", (category_id, _utc_now(), transaction_id))
        after = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        _audit(connection, transaction_id, "category_changed", before, after, reason)


def void_cash_transaction(path: str | Path, transaction_id: str, *, reason: str) -> None:
    if not reason.strip():
        raise ValueError("void reason is required")
    with connect_database(path, writable=True) as connection:
        before = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        if before is None or before["status"] != "posted":
            raise ValueError("only a posted transaction can be voided")
        now = _utc_now()
        connection.execute("""UPDATE cash_transactions SET status = 'void', voided_at = ?, void_reason = ?,
                           row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                           (now, reason.strip(), now, transaction_id))
        after = connection.execute("SELECT * FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        _audit(connection, transaction_id, "voided", before, after, reason)


def add_asset_account(path: str | Path, account_id: str, name: str, *,
                      asset_type_id: str | None = None,
                      include_in_capital: bool = True) -> None:
    if not account_id or not name.strip():
        raise ValueError("account ID and name are required")
    if not asset_type_id:
        raise ValueError("asset type is required for a new account")
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        if not connection.execute(
            "SELECT 1 FROM asset_types WHERE id = ? AND active = 1", (asset_type_id,)
        ).fetchone():
            raise ValueError("asset type must be active")
        connection.execute(
            """INSERT INTO asset_accounts
              (id, name, active, created_at, updated_at, asset_type_id, include_in_capital)
              VALUES (?, ?, 1, ?, ?, ?, ?)""",
            (account_id, name.strip(), now, now, asset_type_id,
             int(bool(include_in_capital))),
        )


def asset_types(path: str | Path, *, include_inactive: bool = False) -> list[dict]:
    query = "SELECT * FROM asset_types"
    if not include_inactive:
        query += " WHERE active = 1"
    query += " ORDER BY sort_order, name_ru, id"
    with connect_database(path) as connection:
        return [dict(row) for row in connection.execute(query).fetchall()]


def liquidity_classes(path: str | Path, *, include_inactive: bool = False) -> list[dict]:
    query = "SELECT * FROM liquidity_classes"
    if not include_inactive:
        query += " WHERE active = 1"
    query += " ORDER BY sort_order, id"
    with connect_database(path) as connection:
        return [dict(row) for row in connection.execute(query).fetchall()]


def asset_accounts(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT a.id, a.name, a.active, a.asset_type_id,
            t.name_ru AS asset_type_name_ru, t.name_en AS asset_type_name_en,
            a.liquidity_class_override_id,
            COALESCE(a.liquidity_class_override_id, d.liquidity_class_id) AS liquidity_class_id,
            CASE WHEN a.liquidity_class_override_id IS NOT NULL THEN 'manual'
              WHEN d.liquidity_class_id IS NOT NULL THEN 'suggested'
              ELSE 'unclassified' END AS liquidity_source,
            a.include_in_capital, a.closed_period, a.created_at, a.updated_at,
            COUNT(s.id) AS snapshot_count, MIN(s.period) AS first_period,
            MAX(s.period) AS last_period
            FROM asset_accounts a LEFT JOIN asset_types t ON t.id = a.asset_type_id
            LEFT JOIN asset_type_liquidity_defaults d ON d.asset_type_id = a.asset_type_id
            LEFT JOIN asset_snapshots s ON s.account_id = a.id
            GROUP BY a.id, a.name, a.active, a.asset_type_id, t.name_ru, t.name_en,
              a.liquidity_class_override_id, d.liquidity_class_id,
              a.include_in_capital, a.closed_period, a.created_at, a.updated_at
            ORDER BY a.name, a.id""").fetchall()
    return [dict(row) for row in rows]


def set_asset_account_classification(path: str | Path, account_id: str, *,
                                     asset_type_id: str | None,
                                     include_in_capital: bool,
                                     reason: str,
                                     liquidity_class_override_id: str | None = None) -> None:
    set_asset_account_classifications(
        path,
        [{
            "account_id": account_id,
            "asset_type_id": asset_type_id,
            "liquidity_class_override_id": liquidity_class_override_id,
            "include_in_capital": include_in_capital,
        }],
        reason=reason,
    )


def set_asset_account_classifications(path: str | Path, rows: list[dict], *,
                                      reason: str) -> dict:
    if not reason.strip():
        raise ValueError("change reason is required")
    normalized = []
    seen = set()
    for row in rows:
        account_id = str(row.get("account_id", "")).strip()
        asset_type_id = str(row.get("asset_type_id") or "").strip() or None
        liquidity_override = str(
            row.get("liquidity_class_override_id") or "").strip() or None
        include_in_capital = row.get("include_in_capital")
        active = row.get("active")
        closed_period = str(row.get("closed_period") or "").strip() or None
        if not account_id:
            raise ValueError("asset account ID is required")
        if account_id in seen:
            raise ValueError("asset account classification contains a duplicate account")
        if not isinstance(include_in_capital, bool):
            raise ValueError("include_in_capital must be boolean")
        if active is not None and not isinstance(active, bool):
            raise ValueError("active must be boolean")
        if active is True and closed_period is not None:
            raise ValueError("active asset account cannot have a closed period")
        if active is False and closed_period is None:
            raise ValueError("closed period is required for an archived asset account")
        if active is None and closed_period is not None:
            raise ValueError("active is required when closed period is supplied")
        if closed_period is not None:
            _period(closed_period)
        if liquidity_override is not None:
            raise ValueError("liquidity is determined by asset type")
        seen.add(account_id)
        normalized.append({
            "account_id": account_id,
            "asset_type_id": asset_type_id,
            "liquidity_override": liquidity_override,
            "include_in_capital": include_in_capital,
            "active": active,
            "closed_period": closed_period,
        })

    updated = 0
    with connect_database(path, writable=True) as connection:
        active_types = {
            row["id"] for row in connection.execute(
                "SELECT id FROM asset_types WHERE active = 1").fetchall()
        }
        active_liquidity_classes = {
            row["id"] for row in connection.execute(
                "SELECT id FROM liquidity_classes WHERE active = 1").fetchall()
        }
        current = {}
        for item in normalized:
            account_id = item["account_id"]
            asset_type_id = item["asset_type_id"]
            liquidity_override = item["liquidity_override"]
            account = connection.execute(
                "SELECT * FROM asset_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if account is None:
                raise ValueError("unknown asset account")
            if asset_type_id is not None and asset_type_id not in active_types:
                raise ValueError("asset type must be active")
            if (liquidity_override is not None
                    and liquidity_override not in active_liquidity_classes):
                raise ValueError("liquidity class must be active")
            current[account_id] = account

        now = _utc_now()
        for item in normalized:
            account_id = item["account_id"]
            asset_type_id = item["asset_type_id"]
            liquidity_override = item["liquidity_override"]
            include_in_capital = item["include_in_capital"]
            before = current[account_id]
            included = int(include_in_capital)
            active = before["active"] if item["active"] is None else int(item["active"])
            if active and asset_type_id is None and before["asset_type_id"] is not None:
                raise ValueError(f"asset type is required: {before['name']}")
            closed_period = (
                before["closed_period"] if item["active"] is None
                else item["closed_period"]
            )
            last_snapshot = connection.execute(
                "SELECT MAX(period) FROM asset_snapshots WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
            if closed_period is not None and last_snapshot is not None and closed_period < last_snapshot:
                raise ValueError(
                    f"closed period cannot precede the last snapshot ({last_snapshot})"
                )
            if not active:
                _require_zero_closing_balances(connection, account_id, closed_period)
            if (before["asset_type_id"], before["liquidity_class_override_id"],
                    before["include_in_capital"], before["active"],
                    before["closed_period"]) == (
                    asset_type_id, liquidity_override, included, active, closed_period):
                continue
            connection.execute(
                """UPDATE asset_accounts SET asset_type_id = ?,
                  liquidity_class_override_id = ?, include_in_capital = ?,
                  active = ?, closed_period = ?, updated_at = ? WHERE id = ?""",
                (asset_type_id, liquidity_override, included, active, closed_period,
                 now, account_id),
            )
            after = connection.execute(
                "SELECT * FROM asset_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            action = "classification_changed"
            if before["active"] and not active:
                action = "archived"
            elif not before["active"] and active:
                action = "reopened"
            connection.execute("""INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_account', ?, ?, ?, ?, ?, ?)""",
                (account_id, action,
                 json.dumps(dict(before), ensure_ascii=False, sort_keys=True),
                 json.dumps(dict(after), ensure_ascii=False, sort_keys=True),
                 reason.strip(), now))
            updated += 1
    return {"submitted": len(normalized), "updated": updated}


def archive_asset_accounts(path: str | Path, account_names: list[str], *,
                           period: str) -> dict:
    """Archive selected accounts from the supplied month onward."""
    period = _period(period)
    names = list(dict.fromkeys(
        str(name).strip() for name in account_names if str(name).strip()
    ))
    if not names:
        raise ValueError("select asset accounts to archive")

    archived = 0
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        for name in names:
            account = connection.execute(
                "SELECT * FROM asset_accounts WHERE name = ?", (name,)
            ).fetchone()
            if account is None:
                raise ValueError(f"unknown asset account: {name}")
            last_period = connection.execute(
                "SELECT MAX(period) FROM asset_snapshots WHERE account_id = ?",
                (account["id"],),
            ).fetchone()[0]
            if last_period is not None and period < last_period:
                raise ValueError(
                    f"archive month cannot precede the last snapshot ({last_period}): {name}"
                )
            if not account["active"] and account["closed_period"] == period:
                continue
            if not account["active"]:
                raise ValueError(f"asset account is already archived: {name}")
            _require_zero_closing_balances(connection, account["id"], period)
            connection.execute(
                """UPDATE asset_accounts SET active = 0, closed_period = ?,
                updated_at = ? WHERE id = ?""",
                (period, now, account["id"]),
            )
            after = connection.execute(
                "SELECT * FROM asset_accounts WHERE id = ?", (account["id"],)
            ).fetchone()
            connection.execute(
                """INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_account', ?, 'archived', ?, ?,
                'archived from asset snapshot editor', ?)""",
                (
                    account["id"],
                    json.dumps(dict(account), ensure_ascii=False, sort_keys=True),
                    json.dumps(dict(after), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            archived += 1
    return {"submitted": len(names), "archived": archived, "period": period}


def _require_zero_closing_balances(
        connection: sqlite3.Connection, account_id: str, period: str | None) -> None:
    if period is None:
        raise ValueError("closed period is required")
    rows = connection.execute("""SELECT currency_code,
        MAX(CASE WHEN period = ? AND amount_minor = 0 THEN 1 ELSE 0 END) AS zero_this_month
        FROM asset_snapshots WHERE account_id = ? AND period <= ?
        GROUP BY currency_code""", (period, account_id, period)).fetchall()
    missing = [row["currency_code"] for row in rows if not row["zero_this_month"]]
    if missing:
        raise ValueError(
            f"save a zero balance for {period} before archiving: {', '.join(missing)}"
        )


def restore_asset_accounts_to_snapshot(path: str | Path, account_ids: list[str], *,
                                       period: str) -> dict:
    """Reopen archived accounts and copy their latest values into one snapshot."""
    period = _period(period)
    ids = list(dict.fromkeys(
        str(account_id).strip() for account_id in account_ids
        if str(account_id).strip()
    ))
    if not ids:
        raise ValueError("select archived asset accounts to restore")

    reopened = inserted = existing = 0
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        for account_id in ids:
            account = connection.execute(
                "SELECT * FROM asset_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if account is None:
                raise ValueError("unknown asset account")
            if account["active"]:
                raise ValueError(f"asset account is not archived: {account['name']}")
            latest_period = connection.execute(
                "SELECT MAX(period) FROM asset_snapshots WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
            if latest_period is None:
                raise ValueError(
                    f"asset account has no valuation to restore: {account['name']}"
                )
            if period < latest_period:
                raise ValueError(
                    f"restore month cannot precede the latest snapshot ({latest_period}): "
                    f"{account['name']}"
                )
            latest_rows = connection.execute(
                """SELECT * FROM asset_snapshots
                WHERE account_id = ? AND period = ? ORDER BY currency_code""",
                (account_id, latest_period),
            ).fetchall()
            connection.execute(
                """UPDATE asset_accounts SET active = 1, closed_period = NULL,
                updated_at = ? WHERE id = ?""",
                (now, account_id),
            )
            for snapshot in latest_rows:
                current = connection.execute(
                    """SELECT id FROM asset_snapshots
                    WHERE account_id = ? AND period = ? AND currency_code = ?""",
                    (account_id, period, snapshot["currency_code"]),
                ).fetchone()
                if current is not None:
                    existing += 1
                    continue
                snapshot_id = hashlib.sha256(
                    f"asset-snapshot\0{account_id}\0{period}\0{snapshot['currency_code']}".encode()
                ).hexdigest()[:32]
                connection.execute(
                    """INSERT INTO asset_snapshots
                    (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        snapshot_id,
                        account_id,
                        period,
                        snapshot["currency_code"],
                        snapshot["amount_minor"],
                        now,
                        now,
                    ),
                )
                inserted += 1
            after = connection.execute(
                "SELECT * FROM asset_accounts WHERE id = ?", (account_id,)
            ).fetchone()
            connection.execute(
                """INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_account', ?, 'reopened', ?, ?,
                'restored to selected asset snapshot', ?)""",
                (
                    account_id,
                    json.dumps(dict(account), ensure_ascii=False, sort_keys=True),
                    json.dumps(dict(after), ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            reopened += 1
        _mark_period(connection, period, "asset_snapshots")
    return {
        "submitted": len(ids),
        "reopened": reopened,
        "inserted": inserted,
        "existing": existing,
        "period": period,
    }


def add_asset_snapshot(path: str | Path, *, snapshot_id: str, account_id: str,
                       period: str, amount, currency: str) -> None:
    if not snapshot_id:
        raise ValueError("snapshot ID is required")
    period = _period(period)
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        amount_minor = _minor_units(connection, currency, amount, allow_zero=True)
        account = connection.execute(
            "SELECT closed_period FROM asset_accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is not None and account["closed_period"] == period and amount_minor != 0:
            raise ValueError("closing month balance must be zero")
        connection.execute(
            """INSERT INTO asset_snapshots
              (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
              VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, account_id, period, currency.upper(), amount_minor, now, now),
        )
        _mark_period(connection, period, "asset_snapshots")


def _upsert_asset_snapshot(connection: sqlite3.Connection, *, account_id: str,
                           period: str, amount, currency: str, reason: str) -> dict:
    if not account_id:
        raise ValueError("asset account is required")
    if not reason.strip():
        raise ValueError("change reason is required")
    period = _period(period)
    currency = currency.upper()
    now = _utc_now()
    account = connection.execute(
        "SELECT id, name, active FROM asset_accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None:
        raise ValueError("unknown asset account")
    if not account["active"]:
        raise ValueError("asset account is archived")
    amount_minor = _minor_units(connection, currency, amount, allow_zero=True)
    current = connection.execute(
        """SELECT * FROM asset_snapshots
            WHERE account_id = ? AND period = ? AND currency_code = ?""",
        (account_id, period, currency),
    ).fetchone()
    if current is None:
        snapshot_id = hashlib.sha256(
            f"asset-snapshot\0{account_id}\0{period}\0{currency}".encode()
        ).hexdigest()[:32]
        connection.execute(
            """INSERT INTO asset_snapshots
                (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (snapshot_id, account_id, period, currency, amount_minor, now, now),
        )
        action = "inserted"
    elif current["amount_minor"] == amount_minor:
        snapshot_id = current["id"]
        action = "unchanged"
    else:
        snapshot_id = current["id"]
        connection.execute(
            """UPDATE asset_snapshots SET amount_minor = ?,
                row_version = row_version + 1, updated_at = ? WHERE id = ?""",
            (amount_minor, now, snapshot_id),
        )
        after = connection.execute(
            "SELECT * FROM asset_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        connection.execute(
            """INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_snapshot', ?, 'amount_changed', ?, ?, ?, ?)""",
            (
                snapshot_id,
                json.dumps(dict(current), ensure_ascii=False, sort_keys=True),
                json.dumps(dict(after), ensure_ascii=False, sort_keys=True),
                reason.strip(),
                now,
            ),
        )
        action = "updated"
    _mark_period(connection, period, "asset_snapshots")
    return {
        "action": action,
        "snapshot_id": snapshot_id,
        "account": account["name"],
        "period": period,
        "currency": currency,
    }


def upsert_asset_snapshot(path: str | Path, *, account_id: str, period: str,
                          amount, currency: str, reason: str) -> dict:
    """Set one account balance without replacing the rest of the month."""
    with connect_database(path, writable=True) as connection:
        return _upsert_asset_snapshot(
            connection, account_id=account_id, period=period,
            amount=amount, currency=currency, reason=reason,
        )


def upsert_asset_snapshot_batch(path: str | Path, rows: list[dict], *, reason: str) -> list[dict]:
    """Atomically set selected balances without replacing other accounts."""
    with connect_database(path, writable=True) as connection:
        return [
            _upsert_asset_snapshot(connection, reason=reason, **row)
            for row in rows
        ]


def asset_snapshots(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT v.*, c.minor_unit FROM v_asset_snapshots v
            JOIN currencies c ON c.code = v.currency_code ORDER BY v.period, v.id""").fetchall()
    return [{**dict(row), "amount": _amount(row["amount_minor"], row["minor_unit"])} for row in rows]


def effective_asset_snapshot_month(path: str | Path, period: str) -> list[dict]:
    """Latest known balance per account/currency, without writing carried values."""
    period = _period(period)
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT v.*, c.minor_unit FROM v_asset_snapshots v
            JOIN currencies c ON c.code = v.currency_code
            WHERE v.period <= ? AND (v.closed_period IS NULL OR v.closed_period >= ?)
            ORDER BY v.period DESC, v.id DESC""", (period, period)).fetchall()
    latest = {}
    for row in rows:
        key = (row["account_id"], row["currency_code"])
        if key not in latest:
            latest[key] = {
                **dict(row), "source_period": row["period"],
                "period": period, "carried": row["period"] != period,
                "age_months": _asset_balance_age(period, row["period"]),
                "amount": _amount(row["amount_minor"], row["minor_unit"]),
            }
    return sorted(latest.values(), key=lambda row: (row["account_name"], row["currency_code"]))


def effective_asset_snapshot_history(path: str | Path) -> list[dict]:
    """Effective monthly values from the first valuation through the current month."""
    snapshots = asset_snapshots(path)
    if not snapshots:
        return []
    snapshots.sort(key=lambda row: (row["period"], row["id"]))
    period = snapshots[0]["period"]
    end = max(date.today().strftime("%Y-%m"), snapshots[-1]["period"])
    latest = {}
    result = []
    index = 0
    while period <= end:
        while index < len(snapshots) and snapshots[index]["period"] <= period:
            row = snapshots[index]
            latest[(row["account_id"], row["currency_code"])] = row
            index += 1
        for row in latest.values():
            if row["closed_period"] is not None and period > row["closed_period"]:
                continue
            result.append({
                **row, "source_period": row["period"], "period": period,
                "carried": row["period"] != period,
                "age_months": _asset_balance_age(period, row["period"]),
            })
        year, month = map(int, period.split("-"))
        period = f"{year + (month == 12):04d}-{month % 12 + 1:02d}"
    return result


def _asset_balance_age(period: str, source_period: str) -> int:
    return (int(period[:4]) - int(source_period[:4])) * 12 + int(period[5:]) - int(source_period[5:])


def replace_asset_snapshot_month(path: str | Path, *, period: str,
                                 rows: list[dict]) -> dict:
    """Atomically make one period equal to the supplied account/currency rows."""
    period = _period(period)
    normalized = []
    seen = set()
    for row in rows:
        account_name = str(row.get("account", "")).strip()
        currency = str(row.get("currency", "")).strip().upper()
        if not account_name:
            raise ValueError("asset account name is required")
        key = (account_name, currency)
        if key in seen:
            raise ValueError("asset snapshot contains a duplicate account/currency")
        seen.add(key)
        normalized.append((account_name, currency, row.get("amount"), row.get("asset_type_id")))

    now = _utc_now()
    inserted = updated = deleted = 0
    with connect_database(path, writable=True) as connection:
        existing_rows = connection.execute(
            "SELECT * FROM asset_snapshots WHERE period = ?", (period,)).fetchall()
        existing = {(row["account_id"], row["currency_code"]): row for row in existing_rows}
        wanted = set()
        for account_name, currency, amount, asset_type_id in normalized:
            accounts = connection.execute(
                "SELECT id, active, closed_period FROM asset_accounts WHERE name = ? ORDER BY id",
                (account_name,),
            ).fetchall()
            if len(accounts) > 1:
                raise ValueError(
                    f"multiple asset accounts have the same name: {account_name}"
                )
            if accounts:
                account = accounts[0]
                account_id = account["id"]
                if account["closed_period"] is not None and period > account["closed_period"]:
                    raise ValueError(
                        f"asset account is archived after {account['closed_period']}: {account_name}"
                    )
            else:
                if not asset_type_id or not connection.execute(
                    "SELECT 1 FROM asset_types WHERE id = ? AND active = 1",
                    (asset_type_id,),
                ).fetchone():
                    raise ValueError(f"asset type is required for a new account: {account_name}")
                account_id = hashlib.sha256(
                    f"asset-account\0{account_name}".encode()).hexdigest()[:32]
                connection.execute("""INSERT INTO asset_accounts
                    (id, name, active, asset_type_id, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?, ?)""",
                    (account_id, account_name, asset_type_id, now, now))
            key = (account_id, currency)
            wanted.add(key)
            amount_minor = _minor_units(connection, currency, amount, allow_zero=True)
            if accounts and account["closed_period"] == period and amount_minor != 0:
                raise ValueError(f"closing month balance must be zero: {account_name}")
            current = existing.get(key)
            if current is None:
                snapshot_id = hashlib.sha256(
                    f"asset-snapshot\0{account_id}\0{period}\0{currency}".encode()
                ).hexdigest()[:32]
                connection.execute("""INSERT INTO asset_snapshots
                    (id, account_id, period, currency_code, amount_minor, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (snapshot_id, account_id, period, currency, amount_minor, now, now))
                inserted += 1
            elif current["amount_minor"] != amount_minor:
                connection.execute("""UPDATE asset_snapshots SET amount_minor = ?,
                    row_version = row_version + 1, updated_at = ? WHERE id = ?""",
                    (amount_minor, now, current["id"]))
                connection.execute("""INSERT INTO audit_events
                    (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                    VALUES ('asset_snapshot', ?, 'amount_changed', ?, ?,
                    'monthly snapshot replacement', ?)""",
                    (current["id"], json.dumps(dict(current), sort_keys=True),
                     json.dumps({**dict(current), "amount_minor": amount_minor}, sort_keys=True), now))
                updated += 1

        for key, current in existing.items():
            if key in wanted:
                continue
            account = connection.execute(
                "SELECT name, closed_period FROM asset_accounts WHERE id = ?",
                (current["account_id"],),
            ).fetchone()
            if account["closed_period"] == period:
                raise ValueError(f"cannot remove a closing zero balance: {account['name']}")
            if not connection.execute(
                """SELECT 1 FROM asset_snapshots
                WHERE account_id = ? AND currency_code = ? AND period < ? LIMIT 1""",
                (current["account_id"], current["currency_code"], period),
            ).fetchone():
                raise ValueError(f"cannot remove the first valuation: {account['name']}")
            connection.execute("""DELETE FROM entity_source_links
                WHERE entity_type = 'asset_snapshot' AND entity_id = ?""", (current["id"],))
            connection.execute("DELETE FROM asset_snapshots WHERE id = ?", (current["id"],))
            connection.execute("""INSERT INTO audit_events
                (entity_type, entity_id, action, before_json, after_json, reason, occurred_at)
                VALUES ('asset_snapshot', ?, 'removed', ?, NULL,
                'monthly snapshot replacement', ?)""",
                (current["id"], json.dumps(dict(current), sort_keys=True), now))
            deleted += 1
        _mark_period(connection, period, "asset_snapshots")
    return {"inserted": inserted, "updated": updated, "deleted": deleted,
            "rows": len(normalized)}


def asset_snapshot_month(path: str | Path, period: str) -> list[dict]:
    period = _period(period)
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT v.*, c.minor_unit FROM v_asset_snapshots v
            JOIN currencies c ON c.code = v.currency_code WHERE v.period = ?
            ORDER BY v.account_name, v.currency_code""", (period,)).fetchall()
    return [{**dict(row), "amount": _amount(row["amount_minor"], row["minor_unit"])}
            for row in rows]


def upsert_annual_goal(path: str | Path, *, year: int, currency: str,
                       target_capital=None, target_monthly_income=None,
                       target_monthly_expense=None,
                       target_expense_months=None, notes: str = "") -> None:
    if not 1900 <= int(year) <= 9999:
        raise ValueError("goal year is out of range")
    currency = currency.upper()
    with connect_database(path, writable=True) as connection:
        values = [
            None if value is None or str(value).strip() == "" else
            _minor_units(connection, currency, value, allow_zero=True)
            for value in (target_capital, target_monthly_income, target_monthly_expense)
        ]
        if target_expense_months is None or str(target_expense_months).strip() == "":
            expense_months = None
        else:
            parsed_months = parse_money_amount(
                target_expense_months, field_name="target expense months")
            if parsed_months <= 0 or parsed_months != parsed_months.to_integral_value():
                raise ValueError("target expense months must be a positive integer")
            expense_months = int(parsed_months)
        connection.execute("""INSERT INTO annual_goals
            (year, currency_code, target_capital_minor, target_monthly_income_minor,
             target_monthly_expense_minor, target_expense_months, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(year, currency_code) DO UPDATE SET
              target_capital_minor = excluded.target_capital_minor,
              target_monthly_income_minor = excluded.target_monthly_income_minor,
              target_monthly_expense_minor = excluded.target_monthly_expense_minor,
              target_expense_months = excluded.target_expense_months,
              notes = excluded.notes, updated_at = excluded.updated_at""",
            (int(year), currency, *values, expense_months, notes, _utc_now()))


def annual_goals(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT g.*, c.minor_unit FROM annual_goals g
            JOIN currencies c ON c.code = g.currency_code ORDER BY g.year, g.currency_code""").fetchall()
    amount_fields = ("target_capital", "target_monthly_income", "target_monthly_expense")
    result = []
    for row in rows:
        item = dict(row)
        for field in amount_fields:
            minor = item[f"{field}_minor"]
            item[field] = None if minor is None else _amount(minor, item["minor_unit"])
        result.append(item)
    return result


def create_debt_record(path: str | Path, *, kind: str, counterparty: str,
                       opened_on: str, principal_amount, currency: str,
                       operation_key: str, comment: str = "",
                       create_draft: bool = True) -> dict:
    """Atomically create a new same-currency personal debt and optional cash draft."""
    if kind not in {"receivable", "liability"}:
        raise ValueError("debt kind must be receivable or liability")
    if not counterparty.strip() or not operation_key.strip():
        raise ValueError("counterparty and operation key are required")
    opened_on = _iso_date(opened_on, "opened_on")
    currency = currency.upper()
    request = {
        "kind": kind, "counterparty": counterparty.strip(), "opened_on": opened_on,
        "principal_amount": str(parse_money_amount(principal_amount)), "currency": currency,
        "comment": comment, "create_draft": bool(create_draft),
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "create_debt" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        principal_minor = _minor_units(
            connection, currency, principal_amount, allow_zero=False)
        debt_id = f"debt-{uuid4().hex[:12]}"
        now = _utc_now()
        connection.execute("""INSERT INTO debts
            (id, kind, counterparty, opened_on, principal_amount_minor,
             principal_currency_code, cash_amount_minor, cash_currency_code,
             comment, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (debt_id, kind, counterparty.strip(), opened_on, principal_minor,
             currency, principal_minor, currency, comment, now, now))
        draft_id = None
        if create_draft:
            draft_id = hashlib.sha256(f"debt-draft\0{debt_id}\0open".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO transaction_drafts
                (id, occurred_on, draft_kind, domain_action, flow_direction,
                 amount_minor, currency_code, category_id, comment, source_record_id,
                 origin_kind, origin_key, status, created_at, updated_at)
                VALUES (?, ?, 'debt', ?, NULL, ?, ?, NULL, ?, NULL,
                 'debt', ?, 'ready', ?, ?)""",
                (draft_id, opened_on, f"{kind}_opening", principal_minor, currency,
                 comment, f"{debt_id}:open", now, now))
        result = {"debt_id": debt_id, "draft_id": draft_id,
                  "draft_created": bool(create_draft)}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'create_debt', 'debt', ?, ?, ?)""",
            (operation_key, debt_id, json.dumps(payload, sort_keys=True), now))
        return result


def record_debt_payment(path: str | Path, *, debt_id: str, occurred_on: str,
                        amount, operation_key: str, comment: str = "",
                        create_draft: bool = True) -> dict:
    """Atomically record a same-currency payment, forbidding chronological overpayment."""
    if not operation_key.strip():
        raise ValueError("operation key is required")
    occurred_on = _iso_date(occurred_on, "occurred_on")
    normalized_amount = str(parse_money_amount(amount))
    request = {
        "debt_id": debt_id, "occurred_on": occurred_on, "amount": normalized_amount,
        "comment": comment, "create_draft": bool(create_draft),
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "record_debt_payment" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        debt = connection.execute("SELECT * FROM debts WHERE id = ?", (debt_id,)).fetchone()
        if debt is None:
            raise ValueError("unknown debt")
        if occurred_on < debt["opened_on"]:
            raise ValueError("debt payment cannot precede debt opening")
        currency = debt["principal_currency_code"]
        amount_minor = _minor_units(connection, currency, amount, allow_zero=False)
        paid_minor = connection.execute(
            "SELECT COALESCE(SUM(principal_amount_minor), 0) FROM debt_payments WHERE debt_id = ?",
            (debt_id,)).fetchone()[0]
        outstanding_minor = debt["principal_amount_minor"] - paid_minor
        if amount_minor > outstanding_minor:
            raise ValueError("debt payment exceeds outstanding principal")
        if debt["status"] == "closed" or outstanding_minor <= 0:
            raise ValueError("closed debt cannot receive another payment")

        payment_id = f"payment-{uuid4().hex[:12]}"
        now = _utc_now()
        connection.execute("""INSERT INTO debt_payments
            (id, debt_id, occurred_on, principal_amount_minor, cash_amount_minor,
             cash_currency_code, comment, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'posted', ?)""",
            (payment_id, debt_id, occurred_on, amount_minor, amount_minor,
             currency, comment, now))
        remaining_minor = outstanding_minor - amount_minor
        if remaining_minor == 0:
            connection.execute(
                "UPDATE debts SET status = 'closed', updated_at = ? WHERE id = ?",
                (now, debt_id))
        draft_id = None
        if create_draft:
            draft_id = hashlib.sha256(
                f"debt-draft\0{payment_id}\0payment".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO transaction_drafts
                (id, occurred_on, draft_kind, domain_action, flow_direction,
                 amount_minor, currency_code, category_id, comment, source_record_id,
                 origin_kind, origin_key, status, created_at, updated_at)
                VALUES (?, ?, 'debt', ?, NULL, ?, ?, NULL, ?, NULL,
                 'debt', ?, 'ready', ?, ?)""",
                (draft_id, occurred_on, f"{debt['kind']}_payment", amount_minor,
                 currency, comment, f"{payment_id}:payment", now, now))
        result = {"payment_id": payment_id, "debt_id": debt_id,
                  "draft_id": draft_id, "remaining_minor": remaining_minor,
                  "closed": remaining_minor == 0}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'record_debt_payment',
             'debt_payment', ?, ?, ?)""",
            (operation_key, payment_id, json.dumps(payload, sort_keys=True), now))
        return result


def record_investment_trade(
    path: str | Path,
    *,
    occurred_on: str,
    operation: str,
    ticker: str,
    asset_type: str,
    quantity,
    unit_price,
    currency: str,
    fee=0,
    operation_key: str,
    instrument_name: str = "",
    provider: str = "",
    exchange: str = "",
    account_label: str = "",
    comment: str = "",
) -> dict:
    """Atomically create an instrument if needed and record one idempotent trade."""
    occurred_on = _iso_date(occurred_on, "occurred_on")
    operation = operation.lower()
    ticker = ticker.strip().upper()
    asset_type = asset_type.lower()
    currency = currency.upper()
    if operation not in {"buy", "sell"}:
        raise ValueError("investment operation must be buy or sell")
    if asset_type not in {"stocks", "funds", "crypto"}:
        raise ValueError("unsupported investment asset type")
    if not ticker or not operation_key.strip():
        raise ValueError("ticker and operation key are required")
    quantity_text = _positive_decimal_text(quantity, "quantity")
    price_text = _non_negative_decimal_text(unit_price, "unit price")
    fee_value = parse_money_amount(fee, field_name="fee")
    if fee_value < 0:
        raise ValueError("fee must be non-negative")
    request = {
        "occurred_on": occurred_on, "operation": operation, "ticker": ticker,
        "asset_type": asset_type, "quantity": quantity_text, "unit_price": price_text,
        "currency": currency, "fee": format(fee_value, "f"),
        "instrument_name": instrument_name.strip(), "provider": provider.strip(),
        "exchange": exchange.strip(), "account_label": account_label.strip(),
        "comment": comment,
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "record_investment_trade" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        if connection.execute("SELECT 1 FROM currencies WHERE code = ?", (currency,)).fetchone() is None:
            raise ValueError("unsupported currency")
        instrument = connection.execute(
            "SELECT * FROM instruments WHERE ticker = ?", (ticker,)).fetchone()
        instrument_id = hashlib.sha256(f"instrument\0{ticker}".encode()).hexdigest()[:32]
        now = _utc_now()
        if instrument is None:
            connection.execute("""INSERT INTO instruments
                (id, ticker, name, asset_type, quote_currency_code, provider, exchange,
                 active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (instrument_id, ticker, instrument_name.strip() or ticker, asset_type,
                 currency, provider.strip(), exchange.strip(), now, now))
        else:
            instrument_id = instrument["id"]
            if (instrument["asset_type"], instrument["quote_currency_code"]) != (asset_type, currency):
                raise ValueError("ticker is already registered with another type or currency")
        quantity_value = Decimal(quantity_text)
        if operation == "sell":
            rows = connection.execute("""SELECT operation, quantity_text, occurred_on
                FROM investment_trades WHERE instrument_id = ?""", (instrument_id,)).fetchall()
            position_before = sum(
                (Decimal(row["quantity_text"]) * (-1 if row["operation"] == "sell" else 1)
                 for row in rows if row["occurred_on"] <= occurred_on),
                Decimal("0"),
            )
            current_position = sum(
                (Decimal(row["quantity_text"]) * (-1 if row["operation"] == "sell" else 1)
                 for row in rows),
                Decimal("0"),
            )
            if quantity_value > position_before or quantity_value > current_position:
                raise ValueError("sale exceeds the available instrument position")
        fee_minor = _minor_units(connection, currency, fee_value, allow_zero=True)
        trade_id = f"trade-{uuid4().hex[:12]}"
        connection.execute("""INSERT INTO investment_trades
            (id, occurred_on, operation, instrument_id, quantity_text, unit_price_text,
             price_currency_code, fee_minor, account_label, comment, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, occurred_on, operation, instrument_id, quantity_text, price_text,
             currency, fee_minor, account_label.strip(), comment, now))
        result = {"trade_id": trade_id, "instrument_id": instrument_id}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'record_investment_trade',
             'investment_trade', ?, ?, ?)""",
            (operation_key, trade_id, json.dumps(payload, sort_keys=True), now))
        return result


def upsert_crypto_wallet(path: str | Path, *, account_label: str, chain: str,
                         asset_code: str, address: str, token_contract: str = "",
                         label: str = "", enabled: bool = True) -> str:
    """Register public wallet identity; private credentials have no storage field."""
    account_label = account_label.strip()
    chain = chain.strip().lower()
    asset_code = asset_code.strip().upper()
    address = address.strip()
    token_contract = token_contract.strip().lower()
    if not all((account_label, chain, asset_code, address)):
        raise ValueError("wallet account, chain, asset and public address are required")
    wallet_id = hashlib.sha256(
        f"crypto-wallet\0{chain}\0{asset_code}\0{address}\0{token_contract}".encode()
    ).hexdigest()[:32]
    now = _utc_now()
    with connect_database(path, writable=True) as connection:
        connection.execute("""INSERT INTO crypto_wallets
            (id, account_label, chain, asset_code, address, token_contract, label,
             enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chain, asset_code, address, token_contract) DO UPDATE SET
              account_label = excluded.account_label, label = excluded.label,
              enabled = excluded.enabled, updated_at = excluded.updated_at""",
            (wallet_id, account_label, chain, asset_code, address, token_contract,
             label.strip(), int(bool(enabled)), now, now))
        stored = connection.execute("""SELECT id FROM crypto_wallets
            WHERE chain = ? AND asset_code = ? AND address = ? AND token_contract = ?""",
            (chain, asset_code, address, token_contract)).fetchone()
        return stored["id"]


def record_crypto_refresh(
    path: str | Path,
    *,
    wallet_id: str,
    fetched_at: str,
    status: str,
    operation_key: str,
    source: str,
    balance=None,
    transactions: list[dict] | None = None,
    message: str = "",
) -> dict:
    """Atomically persist a wallet refresh, observations, transactions and receipt."""
    status = status.strip().lower()
    if status not in {"ok", "error"}:
        raise ValueError("crypto refresh status must be ok or error")
    if not fetched_at.strip() or not source.strip() or not operation_key.strip():
        raise ValueError("refresh time, source and operation key are required")
    balance_text = None if balance is None else _non_negative_decimal_text(balance, "balance")
    if status == "error" and (balance_text is not None or transactions):
        raise ValueError("failed crypto refresh cannot contain new observations")
    normalized_transactions = []
    for item in transactions or []:
        occurred_on = _iso_date(str(item.get("occurred_on", "")), "occurred_on")
        chain_tx_id = str(item.get("chain_tx_id", "")).strip()
        operation = str(item.get("operation", "")).strip()
        if not chain_tx_id or not operation:
            raise ValueError("crypto transaction ID and operation are required")
        quantity = item.get("quantity")
        fee = item.get("fee")
        normalized_transactions.append({
            "occurred_on": occurred_on,
            "chain_tx_id": chain_tx_id,
            "operation": operation,
            "quantity": None if quantity is None or str(quantity).strip() == "" else
                _positive_decimal_text(quantity, "crypto quantity"),
            "fee": None if fee is None or str(fee).strip() == "" else
                _non_negative_decimal_text(fee, "crypto fee"),
            "counterparty": str(item.get("counterparty", "")),
            "comment": str(item.get("comment", "")),
        })
    normalized_transactions.sort(key=lambda item: (
        item["occurred_on"], item["chain_tx_id"], item["operation"]))
    request = {
        "wallet_id": wallet_id, "fetched_at": fetched_at.strip(), "status": status,
        "source": source.strip(), "balance": balance_text,
        "transactions": normalized_transactions, "message": message,
    }
    with connect_database(path, writable=True) as connection:
        receipt = connection.execute(
            "SELECT operation_kind, result_json FROM operation_receipts WHERE operation_key = ?",
            (operation_key,)).fetchone()
        if receipt is not None:
            payload = json.loads(receipt["result_json"])
            if receipt["operation_kind"] != "record_crypto_refresh" or payload.get("request") != request:
                raise ValueError("operation key was reused with another command or payload")
            return payload["result"]
        wallet = connection.execute(
            "SELECT * FROM crypto_wallets WHERE id = ?", (wallet_id,)).fetchone()
        if wallet is None:
            raise ValueError("unknown crypto wallet")
        observation_id = None
        if balance_text is not None:
            observation_id = hashlib.sha256(
                f"crypto-balance\0{wallet_id}\0{operation_key}".encode()).hexdigest()[:32]
            connection.execute("""INSERT INTO crypto_balance_observations
                (id, wallet_id, fetched_at, quantity_text, source)
                VALUES (?, ?, ?, ?, ?)""",
                (observation_id, wallet_id, fetched_at.strip(), balance_text, source.strip()))
        transaction_ids = []
        for item in normalized_transactions:
            transaction_id = hashlib.sha256(
                f"crypto-transaction\0{wallet_id}\0{item['chain_tx_id']}\0{item['operation']}".encode()
            ).hexdigest()[:32]
            existing = connection.execute("""SELECT occurred_on, quantity_text, fee_text,
                counterparty, source, comment FROM crypto_transactions
                WHERE wallet_id = ? AND chain_tx_id = ? AND operation = ?""",
                (wallet_id, item["chain_tx_id"], item["operation"])).fetchone()
            values = (item["occurred_on"], item["quantity"], item["fee"],
                      item["counterparty"], source.strip(), item["comment"])
            if existing is None:
                connection.execute("""INSERT INTO crypto_transactions
                    (id, wallet_id, chain_tx_id, occurred_on, operation, quantity_text,
                     fee_text, counterparty, source, comment)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (transaction_id, wallet_id, item["chain_tx_id"], item["occurred_on"],
                     item["operation"], item["quantity"], item["fee"], item["counterparty"],
                     source.strip(), item["comment"]))
            elif tuple(existing) != values:
                raise ValueError("network transaction identity has conflicting payload")
            transaction_ids.append(transaction_id)
        refresh_id = hashlib.sha256(
            f"crypto-refresh\0{wallet_id}\0{operation_key}".encode()).hexdigest()[:32]
        connection.execute("""INSERT INTO crypto_refresh_results
            (id, fetched_at, wallet_id, observed_account, observed_chain,
             observed_asset, observed_address, status, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (refresh_id, fetched_at.strip(), wallet_id, wallet["account_label"],
             wallet["chain"], wallet["asset_code"], wallet["address"], status, message))
        result = {"refresh_id": refresh_id, "observation_id": observation_id,
                  "transaction_ids": transaction_ids, "status": status}
        payload = {"request": request, "result": result}
        connection.execute("""INSERT INTO operation_receipts
            (operation_key, operation_kind, result_entity_type, result_entity_id,
             result_json, created_at) VALUES (?, 'record_crypto_refresh',
             'crypto_refresh_result', ?, ?, ?)""",
            (operation_key, refresh_id, json.dumps(payload, sort_keys=True), _utc_now()))
        return result


def save_fx_rate(path: str | Path, *, rate_date: str, currency: str, usd_rate,
                 source: str = "manual", fetched_at: str = "", sequence: int = 0,
                 observation_id: str | None = None) -> str:
    rate_date = _iso_date(rate_date, "rate_date")
    if not source.strip():
        raise ValueError("FX source is required")
    rate = parse_money_amount(usd_rate, field_name="usd_rate")
    if rate <= 0:
        raise ValueError("usd_rate must be positive")
    with connect_database(path, writable=True) as connection:
        if connection.execute("SELECT 1 FROM currencies WHERE code = ?", (currency.upper(),)).fetchone() is None:
            raise ValueError("unsupported currency")
        observation_id = observation_id or uuid4().hex
        connection.execute(
            """INSERT INTO fx_rate_observations VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (observation_id, rate_date, currency.upper(), format(rate, "f"), source.strip(),
             fetched_at or _utc_now(), sequence),
        )
    return observation_id


def fx_rates(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute("""SELECT rate_date AS date, currency_code AS currency,
            usd_per_unit_text AS usd_rate_text, source, fetched_at, sequence
            FROM v_effective_fx_rates ORDER BY rate_date, currency_code""").fetchall()
    return [{**dict(row), "usd_rate": Decimal(row["usd_rate_text"])} for row in rows]


def cpi_series(path: str | Path) -> list[dict]:
    with connect_database(path) as connection:
        rows = connection.execute(
            """SELECT currency_code, territory_code, provider_id, series_code,
              source_name, source_url, index_method, active
              FROM cpi_series ORDER BY currency_code"""
        ).fetchall()
    return [dict(row) for row in rows]


def save_cpi_observations(
    path: str | Path,
    *,
    currency: str,
    observations: list[dict],
    source_version: str,
    payload_sha256: str,
    fetched_at: str,
    published_on: str | None = None,
) -> dict:
    """Atomically append one version of monthly CPI observations."""
    currency = currency.strip().upper()
    if not source_version.strip() or not fetched_at.strip():
        raise ValueError("CPI source_version and fetched_at are required")
    if len(payload_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in payload_sha256.lower()):
        raise ValueError("CPI payload_sha256 must contain 64 hexadecimal characters")
    if published_on is not None:
        published_on = _iso_date(published_on, "published_on")
    normalized = []
    seen_periods = set()
    for item in observations:
        period = _period(item.get("period"))
        if period in seen_periods:
            raise ValueError(f"duplicate CPI period in one source version: {period}")
        seen_periods.add(period)
        value = parse_money_amount(item.get("index_value"), field_name="CPI index value")
        if value <= 0:
            raise ValueError("CPI index value must be positive")
        source = tuple(item.get(field) for field in (
            "provider_id", "source_name", "source_url"))
        if any(value is not None for value in source) and not all(
                isinstance(value, str) and value.strip() for value in source):
            raise ValueError("CPI observation source fields must be provided together")
        if source[2] is not None and not source[2].startswith("https://"):
            raise ValueError("CPI observation source URL must use HTTPS")
        normalized.append((period, format(value, "f"), source))
    if not normalized:
        raise ValueError("at least one CPI observation is required")

    inserted = 0
    unchanged = 0
    with connect_database(path, writable=True) as connection:
        series = connection.execute(
            """SELECT provider_id, source_name, source_url FROM cpi_series
              WHERE currency_code = ? AND active = 1""",
            (currency,),
        ).fetchone()
        if series is None:
            raise ValueError("unsupported or inactive CPI currency")
        default_source = tuple(series)
        for period, value_text, source in normalized:
            source = default_source if source[0] is None else tuple(
                value.strip() for value in source)
            existing = connection.execute(
                """SELECT id, index_value_text, published_on, payload_sha256
                  FROM cpi_observations
                  WHERE currency_code = ? AND period = ? AND source_version = ?""",
                (currency, period, source_version.strip()),
            ).fetchone()
            expected = (value_text, published_on, payload_sha256.lower())
            if existing is not None:
                if tuple(existing)[1:] != expected:
                    raise ValueError("CPI source version conflicts with stored observation")
                stored_source = connection.execute(
                    """SELECT provider_id, source_name, source_url
                      FROM cpi_observation_sources WHERE observation_id = ?""",
                    (existing["id"],),
                ).fetchone()
                if stored_source is None:
                    connection.execute(
                        "INSERT INTO cpi_observation_sources VALUES (?, ?, ?, ?)",
                        (existing["id"], *source),
                    )
                elif tuple(stored_source) != source:
                    raise ValueError("CPI source version conflicts with stored provenance")
                unchanged += 1
                continue
            identity = f"{currency}\0{period}\0{source_version.strip()}"
            observation_id = hashlib.sha256(f"cpi\0{identity}".encode()).hexdigest()[:32]
            connection.execute(
                """INSERT INTO cpi_observations
                  (id, currency_code, period, index_value_text, published_on,
                   source_version, payload_sha256, fetched_at)
                  VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (observation_id, currency, period, value_text, published_on,
                 source_version.strip(), payload_sha256.lower(), fetched_at.strip()),
            )
            connection.execute(
                "INSERT INTO cpi_observation_sources VALUES (?, ?, ?, ?)",
                (observation_id, *source),
            )
            inserted += 1
    return {"submitted": len(normalized), "inserted": inserted, "unchanged": unchanged}


def cpi_observations(path: str | Path, *, currency: str | None = None) -> list[dict]:
    parameters = ()
    where = ""
    if currency is not None:
        where = "WHERE o.currency_code = ?"
        parameters = (currency.strip().upper(),)
    with connect_database(path) as connection:
        rows = connection.execute(
            f"""SELECT o.currency_code, s.territory_code, o.period,
              o.index_value_text, o.published_on, o.source_version,
              o.payload_sha256, o.fetched_at, p.provider_id,
              p.source_name, p.source_url
              FROM v_effective_cpi o
              JOIN cpi_series s ON s.currency_code = o.currency_code
              JOIN cpi_observation_sources p ON p.observation_id = o.id
              {where} ORDER BY o.currency_code, o.period""",
            parameters,
        ).fetchall()
    return [
        {**dict(row), "index_value": Decimal(row["index_value_text"])}
        for row in rows
    ]


def save_market_price(path: str | Path, *, ticker: str, price_date: str, price,
                      currency: str, source: str, fetched_at: str,
                      sequence: int = 0) -> str:
    ticker = ticker.strip().upper()
    price_date = _iso_date(price_date, "price_date")
    price_text = _positive_decimal_text(price, "market price")
    currency = currency.upper()
    if not ticker or not source.strip() or not fetched_at.strip() or sequence < 0:
        raise ValueError("price ticker, source, fetched_at and non-negative sequence are required")
    identity = f"{ticker}\0{price_date}\0{source.strip()}\0{fetched_at.strip()}\0{sequence}"
    observation_id = hashlib.sha256(f"market-price\0{identity}".encode()).hexdigest()[:32]
    with connect_database(path, writable=True) as connection:
        instrument = connection.execute(
            "SELECT id FROM instruments WHERE ticker = ?", (ticker,)).fetchone()
        if instrument is None:
            raise ValueError("market price needs a known instrument")
        if connection.execute("SELECT 1 FROM currencies WHERE code = ?", (currency,)).fetchone() is None:
            raise ValueError("unsupported currency")
        same_observation = connection.execute("""SELECT id FROM market_price_observations
            WHERE instrument_id = ? AND price_date = ? AND price_text = ?
            AND currency_code = ? AND source = ? AND fetched_at = ? LIMIT 1""",
            (instrument["id"], price_date, price_text, currency,
             source.strip(), fetched_at.strip())).fetchone()
        if same_observation is not None:
            return same_observation["id"]
        existing = connection.execute("""SELECT id, price_text, currency_code
            FROM market_price_observations WHERE instrument_id = ? AND price_date = ?
            AND source = ? AND fetched_at = ? AND sequence = ?""",
            (instrument["id"], price_date, source.strip(), fetched_at.strip(), sequence)).fetchone()
        if existing is not None:
            if (existing["price_text"], existing["currency_code"]) != (price_text, currency):
                raise ValueError("market price identity has conflicting payload")
            return existing["id"]
        connection.execute("""INSERT INTO market_price_observations
            (id, instrument_id, price_date, price_text, currency_code, source,
             fetched_at, sequence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (observation_id, instrument["id"], price_date, price_text, currency,
             source.strip(), fetched_at.strip(), sequence))
    return observation_id


def register_source_record(path: str | Path, *, source_kind: str, document_hash: str,
                           parser_version: str, record_key: str,
                           payload_hash: str) -> tuple[str, str]:
    if any(len(value) != 64 for value in (document_hash, payload_hash)):
        raise ValueError("source hashes must be SHA-256 hex digests")
    with connect_database(path, writable=True) as connection:
        batch = connection.execute("""SELECT id FROM source_batches WHERE source_kind = ?
            AND document_hash = ? AND parser_version = ?""",
            (source_kind, document_hash, parser_version)).fetchone()
        batch_id = batch[0] if batch else hashlib.sha256(
            f"batch\0{source_kind}\0{document_hash}\0{parser_version}".encode()
        ).hexdigest()[:32]
        if batch is None:
            connection.execute("INSERT INTO source_batches VALUES (?, ?, ?, ?, 'accepted', ?)",
                               (batch_id, source_kind, document_hash, parser_version, _utc_now()))
        record = connection.execute("SELECT id, payload_hash FROM source_records WHERE batch_id = ? AND record_key = ?",
                                    (batch_id, record_key)).fetchone()
        if record and record["payload_hash"] != payload_hash:
            raise ValueError("source record key was reused with another payload")
        record_id = record["id"] if record else hashlib.sha256(
            f"record\0{batch_id}\0{record_key}".encode()
        ).hexdigest()[:32]
        if record is None:
            connection.execute("INSERT INTO source_records VALUES (?, ?, ?, ?, ?)",
                               (record_id, batch_id, record_key, payload_hash, _utc_now()))
        return batch_id, record_id


def link_transaction_source(path: str | Path, transaction_id: str, source_record_id: str,
                            *, role: str = "original",
                            allocated_amount_minor: int | None = None) -> None:
    with connect_database(path, writable=True) as connection:
        connection.execute("INSERT INTO transaction_source_links VALUES (?, ?, ?, ?)",
                           (transaction_id, source_record_id, role, allocated_amount_minor))


def link_entity_source(path: str | Path, entity_type: str, entity_id: str,
                       source_record_id: str, *, role: str = "original") -> None:
    with connect_database(path, writable=True) as connection:
        connection.execute("INSERT INTO entity_source_links VALUES (?, ?, ?, ?)",
                           (entity_type, entity_id, source_record_id, role))


def backup_database(source_path: str | Path, destination_path: str | Path) -> None:
    """Create and validate a consistent snapshot through SQLite Backup API."""
    destination = Path(destination_path).resolve()
    if destination.exists():
        raise FileExistsError("backup destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    config.require_writable_mode()
    try:
        with connect_database(source_path) as source, sqlite3.connect(destination) as target:
            source.backup(target)
            target.row_factory = sqlite3.Row
            target.execute("PRAGMA foreign_keys = ON")
            if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("backup failed SQLite integrity_check")
            if target.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("backup failed SQLite foreign_key_check")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
