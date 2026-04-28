import os.path
import time
import logging
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

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

_CBR_RATES_CACHE = {}
_CBR_SERIES_CACHE = {}
_CBR_VALUTE_IDS = {
    'USD': 'R01235',
    'EUR': 'R01239',
    'GBP': 'R01035',
    'KZT': 'R01335',
    'CHF': 'R01775',
    'CNY': 'R01375',
    'JPY': 'R01820',
}


def _build_retry_session():
    session = requests.Session()
    retries = Retry(
        total=2,
        read=2,
        connect=2,
        backoff_factor=0.8,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


_CBR_SESSION = _build_retry_session()
_FX_NETWORK_ENABLED = True
logger.info(
    "FX network mode: %s",
    "online" if _FX_NETWORK_ENABLED else "offline",
)


def set_fx_network_enabled(enabled: bool):
    """
    Set FX network mode globally for this process.
    """
    global _FX_NETWORK_ENABLED
    prev_value = _FX_NETWORK_ENABLED
    _FX_NETWORK_ENABLED = bool(enabled)
    logger.info("FX network mode switched to: %s", "online" if _FX_NETWORK_ENABLED else "offline")
    return prev_value


def _is_fx_ticker(ticker: str) -> bool:
    return isinstance(ticker, str) and ticker.endswith('=X') and len(ticker) >= 7


def _to_date(value):
    return pd.to_datetime(value).date()


def _fallback_rate_from_config(from_curr, to_curr):
    """
    Calculate cross-rate from config.FALLBACK_RATES (which stores currency->USD).
    """
    from_curr = str(from_curr).upper()
    to_curr = str(to_curr).upper()
    if from_curr == to_curr:
        return 1.0
    if from_curr == 'USD':
        to_usd = config.FALLBACK_RATES.get(to_curr, {}).get('USD')
        return (1.0 / to_usd) if to_usd else None
    if to_curr == 'USD':
        return config.FALLBACK_RATES.get(from_curr, {}).get('USD')

    from_usd = config.FALLBACK_RATES.get(from_curr, {}).get('USD')
    to_usd = config.FALLBACK_RATES.get(to_curr, {}).get('USD')
    if from_usd is None or to_usd in (None, 0):
        return None
    return from_usd / to_usd


def _fetch_cbr_currency_series(currency, min_date, max_date):
    """
    Fetch RUB-per-currency series from CBR in one request for the whole period.
    """
    curr = currency.upper()
    if curr == 'RUB':
        full_ind = pd.date_range(_to_date(min_date), _to_date(max_date), freq='D')
        return pd.Series(1.0, index=full_ind, name='RUB')

    val_id = _CBR_VALUTE_IDS.get(curr)
    if not val_id:
        return pd.Series(dtype=float, name=curr)

    start = _to_date(min_date)
    end = _to_date(max_date)
    cache_key = (curr, start.isoformat(), end.isoformat())
    if cache_key in _CBR_SERIES_CACHE:
        return _CBR_SERIES_CACHE[cache_key]

    params = {
        'date_req1': start.strftime('%d/%m/%Y'),
        'date_req2': end.strftime('%d/%m/%Y'),
        'VAL_NM_RQ': val_id,
    }
    if not _FX_NETWORK_ENABLED:
        return pd.Series(dtype=float, name=curr)
    try:
        response = _CBR_SESSION.get(
            "https://www.cbr.ru/scripts/XML_dynamic.asp",
            params=params,
            timeout=(5, 20),
        )
        response.raise_for_status()
        root = ET.fromstring(response.content)
        values = {}
        for record in root.findall('Record'):
            date_text = record.get('Date')
            nominal_text = record.findtext('Nominal')
            value_text = record.findtext('Value')
            if not date_text or not nominal_text or not value_text:
                continue
            nominal = float(nominal_text.replace(',', '.'))
            value = float(value_text.replace(',', '.'))
            if nominal != 0:
                day = datetime.strptime(date_text, '%d.%m.%Y').date()
                values[pd.Timestamp(day)] = value / nominal

        series = pd.Series(values, dtype=float, name=curr).sort_index()
        full_ind = pd.date_range(start, end, freq='D')
        series = series.reindex(full_ind).ffill().bfill()
        _CBR_SERIES_CACHE[cache_key] = series
        return series
    except Exception as e:
        logger.warning(f"CBR dynamic request failed for {curr} [{start}..{end}]: {e}")
        empty_series = pd.Series(dtype=float, name=curr)
        _CBR_SERIES_CACHE[cache_key] = empty_series
        return empty_series


def _get_cbr_rates_df(tickers, min_date, max_date):
    """
    Get FX rates for Yahoo-style tickers via CBR.
    """
    fx_tickers = [ticker for ticker in tickers if _is_fx_ticker(ticker)]
    if not fx_tickers:
        return pd.DataFrame()

    start = _to_date(min_date)
    end = _to_date(max_date)
    full_ind = pd.date_range(start, end, freq='D')
    out = pd.DataFrame(index=full_ind)
    currencies = set()
    for ticker in fx_tickers:
        currencies.add(ticker[:3].upper())
        currencies.add(ticker[3:6].upper())

    rub_per_currency = {}
    for curr in currencies:
        rub_per_currency[curr] = _fetch_cbr_currency_series(curr, start, end)

    for ticker in fx_tickers:
        from_curr = ticker[:3].upper()
        to_curr = ticker[3:6].upper()
        from_series = rub_per_currency.get(from_curr, pd.Series(dtype=float))
        to_series = rub_per_currency.get(to_curr, pd.Series(dtype=float))
        if from_series.empty or to_series.empty:
            continue
        merged = pd.concat([from_series.rename('from'), to_series.rename('to')], axis=1).reindex(full_ind)
        with np.errstate(divide='ignore', invalid='ignore'):
            out[ticker] = merged['from'] / merged['to']

    if out.empty:
        return pd.DataFrame()

    out = out.apply(pd.to_numeric, errors='coerce').ffill().bfill()
    return out


def _to_close_series(data, name=None):
    """
    Convert yfinance download result to a float Series.
    """
    if data is None or (hasattr(data, "empty") and data.empty):
        return pd.Series(dtype=float, name=name)

    if isinstance(data, pd.DataFrame):
        if 'Close' in data.columns:
            series = data['Close']
        else:
            series = data.iloc[:, -1]
    else:
        series = data

    if isinstance(series, pd.DataFrame):
        series = series.iloc[:, 0]

    series = pd.to_numeric(series, errors='coerce')
    if name:
        series.name = name
    return series


def _download_fx_pair_series(ticker, min_date=None, max_date=None):
    """
    Download FX pair with Yahoo-compatible aliases and return a canonical series for ticker.
    Supports both direct pair symbols and USD-quoted single-currency symbols (e.g., RUB=X).
    """
    if '=X' not in ticker or len(ticker) < 7:
        return pd.Series(dtype=float, name=ticker)

    from_curr = ticker[:3]
    to_curr = ticker[3:6]
    canonical_name = f'{from_curr}{to_curr}=X'

    if not _FX_NETWORK_ENABLED:
        return pd.Series(dtype=float, name=canonical_name)

    # Prefer CBR for FX pairs (more stable than Yahoo for RUB/KZT pairs).
    try:
        cbr_min = min_date if min_date is not None else datetime.now().date()
        cbr_max = max_date if max_date is not None else datetime.now().date()
        cbr_df = _get_cbr_rates_df([canonical_name], cbr_min, cbr_max)
        if not cbr_df.empty and canonical_name in cbr_df.columns:
            cbr_series = pd.to_numeric(cbr_df[canonical_name], errors='coerce')
            if not cbr_series.empty and not cbr_series.isna().all():
                cbr_series.name = canonical_name
                return cbr_series
    except Exception as e:
        logger.warning(f"CBR rate fetch failed for {canonical_name}, falling back to Yahoo: {e}")

    start = min_date
    end = max_date
    if start is None or end is None:
        period = "5d"
        direct = _to_close_series(yf.download(ticker, period=period, progress=False), name=canonical_name)
    else:
        direct = _to_close_series(yf.download(ticker, start, end, progress=False), name=canonical_name)

    if not direct.empty and not direct.isna().all():
        return direct

    # Handle pairs involving USD via single-currency Yahoo symbols.
    if from_curr == 'USD':
        # USDXXX can often be fetched as XXX=X (e.g., RUB=X, KZT=X).
        alias = f'{to_curr}=X'
        if start is None or end is None:
            alias_series = _to_close_series(yf.download(alias, period="5d", progress=False), name=canonical_name)
        else:
            alias_series = _to_close_series(yf.download(alias, start, end, progress=False), name=canonical_name)
        return alias_series

    if to_curr == 'USD':
        # XXXUSD can often be derived as 1 / (USDXXX) from XXX=X.
        alias = f'{from_curr}=X'
        if start is None or end is None:
            alias_series = _to_close_series(yf.download(alias, period="5d", progress=False), name=canonical_name)
        else:
            alias_series = _to_close_series(yf.download(alias, start, end, progress=False), name=canonical_name)
        if alias_series.empty:
            return alias_series
        with np.errstate(divide='ignore', invalid='ignore'):
            inverted = 1.0 / alias_series
        inverted.name = canonical_name
        return inverted

    # Generic cross-rate via USD single-currency symbols.
    from_alias = f'{from_curr}=X'
    to_alias = f'{to_curr}=X'
    if start is None or end is None:
        from_series = _to_close_series(yf.download(from_alias, period="5d", progress=False), name=from_alias)
        to_series = _to_close_series(yf.download(to_alias, period="5d", progress=False), name=to_alias)
    else:
        from_series = _to_close_series(yf.download(from_alias, start, end, progress=False), name=from_alias)
        to_series = _to_close_series(yf.download(to_alias, start, end, progress=False), name=to_alias)

    if from_series.empty or to_series.empty:
        return pd.Series(dtype=float, name=canonical_name)

    merged = pd.concat([from_series, to_series], axis=1)
    with np.errstate(divide='ignore', invalid='ignore'):
        # from/to = (USD/to) / (USD/from)
        cross = merged[to_alias] / merged[from_alias]
    cross.name = canonical_name
    return cross


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

    # Offline-first: direct config cross-rates for FX, no network and no Yahoo retries.
    if not _FX_NETWORK_ENABLED:
        cfg_rates = []
        for ticker in tickers:
            if _is_fx_ticker(ticker):
                rate = _fallback_rate_from_config(ticker[:3], ticker[3:6])
                if rate is not None:
                    cfg_rates.append({'ticker_': ticker, f'Актуальная_цена_{config.STOCK_API}': float(rate)})
        if cfg_rates:
            return pd.DataFrame(cfg_rates)

    # Try CBR as a stable fallback for FX first
    try:
        today = datetime.now().date()
        cbr_df = _get_cbr_rates_df(tickers, today, today)
        if not cbr_df.empty:
            rates_data = []
            for ticker in tickers:
                if ticker in cbr_df.columns:
                    rate = pd.to_numeric(cbr_df[ticker], errors='coerce').dropna()
                    if not rate.empty:
                        rates_data.append({
                            'ticker_': ticker,
                            f'Актуальная_цена_{config.STOCK_API}': float(rate.iloc[-1])
                        })
            if rates_data:
                return pd.DataFrame(rates_data)
    except Exception as e:
        logger.warning(f"CBR fallback failed: {e}")
    
    # Try Alpha Vantage as fallback (only if network explicitly enabled)
    if _FX_NETWORK_ENABLED:
        try:
            av_key = utils.get_secrets('ALPHA_VANTAGE_KEY')
            if av_key:
                ts = TimeSeries(key=av_key, output_format='pandas')
                rates_data = []
                
                for ticker in tickers:
                    try:
                        # Convert Yahoo Finance format to Alpha Vantage format
                        if '=X' in ticker:
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
            if ticker.startswith('KZTRUB'):
                # KZT to RUB: Use USD as intermediate
                kzt_usd = get_single_rate('KZTUSD=X')
                usd_rub = get_single_rate('USDRUB=X')
                if kzt_usd is not None and usd_rub is not None:
                    rate = kzt_usd * usd_rub
                    rates_data.append({'ticker_': ticker, 'Актуальная_цена_manual': rate})
                else:
                    # Fallback: derive cross-rate from config base rates
                    fallback_rate = _fallback_rate_from_config('KZT', 'RUB')
                    logger.warning(f"Using fallback rate for {ticker}: {fallback_rate}")
                    rates_data.append({'ticker_': ticker, 'Актуальная_цена_manual': fallback_rate})
            elif ticker.startswith('RUBKZT'):
                # RUB to KZT: Use USD as intermediate
                rub_usd = get_single_rate('RUBUSD=X')
                usd_kzt = get_single_rate('USDKZT=X')
                if rub_usd is not None and usd_kzt is not None:
                    rate = rub_usd * usd_kzt
                    rates_data.append({'ticker_': ticker, 'Актуальная_цена_manual': rate})
                else:
                    # Fallback: derive cross-rate from config base rates
                    fallback_rate = _fallback_rate_from_config('RUB', 'KZT')
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
            series = _download_fx_pair_series(ticker)
            if not series.empty:
                rate = pd.to_numeric(series, errors='coerce').dropna()
                if not rate.empty:
                    return float(rate.iloc[-1])
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
            start = min_date - timedelta(days=5)
            full_ind = pd.date_range(start, max_date, freq='D')
            rates_df_upd = pd.DataFrame(index=full_ind)

            for ticker in tickers:
                try:
                    if is_problematic_ticker(ticker):
                        logger.warning(f"Ticker {ticker} is known to be problematic, trying alias/cross-rate flow")

                    if _is_fx_ticker(ticker):
                        cbr_series = _get_cbr_rates_df([ticker], start, max_date)
                        if not cbr_series.empty and ticker in cbr_series.columns:
                            s = pd.to_numeric(cbr_series[ticker], errors='coerce')
                            if not s.empty and not s.isna().all():
                                rates_df_upd[ticker] = s.reindex(full_ind, fill_value=np.nan).ffill().bfill()
                                continue

                    series = _download_fx_pair_series(ticker, start, max_date)
                    if not series.empty and not series.isna().all():
                        series.index = pd.DatetimeIndex(pd.to_datetime(series.index).date)
                        series = pd.to_numeric(series, errors='coerce')
                        rates_df_upd[ticker] = series.reindex(full_ind, fill_value=np.nan).ffill().bfill()
                        continue

                    cross_rate = get_cross_rate_for_ticker(ticker, min_date, max_date)
                    if cross_rate is not None and not cross_rate.empty and ticker in cross_rate.columns:
                        cr_series = pd.to_numeric(cross_rate[ticker].squeeze(), errors='coerce')
                        rates_df_upd[ticker] = cr_series.reindex(full_ind, fill_value=np.nan).ffill().bfill()
                        continue

                    # Fast offline path for FX to avoid noisy fallback chain.
                    if _is_fx_ticker(ticker) and not _FX_NETWORK_ENABLED:
                        cfg_rate = _fallback_rate_from_config(ticker[:3], ticker[3:6])
                        if cfg_rate is not None:
                            rates_df_upd[ticker] = float(cfg_rate)
                            logger.info(f"Using config fallback rate for {ticker}: {cfg_rate}")
                            continue

                    fallback_rates = get_rates_fallback([ticker])
                    if not fallback_rates.empty and f'Актуальная_цена_{config.STOCK_API}' in fallback_rates.columns:
                        fallback_rate = pd.to_numeric(
                            fallback_rates[f'Актуальная_цена_{config.STOCK_API}'],
                            errors='coerce'
                        ).iloc[0]
                        if not pd.isna(fallback_rate):
                            rates_df_upd[ticker] = float(fallback_rate)
                            logger.info(f"Using fallback rate for {ticker}: {fallback_rate}")
                            continue

                    # Final offline fallback from config cross-rates
                    if _is_fx_ticker(ticker):
                        cfg_rate = _fallback_rate_from_config(ticker[:3], ticker[3:6])
                        if cfg_rate is not None:
                            rates_df_upd[ticker] = float(cfg_rate)
                            logger.info(f"Using config fallback rate for {ticker}: {cfg_rate}")
                except Exception as e:
                    logger.warning(f"Failed to fetch ticker {ticker}: {e}")
            
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
        'USDRUB=X', 'RUBUSD=X',  # USD-RUB direct pairs now often fail on Yahoo
    ]
    return ticker in problematic_patterns


