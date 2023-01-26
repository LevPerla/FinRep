import numpy as np
import pandas as pd

from src import config, utils
from src.data.get import get_investments, get_transactions, get_assets
from src.data.get_finance import get_actual_rates, get_act_moex
from src.data.proccess import convert_transaction


def create_invest_tbl(max_date=None):
    # Выгружаем данные по инвестициям
    investments_df = get_investments()
    if max_date is not None:
        investments_df = investments_df[investments_df['Дата'] <= max_date]
    investments_df['Валюта'] = investments_df['Цена'].apply(lambda x: x.split('|')[1])
    investments_df['Цена'] = investments_df['Цена'].apply(lambda x: x.split('|')[0].replace(',', '.')).astype(float)

    buy_df = investments_df[investments_df['Тип_транзакции'] == 'Покупка']
    sell_df = investments_df[investments_df['Тип_транзакции'] == 'Продажа']
    sell_df['Прибыль/убыток'] = np.nan

    # Обрабатываем продажи
    for sell_index, sell_row in sell_df.iterrows():
        buy_df_smpl = buy_df[(buy_df['Тикер'] == sell_row['Тикер']) &
                             (buy_df['Дата'] <= sell_row['Дата'])].sort_values('Дата')
        need_to_sell = sell_row['Количество']
        sell_row['Прибыль/убыток'] = sell_row['Количество'] * sell_row['Цена']

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
    # Считаем тикеры
    if config.STOCK_API == 'yf':
        buy_df.loc[buy_df['Актив'] == 'Валюта',
                   'ticker_'] = (buy_df['Тикер'] +
                                 buy_df['Валюта'].apply(lambda x: x + '=X'))
    elif config.STOCK_API == 'td':
        buy_df.loc[buy_df['Актив'] == 'Валюта',
                   'ticker_'] = (buy_df['Тикер'].apply(lambda x: x + '/') +
                                 buy_df['Валюта'])
    buy_df.loc[buy_df['Актив'].isin(['Акции', 'Фонды']), 'ticker_'] = buy_df['Тикер']

    # Заполняем актуальными ценами на MOEX
    moex_stocks_rates = get_act_moex(mode='stocks').rename(columns={'Тикер': 'ticker_'})
    etf_stocks_rates = get_act_moex(mode='ETF').rename(columns={'Тикер': 'ticker_'})
    buy_df = (buy_df.merge(moex_stocks_rates, on='ticker_', how='left')
              .merge(etf_stocks_rates, on='ticker_', how='left')
              )
    buy_df['Актуальная цена'] = buy_df[['Актуальная_цена_moex_stocks',
                                        'Актуальная_цена_moex_ETF']].sum(axis=1, min_count=1)

    # Для тикеров не из MOEX заполняем из других api
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
                 ], axis=1, inplace=True)

    # Считаем аналитики
    buy_df['Прибыль'] = buy_df['Количество'] * buy_df['Актуальная цена'] - buy_df['Сумма']
    buy_df['Доходность'] = (buy_df['Прибыль'] / buy_df['Сумма']).round(3) * 100
    buy_df = buy_df.round(2)

    return buy_df, sell_df


def get_balance_by_month(currency):
    transactions_df = get_transactions()
    buy_df, sell_df = create_invest_tbl()

    # Приводим валюты
    if not config.DEBUG:
        transactions_df = convert_transaction(df_to_convert=transactions_df, to_curr=currency, target_col='Значение')
        buy_df = convert_transaction(buy_df, to_curr=currency, target_col='Сумма')
        sell_df = convert_transaction(sell_df, to_curr=currency, target_col='Прибыль/убыток')

    all_stats_df = (transactions_df[transactions_df.Категория.isin(config.NOT_COST_COLS)]
                    .pivot_table(values='Значение', index=['Дата'], columns=['Категория'], aggfunc=np.sum)
                    .fillna(0)
                    .resample('M').sum()
                    )
    all_stats_df['Расход'] = (transactions_df[~transactions_df.Категория.isin(config.NOT_COST_COLS)]
                              .set_index('Дата').resample('M')['Значение'].sum())

    all_stats_df['Инвестировано'] = buy_df.set_index('Дата').resample('M')['Сумма'].sum()
    all_stats_df['Доход от инвестирования'] = sell_df.set_index('Дата').resample('M')['Прибыль/убыток'].sum()
    all_stats_df = all_stats_df.fillna(0)

    all_stats_df['Баланс'] = (all_stats_df['Доход'] + all_stats_df['Сбережения']
                              - all_stats_df['Инвестировано'] + all_stats_df['Доход от инвестирования']
                              - all_stats_df['Дебиторская задолженность'] + all_stats_df['Погашение деб. зад.']
                              + all_stats_df['Кредиторская задолженность'] - all_stats_df['Погашение кред. зад.']
                              - all_stats_df['Расход']
                              )
    all_stats_df['Капитал'] = all_stats_df['Баланс'].cumsum()
    return all_stats_df


def get_act_receivables():
    transactions_df = get_transactions()
    receivable_df = transactions_df[(transactions_df['Категория'] == 'Дебиторская задолженность') &
                                    (transactions_df['Значение'] != 0)][
        ['Дата', 'Значение', 'Комментарий']].sort_values('Дата')
    paid_receivable_df = (transactions_df[(transactions_df['Категория'] == 'Погашение деб. зад.') &
                                          (transactions_df['Значение'] != 0)]
                          [['Дата', 'Значение', 'Комментарий']]
                          .sort_values('Дата')
                          )
    receivable_df = pd.concat(
        [receivable_df.groupby('Комментарий')['Значение'].sum().rename('Дебиторская задолженность'),
         paid_receivable_df.groupby('Комментарий')['Значение'].sum().rename('Погашение деб. зад.')
         ], axis=1)
    receivable_df['Погашение деб. зад.'] = receivable_df['Погашение деб. зад.'].fillna(0)
    receivable_df['Дебиторская задолженность'] = receivable_df['Дебиторская задолженность'] - receivable_df[
        'Погашение деб. зад.']
    receivable_df = receivable_df[receivable_df['Дебиторская задолженность'] != 0]['Дебиторская задолженность']
    return receivable_df.reset_index()


