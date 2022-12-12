import pandas as pd
import numpy as np
from src import utils
import yfinance as yf
from datetime import datetime
from dateutil.relativedelta import relativedelta


def convert_transaction(df_to_convert, to_curr, target_col):
    currency_to_convert = set(df_to_convert['Валюта'].unique()) - {to_curr}

    for curr_name in currency_to_convert:
        curr_smpl = df_to_convert[df_to_convert['Валюта'] == curr_name]

        smpl_index = curr_smpl.index
        if curr_name == 'KZT' and to_curr == 'RUB':
            curr_rates = utils.get_cross_rates(from_curr='KZT', to_curr='RUB',
                                               min_date=curr_smpl['Дата'].min(),
                                               max_date=curr_smpl['Дата'].max())
        elif curr_name == 'RUB' and to_curr == 'KZT':
            curr_rates = utils.get_cross_rates(from_curr='RUB', to_curr='KZT',
                                               min_date=curr_smpl['Дата'].min(),
                                               max_date=curr_smpl['Дата'].max())
        else:
            curr_rates = yf.download(f'{curr_name}{to_curr}=X',
                                     curr_smpl['Дата'].min() - relativedelta(days=5),
                                     curr_smpl['Дата'].max()
                                     )['Adj Close']
            full_ind = pd.date_range(curr_smpl['Дата'].min() - relativedelta(days=5), curr_smpl['Дата'].max())
            curr_rates = (curr_rates.reindex(full_ind, fill_value=np.nan)
                          .interpolate(limit_direction='both')
                          .rename(f'{curr_name}{to_curr}=X'))
        curr_smpl = (curr_smpl.merge(curr_rates.reset_index().rename(columns={"index": "Дата", "Date": "Дата"},
                                                                     errors='ignore'), on='Дата', how='left'))
        curr_smpl[target_col] = curr_smpl[target_col] * curr_smpl[f'{curr_name}{to_curr}=X']
        curr_smpl['Валюта'] = to_curr
        curr_smpl.drop(f'{curr_name}{to_curr}=X', axis=1, inplace=True)
        curr_smpl.index = smpl_index
        df_to_convert.loc[df_to_convert['Валюта'] == curr_name] = curr_smpl
    return df_to_convert.round(2)

if __name__ == '__main__':
    pd.options.display.max_columns = 40
    pd.options.display.max_rows = 40
    from src.model.create_tables import create_invest_tbl
    buy_df, sell_df = create_invest_tbl(max_date=None)
    test = convert_transaction(sell_df, to_curr='USD', target_col='Прибыль/убыток')
    print(test)