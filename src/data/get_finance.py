import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import yfinance.shared as shared
import yfinance as yf
from twelvedata import TDClient
import pandas as pd
import numpy as np

from src import config, utils


def get_actual_rates(tickers, days_before=7):
    if config.STOCK_API == 'yf':
        cur_date = datetime.now().date()
        last_date = cur_date - timedelta(days=days_before)

        curr_rates = yf.download(tickers, last_date, cur_date)['Adj Close']
        full_ind = pd.date_range(last_date, cur_date)
        curr_rates = (curr_rates.reindex(full_ind, fill_value=np.nan)
                      .interpolate(limit_direction='both'))

        if isinstance(curr_rates, pd.Series):
            if len(tickers) == 1:
                curr_rates = curr_rates.rename(tickers[0]).to_frame()
            else:
                curr_rates = curr_rates.rename(tickers[0]).to_frame()

        nan_cols = list(curr_rates.loc[:, curr_rates.isna().all(axis=0)].columns)
        download_errors = list(shared._ERRORS.keys())
        for ticker_name in download_errors + nan_cols:
            if '=X' in ticker_name:
                cross_rate_df = utils.get_cross_rates(from_curr=ticker_name[:3],
                                                      to_curr=ticker_name[3:6],
                                                      min_date=last_date,
                                                      max_date=cur_date)
                curr_rates[ticker_name] = cross_rate_df[ticker_name]

        curr_rates = curr_rates.tail(1).T.reset_index()
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
    print(get_act_moex(mode='stocks').head())
    print(get_act_moex(mode='ETF').head())
    print(get_act_moex(mode='OFZ').head())
