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

STOCK_API = 'yf'  # yf, td

# Fallback exchange rates when API fails
# Base rates to USD (most common base currency)
FALLBACK_RATES = {
    'KZT': {'USD': 0.002},  # 1 KZT = 0.002 USD
    'RUB': {'USD': 0.01},   # 1 RUB = 0.01 USD (100 RUB = 1 USD)
    'EUR': {'USD': 1.1},    # 1 EUR = 1.1 USD
    'GBP': {'USD': 1.25}    # 1 GBP = 1.25 USD
}

NOT_COST_COLS = ['Доход', 'Сбережения', 'Инвестиции',
                 'Дебиторская задолженность', 'Погашение деб. зад.',
                 'Кредиторская задолженность', 'Погашение кред. зад.']
DEBUG = False

REPORTS_TYPES = ['main', 'year', 'month']
DATA_TYPES = ['transactions', 'assets', 'investments']
TRANSACTIONS_COLUMNS = ['Дата', 'Категория', 'Валюта', 'Значение', 'Комментарий']
ASSETS_COLUMNS = ['Счет', 'Валюта', 'Значение', 'Год', 'Месяц']