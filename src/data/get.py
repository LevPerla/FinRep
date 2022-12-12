import pandas as pd
import numpy as np
import os
import warnings

warnings.filterwarnings('ignore')

from src import config, utils


def get_transactions():
    transactions_df = pd.DataFrame()
    for folder_name in os.listdir(config.TRANSACTIONS_INFO_PATH):
        if folder_name == '.DS_Store':
            continue
        for file_name in os.listdir(os.path.join(config.TRANSACTIONS_INFO_PATH, folder_name)):
            # print(file_name)
            if file_name == '.DS_Store':
                continue
            month_df = pd.read_csv(os.path.join(config.TRANSACTIONS_INFO_PATH, folder_name, file_name), sep=';',
                                   decimal=',',
                                   parse_dates=True,
                                   dayfirst=True,
                                   index_col='Дата',
                                   infer_datetime_format=True)
            month_df = month_df.rename(columns={'Долги (у меня)': 'Дебиторская задолженность',
                                                'Крупные покупки/ Поездки': 'Поездки'},
                                       errors='ignore')
            month_df = month_df.reset_index().melt(id_vars='Дата', var_name='Категория')

            month_df['value'] = month_df['value'].astype(str).apply(lambda x: x.split('#'))
            month_df = month_df.explode('value')
            month_df['value'] = (month_df['value'].astype(str)
                                 .str.replace(',', '.')
                                 .str.replace('\\xa0', '')
                                 .str.replace(' ₽', '')
                                 .apply(lambda x: x.split('|') if len(x.split('|')) == 3 else [x, 'RUB', np.nan])
                                 .apply(lambda x: {'Значение': float(x[0]),
                                                   'Валюта': x[1].upper(),
                                                   'Комментарий': x[2]})
                                 )

            month_df['Валюта'] = month_df['value'].apply(lambda x: x['Валюта'])
            month_df['Значение'] = month_df['value'].apply(lambda x: x['Значение'])
            month_df['Комментарий'] = month_df['value'].apply(lambda x: x['Комментарий'])
            month_df.drop('value', axis=1, inplace=True)

            month_df['Год'] = month_df['Дата'].apply(lambda x: x.year).astype(str)
            month_df['Квартал'] = month_df['Дата'].apply(lambda x: x.quarter).astype(str)
            month_df['Месяц'] = month_df['Дата'].apply(lambda x: x.month).astype(str)

            assert len(
                set(month_df['Валюта'].unique()) - config.UNIQUE_TICKERS.keys()) == 0, 'Есть недопустимые тикеры валют'
            transactions_df = pd.concat([transactions_df, month_df], axis=0)
    return transactions_df.reset_index().drop('index', axis=1)


def get_assets():
    assets_df = pd.DataFrame()
    for folder_name in os.listdir(config.ASSETS_INFO_PATH):
        if folder_name == '.DS_Store':
            continue
        for file_name in os.listdir(os.path.join(config.ASSETS_INFO_PATH, folder_name)):
            # print(file_name)
            if file_name == '.DS_Store':
                continue
            month_df = pd.read_csv(os.path.join(config.ASSETS_INFO_PATH, folder_name, file_name),
                                   sep=';', decimal=',', index_col='Счет')

            for col_name in month_df.columns:
                month_df[col_name] = (month_df[col_name].astype(str)
                                      .str.replace(',', '.')
                                      .str.replace('\\xa0', '')
                                      .apply(lambda x: x.split('|') if len(x.split('|')) == 2 else [x, 'RUB'])
                                      .apply(lambda x: {'Значение': float(x[0]),
                                                        'Валюта': x[1].upper()})
                                      )
            month_df = month_df.reset_index()
            month_df['Дата'] = pd.Period(file_name.split('.')[0].replace('_', '-'))
            month_df['Валюта'] = month_df['Сумма'].apply(lambda x: x['Валюта'])
            month_df['Значение'] = month_df['Сумма'].apply(lambda x: x['Значение'])

            month_df['Год'] = month_df['Дата'].apply(lambda x: x.year).astype(str)
            month_df['Квартал'] = month_df['Дата'].apply(lambda x: x.quarter).astype(str)
            month_df['Месяц'] = month_df['Дата'].apply(lambda x: x.month).astype(str)
            month_df.drop(['Сумма', 'Дата'], axis=1, inplace=True)

            assert len(
                set(month_df['Валюта'].unique()) - config.UNIQUE_TICKERS.keys()) == 0, 'Есть недопустимые тикеры валют'

            assets_df = pd.concat([assets_df, month_df], axis=0)
    return assets_df.reset_index().drop('index', axis=1)


def get_investments():
    data = pd.read_csv(config.INVESTMENTS_PATH, sep=';', decimal=',')
    data['Дата'] = data['Дата'].astype('datetime64[ns]')
    return data
