import os

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import plotly.io as pio
import io

from src import config, utils
from src.model.create_tables import get_balance_by_month

def create_main_report(currency: str,
                       return_image: bool = False,
                       return_fig: bool = False) -> None:
    """
    function to create month report of all years

    :param currency: str of currency ticker
    :param return_image: switcher to save as image instead of html
    """
    assert currency in config.UNIQUE_TICKERS.keys(), f'currency должно быть из {config.UNIQUE_TICKERS.keys()}'

    fig = make_subplots(
        rows=4, cols=2,
        shared_xaxes=True,
        vertical_spacing=0.1,
        specs=[[{"type": "table", "colspan": 2}, None],
               [{"type": "scatter", "colspan": 2}, None],
               [{"type": "scatter", "colspan": 2}, None],
               [{"type": "bar", "colspan": 2}, None],
               ],
        subplot_titles=('Статистика по годам', 'Динамика доходов и расходов', 'Дельты', 'Динамика капитала'),
        row_heights=[0.3, 0.4, 0.3, 0.3],
        # column_widths=[0.23, 0.52, 0.25]
    )
    balance_df = get_balance_by_month(currency)

    # Add table with sum income and cost stats by year
    all_stats_df = _create_sum_stats(balance_df, currency)
    fig.add_trace(
        go.Table(
            header=dict(values=list(all_stats_df.columns),
                        fill_color='paleturquoise',
                        align='left'),
            cells=dict(values=[all_stats_df[colname] for colname in all_stats_df.columns],
                       fill_color='lavender',
                       align='left'),
        ),
        row=1, col=1
    )

    # Add plots of cost and income changing by years
    income_df = balance_df['Доход'].reset_index()
    fig.add_trace(go.Scatter(x=income_df['Дата'],
                             y=income_df['Доход'],
                             mode='lines+markers',
                             name='Доход',
                             line=dict(color='royalblue', width=2),
                             ),
                  row=2, col=1
                  )
    cost_df = balance_df['Расход'].reset_index()
    fig.add_trace(go.Scatter(x=cost_df['Дата'],
                             y=cost_df['Расход'],
                             mode='lines+markers',
                             name='Расход',
                             line=dict(color='firebrick', width=2),
                             ),
                  row=2, col=1
                  )
    
        # Add plot of deltas changing
    delta_df = balance_df['Дельта'].reset_index()
    fig.add_trace(go.Bar(x=delta_df['Дата'],
                             y=delta_df['Дельта'],
                            #  mode='lines+markers',
                             name='Дельта',
                             texttemplate='%{text:.2s}',
                             textposition='outside',
                             text=delta_df['Дельта']
                            #  line=dict(color='gray', width=2),
                             ),
                  row=3, col=1
                  )

    # Add plot of capital changing
    capital_df = balance_df['Капитал'].reset_index()
    fig.add_trace(go.Scatter(x=capital_df['Дата'],
                             y=capital_df['Капитал'],
                             mode='lines+markers',
                             name='Капитал',
                             line=dict(color='green', width=2),
                             ),
                  row=4, col=1
                  )


    fig.update_layout(
        height=1700,
        showlegend=True,
        legend=dict(
            orientation="v",
            yanchor="bottom",
            y=0.55,
            xanchor="right",
            x=1.1),
        legend_tracegroupgap=180,
        title_text=f"Основной отчет в валюте {currency}",
    )

    if return_fig:
        return fig

    if return_image:
        # Create a BytesIO object to hold the bytes
        img_byte_arr = io.BytesIO()
        # Save the image to the BytesIO object
        pio.write_image(fig, img_byte_arr, format='png', scale=1, width=1200, height=1200)
        # Reset the file pointer to the beginning of the BytesIO object
        img_byte_arr.seek(0)
        return img_byte_arr
    else:
        fig.write_html(os.path.join(config.REPORTS_PATH, f"Основной отчет в валюте {currency}.html"))
        fig.show()


def _create_sum_stats(balance_df: pd.DataFrame, currency: str) -> pd.DataFrame:
    """
    function that create table with sum income and costs stats by years
    :param balance_df: df with transactions
    :param currency: str of currency ticker
    """
    all_stats_df = balance_df[['Доход', 'Расход']].resample('Y').sum()
    all_stats_df.index = all_stats_df.index.strftime("%Y")
    all_stats_df['Сальдо'] = (all_stats_df['Доход'] - all_stats_df['Расход'])
    all_stats_df.loc['Всего'] = all_stats_df.sum(axis=0)
    all_stats_df['Процент дохода'] = (all_stats_df['Доход'] / all_stats_df['Расход'] * 100).round(2).astype(str) + '%'
    all_stats_df = utils.process_num_cols(all_stats_df, not_num_cols=['Процент дохода'], currency=currency)
    all_stats_df = all_stats_df.reset_index()
    return all_stats_df


if __name__ == '__main__':
    create_main_report(currency='RUB')
