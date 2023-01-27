import os.path
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import yfinance.shared as shared
from bs4 import BeautifulSoup
from twelvedata import TDClient

from src import config, utils


def get_actual_rates(tickers):
    """
    Get rates of current day
    :param tickers:
    :return:
    """
    if config.STOCK_API == 'yf':
        cur_date = datetime.now().date()
        curr_rates = get_rates(tickers, min_date=cur_date, max_date=cur_date).reset_index(drop=True).T.reset_index()
        curr_rates.columns = ['ticker_', 'Актуальная_цена_yf']
    elif config.STOCK_API == 'td':
        td = TDClient(apikey=utils.get_secrets('TD_API_KEY'))
        curr_rates = td.time_series(
            symbol=tickers,
            # exchange="S&P 500",
            interval="5min",
            outputsize=1,
            # timezone="America/New_York",
        ).as_pandas()['close']

        if isinstance(tickers, str) or len(tickers) == 1:
            curr_rates = curr_rates.rename(tickers).to_frame().T.reset_index()
        else:
            curr_rates = curr_rates.reset_index().drop('level_1', axis=1, errors='ignore')
        curr_rates.columns = ['ticker_', 'Актуальная_цена_td']
    else:
        raise ValueError('config.STOCK_API должно быть из [yf, td]')
    return curr_rates


def get_rates(tickers: list, min_date, max_date) -> pd.DataFrame:
    """
    Get rates from selected range of dates
    :param tickers: list of tickers
    :param min_date: minimal date to get
    :param max_date: maximal date to get
    :return:
    """
    RATES_PATH = os.path.join(config.DATA_PATH, 'rates')
    rates_df_sml = None
    rates_df = None
    min_date = pd.to_datetime(min_date)
    max_date = pd.to_datetime(max_date)

    # Create rates file if not exist
    if 'rates' not in os.listdir(config.DATA_PATH):
        os.makedirs(RATES_PATH)

    # if data exist return it
    if 'currency_rates.csv' in os.listdir(RATES_PATH):
        rates_df = pd.read_csv(os.path.join(RATES_PATH, 'currency_rates.csv'),
                               sep=';', index_col='Дата', parse_dates=True)
        try:
            rates_df_sml = rates_df.loc[min_date:max_date, tickers]
            if (rates_df_sml.size != 0) and \
                    (rates_df_sml.index.min() == min_date) and \
                    (rates_df_sml.index.max() == max_date) and \
                    (rates_df_sml.isna().sum().sum() == 0):
                return rates_df_sml
        except KeyError:
            pass

    # Get rates to update
    rates_df_upd = yf.download(tickers, min_date - timedelta(days=5), max_date)['Adj Close']
    rates_df_upd.index = pd.DatetimeIndex(pd.to_datetime(rates_df_upd.index).date)
    full_ind = pd.date_range(min_date - timedelta(days=5), max_date, freq='D')

    # Interpolate missing dates
    rates_df_upd = (rates_df_upd.reindex(full_ind, fill_value=np.nan).interpolate(limit_direction='both'))

    if isinstance(rates_df_upd, pd.Series):
        rates_df_upd = rates_df_upd.rename(tickers[0]).to_frame()

    # Get crossrates
    nan_cols = set(rates_df_upd.loc[:, rates_df_upd.isna().all(axis=0)].columns)
    download_errors = set(shared._ERRORS.keys())
    for ticker_name in download_errors.intersection(nan_cols):
        if '=X' in ticker_name:
            cross_rate_df = get_cross_rates(from_curr=ticker_name[:3],
                                            to_curr=ticker_name[3:6],
                                            min_date=min_date - timedelta(days=5),
                                            max_date=max_date)
            rates_df_upd[ticker_name] = cross_rate_df[ticker_name]

    rates_df_upd.index.name = 'Дата'

    # if we don't have stored data at all
    if rates_df is None:
        rates_df = rates_df_upd
        rates_df_sml = rates_df_upd

    # if we don't have stored data in selected range
    elif rates_df_sml is None:
        rates_df_sml = rates_df_upd
        for col_name, col in rates_df_upd.items():
            for date_ind, value in col.items():
                rates_df.loc[date_ind, col_name] = value
    else:
        for col_name, col in rates_df_upd.items():
            for date_ind, value in col.items():
                rates_df.loc[date_ind, col_name] = value
                rates_df_sml.loc[date_ind, col_name] = value

    # Save updated df
    (
        rates_df.reset_index()
            .sort_values('Дата')
            .to_csv(os.path.join(RATES_PATH, 'currency_rates.csv'), index=False, sep=';')
    )
    return rates_df_sml.loc[min_date:max_date].sort_index()


