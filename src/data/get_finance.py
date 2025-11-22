import os.path
import time
import logging
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import requests
import yfinance as yf
import yfinance.shared as shared
from bs4 import BeautifulSoup
from twelvedata import TDClient
from alpha_vantage.timeseries import TimeSeries

from src import config, utils

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def get_actual_rates(tickers, max_retries=3):
    """
    Get rates of current day with fallback mechanisms
    :param tickers: list of tickers to fetch
    :param max_retries: maximum number of retry attempts
    :return: DataFrame with rates
    """
    if not isinstance(tickers, list):
        tickers = [tickers]
    
    logger.info(f"Fetching rates for tickers: {tickers}")
    
    # Try primary API first
    try:
        if config.STOCK_API == 'yf':
            cur_date = datetime.now().date()
            curr_rates = get_rates_with_fallback(tickers, min_date=cur_date, max_date=cur_date, max_retries=max_retries)
            if curr_rates is not None and not curr_rates.empty:
                curr_rates = curr_rates.reset_index(drop=True).T.reset_index()
                curr_rates.columns = ['ticker_', 'Актуальная_цена_yf']
                return curr_rates
        elif config.STOCK_API == 'td':
            td = TDClient(apikey=utils.get_secrets('TD_API_KEY'))
            curr_rates = td.time_series(
                symbol=tickers,
                interval="5min",
                outputsize=1,
            ).as_pandas()['close']

            if isinstance(tickers, str) or len(tickers) == 1:
                ticker_name = tickers if isinstance(tickers, str) else tickers[0]
                curr_rates = curr_rates.rename(ticker_name).to_frame().T.reset_index()
            else:
                curr_rates = curr_rates.reset_index().drop('level_1', axis=1, errors='ignore')
            curr_rates.columns = ['ticker_', 'Актуальная_цена_td']
            return curr_rates
    except Exception as e:
        logger.warning(f"Primary API failed: {e}")
    
    # Fallback to alternative APIs
    return get_rates_fallback(tickers)


def get_rates_fallback(tickers):
    """
    Fallback method to get rates using alternative sources
    """
    logger.info("Using fallback methods for rate fetching")
    
    # Try Alpha Vantage as fallback
    try:
        av_key = utils.get_secrets('ALPHA_VANTAGE_KEY')
        if av_key:
            ts = TimeSeries(key=av_key, output_format='pandas')
            rates_data = []
            
            for ticker in tickers:
                try:
                    # Convert Yahoo Finance format to Alpha Vantage format
                    if '=X' in ticker:
                        # Currency pair
                        pair = ticker.replace('=X', '')
                        data, _ = ts.get_currency_exchange_rate(from_currency=pair[:3], to_currency=pair[3:])
                        if not data.empty:
                            rate = data.iloc[0]['5. Exchange Rate']
                            rates_data.append({'ticker_': ticker, 'Актуальная_цена_av': rate})
                except Exception as e:
                    logger.warning(f"Alpha Vantage failed for {ticker}: {e}")
                    continue
            
            if rates_data:
                return pd.DataFrame(rates_data)
    except Exception as e:
        logger.warning(f"Alpha Vantage fallback failed: {e}")
    
    # Try manual cross-rate calculation for problematic pairs
    manual_rates = get_cross_rates_manual(tickers)
    if not manual_rates.empty:
        # Rename the column to match the expected format
        manual_rates = manual_rates.rename(columns={'Актуальная_цена_manual': f'Актуальная_цена_{config.STOCK_API}'})
        return manual_rates
    else:
        return pd.DataFrame()


def get_cross_rates_manual(tickers):
    """
    Manual cross-rate calculation for problematic currency pairs
    """
    logger.info("Using manual cross-rate calculation")
    rates_data = []
    
    for ticker in tickers:
        try:
            if 'KZT' in ticker and 'RUB' in ticker:
                # KZT to RUB: Use USD as intermediate
                kzt_usd = get_single_rate('KZTUSD=X')
                usd_rub = get_single_rate('USDRUB=X')
                if kzt_usd is not None and usd_rub is not None:
                    rate = kzt_usd * usd_rub
                    rates_data.append({'ticker_': ticker, 'Актуальная_цена_manual': rate})
                else:
                    # Fallback: Use rate from config
                    fallback_rate = config.FALLBACK_RATES.get('KZT', {}).get('RUB', 0.15)
                    logger.warning(f"Using fallback rate for {ticker}: {fallback_rate}")
                    rates_data.append({'ticker_': ticker, 'Актуальная_цена_manual': fallback_rate})
            elif 'RUB' in ticker and 'KZT' in ticker:
                # RUB to KZT: Use USD as intermediate
                rub_usd = get_single_rate('RUBUSD=X')
                usd_kzt = get_single_rate('USDKZT=X')
                if rub_usd is not None and usd_kzt is not None:
                    rate = rub_usd * usd_kzt
                    rates_data.append({'ticker_': ticker, 'Актуальная_цена_manual': rate})
                else:
                    # Fallback: Use rate from config
                    fallback_rate = config.FALLBACK_RATES.get('RUB', {}).get('KZT', 6.67)
                    logger.warning(f"Using fallback rate for {ticker}: {fallback_rate}")
                    rates_data.append({'ticker_': ticker, 'Актуальная_цена_manual': fallback_rate})
        except Exception as e:
            logger.warning(f"Manual calculation failed for {ticker}: {e}")
    
    return pd.DataFrame(rates_data) if rates_data else pd.DataFrame()


