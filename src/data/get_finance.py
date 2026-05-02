import logging
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FX_CACHE_COLUMNS = ['date', 'currency', 'usd_rate', 'source', 'fetched_at']
_FX_NETWORK_ENABLED = False
_FX_CACHE_DF = None
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


_HTTP_SESSION = _build_retry_session()
logger.info("FX network mode: %s", "online" if _FX_NETWORK_ENABLED else "offline")


def set_fx_network_enabled(enabled: bool):
    """
    Set FX network mode globally for this process.
    """
    global _FX_NETWORK_ENABLED
    prev_value = _FX_NETWORK_ENABLED
    _FX_NETWORK_ENABLED = bool(enabled)
    logger.info("FX network mode switched to: %s", "online" if _FX_NETWORK_ENABLED else "offline")
    return prev_value


def get_usd_rates(currencies, min_date, max_date) -> pd.DataFrame:
    """
    Return daily rates where each value means: 1 currency = N USD.
    The persistent cache is the primary source; providers fill only missing coverage.
    """
    currencies = sorted({str(currency).upper() for currency in np.array(currencies).flat})
    start = _to_timestamp(min_date)
    end = _to_timestamp(max_date)
    if start > end:
        start, end = end, start

    _ensure_cache_file()
    for currency in currencies:
        _ensure_currency_cached(currency, start, end)

    out = pd.DataFrame(index=pd.date_range(start, end, freq='D'))
    for currency in currencies:
        out[currency] = _series_from_cache(currency, start, end)
    out.index.name = 'Дата'
    return out.astype(float)


def get_fx_rates(from_curr, to_curr, min_date, max_date) -> pd.DataFrame:
    """
    Return daily cross-rate for a pair. Example: KZT/RUB = KZT_USD / RUB_USD.
    """
    from_curr = str(from_curr).upper()
    to_curr = str(to_curr).upper()
    ticker = _pair_ticker(from_curr, to_curr)
    start = _to_timestamp(min_date)
    end = _to_timestamp(max_date)

    if from_curr == to_curr:
        rates = pd.DataFrame(index=pd.date_range(start, end, freq='D'))
        rates[ticker] = 1.0
        rates.index.name = 'Дата'
        return rates

    usd_rates = get_usd_rates([from_curr, to_curr], start, end)
    with np.errstate(divide='ignore', invalid='ignore'):
        rates = pd.DataFrame({ticker: usd_rates[from_curr] / usd_rates[to_curr]}, index=usd_rates.index)
    rates.index.name = 'Дата'
    return rates.replace([np.inf, -np.inf], np.nan).ffill().bfill()


def get_actual_fx_rate(from_curr, to_curr):
    """
    Return the latest known FX rate for a pair.
    """
    today = pd.Timestamp(datetime.now().date())
    rates = get_fx_rates(from_curr, to_curr, today, today)
    if rates.empty:
        return None
    values = pd.to_numeric(rates.iloc[:, 0], errors='coerce').dropna()
    return float(values.iloc[-1]) if not values.empty else None


def get_fx_rate_info(from_curr, to_curr, as_of_date=None, lookback_days=7) -> dict:
    """
    Return a cross-rate plus cache/provider metadata used to calculate it.
    """
    as_of = _to_timestamp(as_of_date or datetime.now().date())
    start = as_of - timedelta(days=lookback_days)
    from_curr = str(from_curr).upper()
    to_curr = str(to_curr).upper()

    rates = get_fx_rates(from_curr, to_curr, start, as_of)
    rate_series = pd.to_numeric(rates.iloc[:, 0], errors='coerce').dropna() if not rates.empty else pd.Series(dtype=float)
    if rate_series.empty:
        return {
            'from_currency': from_curr,
            'to_currency': to_curr,
            'rate': None,
            'rate_date': None,
            'previous_rate': None,
            'previous_rate_date': None,
            'change_pct': None,
            'source': 'Недоступно',
            'legs': [],
        }

    latest_date = pd.Timestamp(rate_series.index[-1]).normalize()
    latest_rate = float(rate_series.iloc[-1])
    previous = rate_series[rate_series.index < latest_date]
    previous_rate = float(previous.iloc[-1]) if not previous.empty else None
    previous_date = pd.Timestamp(previous.index[-1]).normalize() if not previous.empty else None
    change_pct = None
    if previous_rate not in (None, 0):
        change_pct = (latest_rate - previous_rate) / previous_rate * 100

    legs = [
        _usd_rate_metadata(from_curr, latest_date),
        _usd_rate_metadata(to_curr, latest_date),
    ]
    source = _format_fx_source(legs)

    return {
        'from_currency': from_curr,
        'to_currency': to_curr,
        'rate': latest_rate,
        'rate_date': latest_date,
        'previous_rate': previous_rate,
        'previous_rate_date': previous_date,
        'change_pct': change_pct,
        'source': source,
        'legs': legs,
    }