def get_cross_rate_for_ticker(ticker, min_date, max_date):
    """
    Calculate cross-rate for problematic tickers
    """
    try:
        if 'KZT' in ticker and 'RUB' in ticker:
            # KZT to RUB via USD
            kzt_usd_data = _download_fx_pair_series('KZTUSD=X', min_date, max_date)
            usd_rub_data = _download_fx_pair_series('USDRUB=X', min_date, max_date)
            
            kzt_usd = _to_close_series(kzt_usd_data, 'KZTUSD=X')
            usd_rub = _to_close_series(usd_rub_data, 'USDRUB=X')
                
            if not kzt_usd.empty and not usd_rub.empty:
                cross_rate = kzt_usd * usd_rub
                return pd.DataFrame({ticker: cross_rate}, index=cross_rate.index)
        elif 'RUB' in ticker and 'KZT' in ticker:
            # RUB to KZT via USD
            rub_usd_data = _download_fx_pair_series('RUBUSD=X', min_date, max_date)
            usd_kzt_data = _download_fx_pair_series('USDKZT=X', min_date, max_date)
            
            rub_usd = _to_close_series(rub_usd_data, 'RUBUSD=X')
            usd_kzt = _to_close_series(usd_kzt_data, 'USDKZT=X')
                
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
        curr_USD_rates = _download_fx_pair_series(f'{from_curr}USD=X', min_date, max_date)
        curr_USD_rates = _to_close_series(curr_USD_rates, f'{from_curr}USD=X')
        curr_USD_rates.index = pd.DatetimeIndex(pd.to_datetime(curr_USD_rates.index).date)
        full_ind = pd.date_range(min_date, max_date)
        curr_USD_rates = (curr_USD_rates.reindex(full_ind, fill_value=np.nan)
                          .interpolate(limit_direction='both'))

        curr_rates = _download_fx_pair_series(f'USD{to_curr}=X', min_date, max_date)
        curr_rates = _to_close_series(curr_rates, f'USD{to_curr}=X')
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