def get_single_rate(ticker, max_retries=3):
    """
    Get a single rate with retry logic
    """
    for attempt in range(max_retries):
        try:
            data = yf.download(ticker, period="1d", progress=False)
            if not data.empty and 'Close' in data.columns:
                rate = data['Close'].iloc[-1]
                # Ensure we return a scalar value, not a Series
                if hasattr(rate, 'iloc'):
                    return rate.iloc[0] if len(rate) > 0 else None
                return rate
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed for {ticker}: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
    return None


def get_rates_with_fallback(tickers: list, min_date, max_date, max_retries=3) -> pd.DataFrame:
    """
    Get rates with improved error handling and fallback mechanisms
    """
    logger.info(f"Fetching rates for {tickers} from {min_date} to {max_date}")
    
    for attempt in range(max_retries):
        try:
            # Filter out problematic tickers
            valid_tickers = []
            problematic_tickers = []
            
            for ticker in tickers:
                if is_problematic_ticker(ticker):
                    problematic_tickers.append(ticker)
                    logger.warning(f"Ticker {ticker} is known to be problematic, will use fallback")
                else:
                    valid_tickers.append(ticker)
            
            # Try to get rates for valid tickers
            if valid_tickers:
                rates_df_upd = yf.download(valid_tickers, min_date - timedelta(days=5), max_date, progress=False)['Close']
                rates_df_upd.index = pd.DatetimeIndex(pd.to_datetime(rates_df_upd.index).date)
                full_ind = pd.date_range(min_date - timedelta(days=5), max_date, freq='D')
                rates_df_upd = (rates_df_upd.reindex(full_ind, fill_value=np.nan).ffill().bfill())
                
                if isinstance(rates_df_upd, pd.Series):
                    rates_df_upd = rates_df_upd.rename(valid_tickers[0]).to_frame()
            else:
                # If all tickers are problematic, create empty DataFrame with proper structure
                rates_df_upd = pd.DataFrame(index=pd.date_range(min_date - timedelta(days=5), max_date, freq='D'))
            
            # Handle problematic tickers with cross-rate calculation
            for ticker in problematic_tickers:
                try:
                    cross_rate = get_cross_rate_for_ticker(ticker, min_date, max_date)
                    if cross_rate is not None and not cross_rate.empty:
                        rates_df_upd[ticker] = cross_rate[ticker].squeeze()
                    else:
                        # Try fallback rates
                        fallback_rates = get_rates_fallback([ticker])
                        if not fallback_rates.empty and f'Актуальная_цена_{config.STOCK_API}' in fallback_rates.columns:
                            fallback_rate = fallback_rates[f'Актуальная_цена_{config.STOCK_API}'].iloc[0]
                            if not pd.isna(fallback_rate):
                                rates_df_upd[ticker] = fallback_rate
                                logger.info(f"Using fallback rate for {ticker}: {fallback_rate}")
                except Exception as e:
                    logger.warning(f"Cross-rate calculation failed for {ticker}: {e}")
            
            return rates_df_upd.loc[min_date:max_date].sort_index()
            
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed: {e}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # Exponential backoff
    
    logger.error(f"All attempts failed for tickers: {tickers}")
    return None


def is_problematic_ticker(ticker):
    """
    Check if a ticker is known to cause issues
    """
    problematic_patterns = [
        'KZTRUB=X', 'RUBKZT=X',  # KZT-RUB pairs often fail
        'KZTUSD=X', 'USDKZT=X',  # KZT-USD pairs sometimes fail
    ]
    return ticker in problematic_patterns


