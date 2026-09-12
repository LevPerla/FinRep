import pandas as pd

from src.data.get_finance import get_actual_fx_rate, get_rates, require_fx_rate


def convert_transaction(df_to_convert: pd.DataFrame, to_curr: str, target_col: str, use_current_rate: bool = False):
    """
    Convert values of transactions to chosen currency with improved error handling.

    :param df_to_convert:
    :param to_curr:
    :param target_col:
    :return:
    """
    import logging
    logger = logging.getLogger(__name__)
    df_to_convert = df_to_convert.copy()
    
    currency_to_convert = set(df_to_convert['Валюта'].unique()) - {to_curr}

    for curr_name in currency_to_convert:
        curr_smpl = df_to_convert[df_to_convert['Валюта'] == curr_name]
        smpl_index = curr_smpl.index
        ticker = f'{curr_name}{to_curr}=X'

        if use_current_rate:
            current_rate = get_actual_fx_rate(curr_name, to_curr)
            rate_to_apply = require_fx_rate(current_rate, curr_name, to_curr)
            curr_smpl[target_col] = curr_smpl[target_col] * rate_to_apply
        else:
            curr_rates = None
            try:
                curr_rates = get_rates(tickers=[ticker],
                                       min_date=curr_smpl['Дата'].min(),
                                       max_date=curr_smpl['Дата'].max())
            except Exception as e:
                logger.warning(f"Failed to get FX rates for {curr_name} to {to_curr}: {e}")
            if curr_rates is not None and not curr_rates.empty and ticker in curr_rates.columns:
                curr_smpl = (curr_smpl.merge(curr_rates.reset_index().rename(columns={"index": "Дата",
                                                                                    "Date": "Дата"},
                                                                            errors='ignore'),
                                            on='Дата', how='left'))
                if curr_smpl[ticker].isna().any():
                    missing_date = curr_smpl.loc[curr_smpl[ticker].isna(), 'Дата'].min()
                    require_fx_rate(None, curr_name, to_curr, missing_date)
                curr_smpl[target_col] = curr_smpl[target_col] * curr_smpl[ticker]
                curr_smpl.drop(ticker, axis=1, inplace=True)
            else:
                require_fx_rate(None, curr_name, to_curr, curr_smpl['Дата'].min())
        
        curr_smpl['Валюта'] = to_curr
        curr_smpl.index = smpl_index
        df_to_convert.loc[df_to_convert['Валюта'] == curr_name] = curr_smpl
            
    return df_to_convert.round(2)


def _latest_rate(rates: pd.DataFrame | None, ticker: str):
    if rates is None or rates.empty:
        return None
    if ticker in rates.columns:
        values = pd.to_numeric(rates[ticker], errors='coerce').dropna()
    else:
        values = pd.to_numeric(rates.stack(), errors='coerce').dropna()
    if values.empty:
        return None
    return float(values.iloc[-1])


if __name__ == '__main__':
    pd.options.display.max_columns = 40
    pd.options.display.max_rows = 40
    from src.data.get import get_transactions

    transactions_df = get_transactions()
    
    sell_df = transactions_df[transactions_df.Категория.isin(['Доход'])]
    
    print(sell_df[sell_df['Дата'] == '2016-10-24'])
    
    test = convert_transaction(sell_df, to_curr='USD', target_col='Значение', use_current_rate=False)
    # print(test[test['Значение'] != 0])
    
    print(test[test['Дата'] == '2016-10-24'])