def update_fx_cache_interactive(currencies, min_date, max_date, tolerance_pct=1.0, providers=None):
    """
    Fetch USD rates from configured providers, compare them, and ask what to cache.

    Stored values are always direct provider values: 1 currency = N USD.
    """
    currencies = sorted({str(currency).upper() for currency in np.array(currencies).flat})
    currencies = [currency for currency in currencies if currency != config.FX_BASE_CURRENCY]
    start = _to_timestamp(min_date)
    end = _to_timestamp(max_date)
    if start > end:
        start, end = end, start

    provider_names = list(providers or config.FX_PROVIDER_ORDER)
    unknown_providers = [name for name in provider_names if name not in _PROVIDERS]
    if unknown_providers:
        raise ValueError(f"Unknown FX providers: {unknown_providers}")

    _ensure_cache_file()
    previous_network_mode = set_fx_network_enabled(True)
    try:
        results = []
        for currency in currencies:
            provider_series = _fetch_provider_series(currency, start, end, provider_names)
            summary = _compare_provider_series(provider_series)
            _print_provider_summary(currency, provider_series, summary, tolerance_pct)

            available = [name for name in provider_names if name in provider_series and not provider_series[name].empty]
            if not available:
                print(f"{currency}: no provider data available, skipped.")
                results.append({'currency': currency, 'source': None, 'rows': 0, 'status': 'no_data'})
                continue

            default_choice = available[0]
            choice = _ask_provider_choice(currency, available, default_choice)
            if choice == 'skip':
                results.append({'currency': currency, 'source': None, 'rows': 0, 'status': 'skipped'})
                continue

            selected = provider_series[choice]
            _append_cache_rows(currency, selected, choice)
            results.append({'currency': currency, 'source': choice, 'rows': len(selected), 'status': 'updated'})
            print(f"{currency}: cached {len(selected)} rows from {choice}.")

        return pd.DataFrame(results)
    finally:
        set_fx_network_enabled(previous_network_mode)


def get_fallback_rate(from_curr, to_curr):
    """
    Return a pair rate from the latest cache values.
    """
    from_curr = str(from_curr).upper()
    to_curr = str(to_curr).upper()
    if from_curr == to_curr:
        return 1.0

    from_usd = _latest_cached_usd_rate(from_curr)
    to_usd = _latest_cached_usd_rate(to_curr)
    if from_usd is None or to_usd in (None, 0):
        return None
    return float(from_usd) / float(to_usd)


def get_rates(tickers: list, min_date, max_date) -> pd.DataFrame:
    """
    Backward-compatible API: return columns named like Yahoo FX tickers.
    """
    if not isinstance(tickers, list):
        tickers = [tickers]

    start = _to_timestamp(min_date)
    end = _to_timestamp(max_date)
    out = pd.DataFrame(index=pd.date_range(start, end, freq='D'))
    for ticker in tickers:
        if _is_fx_ticker(ticker):
            from_curr, to_curr = _parse_fx_ticker(ticker)
            pair_rates = get_fx_rates(from_curr, to_curr, start, end)
            out[ticker] = pair_rates[_pair_ticker(from_curr, to_curr)]
    out.index.name = 'Дата'
    return out.sort_index()


