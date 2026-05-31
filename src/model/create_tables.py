import numpy as np
import pandas as pd
from functools import lru_cache

from src import config
from src.data.get import get_investments, get_transactions, get_assets
from src.data.debts import active_debt_balances
from src.data.investment_calculations import current_investment_value
from src.data.get_finance import get_actual_rates, get_act_moex, get_fallback_rate, get_fx_rates
from src.data.proccess import convert_transaction


def create_invest_tbl() -> (pd.DataFrame, pd.DataFrame):
    """
    Create PNL of investments
    :return:
    """
    # Get invest transactions
    investments_df = get_investments()

    # Divide by transaction type
    buy_df = investments_df[investments_df['Тип_транзакции'] == 'Покупка']
    sell_df = investments_df[investments_df['Тип_транзакции'] == 'Продажа']
    sell_df['Прибыль/убыток'] = np.nan

    # Process sells
    for sell_index, sell_row in sell_df.iterrows():
        # Find all buy transactions before sell tr-on
        buy_df_smpl = buy_df[(buy_df['Тикер'] == sell_row['Тикер']) &
                             (buy_df['Дата'] <= sell_row['Дата'])].sort_values('Дата')
        need_to_sell = sell_row['Количество']
        sell_row['Прибыль/убыток'] = sell_row['Количество'] * sell_row['Цена']

        # Decrease balance by sell amount in FIFO
        for buy_index, buy_row in buy_df_smpl.iterrows():
            need_to_sell = need_to_sell - buy_row['Количество']
            if need_to_sell >= 0:
                sell_row['Прибыль/убыток'] = sell_row['Прибыль/убыток'] - buy_row['Количество'] * buy_row['Цена']
                buy_row['Количество'] = np.nan
                buy_df.loc[buy_index] = buy_row
                sell_df.loc[sell_index] = sell_row
            else:
                sell_row['Прибыль/убыток'] = sell_row['Прибыль/убыток'] - (buy_row['Количество'] + need_to_sell) * \
                                             buy_row['Цена']
                buy_row['Количество'] = -need_to_sell
                buy_df.loc[buy_index] = buy_row
                sell_df.loc[sell_index] = sell_row
                break
        buy_df = buy_df[buy_df['Количество'].notna()]

    buy_df['Сумма'] = buy_df['Количество'] * buy_df['Цена']

    # Solve tickers
    if config.STOCK_API == 'yf':
        buy_df.loc[buy_df['Актив'] == 'Валюта', 'ticker_'] = (buy_df['Тикер'] +
                                                              buy_df['Валюта'].apply(lambda x: x + '=X'))
    elif config.STOCK_API == 'td':
        buy_df.loc[buy_df['Актив'] == 'Валюта', 'ticker_'] = (buy_df['Тикер'].apply(lambda x: x + '/') +
                                                              buy_df['Валюта'])
    buy_df.loc[buy_df['Актив'].isin(['Акции', 'Фонды']), 'ticker_'] = buy_df['Тикер']

    # Fill by actual MOEX price
    moex_stocks_rates = get_act_moex(mode='stocks').rename(columns={'Тикер': 'ticker_'})
    etf_stocks_rates = get_act_moex(mode='ETF').rename(columns={'Тикер': 'ticker_'})
    buy_df = (buy_df.merge(moex_stocks_rates, on='ticker_', how='left')
              .merge(etf_stocks_rates, on='ticker_', how='left')
              )
    buy_df['Актуальная цена'] = buy_df[['Актуальная_цена_moex_stocks',
                                        'Актуальная_цена_moex_ETF']].sum(axis=1, min_count=1)

    # If ticker not in MOEX fill by another api
    not_moex_tickers = buy_df.loc[buy_df['Актуальная цена'].isna(), 'ticker_']
    if len(not_moex_tickers) != 0:
        actual_rates = get_actual_rates(tickers=buy_df.loc[buy_df['Актуальная цена'].isna(), 'ticker_']
                                        .unique()
                                        .tolist())
        buy_df = buy_df.merge(actual_rates, on='ticker_', how='left')
        buy_df['Актуальная цена'] = buy_df[['Актуальная цена',
                                            f'Актуальная_цена_{config.STOCK_API}']].sum(axis=1, min_count=1)
    buy_df.drop(['ticker_',
                 'Актуальная_цена_moex_stocks',
                 'Актуальная_цена_moex_ETF',
                 f'Актуальная_цена_{config.STOCK_API}'
                 ], axis=1, errors='ignore', inplace=True)

    # Calculate metrics
    buy_df['Потенциальная прибыль'] = buy_df['Количество'] * buy_df['Актуальная цена'] - buy_df['Сумма']
    buy_df['Доходность'] = (buy_df['Потенциальная прибыль'] / buy_df['Сумма']).round(3) * 100
    buy_df = buy_df.round(2)

    return buy_df, sell_df


