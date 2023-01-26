import json
import pandas as pd

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

def get_secrets(key_: str):
    with open(config.SECRETS_PATH, mode='r') as f:
        SECRETS = json.loads(f.read())
        return SECRETS[key_]

def fill_if_empty(df: pd.DataFrame) -> pd.DataFrame:
    """
    Function to fill empty df with string of '-'
    :type df: pd.DataFrame
    """
    if df.empty:
        df = pd.concat([df, pd.DataFrame({column_name: '-' for column_name in df.columns}, index=[0])],
                       axis=0,
                       ignore_index=True
                       )
    return df

def process_num_cols(df, not_num_cols, currency):
    for col_name in df:
        if col_name not in not_num_cols:
            df.loc[:, col_name] = (df[col_name].astype(float)
                                   .map('{:,.2f}'.format)
                                   .str.replace(',', ' ') +
                                   config.UNIQUE_TICKERS[currency])
    return df