import os

UNIQUE_TICKERS = {'RUB': '₽', 'USD': '$', 'EUR': '€', 'KZT': '₸'}

PROJECT_PATH = os.path.join('//', 'Users', 'levperla', 'PycharmProjects', 'FinRep')
TRANSACTIONS_INFO_PATH = os.path.join(PROJECT_PATH, 'data', 'transactions_info')
ASSETS_INFO_PATH = os.path.join(PROJECT_PATH, 'data', 'assets_info')
INVESTMENTS_PATH = os.path.join(PROJECT_PATH, 'data', 'investments.csv')
REPORTS_PATH = os.path.join(PROJECT_PATH, 'reports')
SECRETS_PATH = os.path.join(PROJECT_PATH, 'src', 'secrets.json')
IMAGE_TO_BOT_PATH = os.path.join(REPORTS_PATH, 'image_to_bot', "report_to_bot.png")

STOCK_API = 'yf'  # yf, td

NOT_COST_COLS = ['Доход', 'Сбережения', 'Инвестиции',
                 'Дебиторская задолженность', 'Погашение деб. зад.',
                 'Кредиторская задолженность', 'Погашение кред. зад.']
DEBUG = False

REPORTS_TYPES = ['main', 'year', 'month']
DATA_TYPES = ['transactions', 'assets', 'investments']
TRANSACTIONS_COLUMNS = ['Дата', 'Категория', 'Валюта', 'Значение', 'Комментарий']
ASSETS_COLUMNS = ['Счет', 'Валюта', 'Значение', 'Год', 'Месяц']


DB_NAME = 'FINREPDB'
DB_USER = 'FINREP'