def get_balance_by_month(currency: str) -> pd.DataFrame:
    return _get_balance_by_month_cached(str(currency).upper()).copy(deep=True)


@lru_cache(maxsize=None)
def _get_balance_by_month_cached(currency: str) -> pd.DataFrame:
    """
    Get PNL of all transactions
    :param currency: ticker of currency
    :return:
    """
    transactions_df = get_transactions()
    # buy_df, sell_df = create_invest_tbl()и

    # Convert every transaction at its own historical date. This keeps past reports
    # stable when current FX rates move.
    if not config.DEBUG:
        transactions_df = convert_transaction(
            df_to_convert=transactions_df,
            to_curr=currency,
            target_col='Значение',
            use_current_rate=False,
        )
        # buy_df = convert_transaction(buy_df, to_curr=currency, target_col='Сумма')
        # sell_df = convert_transaction(sell_df, to_curr=currency, target_col='Прибыль/убыток')

    all_stats_df = (transactions_df[transactions_df.Категория.isin(config.NOT_COST_COLS)]
                    .pivot_table(values='Значение', index=['Дата'], columns=['Категория'], aggfunc=np.sum)
                    .fillna(0)
                    .resample('M').sum()
                    )
    all_stats_df['Расход'] = (transactions_df[~transactions_df.Категория.isin(config.NOT_COST_COLS)]
                              .set_index('Дата').resample('M')['Значение'].sum())

    # all_stats_df['Потенциальная прибыль'] = buy_df.set_index('Дата').resample('M')['Потенциальная прибыль'].sum()
    # all_stats_df['Доход от инвестирования'] = sell_df.set_index('Дата').resample('M')['Прибыль/убыток'].sum()
    all_stats_df = all_stats_df.fillna(0)

    all_stats_df['Баланс'] = (all_stats_df['Доход'] + all_stats_df['Сбережения']
                            #   + all_stats_df['Потенциальная прибыль']
                            #   + all_stats_df['Доход от инвестирования']
                              - all_stats_df['Дебиторская задолженность'] + all_stats_df['Погашение деб. зад.']
                              + all_stats_df['Кредиторская задолженность'] - all_stats_df['Погашение кред. зад.']
                              - all_stats_df['Расход']
                              )
    all_stats_df['Капитал'] = all_stats_df['Баланс'].cumsum()
    all_stats_df['Дельта'] = all_stats_df['Доход'] - all_stats_df['Расход']
    asset_capital = _get_asset_capital_by_month_cached(currency)
    if not asset_capital.empty:
        all_stats_df = all_stats_df.join(asset_capital, how='left')
        asset_delta = all_stats_df['Капитал по активам'].diff()
        first_asset_index = all_stats_df['Капитал по активам'].first_valid_index()
        if first_asset_index is not None:
            asset_delta.loc[first_asset_index] = (
                all_stats_df.loc[first_asset_index, 'Капитал по активам']
                - all_stats_df.loc[first_asset_index, 'Капитал']
            )
        all_stats_df['Валютная переоценка'] = asset_delta - all_stats_df['Баланс']
        all_stats_df['Расхождение с активами'] = all_stats_df['Капитал по активам'] - all_stats_df['Капитал']
    else:
        all_stats_df['Капитал по активам'] = np.nan
        all_stats_df['Валютная переоценка'] = np.nan
        all_stats_df['Расхождение с активами'] = np.nan
    return all_stats_df


def get_act_receivables(currency: str | None = None):
    return _get_act_receivables_cached(_normalize_currency_arg(currency)).copy(deep=True)


@lru_cache(maxsize=None)
def _get_act_receivables_cached(currency: str | None):
    return _ledger_debt_balance("receivable", "Дебиторская задолженность", currency)


