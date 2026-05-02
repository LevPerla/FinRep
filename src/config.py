import os
from pathlib import Path

UNIQUE_TICKERS = {'RUB': '₽', 'USD': '$', 'EUR': '€', 'KZT': '₸', 'GBP': '£'}

PROJECT_PATH = Path(__file__).parent.parent
DATA_PATH = os.path.join(PROJECT_PATH, 'data')
REPORTS_PATH = os.path.join(PROJECT_PATH, 'reports')
SECRETS_PATH = os.path.join(PROJECT_PATH, 'src', 'secrets.json')

TRANSACTIONS_INFO_PATH = os.path.join(DATA_PATH, 'transactions_info')
ASSETS_INFO_PATH = os.path.join(DATA_PATH, 'assets_info')
INVESTMENTS_PATH = os.path.join(DATA_PATH, 'investments.csv')
FX_CACHE_PATH = os.path.join(DATA_PATH, 'rates', 'fx_rates.csv')
STAGING_PATH = os.path.join(DATA_PATH, 'staging')
BACKUPS_PATH = os.path.join(DATA_PATH, 'backups')
TRANSACTION_BACKUPS_PATH = os.path.join(BACKUPS_PATH, 'transactions_info')
ASSET_BACKUPS_PATH = os.path.join(BACKUPS_PATH, 'assets_info')
TRANSACTION_DRAFTS_PATH = os.path.join(STAGING_PATH, 'transaction_drafts.csv')

STOCK_API = 'yf'  # yf, td
FX_BASE_CURRENCY = 'USD'
FX_PROVIDER_ORDER = ['yfinance', 'cbr']

NOT_COST_COLS = ['Доход', 'Сбережения', 'Инвестиции',
                 'Дебиторская задолженность', 'Погашение деб. зад.',
                 'Кредиторская задолженность', 'Погашение кред. зад.']
DEBUG = False

REPORTS_TYPES = ['main', 'year', 'month']
DATA_TYPES = ['transactions', 'assets', 'investments']
TRANSACTIONS_COLUMNS = ['Дата', 'Категория', 'Валюта', 'Значение', 'Комментарий']
ASSETS_COLUMNS = ['Счет', 'Валюта', 'Значение', 'Год', 'Месяц']
