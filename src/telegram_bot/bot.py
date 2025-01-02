import pandas as pd
import telebot
from telebot import types
from datetime import datetime


from src import utils, config
from src.reports.year_report import create_year_report
from src.reports.month_report import create_month_report
from src.reports.main_report import create_main_report
from src.data.get import get_transactions, get_assets

bot = telebot.TeleBot(utils.get_secrets('TELEGRAM_TOKEN'))

report_type = None
currency = None
year = None
month = None
data_type = None


current_assets_list = []
assets_month = None
asset_name = None
unique_assets_list = get_assets()[['Счет', 'Валюта']].drop_duplicates().agg('_'.join, axis=1).tolist()

@bot.message_handler(content_types=['text'])
def start(message):
    global report_type
    global year
    global month

    if message.text in ['/get_report']:
        keyboard = types.InlineKeyboardMarkup()
        for button_ in config.REPORTS_TYPES:
            button_key = types.InlineKeyboardButton(text=button_, callback_data=button_)
            keyboard.add(button_key)
        bot.send_message(message.from_user.id, text="Choose type of report", reply_markup=keyboard)
    elif message.text == '/get_actual_reports':
        report_type = 'actual'
        now = datetime.now()
        year = datetime.strftime(now, '%Y')
        month = datetime.strftime(now, '%m')

        bot.send_message(message.from_user.id, f'actual reports has been chosen')
        keyboard = types.InlineKeyboardMarkup()
        for button_ in config.UNIQUE_TICKERS.keys():
            button_key = types.InlineKeyboardButton(text=button_, callback_data=button_)
            keyboard.add(button_key)
        bot.send_message(message.from_user.id, text=f'Choose currency', reply_markup=keyboard)
    elif message.text == '/add_data':
        keyboard = types.InlineKeyboardMarkup()
        for button_ in config.DATA_TYPES:
            button_key = types.InlineKeyboardButton(text=button_, callback_data=button_)
            keyboard.add(button_key)
        bot.send_message(message.from_user.id, text=f'Choose type of data to add', reply_markup=keyboard)
    else:
        bot.send_message(message.from_user.id, 'Write /get_report, /add_data, /get_actual_reports')


def proccess_main_report(bot_response):
    try:
        current_id = bot_response.message.chat.id
    except AttributeError:
        current_id = bot_response.from_user.id
    bot.send_message(current_id, text=f'Start making main report in {currency}')
    create_main_report(currency=currency, return_image=True)
    bot.send_document(current_id, open(config.IMAGE_TO_BOT_PATH, 'rb'))

def proccess_year_report(bot_response):
    try:
        current_id = bot_response.message.chat.id
    except AttributeError:
        current_id = bot_response.from_user.id
    bot.send_message(current_id, text=f'Start making {year} year report in {currency}')
    create_year_report(year=year, currency=currency, return_image=True)
    bot.send_document(current_id, open(config.IMAGE_TO_BOT_PATH, 'rb'))

def proccess_month_report(bot_response):
    try:
        current_id = bot_response.message.chat.id
    except AttributeError:
        current_id = bot_response.from_user.id
    bot.send_message(current_id, text=f'Start making {year}-{month} month report in {currency}')
    create_month_report(year=year, currency=currency, month=month, return_image=True)
    bot.send_document(current_id, open(config.IMAGE_TO_BOT_PATH, 'rb'))

def choose_asset_month(message):
    global assets_month
    global unique_assets_list
    if message.text in ['Да', 'да', 'Yes', 'yes']:
        assets_month = datetime.strftime(datetime.now(), '%Y-%m')
        bot.send_message(message.from_user.id, text=f'Please add assets for {assets_month}')

        keyboard = types.InlineKeyboardMarkup()
        for button_ in unique_assets_list:
            button_key = types.InlineKeyboardButton(text=button_, callback_data=button_)
            keyboard.add(button_key)
        bot.send_message(message.from_user.id, text="Choose asset", reply_markup=keyboard)
    else:
        bot.send_message(message.from_user.id, text=f'Please write month in format year-month')

def add_asset(message):
    global asset_name
    global assets_month

    assets_dict = {}
    assets_dict['Счет'] = asset_name.split('_')[0].strip()
    assets_dict['Валюта'] = asset_name.split('_')[1].strip()
    assets_dict['Значение'] = float(message.text)
    assets_dict['Год'] = assets_month[:4]
    assets_dict['Месяц'] = assets_month[5:]
    current_assets_list.append(assets_dict)
    print(current_assets_list)
    bot.send_message(message.from_user.id, text=pd.DataFrame(current_assets_list).to_string())

@bot.callback_query_handler(func=lambda call: True)
def callback_worker(call):
    global report_type
    global currency
    global year
    global month
    global data_type
    global unique_assets_list
    global asset_name
    month_list = list(map(lambda x: str(x), range(1, 13)))

    # Обработка типов репопортов
    if call.data in config.REPORTS_TYPES:
        report_type = call.data
        bot.send_message(call.message.chat.id, f'{call.data} report has been chosen')
        keyboard = types.InlineKeyboardMarkup()
        for button_ in config.UNIQUE_TICKERS.keys():
            button_key = types.InlineKeyboardButton(text=button_, callback_data=button_)
            keyboard.add(button_key)
        bot.send_message(call.message.chat.id, text=f'Choose currency', reply_markup=keyboard)

    # Обработка валют
    elif call.data in config.UNIQUE_TICKERS.keys():
        currency = call.data
        bot.send_message(call.message.chat.id, f'{call.data} has been chosen')
        if report_type == 'main':
            proccess_main_report(call)
        elif report_type in ['year', 'month']:
            keyboard = types.InlineKeyboardMarkup()
            for button_ in utils.get_reports_years():
                button_key = types.InlineKeyboardButton(text=button_, callback_data=button_)
                keyboard.add(button_key)
            bot.send_message(call.message.chat.id, text="Choose year of report", reply_markup=keyboard)
        elif report_type == 'actual':
            proccess_main_report(call)
            proccess_year_report(call)
            proccess_month_report(call)
        else:
            bot.send_message(call.message.chat.id, text=f'Error')

    # Обработка годов
    elif call.data in utils.get_reports_years():
        year = call.data
        if report_type == 'year':
            proccess_year_report(call)
        else:
            keyboard = types.InlineKeyboardMarkup()
            for button_ in month_list:
                button_key = types.InlineKeyboardButton(text=button_, callback_data=button_)
                keyboard.add(button_key)
            bot.send_message(call.message.chat.id, text="Choose month of report", reply_markup=keyboard)

    # Обработка месяцев
    elif call.data in month_list:
        month = call.data if len(call.data) == 2 else '0' + call.data
        if report_type == 'month':
            proccess_month_report(call)

    # Обработка типов обновляемых данных
    elif call.data in config.DATA_TYPES:
        data_type = call.data
        bot.send_message(call.message.chat.id, text="Do you want to add assets of current month?")
        bot.register_next_step_handler(call.message, choose_asset_month)

    # Обработка счетов
    elif call.data in unique_assets_list:
        asset_name = call.data
        bot.send_message(call.message.chat.id, text="Write asset value")
        bot.register_next_step_handler(call.message, add_asset)
    else:
        bot.send_message(call.message.chat.id, f'{call.data}')