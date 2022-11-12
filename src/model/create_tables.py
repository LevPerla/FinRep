import numpy as np
import pandas as pd

from src.data.get import get_investments, get_transactions
from src.data.proccess import convert_transaction
from src.data.get_finance import get_actual_rates, get_act_moex
from src import config


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


def get_capital_by_month(currency):
    transactions_df = get_transactions()
    buy_df, sell_df = create_invest_tbl()

    # Приводим валюты
    if not config.DEBUG:
        transactions_df = convert_transaction(df_to_convert=transactions_df, to_curr=currency)

    all_stats_df = (transactions_df[transactions_df.Категория.isin(config.NOT_COST_COLS)]
                    .pivot_table(values='Значение', index=['Дата'], columns=['Категория'], aggfunc=np.sum)
                    .fillna(0)
                    .resample('M').sum()
                    )
    all_stats_df['Расход'] = (transactions_df[~transactions_df.Категория.isin(config.NOT_COST_COLS)]
                              .set_index('Дата').resample('M')['Значение'].sum())

    all_stats_df['Инвстировано'] = buy_df.set_index('Дата').resample('M')['Сумма'].sum()
    all_stats_df['Доход_расход_от_инвест'] = sell_df.set_index('Дата').resample('M')['Прибыль/убыток'].sum()
    all_stats_df = all_stats_df.fillna(0)

    all_stats_df['Итог'] = (all_stats_df['Доход'] + all_stats_df['Сбережения']
                            - all_stats_df['Инвстировано'] + all_stats_df['Доход_расход_от_инвест']
                            - all_stats_df['Дебиторская задолженность'] + all_stats_df['Погашение деб. зад.']
                            + all_stats_df['Кредиторская задолженность'] - all_stats_df['Погашение кред. зад.']
                            - all_stats_df['Расход']
                            )
    all_stats_df['Капитал'] = all_stats_df['Итог'].cumsum()
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