def get_actual_rates(tickers, max_retries=3):
    """
    Backward-compatible API used by investment and asset reports.
    FX tickers are resolved through the USD cache; non-FX tickers still use yfinance.
    """
    if not isinstance(tickers, list):
        tickers = [tickers]

    rows = []
    for ticker in tickers:
        try:
            if _is_fx_ticker(ticker):
                from_curr, to_curr = _parse_fx_ticker(ticker)
                rate = get_actual_fx_rate(from_curr, to_curr)
            else:
                rate = _download_latest_market_price(ticker)
            if rate is not None and not pd.isna(rate):
                rows.append({'ticker_': ticker, f'Актуальная_цена_{config.STOCK_API}': float(rate)})
        except Exception as e:
            logger.warning(f"Could not get actual rate for {ticker}: {e}")
    return pd.DataFrame(rows)


def get_rates_with_fallback(tickers: list, min_date, max_date, max_retries=3) -> pd.DataFrame:
    return get_rates(tickers, min_date, max_date)


def get_rates_fallback(tickers):
    rows = []
    for ticker in tickers:
        if not _is_fx_ticker(ticker):
            continue
        from_curr, to_curr = _parse_fx_ticker(ticker)
        rate = get_fallback_rate(from_curr, to_curr)
        if rate is not None:
            rows.append({'ticker_': ticker, f'Актуальная_цена_{config.STOCK_API}': float(rate)})
    return pd.DataFrame(rows)


def get_single_rate(ticker, max_retries=3):
    actual = get_actual_rates([ticker], max_retries=max_retries)
    rate_col = f'Актуальная_цена_{config.STOCK_API}'
    if actual.empty or rate_col not in actual.columns:
        return None
    values = pd.to_numeric(actual[rate_col], errors='coerce').dropna()
    return float(values.iloc[0]) if not values.empty else None


def get_cross_rates(from_curr, to_curr, min_date, max_date):
    return get_fx_rates(from_curr, to_curr, min_date, max_date)


def get_cross_rate_for_ticker(ticker, min_date, max_date):
    if not _is_fx_ticker(ticker):
        return None
    from_curr, to_curr = _parse_fx_ticker(ticker)
    return get_fx_rates(from_curr, to_curr, min_date, max_date)


def get_cross_rates_manual(tickers):
    return get_rates_fallback(tickers).rename(columns={
        f'Актуальная_цена_{config.STOCK_API}': 'Актуальная_цена_manual'
    })


def is_problematic_ticker(ticker):
    return _is_fx_ticker(ticker)


def _ensure_currency_cached(currency: str, min_date: pd.Timestamp, max_date: pd.Timestamp):
    currency = currency.upper()
    if currency == config.FX_BASE_CURRENCY:
        return

    missing_dates = _missing_dates(currency, min_date, max_date)
    if not missing_dates:
        return

    fetch_dates = _fetchable_missing_dates(currency, missing_dates)
    if _FX_NETWORK_ENABLED and fetch_dates:
        fetched = _fetch_missing_usd_rates(currency, min(fetch_dates), max(fetch_dates))
        if not fetched.empty:
            fetched = fetched.loc[(fetched.index >= min_date) & (fetched.index <= max_date)]
            if not fetched.empty:
                _append_cache_rows(currency, fetched, fetched.name or 'provider')
                missing_dates = _missing_dates(currency, min_date, max_date)
                fetch_dates = _fetchable_missing_dates(currency, missing_dates)
                if not fetch_dates:
                    return

    if _FX_NETWORK_ENABLED and fetch_dates:
        preview_dates = ", ".join(date.date().isoformat() for date in fetch_dates[:5])
        suffix = "..." if len(fetch_dates) > 5 else ""
        logger.warning(
            "No exact FX cache/provider rows for %s/USD on fetchable dates %s%s; calculations will use nearest cached values where possible",
            currency,
            preview_dates,
            suffix,
        )


def _fetch_missing_usd_rates(currency: str, min_date: pd.Timestamp, max_date: pd.Timestamp) -> pd.Series:
    for provider_name in config.FX_PROVIDER_ORDER:
        provider = _PROVIDERS.get(provider_name)
        if provider is None:
            logger.warning("Unknown FX provider configured: %s", provider_name)
            continue
        try:
            series = provider(currency, min_date, max_date)
            series = _clean_rate_series(series)
            if not series.empty:
                series.name = provider_name
                return series
        except Exception as e:
            logger.warning("FX provider %s failed for %s/USD: %s", provider_name, currency, e)
    return pd.Series(dtype=float)


