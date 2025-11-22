from sys import path
path.append('/Users/levperla/PycharmProjects/FinRep')

import pandas as pd

from src.data.get_finance import get_rates
from src import config


def get_cross_rate(from_currency: str, to_currency: str) -> float:
    """
    Calculate cross rate between two currencies using USD as base
    Formula: from_currency/to_currency = (from_currency/USD) / (to_currency/USD)
    """
    if from_currency == to_currency:
        return 1.0
    
    if from_currency == 'USD':
        # Direct rate from USD to target
        # If we have target_currency/USD, then USD/target_currency = 1/(target_currency/USD)
        target_to_usd = config.FALLBACK_RATES.get(to_currency, {}).get('USD', 1.0)
        return 1.0 / target_to_usd if target_to_usd != 0 else 1.0
    
    if to_currency == 'USD':
        # Direct rate from source to USD
        return config.FALLBACK_RATES.get(from_currency, {}).get('USD', 1.0)
    
    # Cross rate calculation: (from_currency/USD) / (to_currency/USD)
    from_usd = config.FALLBACK_RATES.get(from_currency, {}).get('USD', 1.0)
    to_usd = config.FALLBACK_RATES.get(to_currency, {}).get('USD', 1.0)
    
    if to_usd == 0:
        return 1.0  # Avoid division by zero
    
    # For cross rates, we need to invert the formula
    # If we have from_currency/USD and to_currency/USD, then:
    # from_currency/to_currency = (from_currency/USD) / (to_currency/USD)
    # But we need to be careful about the direction
    cross_rate = from_usd / to_usd
    
    return cross_rate


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
    
    currency_to_convert = set(df_to_convert['Валюта'].unique()) - {to_curr}

    for curr_name in currency_to_convert:
        # Get currency df
        curr_smpl = df_to_convert[df_to_convert['Валюта'] == curr_name]
        smpl_index = curr_smpl.index

        # Get rates with improved fallback logic
        curr_rates = None
        fallback_rate = None
        
        # Step 1: Try to get rates from yfinance
        try:
            curr_rates = get_rates(tickers=[f'{curr_name}{to_curr}=X'],
                                   min_date=curr_smpl['Дата'].min(),
                                   max_date=curr_smpl['Дата'].max())
        except Exception as e:
            logger.warning(f"Failed to get rates from yfinance for {curr_name} to {to_curr}: {e}")
            curr_rates = None
        
        # Step 2: If yfinance failed or returned empty, try cross-rate via USD
        if curr_rates is None or curr_rates.empty:
            try:
                fallback_rate = get_cross_rate(curr_name, to_curr)
                if fallback_rate and fallback_rate != 1.0:
                    logger.warning(f"Using cross-rate via USD for {curr_name} to {to_curr}: {fallback_rate}")
                else:
                    fallback_rate = None
            except Exception as e:
                logger.warning(f"Failed to calculate cross-rate for {curr_name} to {to_curr}: {e}")
                fallback_rate = None
        
        # Step 3: If both failed, use fallback rates from config
        if fallback_rate is None:
            try:
                fallback_rate = get_cross_rate(curr_name, to_curr)
                if fallback_rate and fallback_rate != 1.0:
                    logger.warning(f"Using fallback rates for {curr_name} to {to_curr}: {fallback_rate}")
                else:
                    logger.warning(f"No rates available for {curr_name} to {to_curr}, skipping conversion")
                    continue
            except Exception as e:
                logger.warning(f"All rate methods failed for {curr_name} to {to_curr}: {e}, skipping conversion")
                continue
        
        # Process conversion based on rate type
        if use_current_rate:
            # For income: use current rate (latest available or fallback)
            if curr_rates is not None and not curr_rates.empty:
                # Try to get the latest non-NaN rate
                latest_rate = None
                for i in range(len(curr_rates) - 1, -1, -1):
                    rate_value = curr_rates.iloc[i].values
                    if len(rate_value) > 0 and not pd.isna(rate_value[0]):
                        latest_rate = rate_value[0]
                        break
                
                if latest_rate is not None:
                    curr_smpl[target_col] = curr_smpl[target_col] * latest_rate
                else:
                    # Use fallback rate
                    curr_smpl[target_col] = curr_smpl[target_col] * fallback_rate
            else:
                # Use fallback rate
                curr_smpl[target_col] = curr_smpl[target_col] * fallback_rate
        else:
            # For expenses: use historical rates by date
            if curr_rates is not None and not curr_rates.empty:
                # Merge with transactions by date
                curr_smpl = (curr_smpl.merge(curr_rates.reset_index().rename(columns={"index": "Дата",
                                                                                    "Date": "Дата"},
                                                                            errors='ignore'),
                                            on='Дата', how='left'))

                # Convert values only if rate column exists and has valid data
                rate_col = f'{curr_name}{to_curr}=X'
                if rate_col in curr_smpl.columns and not curr_smpl[rate_col].isna().all():
                    # Fill NaN rates with fallback rate
                    curr_smpl[rate_col] = curr_smpl[rate_col].fillna(fallback_rate)
                    curr_smpl[target_col] = curr_smpl[target_col] * curr_smpl[rate_col]
                    # Drop rate column
                    curr_smpl.drop(rate_col, axis=1, inplace=True)
                else:
                    # Use fallback rate for all transactions
                    curr_smpl[target_col] = curr_smpl[target_col] * fallback_rate
            else:
                # Use fallback rate for all transactions
                curr_smpl[target_col] = curr_smpl[target_col] * fallback_rate
        
        # Update currency and apply changes
        curr_smpl['Валюта'] = to_curr
        curr_smpl.index = smpl_index
        df_to_convert.loc[df_to_convert['Валюта'] == curr_name] = curr_smpl
            
    return df_to_convert.round(2)


if __name__ == '__main__':
    pd.options.display.max_columns = 40
    pd.options.display.max_rows = 40
    from src.data.get import get_transactions

    transactions_df = get_transactions()
    
    sell_df = transactions_df[transactions_df.Категория.isin(['Доход'])]
    
    print(sell_df[sell_df['Дата'] == '2016-10-24'])
    
    test = convert_transaction(sell_df, to_curr='USD', target_col='Значение', use_current_rate=True)
    # print(test[test['Значение'] != 0])
    
    print(test[test['Дата'] == '2016-10-24'])