def get_act_liabilities(currency: str | None = None):
    return _get_act_liabilities_cached(_normalize_currency_arg(currency)).copy(deep=True)


@lru_cache(maxsize=None)
def _get_act_liabilities_cached(currency: str | None):
    return _ledger_debt_balance("liability", "Кредиторская задолженность", currency)


def _normalize_currency_arg(currency: str | None) -> str | None:
    if currency is None:
        return None
    currency = str(currency).upper()
    if currency not in config.UNIQUE_TICKERS:
        raise ValueError(f"currency must be one of {tuple(config.UNIQUE_TICKERS)}")
    return currency


def _ledger_debt_balance(debt_type: str, result_column: str, currency: str | None) -> pd.DataFrame:
    balances = active_debt_balances(debt_type, currency)
    if balances.empty:
        return pd.DataFrame(
            columns=[
                "ID",
                "Контрагент",
                "Дата",
                "Комментарий",
                "Валюта долга",
                "Сумма долга",
                "Погашено",
                "Остаток",
                result_column,
                "Статус",
            ]
        )

    result = balances.rename(
        columns={
            "debt_id": "ID",
            "counterparty": "Контрагент",
            "opened_date": "Дата",
            "principal_currency": "Валюта долга",
            "principal_amount": "Сумма долга",
            "paid_amount": "Погашено",
            "outstanding_amount": "Остаток",
            "comment": "Комментарий",
            "status": "Статус",
        }
    )
    converted_column = f"outstanding_{currency}" if currency is not None else None
    if converted_column and converted_column in balances.columns:
        result[result_column] = balances[converted_column]
    else:
        result[result_column] = balances["outstanding_amount"]

    columns = [
        "ID",
        "Контрагент",
        "Дата",
        "Комментарий",
        "Валюта долга",
        "Сумма долга",
        "Погашено",
        "Остаток",
        result_column,
        "Статус",
    ]
    return result[columns].sort_values(["Контрагент", "Дата"], kind="mergesort").reset_index(drop=True)


def _get_active_debt_balance(
    debt_category: str,
    payment_category: str,
    result_column: str,
    payment_column: str,
    currency: str | None,
) -> pd.DataFrame:
    transactions_df = get_transactions()
    columns = ['Дата', 'Значение', 'Валюта', 'Комментарий']

    debt_df = (transactions_df[(transactions_df['Категория'] == debt_category) &
                               (transactions_df['Значение'] != 0)]
               [columns]
               .sort_values('Дата'))
    payment_df = (transactions_df[(transactions_df['Категория'] == payment_category) &
                                  (transactions_df['Значение'] != 0)]
                  [columns]
                  .sort_values('Дата'))

    if currency is not None:
        debt_df = _convert_debt_transactions(debt_df, currency)
        payment_df = _convert_debt_transactions(payment_df, currency)

    result = pd.concat(
        [
            debt_df.groupby('Комментарий')['Значение'].sum().rename(result_column),
            payment_df.groupby('Комментарий')['Значение'].sum().rename(payment_column),
        ],
        axis=1,
    )
    if result.empty:
        return pd.DataFrame(columns=['Комментарий', result_column])

    result[payment_column] = result[payment_column].fillna(0)
    result[result_column] = result[result_column].fillna(0) - result[payment_column]
    result = result[result[result_column] != 0][result_column]
    return result.reset_index()


def _convert_debt_transactions(data: pd.DataFrame, currency: str) -> pd.DataFrame:
    if data.empty:
        return data.copy(deep=True)
    return convert_transaction(
        df_to_convert=data.copy(deep=True),
        to_curr=currency,
        target_col='Значение',
        use_current_rate=False,
    )


def get_asset_capital_by_month(currency: str) -> pd.DataFrame:
    return _get_asset_capital_by_month_cached(str(currency).upper()).copy(deep=True)


@lru_cache(maxsize=None)
def _get_asset_capital_by_month_cached(currency: str) -> pd.DataFrame:
    assets_df = get_assets()
    if assets_df.empty:
        return pd.DataFrame(columns=['Капитал по активам'])

    assets_df = assets_df.copy(deep=True)
    assets_df['Дата'] = _asset_snapshot_dates(assets_df)
    assets_df['Значение'] = _convert_asset_values_as_of_snapshot(assets_df, currency)
    result = (
        assets_df
        .groupby('Дата', as_index=True)['Значение']
        .sum()
        .sort_index()
        .rename('Капитал по активам')
        .to_frame()
    )
    if not result.empty:
        investment_value = current_investment_value(currency)
        if investment_value:
            result.loc[result.index.max(), 'Капитал по активам'] += investment_value
    return result.round(2)


