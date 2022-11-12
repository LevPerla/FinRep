import os

UNIQUE_TICKERS = {'RUB': '₽', 'USD': '$', 'EUR': '€', 'KZT': '₸'}
TRANSACTIONS_INFO_PATH = os.path.join('data', 'transactions_info')
ASSETS_INFO_PATH = os.path.join('data', 'assets_info')
INVESTMENTS_PATH = os.path.join('data', 'investments.csv')
REPORTS_PATH = os.path.join('reports')

STOCK_API = 'yf'  # yf, td

NOT_COST_COLS = ['Доход', 'Сбережения', 'Инвестиции',
                 'Дебиторская задолженность', 'Погашение деб. зад.',
                 'Кредиторская задолженность', 'Погашение кред. зад.']
DEBUG = False
