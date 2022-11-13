import os
import json
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import timedelta

from src import config
from datetime import datetime
from dateutil.relativedelta import relativedelta


def get_reports_years() -> list:
    start_year = '2016'
    report_years_list = [start_year]
    stop_year = datetime.strftime(datetime.now(), '%Y')

    current_year = start_year
    while current_year != stop_year:
        new_date = datetime.strftime(datetime.strptime(current_year, '%Y') + relativedelta(years=1), '%Y')
        report_years_list.append(new_date)
        current_year = new_date
    return report_years_list


def get_cross_rates(from_curr, to_curr, min_date, max_date):
    # Получаем котировки тенге к доллару
    curr_USD_rates = yf.download(f'{from_curr}USD=X',
                                 min_date,
                                 max_date + timedelta(days=1)
                                 )['Adj Close']
    full_ind = pd.date_range(min_date, max_date)
    curr_USD_rates = (curr_USD_rates.reindex(full_ind, fill_value=np.nan)
                      .interpolate(limit_direction='both')
                      .rename(f'{from_curr}USD=X'))

    # Получаем котировки доллара к рублю
    curr_rates = yf.download(f'USD{to_curr}=X',
                             min_date,
                             max_date + timedelta(days=1)
                             )['Adj Close']
    full_ind = pd.date_range(min_date, max_date)
    curr_rates = (curr_rates.reindex(full_ind, fill_value=np.nan)
                  .interpolate(limit_direction='both')
                  .rename(f'USD{to_curr}=X'))
    curr_rates = pd.concat([curr_rates, curr_USD_rates], axis=1)
    curr_rates[f'{from_curr}{to_curr}=X'] = curr_rates[f'{from_curr}USD=X'] * curr_rates[f'USD{to_curr}=X']
    curr_rates.drop([f'{from_curr}USD=X', f'USD{to_curr}=X'], axis=1, inplace=True)
    return curr_rates


def get_secrets(key_: str):
    with open(config.SECRETS_PATH, mode='r') as f:
        SECRETS = json.loads(f.read())
        return SECRETS[key_]