def get_cost_distribution(currency, year, month=None):
    year_key = _as_tuple(year)
    month_key = None if month is None else _as_month_tuple(month)
    return _get_cost_distribution_cached(str(currency).upper(), year_key, month_key).copy(deep=True)


@lru_cache(maxsize=None)
def _get_cost_distribution_cached(currency, year_key, month_key=None):
    transactions_df = get_transactions()

    # Get transaction sample by chosen year
    if month_key is None:
        smpl_tr_df = transactions_df[transactions_df['Год'].isin(list(year_key))].reset_index(drop=True)
    else:
        smpl_tr_df = transactions_df[(transactions_df['Год'].isin(list(year_key))) &
                                     (transactions_df['Месяц'].isin(list(month_key)))
                                     ].reset_index(drop=True)

    # Convert transactions to chosen currency
    if not config.DEBUG:
        smpl_tr_df = convert_transaction(df_to_convert=smpl_tr_df, to_curr=currency, target_col='Значение')

    # Find cost categories
    cost_categories = [x for x in smpl_tr_df.Категория.unique() if x not in config.NOT_COST_COLS]

    cost_stats_df = (smpl_tr_df[smpl_tr_df.Категория.isin(cost_categories)]
                     .groupby('Категория').agg({'Значение': ['sum']})
                     .droplevel(0, axis=1)
                     .rename({'sum': 'Суммарно'}, axis=1)
                     )
    if month_key is None:
        cost_stats_df['Среднее'] = cost_stats_df['Суммарно'] / smpl_tr_df.Месяц.astype(int).max()
    else:
        cost_stats_df['Среднее'] = cost_stats_df['Суммарно'] / 30

    cost_stats_df['Процент'] = (cost_stats_df['Суммарно'] / cost_stats_df['Суммарно'].sum()) * 100
    cost_stats_df = cost_stats_df.T[cost_categories]
    for col_name in cost_stats_df:
        if col_name != 'Показатель':
            cost_stats_df.loc[['Суммарно', 'Среднее'],
                              col_name] = (cost_stats_df.loc[['Суммарно', 'Среднее'], col_name]
                                           .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                           config.UNIQUE_TICKERS[currency])
            cost_stats_df.loc[['Процент'], col_name] = (cost_stats_df.loc[['Процент'],
                                                                          col_name].map('{:,.2f}'.format) + '%')

    cost_stats_df = cost_stats_df.reset_index().rename({'index': 'Показатель'}, axis=1)
    return cost_stats_df