def get_act_liabilities():
    transactions_df = get_transactions()
    liabilities_df = (transactions_df[(transactions_df['Категория'] == 'Кредиторская задолженность') &
                                      (transactions_df['Значение'] != 0)]
                      [['Дата', 'Значение', 'Комментарий']]
                      .sort_values('Дата')
                      )

    paid_liabilities_df = (transactions_df[(transactions_df['Категория'] == 'Погашение кред. зад.') &
                                           (transactions_df['Значение'] != 0)]
                           [['Дата', 'Значение', 'Комментарий']]
                           .sort_values('Дата')
                           )
    liabilities_df = pd.concat(
        [liabilities_df.groupby('Комментарий')['Значение'].sum().rename('Кредиторская задолженность'),
         paid_liabilities_df.groupby('Комментарий')['Значение'].sum().rename('Погашение кред. зад.')
         ], axis=1)
    liabilities_df['Погашение кред. зад.'] = liabilities_df['Погашение кред. зад.'].fillna(0)
    liabilities_df['Кредиторская задолженность'] = liabilities_df['Кредиторская задолженность'] - liabilities_df[
        'Погашение кред. зад.']
    liabilities_df = liabilities_df[liabilities_df['Кредиторская задолженность'] != 0]['Кредиторская задолженность']
    return liabilities_df.reset_index()


def get_cost_distribution(currency, year, month=None):
    transactions_df = get_transactions()

    # Get transaction sample by chosen year
    if month is None:
        smpl_tr_df = transactions_df[transactions_df['Год'].isin(list(np.array(year).flat))].reset_index(drop=True)
    else:
        smpl_tr_df = transactions_df[(transactions_df['Год'].isin(list(np.array(year).flat))) &
                                     (transactions_df['Месяц'].isin(list(np.array(month).astype(int).astype(str).flat)))
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
    if month is None:
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
    assets_df.drop(['Год', 'Месяц', 'Квартал'], axis=1, inplace=True)

    gr_asset_df = assets_df.pivot_table(index=['Счет', 'Валюта'], columns='Валюта',
                                        values='Значение', aggfunc='sum').reset_index()
    gr_asset_df.columns = list(gr_asset_df.columns)
    gr_asset_df['Счет'] = gr_asset_df['Счет'].apply(lambda x: x + ' ') + gr_asset_df['Валюта']
    gr_asset_df = gr_asset_df.drop('Валюта', axis=1).set_index('Счет')

    if not gr_asset_df.empty:
        gr_asset_df.loc[('Всего в валюте')] = gr_asset_df.sum(axis=0, min_count=1)
    gr_asset_df = gr_asset_df.reset_index()
    gr_asset_df_ = gr_asset_df.copy()

    for curr_from in [curr_1 for curr_1 in gr_asset_df.columns if curr_1 not in ['Счет']]:
        sml_df = gr_asset_df[(gr_asset_df[curr_from].notna()) & (gr_asset_df['Счет'] != 'Всего в валюте')]
        for curr_to in [curr_2 for curr_2 in sml_df.columns if curr_2 not in ['Счет', curr_from]]:
            ticker_ = curr_from + curr_to + '=X' if config.STOCK_API == 'yf' else curr_from + '/' + curr_to
            rate = get_actual_rates(tickers=[ticker_])[f'Актуальная_цена_{config.STOCK_API}'].squeeze()
            sml_df[curr_to] = sml_df[curr_from] * rate
        gr_asset_df_.update(sml_df)

    gr_asset_df_ = gr_asset_df_.set_index('Счет')
    if not gr_asset_df.empty:
        gr_asset_df_.loc['Всего'] = gr_asset_df_[gr_asset_df_.index != 'Всего в валюте'].sum(axis=0, min_count=1)
    gr_asset_df_ = gr_asset_df_.reset_index().round(2)

    # Format table
    for col_name in gr_asset_df_:
        if col_name not in ['Счет']:
            gr_asset_df_[col_name] = (gr_asset_df_[col_name]
                                      .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                      config.UNIQUE_TICKERS[col_name])
    return gr_asset_df_


def get_month_transactions(currency, year, month):
    transactions_df = get_transactions()
    smpl_tr_df = transactions_df[(transactions_df['Год'].isin(list(np.array(year).flat))) &
                                 (transactions_df['Месяц'].isin(list(np.array(month).astype(int).astype(str).flat)))
                                 ].reset_index(drop=True)

    # Приводим валюты
    if not config.DEBUG:
        smpl_tr_df = convert_transaction(df_to_convert=smpl_tr_df, to_curr=currency, target_col='Значение')

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
        if col in month_tr_df.columns:
            month_tr_df.insert(num_of_cols - 1, col, month_tr_df.pop(col))
    return month_tr_df

if __name__ == '__main__':
    pd.options.display.max_columns = 40
    pd.options.display.max_rows = 40
    # month_tr_df = get_balance_by_month(currency='EUR')
    month_tr_df = get_month_transactions(currency='EUR', year='2022', month='11')
    print(month_tr_df)