def get_cross_rates(from_curr, to_curr, min_date, max_date):
    # Get rates of from_curr/USD
    curr_USD_rates = yf.download(f'{from_curr}USD=X',
                                 min_date,
                                 max_date
                                 )['Adj Close']
    curr_USD_rates.index = pd.DatetimeIndex(pd.to_datetime(curr_USD_rates.index).date)
    full_ind = pd.date_range(min_date, max_date)
    curr_USD_rates = (curr_USD_rates.reindex(full_ind, fill_value=np.nan)
                      .interpolate(limit_direction='both')
                      .rename(f'{from_curr}USD=X'))

    # Get rates of USD/to_curr
    curr_rates = yf.download(f'USD{to_curr}=X',
                             min_date,
                             max_date
                             )['Adj Close']
    curr_rates.index = pd.DatetimeIndex(pd.to_datetime(curr_rates.index).date)
    full_ind = pd.date_range(min_date, max_date)
    curr_rates = (curr_rates.reindex(full_ind, fill_value=np.nan)
                  .interpolate(limit_direction='both')
                  .rename(f'USD{to_curr}=X'))
    curr_rates = pd.concat([curr_rates, curr_USD_rates], axis=1)
    curr_rates[f'{from_curr}{to_curr}=X'] = curr_rates[f'{from_curr}USD=X'] * curr_rates[f'USD{to_curr}=X']
    curr_rates.drop([f'{from_curr}USD=X', f'USD{to_curr}=X'], axis=1, inplace=True)
    return curr_rates


def get_act_moex(mode='stocks'):
    if mode == 'stocks':
        moex_req = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.xml?iss.meta=off&iss.only=securities&securities.columns=PREVDATE, SECID, PREVADMITTEDQUOTE"
    elif mode == 'ETF':
        moex_req = f"https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQTF/securities.xml?iss.meta=off&iss.only=securities&securities.columns=PREVDATE, SECID, PREVADMITTEDQUOTE"
    elif mode == 'OFZ':
        moex_req = f"https://iss.moex.com/iss/engines/stock/markets/bonds/boards/TQOB/securities.xml?iss.meta=off&iss.only=securities&securities.columns=PREVDATE, SECID, PREVADMITTEDQUOTE"
    else:
        raise ValueError('mode должен быть из [stocks, ETF, OFZ]')

    response = requests.api.get(moex_req)
    soup = BeautifulSoup(response.text, "html.parser")
    shares_df = pd.DataFrame()
    rows = soup.findAll('row')
    for row in rows:
        shares_df = pd.concat([shares_df,
                               pd.DataFrame([{'Дата': row['prevdate'],
                                              'Тикер': row['secid'],
                                              f'Актуальная_цена_moex_{mode}': row['prevadmittedquote']}])
                               ],
                              axis=0)
    shares_df = shares_df.replace('', np.nan)
    shares_df = shares_df.astype({'Дата': 'datetime64[ns]',
                                  'Тикер': str,
                                  f'Актуальная_цена_moex_{mode}': float
                                  }, errors='ignore')
    shares_df.drop('Дата', axis=1, inplace=True)
    return shares_df


if __name__ == '__main__':
    print(get_rates(tickers=[
                             'USDRUB=X',
                             'RUBUSD=X',
                             'RUBKZT=X',
                             'KZTRUB=X'
                             ],
                    min_date='2023-01-26',
                    max_date='2023-01-26'))