def get_assets_by_currencies(year, month) -> pd.DataFrame:
    """
    function to create table with assets distribution by currencies
    :param asset_df: df with info of assets
    :return:
    """
    assets_df = get_assets()

    # Get asset sample by chosen year and month
    assets_df = assets_df[(assets_df['Год'].isin(list(np.array(year).flat))) &
                          (assets_df['Месяц'].isin(list(np.array(month).astype(int).astype(str).flat)))
                          ].reset_index(drop=True)
    snapshot_date = _asset_snapshot_dates(assets_df).max() if not assets_df.empty else None
    assets_df.drop(['Год', 'Месяц', 'Квартал'], axis=1, inplace=True)

    investment_value = current_investment_value('RUB') if _is_latest_asset_snapshot(year, month) else 0
    if investment_value:
        inv_df = pd.DataFrame([{'Счет': 'Инвестиции', 'Валюта': 'RUB', 'Значение': investment_value}])
        assets_df = pd.concat([assets_df, inv_df], ignore_index=True)

    # Pivot data to currency in cols and accounts in rows
    gr_asset_df = assets_df.pivot_table(index=['Счет', 'Валюта'], columns='Валюта',
                                        values='Значение', aggfunc='sum').reset_index()
    gr_asset_df.columns = list(gr_asset_df.columns)
    gr_asset_df['Счет'] = gr_asset_df['Счет'].apply(lambda x: x + ' ') + gr_asset_df['Валюта']
    gr_asset_df = gr_asset_df.drop('Валюта', axis=1).set_index('Счет')

    # Calculate sum of all money in currency
    if not gr_asset_df.empty:
        gr_asset_df.loc[('Всего в валюте')] = gr_asset_df.sum(axis=0, min_count=1)
    gr_asset_df = gr_asset_df.reset_index()

    # Convert currencies in cols
    gr_asset_df_ = gr_asset_df.copy()
    # Go by cols to covert
    for curr_from in [curr_1 for curr_1 in gr_asset_df.columns if curr_1 not in ['Счет']]:
        # Get not na cols to convert
        sml_df = gr_asset_df[(gr_asset_df[curr_from].notna()) & (gr_asset_df['Счет'] != 'Всего в валюте')]

        # Go by another cols
        for curr_to in [curr_2 for curr_2 in sml_df.columns if curr_2 not in ['Счет', curr_from]]:
            rate = _get_fx_rate_as_of(curr_from, curr_to, snapshot_date)
            if rate is None:
                print(f"Warning: No rate available for {curr_from}/{curr_to} as of {snapshot_date}, skipping conversion")
                continue
            sml_df[curr_to] = sml_df[curr_from] * rate
        gr_asset_df_.update(sml_df)

    # Calculate sum of all money by col
    gr_asset_df_ = gr_asset_df_.set_index('Счет')
    if not gr_asset_df.empty:
        account_rows = gr_asset_df_.index != 'Всего в валюте'
        gr_asset_df_.loc['Всего в валюте,%'] = (gr_asset_df_.loc['Всего в валюте'] / gr_asset_df_[account_rows].sum(axis=0, min_count=1)) * 100
        total_rows = ~gr_asset_df_.index.isin(['Всего в валюте', 'Всего в валюте,%'])
        gr_asset_df_.loc['Всего'] = gr_asset_df_[total_rows].sum(axis=0, min_count=1)
    gr_asset_df_ = gr_asset_df_

    # Format table
    for col_name in gr_asset_df_:
        # print(gr_asset_df_.loc[gr_asset_df_.index != 'Всего в валюте,%'])
        
        if col_name not in ['Счет']:
            gr_asset_df_.loc[gr_asset_df_.index != 'Всего в валюте,%', col_name] = (gr_asset_df_.loc[gr_asset_df_.index != 'Всего в валюте,%', col_name]
                                                                                          .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') + config.UNIQUE_TICKERS[col_name])
            gr_asset_df_.loc[gr_asset_df_.index == 'Всего в валюте,%', col_name] = (gr_asset_df_.loc[gr_asset_df_.index == 'Всего в валюте,%', col_name]
                                                                                          .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') + "%")
    return gr_asset_df_.reset_index().round(2)


def get_month_transactions(currency, year, month):
    return _get_month_transactions_cached(
        str(currency).upper(),
        str(year),
        str(int(month)) if str(month).isdigit() else str(month),
    ).copy(deep=True)


@lru_cache(maxsize=None)
def _get_month_transactions_cached(currency, year, month):
    transactions_df = get_transactions()
    smpl_tr_df = transactions_df[(transactions_df['Год'].isin(list(np.array(year).flat))) &
                                 (transactions_df['Месяц'].isin(list(np.array(month).astype(int).astype(str).flat)))
                                 ].reset_index(drop=True)

    # Приводим валюты по историческому курсу даты операции.
    if not config.DEBUG:
        smpl_tr_df = convert_transaction(
            df_to_convert=smpl_tr_df,
            to_curr=currency,
            target_col='Значение',
            use_current_rate=False,
        )
    

    month_tr_df = (smpl_tr_df
                    .pivot_table(values='Значение', index=['Дата'], columns=['Категория'], aggfunc=np.sum)
                    .fillna(0)
                    .resample('D').sum()
                    .reset_index()
                    )
    month_tr_df['Дата'] = month_tr_df['Дата'].dt.strftime("%Y-%m-%d")
    month_tr_df = month_tr_df.rename(columns={'Долги (у меня)': 'Дебиторская задолженность',
                                                'Крупные покупки/ Поездки': 'Поездки'},
                                    errors='ignore')
    num_of_cols = len(month_tr_df.columns)
    if 'Доход' in month_tr_df.columns:
        month_tr_df.insert(1, 'Доход', month_tr_df.pop('Доход'))
    else:
        month_tr_df['Доход'] = 0
    if 'Сбережения' in month_tr_df.columns:
        month_tr_df.insert(2, 'Сбережения', month_tr_df.pop('Сбережения'))
    else:
        month_tr_df['Сбережения'] = 0
    for col in ['Дебиторская задолженность', 'Погашение деб. зад.', 'Кредиторская задолженность', 'Погашение кред. зад.']:
        if col not in month_tr_df.columns:
            month_tr_df[col] = 0
    for col in ['Дебиторская задолженность', 'Погашение деб. зад.', 'Кредиторская задолженность', 'Погашение кред. зад.']:
        if col in month_tr_df.columns:
            month_tr_df.insert(num_of_cols - 1, col, month_tr_df.pop(col))
    return month_tr_df.fillna(0)


def clear_table_cache():
    _get_balance_by_month_cached.cache_clear()
    _get_asset_capital_by_month_cached.cache_clear()
    _get_act_receivables_cached.cache_clear()
    _get_act_liabilities_cached.cache_clear()
    _get_cost_distribution_cached.cache_clear()
    _get_month_transactions_cached.cache_clear()


def _as_tuple(value):
    return tuple(np.array(value).astype(str).flat)


def _as_month_tuple(value):
    return tuple(np.array(value).astype(int).astype(str).flat)


def _asset_snapshot_dates(assets_df: pd.DataFrame) -> pd.Series:
    periods = pd.PeriodIndex(
        year=assets_df['Год'].astype(int),
        month=assets_df['Месяц'].astype(int),
        freq='M',
    )
    return periods.to_timestamp(how='end').normalize()


def _convert_asset_values_as_of_snapshot(assets_df: pd.DataFrame, currency: str) -> pd.Series:
    values = pd.to_numeric(assets_df['Значение'], errors='coerce').copy()
    for (from_curr, snapshot_date), index in assets_df.groupby(['Валюта', 'Дата']).groups.items():
        from_curr = str(from_curr).upper()
        if from_curr == currency:
            continue
        rate = _get_fx_rate_as_of(from_curr, currency, snapshot_date)
        if rate is None:
            print(f"Warning: No rate available for {from_curr}/{currency} as of {snapshot_date}, leaving asset value unconverted")
            continue
        values.loc[index] = values.loc[index] * rate
    return values


def _get_fx_rate_as_of(from_curr: str, to_curr: str, as_of_date) -> float | None:
    from_curr = str(from_curr).upper()
    to_curr = str(to_curr).upper()
    if from_curr == to_curr:
        return 1.0
    if as_of_date is None or pd.isna(as_of_date):
        return None

    as_of = pd.Timestamp(as_of_date).normalize()
    lookback_start = as_of - pd.Timedelta(days=7)
    rates = get_fx_rates(from_curr, to_curr, lookback_start, as_of)
    if rates.empty:
        return get_fallback_rate(from_curr, to_curr)
    values = pd.to_numeric(rates.iloc[:, 0], errors='coerce').dropna()
    if values.empty:
        return get_fallback_rate(from_curr, to_curr)
    values = values[values.index <= as_of]
    if values.empty:
        return get_fallback_rate(from_curr, to_curr)
    return float(values.iloc[-1])


def _is_latest_asset_snapshot(year, month) -> bool:
    assets_df = get_assets()
    if assets_df.empty:
        return False
    latest = pd.PeriodIndex(
        year=assets_df['Год'].astype(int),
        month=assets_df['Месяц'].astype(int),
        freq='M',
    ).max()
    years = list(np.array(year).astype(int).flat)
    months = list(np.array(month).astype(int).flat)
    requested_periods = {
        pd.Period(year=year_value, month=month_value, freq='M')
        for year_value in years
        for month_value in months
    }
    return latest in set(requested_periods)

if __name__ == '__main__':
    pd.options.display.max_columns = 40
    pd.options.display.max_rows = 40

    CURRENCY = 'RUB'
    YEAR = '2023'
    MONTH = '01'

    test = get_month_transactions(currency=CURRENCY, year=YEAR, month=MONTH)
    print(test)

    # buy_df, sell_df = create_invest_tbl()
    # print(buy_df)
    # print(sell_df)
