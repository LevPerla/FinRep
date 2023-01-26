import os

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src import config, utils
from src.data.proccess import convert_transaction
from src.model.create_tables import get_balance_by_month, get_cost_distribution

def create_year_report(year, currency, return_image=False):
    assert currency in config.UNIQUE_TICKERS.keys(), f'currency должно быть из {config.UNIQUE_TICKERS.keys()}'

    # Find balance info
    balance_df = get_balance_by_month(currency)

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

    # Add avg stats by quarter
    quarter_mean_df = _create_quarter_stats(balance_df, year, currency)
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

    # Add costs stats by categories
    cost_stats_df = get_cost_distribution(currency=currency, year=year)
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

    # Add cost distribution bar plot
    cost_plot_df = _create_cost_plot_table(cost_stats_df)
    fig.add_trace(go.Bar(x=cost_plot_df.Показатель, y=cost_plot_df.Суммарно), row=3, col=1)

    # Add income by month stats
    mean_month_income_df = _create_income_table(balance_df, year, currency)
    colors = px.colors.sample_colorscale("geyser", [n / (mean_month_income_df.shape[0]) for n in
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

    # Add costs by month stats
    mean_month_cost_df = _create_costs_table(balance_df, year, currency)
    colors = px.colors.sample_colorscale("geyser", [n / (mean_month_cost_df.shape[0]) for n in
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

    # Add income plot
    fig.add_trace(go.Scatter(x=mean_month_income_df['Дата'],
                             y=mean_month_income_df['Доход'].str[:-1].str.replace(' ', '').astype(float)),
                  row=4, col=2
                  )

    # Add costs plot
    fig.add_trace(go.Scatter(x=mean_month_cost_df['Дата'],
                             y=mean_month_cost_df['Расход'].str[:-1].str.replace(' ', '').astype(float)),
                  row=4, col=2
                  )

    # Add costs and income stats table
    inc_cost_stats_df = _crete_inc_cost_stats(balance_df, year, currency)
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

    # Add capital by month table
    capital_df = _create_capital_table(balance_df, year, currency)
    colors = px.colors.sample_colorscale("geyser", [n / (capital_df.shape[0]) for n in
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

    # Add capital by month plot
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

    if return_image:
        fig.write_image(config.IMAGE_TO_BOT_PATH, scale=1, width=1200, height=2100)
    else:
        fig.write_html(os.path.join(cur_folder_dir, f"Отчет за {year} год.html"))
        fig.show()


def _create_quarter_stats(balance_df, year, currency):
    quarter_mean_df = (balance_df.loc[year, ['Доход', 'Расход']].resample('Q').sum()
                       .reset_index()
                       .rename({'Доход': 'Общий доход',
                                'Расход': 'Общий расход',
                                'Дата': 'Квартал'}, axis=1)
                       )
    quarter_mean_df['Квартал'] = quarter_mean_df['Квартал'].dt.quarter
    quarter_mean_df['Сальдо'] = quarter_mean_df['Общий доход'] - quarter_mean_df['Общий расход']
    quarter_mean_df = utils.process_num_cols(quarter_mean_df, not_num_cols=['Квартал'], currency=currency)
    return quarter_mean_df

def _create_income_table(balance_df, year, currency):
    mean_month_income_df = balance_df.loc[year, 'Доход'].reset_index()
    mean_month_income_df = utils.process_num_cols(mean_month_income_df, not_num_cols=['Дата'], currency=currency)
    return mean_month_income_df

def _create_costs_table(balance_df, year, currency):
    mean_month_cost_df = balance_df.loc[year, 'Расход'].reset_index()
    mean_month_cost_df = utils.process_num_cols(mean_month_cost_df, not_num_cols=['Дата'], currency=currency)
    return mean_month_cost_df

def _create_cost_plot_table(cost_stats_df):
    cost_plot_df = cost_stats_df.T.reset_index()
    cost_plot_df.columns = cost_plot_df.loc[0]
    cost_plot_df = cost_plot_df.loc[1:]
    cost_plot_df['Суммарно'] = cost_plot_df['Суммарно'].apply(lambda x: float(x[:-1].replace(' ', '')))
    return cost_plot_df

def _crete_inc_cost_stats(balance_df, year, currency):
    inc_cost_stats_df = (balance_df.loc[year, ['Доход', 'Расход']]
            .agg({'Доход': ['sum', 'mean', np.median, np.std, np.min, np.max],
                  'Расход': ['sum', 'mean', np.median, np.std, np.min, np.max]
                  }, axis=0)
            .rename(index={'sum': 'Сумма', 'mean': 'Среднее',
                           'median': 'Медиана', 'std': 'Ст. отклонение',
                           'amin': 'Минимум', 'amax': 'Максимум'})
            .reset_index()
            .rename(columns={'index': 'Статистика'})
    )
    inc_cost_stats_df = utils.process_num_cols(inc_cost_stats_df, not_num_cols=['Статистика'], currency=currency)
    return inc_cost_stats_df

def _create_capital_table(balance_df, year, currency):
    capital_df = balance_df.loc[year, 'Капитал'].reset_index()
    capital_df = utils.process_num_cols(capital_df, not_num_cols=['Дата'], currency=currency)
    return capital_df

if __name__ == '__main__':
    create_year_report(year='2022', currency='RUB')