def _fetch_provider_series(currency: str, min_date: pd.Timestamp, max_date: pd.Timestamp, provider_names):
    provider_series = {}
    for provider_name in provider_names:
        provider = _PROVIDERS[provider_name]
        try:
            series = _clean_rate_series(provider(currency, min_date, max_date))
        except Exception as e:
            logger.warning("FX provider %s failed for %s/USD: %s", provider_name, currency, e)
            series = pd.Series(dtype=float)
        provider_series[provider_name] = series
    return provider_series


def _compare_provider_series(provider_series: dict[str, pd.Series]) -> dict:
    non_empty = {name: series for name, series in provider_series.items() if not series.empty}
    if len(non_empty) < 2:
        return {'common_days': 0, 'max_diff_pct': None, 'mean_diff_pct': None}

    aligned = pd.concat(non_empty, axis=1).dropna()
    if aligned.empty:
        return {'common_days': 0, 'max_diff_pct': None, 'mean_diff_pct': None}

    first_provider = aligned.columns[0]
    diffs = []
    for provider_name in aligned.columns[1:]:
        baseline = aligned[first_provider].replace(0, np.nan)
        diff_pct = ((aligned[provider_name] - aligned[first_provider]).abs() / baseline) * 100
        diffs.append(diff_pct.dropna())

    if not diffs:
        return {'common_days': len(aligned), 'max_diff_pct': None, 'mean_diff_pct': None}

    all_diffs = pd.concat(diffs)
    return {
        'common_days': len(aligned),
        'max_diff_pct': float(all_diffs.max()) if not all_diffs.empty else None,
        'mean_diff_pct': float(all_diffs.mean()) if not all_diffs.empty else None,
    }


def _print_provider_summary(currency: str, provider_series: dict[str, pd.Series], summary: dict, tolerance_pct: float):
    print(f"\n{currency}/USD provider check")
    for provider_name, series in provider_series.items():
        if series.empty:
            print(f"  {provider_name}: no data")
            continue
        print(
            f"  {provider_name}: {len(series)} rows, "
            f"{series.index.min().date().isoformat()}..{series.index.max().date().isoformat()}, "
            f"last={series.iloc[-1]:.8f}"
        )

    max_diff_pct = summary.get('max_diff_pct')
    if max_diff_pct is None:
        print("  comparison: not enough overlapping provider data")
        return

    status = "ok" if max_diff_pct <= tolerance_pct else "review"
    print(
        f"  comparison: {summary['common_days']} common days, "
        f"max diff={max_diff_pct:.4f}%, "
        f"mean diff={summary['mean_diff_pct']:.4f}%, "
        f"status={status}"
    )


def _ask_provider_choice(currency: str, available: list[str], default_choice: str) -> str:
    choices = "/".join(available + ["skip"])
    prompt = f"{currency}: cache which source? [{choices}] default={default_choice}: "
    while True:
        raw_choice = input(prompt).strip().lower()
        if not raw_choice:
            return default_choice
        if raw_choice in {"s", "skip"}:
            return "skip"
        for provider_name in available:
            if raw_choice == provider_name.lower():
                return provider_name
        print(f"Unknown choice: {raw_choice}")


def _fetch_yfinance_usd_rate(currency: str, min_date: pd.Timestamp, max_date: pd.Timestamp) -> pd.Series:
    currency = currency.upper()
    if currency == config.FX_BASE_CURRENCY:
        return pd.Series(1.0, index=pd.date_range(min_date, max_date, freq='D'))

    direct = _yf_close(f'{currency}{config.FX_BASE_CURRENCY}=X', min_date, max_date)
    if not direct.empty:
        return direct

    inverse = _yf_close(f'{currency}=X', min_date, max_date)
    if inverse.empty:
        inverse = _yf_close(f'{config.FX_BASE_CURRENCY}{currency}=X', min_date, max_date)
    if inverse.empty:
        return pd.Series(dtype=float)

    with np.errstate(divide='ignore', invalid='ignore'):
        return 1.0 / inverse


