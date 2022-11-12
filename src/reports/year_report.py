from plotly.subplots import make_subplots
import plotly.graph_objects as go
import plotly.express as px
import numpy as np
import pandas as pd
import os

from src.model.create_tables import get_capital_by_month
from src.data.proccess import convert_transaction
from src import config


def create_year_report(transactions_df, year, currency):
    assert currency in config.UNIQUE_TICKERS.keys(), f'currency должно быть из {config.UNIQUE_TICKERS.keys()}'

    # Берем подвыборку транзакций по году
    smpl_tr_df = transactions_df[transactions_df['Год'].isin(list(np.array(year).flat))].reset_index(drop=True)

    # Приводим валюты
    if not config.DEBUG:
        smpl_tr_df = convert_transaction(df_to_convert=smpl_tr_df, to_curr=currency)

    # Находим категории отчета
    cost_categories = [x for x in smpl_tr_df.Категория.unique() if x not in config.NOT_COST_COLS]

    # Создаем маску отчета
    fig = make_subplots(
        rows=6, cols=3,
        shared_xaxes=True,
        vertical_spacing=0.04,
        specs=[[{"type": "table", "colspan": 3}, None, None],
               [{"type": "table", "colspan": 3}, None, None],
               [{"type": "Bar", "colspan": 3}, None, None],
               [{"type": "table"}, {"type": "scatter"}, {"type": "table"}],
               [None, {"type": "table"}, None],
               [{"type": "table"}, {"type": "scatter", "colspan": 2}, None]
               ],
        subplot_titles=('Итоги по кварталам',
                        'Распределение расходов',
                        None,
                        None, 'Динамика доходов/расходов', None,
                        'Описательные статистики',
                        None, 'Динамика капитала'),
        row_heights=[0.10, 0.10, 0.25, 0.25, 0.12, 0.2],
        column_widths=[0.23, 0.52, 0.25],

    )

    # Cоздаем таблицу с суммарными показателями по кварталам
    quarter_mean_df = (smpl_tr_df[smpl_tr_df['Категория'] == 'Доход'].groupby('Квартал').sum()
                       .reset_index()
                       .rename({'Значение': 'Общий доход'}, axis=1)
                       )
    quarter_mean_df['Общий расход'] = (smpl_tr_df[smpl_tr_df.Категория.isin(cost_categories)].groupby('Квартал').sum().
        reset_index()
        .rename({'Значение': 'Общий расход'}, axis=1)['Общий расход']
        )
    quarter_mean_df['Сальдо'] = quarter_mean_df['Общий доход'] - quarter_mean_df['Общий расход']

    for col_name in quarter_mean_df:
        if col_name != 'Квартал':
            quarter_mean_df.loc[:, col_name] = quarter_mean_df[col_name].astype(float).map(
                '{:,.2f}'.format).str.replace(',', ' ') + \
                                               config.UNIQUE_TICKERS[currency]

    fig.add_trace(
        go.Table(
            header=dict(values=list(quarter_mean_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[quarter_mean_df.Квартал, quarter_mean_df['Общий доход'],
                               quarter_mean_df['Общий расход'], quarter_mean_df['Сальдо']],
                       fill_color='lavender',
                       font={'color': 'black', 'size': [10, 12]},
                       align='left'),
        ),
        row=1, col=1
    )

    # Таблица распределения затрат по категориям
    cost_stats_df = (smpl_tr_df[smpl_tr_df.Категория.isin(cost_categories)]
                     .groupby('Категория').agg({'Значение': ['sum']})
                     .droplevel(0, axis=1)
                     .rename({'sum': 'Суммарно в год'}, axis=1)
                     )
    cost_stats_df['Среднее в месяц'] = cost_stats_df['Суммарно в год'] / smpl_tr_df.Месяц.astype(int).max()
    cost_stats_df['Процент'] = (cost_stats_df['Суммарно в год'] / cost_stats_df['Суммарно в год'].sum()).round(2) * 100
    cost_stats_df = cost_stats_df.T[cost_categories]
    for col_name in cost_stats_df:
        if col_name != 'Показатель':
            cost_stats_df.loc[['Суммарно в год', 'Среднее в месяц'],
                              col_name] = (cost_stats_df.loc[['Суммарно в год', 'Среднее в месяц'],
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
        row=2, col=1
    )

    # График распрдееления затрат по категориям
    cost_plot_df = smpl_tr_df[smpl_tr_df.Категория.isin(cost_categories)]
    fig.add_trace(go.Bar(name='test', x=cost_plot_df.Категория, y=cost_plot_df.Значение)
                  , row=3, col=1
                  )

    mean_month_income_df = (smpl_tr_df[smpl_tr_df['Категория'] == 'Доход']
                            .set_index('Дата').resample('M').sum()
                            .reset_index()
                            .rename({'Значение': 'Доход'}, axis=1)
                            .round(2)
                            )
    for col_name in mean_month_income_df:
        if col_name != 'Дата':
            mean_month_income_df.loc[:, col_name] = (mean_month_income_df[col_name]
                                                     .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                                     config.UNIQUE_TICKERS[currency])

    colors = px.colors.sample_colorscale("geyser", [n / (mean_month_income_df.shape[0] - 1) for n in
                                                    range(mean_month_income_df.shape[0])])
    fig.add_trace(
        go.Table(
            header=dict(values=list(mean_month_income_df.columns),
                        fill_color='paleturquoise',
                        line_color=['black', 'black'],
                        align='left'),
            cells=dict(values=[mean_month_income_df['Дата'].dt.strftime('%Y-%m'), mean_month_income_df['Доход']],
                       line_color=['black', 'black'],
                       fill_color=['lavender', np.array(colors)[
                           (mean_month_income_df.Доход.str[:-1].str.replace(' ', '').astype(float).rank(
                               ascending=False) - 1).astype(int).values]],
                       align='left',
                       font={'color': ['black', 'black'], 'size': 11}
                       ),
        ),
        row=4, col=1
    )

    # Таблица с динамикой расходов
    mean_month_cost_df = (smpl_tr_df[smpl_tr_df.Категория.isin(cost_categories)]
                          .set_index('Дата').resample('M').sum()
                          .reset_index()
                          .rename({'Значение': 'Расход'}, axis=1)
                          .round(2)
                          )
    for col_name in mean_month_cost_df:
        if col_name != 'Дата':
            mean_month_cost_df.loc[:, col_name] = (mean_month_cost_df[col_name]
                                                   .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                                   config.UNIQUE_TICKERS[currency])

    colors = px.colors.sample_colorscale("geyser", [n / (mean_month_cost_df.shape[0] - 1) for n in
                                                    range(mean_month_cost_df.shape[0])])
    fig.add_trace(
        go.Table(
            header=dict(values=list(mean_month_cost_df.columns),
                        fill_color='paleturquoise',
                        line_color=['black', 'black'],
                        align='left'),
            cells=dict(values=[mean_month_cost_df['Дата'].dt.strftime('%Y-%m'), mean_month_cost_df['Расход']],
                       line_color=['black', 'black'],
                       fill_color=['lavender', np.array(colors)[
                           (mean_month_cost_df.Расход.str[:-1].str.replace(' ', '').astype(float).rank(
                               ascending=True) - 1).astype(int).values]],
                       align='left',
                       font={'color': ['black', 'black'], 'size': 11}
                       ),
        ),
        row=4, col=3
    )

    fig.add_trace(go.Scatter(x=mean_month_income_df['Дата'],
                             y=mean_month_income_df['Доход'].str[:-1].str.replace(' ', '').astype(float)),
                  row=4, col=2
                  )

    fig.add_trace(go.Scatter(x=mean_month_cost_df['Дата'],
                             y=mean_month_cost_df['Расход'].str[:-1].str.replace(' ', '').astype(float)),
                  row=4, col=2
                  )

    # Статистика доходов и расходов
    inc_cost_stats_df = (
        pd.concat([mean_month_income_df.set_index('Дата')['Доход'].str[:-1].str.replace(' ', '').astype(float),
                   mean_month_cost_df.set_index('Дата')['Расход'].str[:-1].str.replace(' ', '').astype(float)
                   ], axis=1)
        .agg({'Доход': ['sum', 'mean', np.median, np.std, np.min, np.max],
              'Расход': ['sum', 'mean', np.median, np.std, np.min, np.max]
              }, axis=0)
        .rename(index={'sum': 'Сумма', 'mean': 'Среднее',
                       'median': 'Медиана', 'std': 'Ст. отклонение',
                       'amin': 'Минимум', 'amax': 'Максимум'})
        .reset_index()
        .rename(columns={'index': 'Статистика'})
        )
    for col_name in inc_cost_stats_df:
        if col_name != 'Статистика':
            inc_cost_stats_df.loc[:, col_name] = (inc_cost_stats_df[col_name]
                                                  .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                                  config.UNIQUE_TICKERS[currency])
    fig.add_trace(
        go.Table(
            header=dict(values=list(inc_cost_stats_df.columns),
                        fill_color='paleturquoise',
                        line_color=['black', 'black'],
                        align='left'),
            cells=dict(values=[inc_cost_stats_df[colname] for colname in inc_cost_stats_df.columns],
                       line_color=['black', 'black'],
                       align='left',
                       font={'color': ['black', 'black'], 'size': 11}
                       ),
        ),
        row=5, col=2
    )

    # График изменения капитала
    capital_df = get_capital_by_month(currency)['Капитал'].loc[year].reset_index()

    for col_name in capital_df:
        if col_name != 'Дата':
            capital_df.loc[:, col_name] = (capital_df[col_name]
                                           .astype(float).map('{:,.2f}'.format).str.replace(',', ' ') +
                                           config.UNIQUE_TICKERS[currency])

    colors = px.colors.sample_colorscale("geyser", [n / (capital_df.shape[0] - 1) for n in
                                                    range(capital_df.shape[0])])
    fig.add_trace(
        go.Table(
            header=dict(values=list(capital_df.columns),
                        fill_color='paleturquoise',
                        line_color=['black', 'black'],
                        align='left'),
            cells=dict(values=[capital_df['Дата'].dt.strftime('%Y-%m'), capital_df['Капитал']],
                       line_color=['black', 'black'],
                       fill_color=['lavender', np.array(colors)[
                           (capital_df.Капитал.rank(ascending=False) - 1).astype(int).values]],
                       align='left',
                       font={'color': ['black', 'black'], 'size': 11}
                       ),
        ),
        row=6, col=1
    )

    fig.add_trace(go.Scatter(x=capital_df['Дата'],
                             y=capital_df['Капитал'].str[:-1].str.replace(' ', '').astype(float),
                             mode='lines+markers',
                             name='Капитал',
                             line=dict(color='green', width=2),
                             ),
                  row=6, col=2
                  )

    fig.update_layout(
        height=1900,
        showlegend=False,
        title_text=f"Отчет за {year} год в валюте {currency}"
    )

    year_folder_name = os.path.join(config.REPORTS_PATH, 'Годовые отчеты')
    if 'Годовые отчеты' not in os.listdir(config.REPORTS_PATH):
        os.makedirs(year_folder_name)

    cur_folder_dir = os.path.join(year_folder_name, currency)
    if currency not in os.listdir(year_folder_name):
        os.makedirs(cur_folder_dir)

    fig.write_html(os.path.join(cur_folder_dir, f"Отчет за {year} год.html"))
    fig.show()
