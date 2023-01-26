import pandas as pd

from src.data.get_finance import get_rates


def convert_transaction(df_to_convert: pd.DataFrame, to_curr: str, target_col: str):
    """
    Convert values of transactions to chosen currency.

    :param df_to_convert:
    :param to_curr:
    :param target_col:
    :return:
    """
    currency_to_convert = set(df_to_convert['Валюта'].unique()) - {to_curr}

    for curr_name in currency_to_convert:
        # Get currency df
        curr_smpl = df_to_convert[df_to_convert['Валюта'] == curr_name]
        smpl_index = curr_smpl.index

        # Get rates
        curr_rates = get_rates(tickers=[f'{curr_name}{to_curr}=X'],
                               min_date=curr_smpl['Дата'].min(),
                               max_date=curr_smpl['Дата'].max())

        # Merge with transactions by date
        curr_smpl = (curr_smpl.merge(curr_rates.reset_index().rename(columns={"index": "Дата",
                                                                              "Date": "Дата"},
                                                                     errors='ignore'),
                                     on='Дата', how='left'))

        # Convert values
        curr_smpl[target_col] = curr_smpl[target_col] * curr_smpl[f'{curr_name}{to_curr}=X']
        curr_smpl['Валюта'] = to_curr

        # Drop rate column
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
