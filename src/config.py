import os
from pathlib import Path

UNIQUE_TICKERS = {'RUB': '₽', 'USD': '$', 'EUR': '€', 'KZT': '₸', 'GBP': '£'}

PROJECT_PATH = Path(__file__).parent.parent


def _project_path_from_env(env_name: str, default_folder: str) -> str:
    value = os.environ.get(env_name)
    if not value:
        return os.path.join(PROJECT_PATH, default_folder)

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_PATH / path
    return str(path)


DATA_PATH = _project_path_from_env('FINREP_DATA_DIR', 'data')
SAMPLE_DATA_PATH = str(PROJECT_PATH / 'sample_data')
REPORTS_PATH = _project_path_from_env('FINREP_REPORTS_DIR', 'reports')
SECRETS_PATH = os.path.join(PROJECT_PATH, 'src', 'secrets.json')

TRANSACTIONS_INFO_PATH = os.path.join(DATA_PATH, 'transactions_info')
ASSETS_INFO_PATH = os.path.join(DATA_PATH, 'assets_info')
INVESTMENTS_DIR_PATH = os.path.join(DATA_PATH, 'investments')
INVESTMENTS_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'investments.csv')
INVESTMENT_TRANSACTIONS_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'transactions.csv')
INVESTMENT_INSTRUMENTS_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'instruments.csv')
INVESTMENT_PRICE_CACHE_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'price_cache.csv')
CRYPTO_WALLETS_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'crypto_wallets.csv')
CRYPTO_BALANCES_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'crypto_balances.csv')
CRYPTO_TRANSACTIONS_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'crypto_transactions.csv')
CRYPTO_REFRESH_STATUS_PATH = os.path.join(INVESTMENTS_DIR_PATH, 'crypto_refresh_status.csv')
FX_CACHE_PATH = os.path.join(DATA_PATH, 'rates', 'fx_rates.csv')
STAGING_PATH = os.path.join(DATA_PATH, 'staging')
BACKUPS_PATH = os.path.join(DATA_PATH, 'backups')
TRANSACTION_BACKUPS_PATH = os.path.join(BACKUPS_PATH, 'transactions_info')
ASSET_BACKUPS_PATH = os.path.join(BACKUPS_PATH, 'assets_info')
TRANSACTION_DRAFTS_PATH = os.path.join(STAGING_PATH, 'transaction_drafts.csv')
DEBTS_PATH = os.path.join(DATA_PATH, 'debts')
DEBTS_CSV_PATH = os.path.join(DEBTS_PATH, 'debts.csv')
DEBT_PAYMENTS_CSV_PATH = os.path.join(DEBTS_PATH, 'debt_payments.csv')

STOCK_API = 'yf'  # yf, td
FX_BASE_CURRENCY = 'USD'
FX_PROVIDER_ORDER = ['yfinance', 'cbr']

INCOME_CATEGORY_LABELS = ['Зарплата', 'Проценты', 'Инвест доход', 'Прочие доходы']
UNCLASSIFIED_INCOME_LABEL = 'Доход без категории'
NOT_COST_COLS = ['Доход', 'Сбережения', *INCOME_CATEGORY_LABELS,
                 UNCLASSIFIED_INCOME_LABEL, 'Инвестиции',
                 'Дебиторская задолженность', 'Погашение деб. зад.',
                 'Кредиторская задолженность', 'Погашение кред. зад.']
DEBUG = False

REPORTS_TYPES = ['main', 'year', 'month']
DATA_TYPES = ['transactions', 'assets', 'investments']
TRANSACTIONS_COLUMNS = ['Дата', 'Категория', 'Валюта', 'Значение', 'Комментарий']
ASSETS_COLUMNS = ['Счет', 'Валюта', 'Значение', 'Год', 'Месяц']


def get_data_mode() -> str:
    """Return the signed session's data mode, falling back to live for CLI use."""
    try:
        from flask import has_request_context, session

        if has_request_context() and session.get("authenticated"):
            return "test" if session.get("data_mode") == "test" else "live"
    except RuntimeError:
        pass
    return "live"


def is_test_mode() -> bool:
    return get_data_mode() == "test"


def active_data_path(*parts: str) -> Path:
    root = Path(SAMPLE_DATA_PATH if is_test_mode() else DATA_PATH)
    return root.joinpath(*parts)


def get_storage_backend() -> str:
    backend = os.environ.get("FINREP_STORAGE_BACKEND", "sqlite").strip().lower()
    if backend not in {"csv", "sqlite"}:
        raise ValueError("FINREP_STORAGE_BACKEND must be csv or sqlite")
    return backend


def use_sqlite_storage() -> bool:
    # TEST remains the immutable sample CSV dataset until a sample SQLite artifact exists.
    return get_storage_backend() == "sqlite" and not is_test_mode()


def active_database_path() -> Path:
    configured = os.environ.get("FINREP_SQLITE_PATH")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_absolute() else PROJECT_PATH / path
    return Path(DATA_PATH) / "finrep.sqlite3"


def require_writable_mode() -> None:
    if is_test_mode():
        raise PermissionError("Test mode работает только на чтение.")
