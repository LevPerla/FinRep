"""
Exchange rates information module for dashboard display
"""

import sys
import os
sys.path.append('/Users/levperla/PycharmProjects/FinRep')

import pandas as pd
from datetime import datetime, timedelta
import logging

from src.data.get_finance import get_actual_rates, get_rates
from src.data.get import get_transactions
from src import config

logger = logging.getLogger(__name__)

def get_exchange_rates_info(target_currency='RUB'):
    """
    Get information about exchange rates used for conversion
    Returns DataFrame with currency pairs, rates, and freshness info
    """
    try:
        # Get all unique currencies from transactions
        transactions_df = get_transactions()
        unique_currencies = set(transactions_df['Валюта'].unique())
        unique_currencies.discard(target_currency)  # Remove target currency
        
        if not unique_currencies:
            return pd.DataFrame()
        
        rates_info = []
        current_date = datetime.now().date()
        
        for currency in unique_currencies:
            try:
                # Get current rate
                ticker = f'{currency}{target_currency}=X'
                current_rates = get_actual_rates([ticker])
                
                if not current_rates.empty and f'Актуальная_цена_{config.STOCK_API}' in current_rates.columns:
                    rate = pd.to_numeric(
                        current_rates[f'Актуальная_цена_{config.STOCK_API}'],
                        errors='coerce'
                    ).iloc[0]
                    if not pd.isna(rate):
                        # Check if this is actually a fallback rate by comparing with config values
                        fallback_rate = pd.to_numeric(
                            pd.Series([config.FALLBACK_RATES.get(currency, {}).get(target_currency)]),
                            errors='coerce'
                        ).iloc[0]
                        if fallback_rate and abs(rate - fallback_rate) < (fallback_rate * 0.05):  # 5% tolerance
                            # This is very close to our hardcoded fallback rate
                            rate_source = 'Fallback (Hardcoded)'
                            is_fallback = True
                        else:
                            # This came from Yahoo Finance
                            rate_source = f'Yahoo Finance ({config.STOCK_API})'
                            is_fallback = False
                    else:
                        rate = None
                        rate_source = 'Yahoo Finance (No Data)'
                        is_fallback = True
                else:
                    rate = None
                    rate_source = 'Yahoo Finance (Empty)'
                    is_fallback = True
                
                # Use fallback rates if no valid rate was obtained
                if rate is None or pd.isna(rate):
                    fallback_rate = pd.to_numeric(
                        pd.Series([config.FALLBACK_RATES.get(currency, {}).get(target_currency)]),
                        errors='coerce'
                    ).iloc[0]
                    if fallback_rate:
                        rate = fallback_rate
                        rate_source = 'Fallback (Hardcoded)'
                        is_fallback = True
                    else:
                        rate = 'Ошибка'
                        rate_source = 'Недоступно'
                        is_fallback = True
                
                # Get historical rate for comparison (last 7 days)
                try:
                    historical_rates = get_rates(
                        tickers=[ticker],
                        min_date=current_date - timedelta(days=7),
                        max_date=current_date
                    )
                    
                    if not historical_rates.empty:
                        if hasattr(historical_rates, 'columns') and ticker in historical_rates.columns:
                            # DataFrame case
                            last_historical_rate = pd.to_numeric(
                                historical_rates[ticker],
                                errors='coerce'
                            ).dropna().iloc[-1]
                            last_update = historical_rates.index[-1].strftime('%Y-%m-%d')
                        else:
                            # Series case
                            last_historical_rate = pd.to_numeric(
                                historical_rates,
                                errors='coerce'
                            ).dropna().iloc[-1]
                            last_update = historical_rates.index[-1].strftime('%Y-%m-%d')
                        
                        rate_change = ((rate - last_historical_rate) / last_historical_rate * 100) if rate and last_historical_rate else 0
                    else:
                        rate_change = 0
                        last_update = 'N/A'
                except Exception as e:
                    logger.warning(f"Could not get historical rates for {ticker}: {e}")
                    rate_change = 0
                    last_update = 'N/A'
                
                # Calculate inverse rate (1/rate) for reference
                inverse_rate = 1 / rate if isinstance(rate, (int, float)) and rate != 0 else None
                
                rates_info.append({
                    'Валюта': currency,
                    'Курс': f"{rate:.4f}" if rate else "N/A",
                    'Обратный курс': f"{inverse_rate:.4f}" if inverse_rate else "N/A",
                    'Источник': rate_source,
                    'Изменение (%)': f"{rate_change:+.2f}%" if rate_change != 0 else "N/A",
                    'Последнее обновление': last_update,
                    'Fallback': 'Да' if is_fallback else 'Нет'
                })
                
            except Exception as e:
                logger.warning(f"Error getting rate info for {currency}: {e}")
                rates_info.append({
                    'Валюта': currency,
                    'Курс': "Ошибка",
                    'Обратный курс': "N/A",
                    'Источник': "Недоступно",
                    'Изменение (%)': "N/A",
                    'Последнее обновление': "N/A",
                    'Fallback': 'Да'
                })
        
        return pd.DataFrame(rates_info)
        
    except Exception as e:
        logger.error(f"Error getting exchange rates info: {e}")
        return pd.DataFrame()

def get_currency_conversion_summary(target_currency='RUB'):
    """
    Get summary of currency conversion statistics
    """
    try:
        transactions_df = get_transactions()
        unique_currencies = set(transactions_df['Валюта'].unique())
        unique_currencies.discard(target_currency)
        
        summary_info = []
        
        for currency in unique_currencies:
            currency_transactions = transactions_df[transactions_df['Валюта'] == currency]
            total_amount = currency_transactions['Значение'].sum()
            transaction_count = len(currency_transactions)
            
            # Get conversion rate
            ticker = f'{currency}{target_currency}=X'
            try:
                current_rates = get_actual_rates([ticker])
                if not current_rates.empty and f'Актуальная_цена_{config.STOCK_API}' in current_rates.columns:
                    rate = pd.to_numeric(
                        current_rates[f'Актуальная_цена_{config.STOCK_API}'],
                        errors='coerce'
                    ).iloc[0]
                    if pd.isna(rate):
                        rate = config.FALLBACK_RATES.get(currency, {}).get(target_currency, 1.0)
                    converted_amount = total_amount * rate
                else:
                    # Use fallback rates from config
                    rate = config.FALLBACK_RATES.get(currency, {}).get(target_currency, 1.0)
                    converted_amount = total_amount * rate
            except:
                rate = 1.0
                converted_amount = total_amount
            
            summary_info.append({
                'Валюта': currency,
                'Количество транзакций': transaction_count,
                f'Сумма в {currency}': f"{total_amount:,.2f}",
                f'Сумма в {target_currency}': f"{converted_amount:,.2f}",
                'Курс конвертации': f"{rate:.4f}"
            })
        
        return pd.DataFrame(summary_info)
        
    except Exception as e:
        logger.error(f"Error getting currency conversion summary: {e}")
        return pd.DataFrame()

if __name__ == '__main__':
    # Test the functions
    print("Exchange Rates Info:")
    rates_info = get_exchange_rates_info('RUB')
    print(rates_info)
    
    print("\nCurrency Conversion Summary:")
    summary = get_currency_conversion_summary('RUB')
    print(summary)