def get_cross_rate_for_ticker(ticker, min_date, max_date):
    """
    Calculate cross-rate for problematic tickers
    """
    try:
        if 'KZT' in ticker and 'RUB' in ticker:
            # KZT to RUB via USD
            kzt_usd_data = yf.download('KZTUSD=X', min_date, max_date, progress=False)
            usd_rub_data = yf.download('USDRUB=X', min_date, max_date, progress=False)
            
            # Handle multi-dimensional data
            if isinstance(kzt_usd_data, pd.DataFrame):
                kzt_usd = kzt_usd_data['Close'] if 'Close' in kzt_usd_data.columns else kzt_usd_data.iloc[:, -1]
            else:
                kzt_usd = kzt_usd_data
                
            if isinstance(usd_rub_data, pd.DataFrame):
                usd_rub = usd_rub_data['Close'] if 'Close' in usd_rub_data.columns else usd_rub_data.iloc[:, -1]
            else:
                usd_rub = usd_rub_data
                
            # Ensure we have Series
            if isinstance(kzt_usd, pd.DataFrame):
                kzt_usd = kzt_usd.iloc[:, 0]
            if isinstance(usd_rub, pd.DataFrame):
                usd_rub = usd_rub.iloc[:, 0]
                
            if not kzt_usd.empty and not usd_rub.empty:
                cross_rate = kzt_usd * usd_rub
                return pd.DataFrame({ticker: cross_rate}, index=cross_rate.index)
        elif 'RUB' in ticker and 'KZT' in ticker:
            # RUB to KZT via USD
            rub_usd_data = yf.download('RUBUSD=X', min_date, max_date, progress=False)
            usd_kzt_data = yf.download('USDKZT=X', min_date, max_date, progress=False)
            
            # Handle multi-dimensional data
            if isinstance(rub_usd_data, pd.DataFrame):
                rub_usd = rub_usd_data['Close'] if 'Close' in rub_usd_data.columns else rub_usd_data.iloc[:, -1]
            else:
                rub_usd = rub_usd_data
                
            if isinstance(usd_kzt_data, pd.DataFrame):
                usd_kzt = usd_kzt_data['Close'] if 'Close' in usd_kzt_data.columns else usd_kzt_data.iloc[:, -1]
            else:
                usd_kzt = usd_kzt_data
                
            # Ensure we have Series
            if isinstance(rub_usd, pd.DataFrame):
                rub_usd = rub_usd.iloc[:, 0]
            if isinstance(usd_kzt, pd.DataFrame):
                usd_kzt = usd_kzt.iloc[:, 0]
                
            if not rub_usd.empty and not usd_kzt.empty:
                cross_rate = rub_usd * usd_kzt
                return pd.DataFrame({ticker: cross_rate}, index=cross_rate.index)
    except Exception as e:
        logger.warning(f"Cross-rate calculation failed for {ticker}: {e}")
    
    return None


def get_rates(tickers: list, min_date, max_date) -> pd.DataFrame:
    """
    Get rates from selected range of dates with improved error handling
    :param tickers: list of tickers
    :param min_date: minimal date to get
    :param max_date: maximal date to get
    :return: DataFrame with rates
    """
    RATES_PATH = os.path.join(config.DATA_PATH, 'rates')
    min_date = pd.to_datetime(min_date)
    max_date = pd.to_datetime(max_date)

    # Create rates file if not exist
    if 'rates' not in os.listdir(config.DATA_PATH):
        os.makedirs(RATES_PATH)

    # Try to get rates with fallback mechanisms
    rates_df_upd = get_rates_with_fallback(tickers, min_date, max_date)
    
    if rates_df_upd is None or rates_df_upd.empty:
        logger.warning("Failed to get any rates, returning empty DataFrame")
        return pd.DataFrame()

    # Handle any remaining missing data
    nan_cols = set(rates_df_upd.loc[:, rates_df_upd.isna().all(axis=0)].columns)
    download_errors = set(shared._ERRORS.keys())
    
    for ticker_name in download_errors.intersection(nan_cols):
        if '=X' in ticker_name:
            try:
                cross_rate_df = get_cross_rates(from_curr=ticker_name[:3],
                                                to_curr=ticker_name[3:6],
                                                min_date=min_date - timedelta(days=5),
                                                max_date=max_date)
                if not cross_rate_df.empty:
                    rates_df_upd[ticker_name] = cross_rate_df[ticker_name]
            except Exception as e:
                logger.warning(f"Cross-rate calculation failed for {ticker_name}: {e}")
    
    # Try fallback for any remaining missing rates
    for ticker_name in nan_cols:
        if '=X' in ticker_name:
            try:
                fallback_rates = get_rates_fallback([ticker_name])
                if not fallback_rates.empty and f'Актуальная_цена_{config.STOCK_API}' in fallback_rates.columns:
                    # Use the fallback rate for all dates
                    fallback_rate = fallback_rates[f'Актуальная_цена_{config.STOCK_API}'].iloc[0]
                    if not pd.isna(fallback_rate):
                        rates_df_upd[ticker_name] = fallback_rate
                        logger.info(f"Using fallback rate for {ticker_name}: {fallback_rate}")
            except Exception as e:
                logger.warning(f"Fallback failed for {ticker_name}: {e}")

    rates_df_upd.index.name = 'Дата'

    # Save updated rates
    try:
        rates_df_upd.reset_index().sort_values('Дата').to_csv(
            os.path.join(RATES_PATH, 'currency_rates.csv'), 
            index=False, 
            sep=';'
        )
    except Exception as e:
        logger.warning(f"Failed to save rates: {e}")

    return rates_df_upd.loc[min_date:max_date].sort_index()


