from plotly.subplots import make_subplots
import plotly.graph_objects as go
import numpy as np
import pandas as pd
import os

from src.data.proccess import convert_transaction
from src.data.get_finance import get_actual_rates
from src.model.create_tables import get_capital_by_month, get_act_receivables, get_act_liabilities
from src import config


def create_month_report(transactions_df, assets_df, year, month, currency):
    assert currency in config.UNIQUE_TICKERS.keys(), f'currency должно быть из {config.UNIQUE_TICKERS.keys()}'

    # Берем подвыборку транзакций по году
    smpl_tr_df = transactions_df[(transactions_df['Год'].isin(list(np.array(year).flat))) &
                                 (transactions_df['Месяц'].isin(list(np.array(month).flat)))
                                 ].reset_index(drop=True)

    smpl_asset_df = assets_df[(assets_df['Год'].isin(list(np.array(year).flat))) &
                              (assets_df['Месяц'].isin(list(np.array(month).flat)))
                              ].reset_index(drop=True)
    smpl_asset_df.drop(['Год', 'Месяц', 'Квартал'], axis=1, inplace=True)

    capital_df = get_capital_by_month(currency).loc[f'{year}-{month}', 'Капитал']

    # Приводим валюты
    if not config.DEBUG:
        smpl_tr_df = convert_transaction(df_to_convert=smpl_tr_df, to_curr=currency)

    # Находим категории отчета
    cost_categories = [x for x in smpl_tr_df.Категория.unique() if x not in config.NOT_COST_COLS]

    fig = make_subplots(
        rows=6, cols=4,
        shared_xaxes=True,
        vertical_spacing=0.03,
        specs=[[{"type": "table", "colspan": 4}, None, None, None],
               [{"type": "table", "colspan": 4}, None, None, None],
               [{"type": "table", "colspan": 2}, None, {"type": "table", "colspan": 2}, None],
               [{"type": "table", "colspan": 4}, None, None, None],
               [{"type": "Bar", "colspan": 4}, None, None, None],
               [{"type": "table", "colspan": 4}, None, None, None],
               ],
        subplot_titles=('Транзакции', 'Суммарные показатели', 'Дебиторская задолженность',
                        'Кредиторская задолженность', 'Распределение расходов',
                        None, 'Распределение по счетам'
                        ),
        row_heights=[0.7, 0.1, 0.1, 0.1, 0.3, 0.5],
        column_widths=[0.25, 0.25, 0.25, 0.25]
    )

    month_df = pd.read_csv(os.path.join(config.TRANSACTIONS_INFO_PATH, year, f'{year}_{month}.csv'), sep=';',
                           decimal=',',
                           parse_dates=True,
                           dayfirst=True,
                           # index_col='Дата',
                           infer_datetime_format=True)
    month_df = month_df.rename(columns={'Долги (у меня)': 'Дебиторская задолженность',
                                        'Крупные покупки/ Поездки': 'Поездки'},
                               errors='ignore')

    fig.add_trace(
        go.Table(
            header=dict(values=list(month_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[month_df[colname] for colname in month_df.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=1, col=1
    )

    # Добавляем таблицу суммарных показателей
    month_mean_df = (smpl_tr_df[smpl_tr_df.Категория.isin(config.NOT_COST_COLS)]
                     .pivot_table(values='Значение', columns=['Категория'], aggfunc=np.sum)
                     )
    for col_name in config.NOT_COST_COLS:
        if col_name not in month_mean_df.columns:
            month_mean_df.loc[:, col_name] = 0
    month_mean_df = month_mean_df[config.NOT_COST_COLS]

    month_mean_df['Расход'] = (smpl_tr_df[~smpl_tr_df.Категория.isin(config.NOT_COST_COLS)]
                               .groupby('Месяц').sum().reset_index()
                               .rename({'Значение': 'Расход'}, axis=1)['Расход']
                               .squeeze()
                               )
    month_mean_df['Баланс'] = (month_mean_df['Доход'] + month_mean_df['Сбережения']
                               - month_mean_df['Дебиторская задолженность'] + month_mean_df['Погашение деб. зад.']
                               + month_mean_df['Кредиторская задолженность'] - month_mean_df['Погашение кред. зад.']
                               - month_mean_df['Инвестиции']  # Заменить на инвестировано
                               - month_mean_df['Расход'])

    month_mean_df['Капитал'] = capital_df.squeeze()

    for col_name in month_mean_df:
        month_mean_df.loc[:, col_name] = (month_mean_df[col_name].astype(float)
                                          .map('{:,.2f}'.format).str.replace(',', ' ') +
                                          config.UNIQUE_TICKERS[currency])

    fig.add_trace(
        go.Table(
            header=dict(values=list(month_mean_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[month_mean_df[colname] for colname in month_mean_df.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=2, col=1
    )

    receivables_df = get_act_receivables()
    if receivables_df.empty:
        receivables_df = pd.concat([receivables_df,
                                    pd.DataFrame({'Комментарий': '-', 'Дебиторская задолженность': '-'}, index=[0])],
                                   axis=0,
                                   ignore_index=True
                                   )
    fig.add_trace(
        go.Table(
            header=dict(values=list(receivables_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[receivables_df[colname] for colname in receivables_df.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=3, col=1
    )

    liabilities_df = get_act_liabilities()
    if liabilities_df.empty:
        liabilities_df = pd.concat([liabilities_df,
                                    pd.DataFrame({'Комментарий': '-', 'Кредиторская задолженность': '-'}, index=[0])],
                                   axis=0,
                                   ignore_index=True
                                   )
    fig.add_trace(
        go.Table(
            header=dict(values=list(liabilities_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[liabilities_df[colname] for colname in liabilities_df.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=3, col=3
    )

    cost_stats_df = (smpl_tr_df[smpl_tr_df.Категория.isin(cost_categories)]
                     .groupby('Категория').agg({'Значение': ['sum']})
                     .droplevel(0, axis=1)
                     .rename({'sum': 'Суммарно в месяц'}, axis=1)
                     )
    cost_stats_df['Процент'] = (cost_stats_df['Суммарно в месяц'] / cost_stats_df['Суммарно в месяц'].sum()).round(
        2) * 100
    cost_stats_df = cost_stats_df.T[cost_categories]
    for col_name in cost_stats_df:
        cost_stats_df.loc[['Суммарно в месяц'],
                          col_name] = (cost_stats_df.loc[['Суммарно в месяц'],
                                                         col_name]
                                       .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                       config.UNIQUE_TICKERS[currency])
        cost_stats_df.loc[['Процент'], col_name] = (cost_stats_df.loc[['Процент'], col_name].astype(str) + '%')

    cost_stats_df = cost_stats_df.reset_index().rename({'index': 'Показатель'}, axis=1)

    fig.add_trace(
        go.Table(
            header=dict(values=list(cost_stats_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[cost_stats_df[colname] for colname in cost_stats_df.columns],
                       fill_color='lavender',
                       font={'color': ['black', 'black'], 'size': [10, 12]},
                       align='left'),
        ),
        row=4, col=1
    )

    cost_plot_df = smpl_tr_df[smpl_tr_df.Категория.isin(cost_categories)].round(2)
    fig.add_trace(go.Bar(name='test', x=cost_plot_df.Категория, y=cost_plot_df.Значение)
                  , row=5, col=1
                  )

    # Добавляем таблицу активов
    smpl_asset_df = smpl_asset_df.pivot_table(index=['Счет', 'Валюта'], columns='Валюта',
                                              values='Значение', aggfunc='sum').reset_index()
    smpl_asset_df.columns = list(smpl_asset_df.columns)
    smpl_asset_df['Счет'] = smpl_asset_df['Счет'].apply(lambda x: x + ' ') + smpl_asset_df['Валюта']
    smpl_asset_df = smpl_asset_df.drop('Валюта', axis=1).set_index('Счет')
    smpl_asset_df.loc[('Всего в валюте')] = smpl_asset_df.sum(axis=0, min_count=1)
    smpl_asset_df = smpl_asset_df.reset_index()
    smpl_asset_df_ = smpl_asset_df.copy()

    for curr_from in [curr_1 for curr_1 in smpl_asset_df.columns if curr_1 not in ['Счет']]:
        sml_df = smpl_asset_df[(smpl_asset_df[curr_from].notna()) & (smpl_asset_df['Счет'] != 'Всего в валюте')]
        for curr_to in [curr_2 for curr_2 in sml_df.columns if curr_2 not in ['Счет', curr_from]]:
            ticker_ = curr_from + curr_to + '=X' if config.STOCK_API == 'yf' else curr_from + '/' + curr_to
            rate = get_actual_rates(tickers=[ticker_], days_before=7)[f'Актуальная_цена_{config.STOCK_API}'].squeeze()
            sml_df[curr_to] = sml_df[curr_from] * rate
        smpl_asset_df_.update(sml_df)

    smpl_asset_df_ = smpl_asset_df_.set_index('Счет')
    smpl_asset_df_.loc['Всего'] = smpl_asset_df_[smpl_asset_df_.index != 'Всего в валюте'].sum(axis=0, min_count=1)
    smpl_asset_df_ = smpl_asset_df_.reset_index().round(2)
    for col_name in smpl_asset_df_:
        if col_name not in ['Счет']:
            smpl_asset_df_[col_name] = (smpl_asset_df_[col_name]
                                        .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                        config.UNIQUE_TICKERS[col_name])

    fig.add_trace(
        go.Table(
            header=dict(values=list(smpl_asset_df_.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[smpl_asset_df_[colname] for colname in smpl_asset_df_.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=6, col=1
    )

    fig.update_layout(
        height=2000,
        showlegend=False,
        title_text=f"Отчет за {month} месяц {year} года, в валюте {currency}",
    )

    month_folder_name = os.path.join(config.REPORTS_PATH, 'Месячные отчеты')
    if 'Месячные отчеты' not in os.listdir(config.REPORTS_PATH):
        os.makedirs(month_folder_name)

    cur_folder_dir = os.path.join(month_folder_name, currency)
    if currency not in os.listdir(month_folder_name):
        os.makedirs(cur_folder_dir)

    year_folder_dir = os.path.join(cur_folder_dir, year)
    if year not in os.listdir(cur_folder_dir):
        os.makedirs(year_folder_dir)

    fig.write_html(os.path.join(year_folder_dir, f"Отчет за {month} {year} года.html"))
    fig.show()
