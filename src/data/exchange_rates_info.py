"""
Exchange rates information module for dashboard display
"""

import pandas as pd
import logging

from src.data.get_finance import get_actual_fx_rate, get_fx_rate_info
from src.data.get import get_transactions

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
        for currency in unique_currencies:
            try:
                rate_info = get_fx_rate_info(currency, target_currency)
                rate = rate_info['rate']
                if rate is None:
                    rate_source = 'Недоступно'
                    rate_change = None
                else:
                    rate_source = rate_info['source']
                    rate_change = rate_info['change_pct']
                
                # Calculate inverse rate (1/rate) for reference
                inverse_rate = 1 / rate if isinstance(rate, (int, float)) and rate != 0 else None
                
                rates_info.append({
                    'Валюта': currency,
                    'Курс': f"{rate:.4f}" if isinstance(rate, (int, float)) else str(rate),
                    'Обратный курс': f"{inverse_rate:.4f}" if inverse_rate else "N/A",
                    'Источник': rate_source,
                    'Изменение (%)': f"{rate_change:+.2f}%" if rate_change is not None else "N/A",
                })
                
            except Exception as e:
                logger.warning(f"Error getting rate info for {currency}: {e}")
                rates_info.append({
                    'Валюта': currency,
                    'Курс': "Ошибка",
                    'Обратный курс': "N/A",
                    'Источник': "Недоступно",
                    'Изменение (%)': "N/A",
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
            
            rate = get_actual_fx_rate(currency, target_currency) or 1.0
            converted_amount = total_amount * rate
            
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