def get_cross_rates(from_curr, to_curr, min_date, max_date):
    """
    Calculate cross rates with improved error handling
    """
    try:
        # Get rates of from_curr/USD
        curr_USD_data = yf.download(f'{from_curr}USD=X',
                                    min_date,
                                    max_date,
                                    progress=False)
        
        # Handle case where data might be multi-dimensional
        if isinstance(curr_USD_data, pd.DataFrame):
            if 'Close' in curr_USD_data.columns:
                curr_USD_rates = curr_USD_data['Close']
            else:
                # If no 'Close' column, take the last column (usually Close)
                curr_USD_rates = curr_USD_data.iloc[:, -1]
        else:
            curr_USD_rates = curr_USD_data
            
        # Ensure we have a Series
        if isinstance(curr_USD_rates, pd.DataFrame):
            curr_USD_rates = curr_USD_rates.iloc[:, 0]
            
        curr_USD_rates.name = f'{from_curr}USD=X'
        curr_USD_rates.index = pd.DatetimeIndex(pd.to_datetime(curr_USD_rates.index).date)
        full_ind = pd.date_range(min_date, max_date)
        curr_USD_rates = (curr_USD_rates.reindex(full_ind, fill_value=np.nan)
                          .interpolate(limit_direction='both'))

        # Get rates of USD/to_curr
        curr_data = yf.download(f'USD{to_curr}=X',
                               min_date,
                               max_date,
                               progress=False)
        
        # Handle case where data might be multi-dimensional
        if isinstance(curr_data, pd.DataFrame):
            if 'Close' in curr_data.columns:
                curr_rates = curr_data['Close']
            else:
                # If no 'Close' column, take the last column (usually Close)
                curr_rates = curr_data.iloc[:, -1]
        else:
            curr_rates = curr_data
            
        # Ensure we have a Series
        if isinstance(curr_rates, pd.DataFrame):
            curr_rates = curr_rates.iloc[:, 0]
            
        curr_rates.name = f'USD{to_curr}=X'
        curr_rates.index = pd.DatetimeIndex(pd.to_datetime(curr_rates.index).date)
        full_ind = pd.date_range(min_date, max_date)
        curr_rates = (curr_rates.reindex(full_ind, fill_value=np.nan)
                      .interpolate(limit_direction='both'))
        
        # Combine the rates
        combined_rates = pd.concat([curr_rates, curr_USD_rates], axis=1)
        combined_rates[f'{from_curr}{to_curr}=X'] = combined_rates[f'{from_curr}USD=X'] * combined_rates[f'USD{to_curr}=X']
        combined_rates.drop([f'{from_curr}USD=X', f'USD{to_curr}=X'], axis=1, inplace=True)
        return combined_rates
        
    except Exception as e:
        logger.warning(f"Cross-rate calculation failed for {from_curr}/{to_curr}: {e}")
        return pd.DataFrame()


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
    soup = BeautifulSoup(response.text, "lxml")
    shares_df = pd.DataFrame()
    rows = soup.findAll('row')
    for row in rows:
        try:
            shares_df = pd.concat([shares_df,
                                   pd.DataFrame([{'Дата': row['prevdate'],
                                                  'Тикер': row['secid'],
                                                  f'Актуальная_цена_moex_{mode}': row['prevadmittedquote']}])
                                   ],
                                  axis=0)
        except KeyError:
            continue

    if shares_df.size == 0:
        return pd.DataFrame([{'Тикер': 'nan', f'Актуальная_цена_moex_{mode}': 'nan'}])
    else:
        shares_df = shares_df.replace('', np.nan)
        shares_df = shares_df.astype({'Дата': 'datetime64[ns]',
                                      'Тикер': str,
                                      f'Актуальная_цена_moex_{mode}': float
                                      }, errors='ignore')
        shares_df.drop('Дата', axis=1, inplace=True)
        return shares_df


if __name__ == '__main__':
    print(get_act_moex(mode='stocks'))

    # print(get_rates(tickers=[
    #                          'USDRUB=X',
    #                          'RUBUSD=X',
    #                          'RUBKZT=X',
    #                          'KZTRUB=X'
    #                          ],
    #                 min_date='2023-01-26',
    #                 max_date='2023-01-26'))