def _fetch_cbr_usd_rate(currency: str, min_date: pd.Timestamp, max_date: pd.Timestamp) -> pd.Series:
    currency = currency.upper()
    if currency == config.FX_BASE_CURRENCY:
        return pd.Series(1.0, index=pd.date_range(min_date, max_date, freq='D'))

    usd_rub = _fetch_cbr_currency_series(config.FX_BASE_CURRENCY, min_date, max_date)
    if usd_rub.empty:
        return pd.Series(dtype=float)

    if currency == 'RUB':
        with np.errstate(divide='ignore', invalid='ignore'):
            return 1.0 / usd_rub

    currency_rub = _fetch_cbr_currency_series(currency, min_date, max_date)
    if currency_rub.empty:
        return pd.Series(dtype=float)
    return currency_rub / usd_rub


_PROVIDERS = {
    'yfinance': _fetch_yfinance_usd_rate,
    'cbr': _fetch_cbr_usd_rate,
}


def _fetch_cbr_currency_series(currency, min_date, max_date):
    """
    Fetch RUB-per-currency series from CBR.
    """
    if not _FX_NETWORK_ENABLED:
        return pd.Series(dtype=float)

    currency = currency.upper()
    val_id = _CBR_VALUTE_IDS.get(currency)
    if not val_id:
        return pd.Series(dtype=float)

    start = _to_timestamp(min_date)
    end = _to_timestamp(max_date)
    cache_key = (currency, start.date().isoformat(), end.date().isoformat())
    if cache_key in _CBR_SERIES_CACHE:
        return _CBR_SERIES_CACHE[cache_key]

    response = _HTTP_SESSION.get(
        "https://www.cbr.ru/scripts/XML_dynamic.asp",
        params={
            'date_req1': start.strftime('%d/%m/%Y'),
            'date_req2': end.strftime('%d/%m/%Y'),
            'VAL_NM_RQ': val_id,
        },
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
        if nominal:
            values[pd.Timestamp(datetime.strptime(date_text, '%d.%m.%Y').date())] = value / nominal

    series = pd.Series(values, dtype=float).sort_index()
    _CBR_SERIES_CACHE[cache_key] = series
    return series


def _series_from_cache(currency: str, min_date: pd.Timestamp, max_date: pd.Timestamp) -> pd.Series:
    full_index = pd.date_range(min_date, max_date, freq='D')
    if currency == config.FX_BASE_CURRENCY:
        return pd.Series(1.0, index=full_index, dtype=float)

    cache = _read_cache()
    if cache.empty:
        return pd.Series(np.nan, index=full_index, dtype=float)

    currency_cache = cache[cache['currency'] == currency].sort_values('date')
    if currency_cache.empty:
        return pd.Series(np.nan, index=full_index, dtype=float)

    values = currency_cache.set_index('date')['usd_rate'].sort_index()
    seed = values[values.index <= min_date]
    in_range = values[(values.index >= min_date) & (values.index <= max_date)]
    if not seed.empty:
        in_range = pd.concat([seed.tail(1), in_range])
    if in_range.empty:
        return pd.Series(np.nan, index=full_index, dtype=float)
    values_for_fill = in_range[~in_range.index.duplicated(keep='last')]
    fill_index = values_for_fill.index.union(full_index).sort_values()
    return values_for_fill.reindex(fill_index).ffill().bfill().reindex(full_index)


def _missing_dates(currency: str, min_date: pd.Timestamp, max_date: pd.Timestamp) -> list[pd.Timestamp]:
    currency = str(currency).upper()
    if currency == config.FX_BASE_CURRENCY:
        return []

    full_index = pd.date_range(min_date.normalize(), max_date.normalize(), freq='D')
    cache = _read_cache()
    if cache.empty:
        return list(full_index)

    currency_cache = cache[cache['currency'] == currency]
    if currency_cache.empty:
        return list(full_index)

    cached_dates = set(pd.to_datetime(currency_cache['date'], errors='coerce').dropna().dt.normalize())
    return [date for date in full_index if date.normalize() not in cached_dates]


def _fetchable_missing_dates(currency: str, missing_dates: list[pd.Timestamp]) -> list[pd.Timestamp]:
    return [date for date in missing_dates if _is_fx_fetchable_date(currency, date)]


def _is_fx_fetchable_date(currency: str, date: pd.Timestamp) -> bool:
    date = pd.Timestamp(date).normalize()
    if date.weekday() >= 5:
        return False
    return date.strftime('%m-%d') not in _fx_holiday_mmdd(str(currency).upper())


def _fx_holiday_mmdd(currency: str) -> set[str]:
    common = {'01-01', '01-02', '12-25'}
    if currency == 'RUB':
        return common | {'01-03', '01-04', '01-05', '01-06', '01-07', '01-08', '02-23', '03-08', '05-01', '05-09', '06-12', '11-04'}
    if currency == 'KZT':
        return common | {'03-08', '03-21', '03-22', '03-23', '05-01', '05-07', '05-09', '07-06', '08-30', '10-25', '12-16'}
    if currency in {'EUR', 'GBP'}:
        return common | {'12-26'}
    return common


def _latest_cached_usd_rate(currency: str):
    cache = _read_cache()
    if cache.empty:
        return None
    currency_cache = cache[cache['currency'] == currency].sort_values('date')
    if currency_cache.empty:
        return None
    values = pd.to_numeric(currency_cache['usd_rate'], errors='coerce').dropna()
    return float(values.iloc[-1]) if not values.empty else None


def _usd_rate_metadata(currency: str, as_of_date: pd.Timestamp) -> dict:
    currency = str(currency).upper()
    as_of = _to_timestamp(as_of_date)
    if currency == config.FX_BASE_CURRENCY:
        return {
            'currency': currency,
            'usd_rate': 1.0,
            'rate_date': as_of,
            'source': 'base',
            'fetched_at': '',
        }

    cache = _read_cache()
    if cache.empty:
        return {
            'currency': currency,
            'usd_rate': None,
            'rate_date': None,
            'source': 'missing',
            'fetched_at': '',
        }

    currency_cache = cache[(cache['currency'] == currency) & (cache['date'] <= as_of)].sort_values('date')
    if currency_cache.empty:
        return {
            'currency': currency,
            'usd_rate': None,
            'rate_date': None,
            'source': 'missing',
            'fetched_at': '',
        }

    row = currency_cache.iloc[-1]
    return {
        'currency': currency,
        'usd_rate': float(row['usd_rate']),
        'rate_date': pd.Timestamp(row['date']).normalize(),
        'source': str(row['source']),
        'fetched_at': str(row['fetched_at']) if pd.notna(row['fetched_at']) else '',
    }


def _format_fx_source(legs: list[dict]) -> str:
    parts = []
    for leg in legs:
        currency = leg['currency']
        source = leg.get('source') or 'missing'
        rate_date = leg.get('rate_date')
        if rate_date is not None:
            parts.append(f"{currency}/USD: {source} {pd.Timestamp(rate_date).date().isoformat()}")
        else:
            parts.append(f"{currency}/USD: {source}")
    return "; ".join(parts)


def _read_cache() -> pd.DataFrame:
    global _FX_CACHE_DF
    if _FX_CACHE_DF is not None:
        return _FX_CACHE_DF.copy()

    cache_path = Path(config.FX_CACHE_PATH)
    if not cache_path.exists():
        _FX_CACHE_DF = pd.DataFrame(columns=FX_CACHE_COLUMNS)
        return _FX_CACHE_DF.copy()

    cache = pd.read_csv(cache_path, sep=';', dtype={'currency': str, 'source': str, 'fetched_at': str})
    for column in FX_CACHE_COLUMNS:
        if column not in cache.columns:
            cache[column] = np.nan
    cache = cache[FX_CACHE_COLUMNS]
    cache['date'] = pd.to_datetime(cache['date'], errors='coerce')
    cache['currency'] = cache['currency'].astype(str).str.upper()
    cache['usd_rate'] = pd.to_numeric(cache['usd_rate'], errors='coerce')
    cache = cache.dropna(subset=['date', 'currency', 'usd_rate'])
    cache = cache.sort_values(['currency', 'date', 'fetched_at']).drop_duplicates(['date', 'currency'], keep='last')
    _FX_CACHE_DF = cache.reset_index(drop=True)
    return _FX_CACHE_DF.copy()


def _write_cache(cache: pd.DataFrame):
    global _FX_CACHE_DF
    cache_path = Path(config.FX_CACHE_PATH)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = cache.copy()
    cache['date'] = pd.to_datetime(cache['date']).dt.strftime('%Y-%m-%d')
    cache = cache.sort_values(['currency', 'date'])
    cache.to_csv(cache_path, sep=';', index=False)
    _FX_CACHE_DF = None


def _append_cache_rows(currency: str, rates: pd.Series, source: str):
    rates = _clean_rate_series(rates)
    if rates.empty:
        return

    now = datetime.now().isoformat(timespec='seconds')
    new_rows = pd.DataFrame({
        'date': rates.index,
        'currency': currency.upper(),
        'usd_rate': rates.astype(float).values,
        'source': source,
        'fetched_at': now,
    })
    cache = pd.concat([_read_cache(), new_rows], ignore_index=True)
    cache['date'] = pd.to_datetime(cache['date'], errors='coerce')
    cache['currency'] = cache['currency'].astype(str).str.upper()
    cache['usd_rate'] = pd.to_numeric(cache['usd_rate'], errors='coerce')
    cache = cache.dropna(subset=['date', 'currency', 'usd_rate'])
    cache = cache.sort_values(['currency', 'date', 'fetched_at']).drop_duplicates(['date', 'currency'], keep='last')
    _write_cache(cache[FX_CACHE_COLUMNS])


def _ensure_cache_file():
    cache_path = Path(config.FX_CACHE_PATH)
    if not cache_path.exists():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(columns=FX_CACHE_COLUMNS).to_csv(cache_path, sep=';', index=False)


def _clean_rate_series(series: pd.Series) -> pd.Series:
    if series is None or series.empty:
        return pd.Series(dtype=float)
    series = pd.Series(series).copy()
    series.index = pd.to_datetime(series.index, errors='coerce').normalize()
    series = pd.to_numeric(series, errors='coerce')
    series = series.dropna()
    series = series[series > 0]
    return series[~series.index.isna()].sort_index()


def _yf_close(ticker: str, min_date: pd.Timestamp, max_date: pd.Timestamp) -> pd.Series:
    if not _FX_NETWORK_ENABLED:
        return pd.Series(dtype=float)
    data = yf.download(
        ticker,
        start=min_date,
        end=max_date + timedelta(days=1),
        progress=False,
        auto_adjust=False,
    )
    if data is None or data.empty:
        return pd.Series(dtype=float)
    if isinstance(data.columns, pd.MultiIndex):
        if ('Close', ticker) in data.columns:
            close = data[('Close', ticker)]
        else:
            close = data.xs('Close', axis=1, level=0).iloc[:, 0]
    elif 'Close' in data.columns:
        close = data['Close']
    else:
        close = data.iloc[:, -1]
    return _clean_rate_series(close)


def _download_latest_market_price(ticker: str):
    if not _FX_NETWORK_ENABLED:
        return None
    data = yf.download(ticker, period='5d', progress=False, auto_adjust=False)
    if data is None or data.empty:
        return None
    if isinstance(data.columns, pd.MultiIndex):
        close = data.xs('Close', axis=1, level=0).iloc[:, 0]
    elif 'Close' in data.columns:
        close = data['Close']
    else:
        close = data.iloc[:, -1]
    values = pd.to_numeric(close, errors='coerce').dropna()
    return float(values.iloc[-1]) if not values.empty else None


def _to_timestamp(value) -> pd.Timestamp:
    return pd.Timestamp(pd.to_datetime(value)).normalize()


def _is_fx_ticker(ticker: str) -> bool:
    return isinstance(ticker, str) and ticker.endswith('=X') and len(ticker.replace('=X', '')) == 6


def _parse_fx_ticker(ticker: str) -> tuple[str, str]:
    pair = ticker.replace('=X', '').upper()
    return pair[:3], pair[3:6]


def _pair_ticker(from_curr: str, to_curr: str) -> str:
    return f'{from_curr.upper()}{to_curr.upper()}=X'


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
