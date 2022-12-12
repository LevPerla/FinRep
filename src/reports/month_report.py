import os

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from src import config, utils
from src.model.create_tables import get_balance_by_month, get_act_receivables, get_month_transactions,\
                                    get_act_liabilities, get_cost_distribution, get_assets_by_currencies


def create_month_report(year: str,
                        currency: str,
                        month: str,
                        return_image: bool = False) -> None:
    """
    function to create month report
    :param transactions_df: df with transactions
    :param assets_df: df with assets
    :param year: str of year in yyyy
    :param currency: str of currency ticker
    :param month: str of month in mm
    :param return_image: switcher to save as image instead of html
    """
    assert currency in config.UNIQUE_TICKERS.keys(), f'currency need to be from {config.UNIQUE_TICKERS.keys()}'

    # Get capital by chosen year and month
    capital_df = get_balance_by_month(currency).loc[f'{year}-{month}']

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
        row_heights=[0.75, 0.1, 0.1, 0.15, 0.25, 0.5],
        column_widths=[0.25, 0.25, 0.25, 0.25]
    )

    # Add month transactions table
    month_tr_df = get_month_transactions(currency, year, month)
    fig.add_trace(
        go.Table(
            header=dict(values=list(month_tr_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[month_tr_df[colname] for colname in month_tr_df.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=1, col=1
    )

    # Add table with month sum stats
    capital_df_ = utils.process_num_cols(capital_df, not_num_cols=[], currency=currency)
    fig.add_trace(
        go.Table(
            header=dict(values=list(capital_df_.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[capital_df_[colname] for colname in capital_df_.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=2, col=1
    )

    # Add actual receivables table
    receivables_df = get_act_receivables()
    receivables_df = utils.fill_if_empty(receivables_df)
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

    # Add actual liabilities table
    liabilities_df = get_act_liabilities()
    liabilities_df = utils.fill_if_empty(liabilities_df)
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

    # Add costs distribution table
    cost_stats_df = get_cost_distribution(currency=currency, year=year, month=month)
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

    # Add cost distribution bar plot
    cost_plot_df = _create_cost_plot_table(cost_stats_df)
    fig.add_trace(go.Bar(name='test', x=cost_plot_df.Показатель, y=cost_plot_df.Суммарно),
                  row=5, col=1
                  )

    # Add table with distribution of assets in different currencies
    assets_by_currencies = get_assets_by_currencies(year, month)
    assets_by_currencies = utils.fill_if_empty(assets_by_currencies)
    fig.add_trace(
        go.Table(
            header=dict(values=list(assets_by_currencies.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[assets_by_currencies[colname] for colname in assets_by_currencies.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=6, col=1
    )

    fig.update_layout(
        height=2200,
        showlegend=False,
        title_text=f"Отчет за {month} месяц {year} года, в валюте {currency}",
    )

    # Create folder and save the report
    month_folder_name = os.path.join(config.REPORTS_PATH, 'Месячные отчеты')
    if 'Месячные отчеты' not in os.listdir(config.REPORTS_PATH):
        os.makedirs(month_folder_name)

    cur_folder_dir = os.path.join(month_folder_name, currency)
    if currency not in os.listdir(month_folder_name):
        os.makedirs(cur_folder_dir)

    year_folder_dir = os.path.join(cur_folder_dir, year)
    if year not in os.listdir(cur_folder_dir):
        os.makedirs(year_folder_dir)

    if return_image:
        fig.write_image(config.IMAGE_TO_BOT_PATH, scale=1, width=1500, height=2500)
    else:
        fig.write_html(os.path.join(year_folder_dir, f"Отчет за {month} {year} года.html"))
        fig.show()


def _get_month_transactions(year: str, month: str) -> pd.DataFrame:
    """
    get transaction table of month by days

    :param year: str of year in yyyy
    :param month: str of month in mm
    """
    month_df = pd.read_csv(os.path.join(config.TRANSACTIONS_INFO_PATH,
                                        year,
                                        f'{year}_{month if len(str(month)) == 2 else "0" + str(month)}.csv'),
                           sep=';',
                           decimal=',',
                           parse_dates=True,
                           dayfirst=True,
                           infer_datetime_format=True)
    month_df = month_df.rename(columns={'Долги (у меня)': 'Дебиторская задолженность',
                                        'Крупные покупки/ Поездки': 'Поездки'},
                               errors='ignore')
    return month_df

def _create_cost_plot_table(cost_stats_df):
    cost_plot_df = cost_stats_df.T.reset_index()
    cost_plot_df.columns = cost_plot_df.loc[0]
    cost_plot_df = cost_plot_df.loc[1:]
    cost_plot_df['Суммарно'] = cost_plot_df['Суммарно'].apply(lambda x: float(x[:-1].replace(' ', '')))
    return cost_plot_df


if __name__ == '__main__':
    create_month_report(year='2022', currency='RUB', month='11', return_image=False)
