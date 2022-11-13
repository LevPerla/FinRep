import os

UNIQUE_TICKERS = {'RUB': '₽', 'USD': '$', 'EUR': '€', 'KZT': '₸'}

PROJECT_PATH = os.path.abspath('..')
TRANSACTIONS_INFO_PATH = os.path.join(PROJECT_PATH, 'data', 'transactions_info')
ASSETS_INFO_PATH = os.path.join(PROJECT_PATH, 'data', 'assets_info')
INVESTMENTS_PATH = os.path.join(PROJECT_PATH, 'data', 'investments.csv')
REPORTS_PATH = os.path.join(PROJECT_PATH, 'reports')
SECRETS_PATH = os.path.join(PROJECT_PATH, 'src', 'secrets.json')

STOCK_API = 'yf'  # yf, td

NOT_COST_COLS = ['Доход', 'Сбережения', 'Инвестиции',
                 'Дебиторская задолженность', 'Погашение деб. зад.',
                 'Кредиторская задолженность', 'Погашение кред. зад.']
DEBUG = False

REPORTS_TYPES = ['main', 'year', 'month']
IMAGE_TO_BOT_PATH = os.path.join(REPORTS_PATH, 'image_to_bot', "report_to_bot.png")

TRANSACTIONS_COLUMNS = ['Дата', 'Категория', 'Валюта',
                        'Значение', 'Комментарий']