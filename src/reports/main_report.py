from plotly.subplots import make_subplots
import plotly.graph_objects as go
import numpy as np
import os

from src.model.create_tables import get_capital_by_month
from src.data.proccess import convert_transaction
from src import config


def create_main_report(transactions_df, currency, return_pdf=False):
    assert currency in config.UNIQUE_TICKERS.keys(), f'currency должно быть из {config.UNIQUE_TICKERS.keys()}'

    # Приводим валюты
    if not config.DEBUG:
        smpl_tr_df = convert_transaction(df_to_convert=transactions_df, to_curr=currency)
    else:
        smpl_tr_df = transactions_df

    fig = make_subplots(
        rows=3, cols=2,
        shared_xaxes=True,
        vertical_spacing=0.1,
        specs=[[{"type": "table", "colspan": 2}, None],
               [{"type": "scatter", "colspan": 2}, None],
               [{"type": "scatter", "colspan": 2}, None],
               ],
        subplot_titles=('Статистика по годам', 'Динамика доходов и расходов', 'Динамика капитала'),
        row_heights=[0.3, 0.4, 0.3],
        # column_widths=[0.23, 0.52, 0.25]
    )

    # Добавляем таблицу суммарных показателей
    all_stats_df = (smpl_tr_df[smpl_tr_df.Категория.isin(['Доход'])]
                    .pivot_table(values='Значение', index=['Год'], columns=['Категория'], aggfunc=np.sum)
                    )

    all_stats_df['Расход'] = (
        smpl_tr_df[~smpl_tr_df.Категория.isin(config.NOT_COST_COLS)].groupby('Год')['Значение'].sum())
    all_stats_df['Сальдо'] = (all_stats_df['Доход'] - all_stats_df['Расход'])
    all_stats_df.loc['Всего'] = all_stats_df.sum(axis=0)
    all_stats_df['Процент дохода'] = (all_stats_df['Доход'] / all_stats_df['Расход'] * 100).round(2).astype(str) + '%'

    for col_name in all_stats_df:
        if col_name not in ['Процент дохода']:
            all_stats_df.loc[:, col_name] = (all_stats_df[col_name].astype(float)
                                             .map('{:,.2f}'.format).str.replace(',', ' ') +
                                             config.UNIQUE_TICKERS[currency])
    all_stats_df = all_stats_df.reset_index()

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
    mean_stats_df = get_capital_by_month(currency)
    income_df = mean_stats_df['Доход'].reset_index()
    fig.add_trace(go.Scatter(x=income_df['Дата'],
                             y=income_df['Доход'],
                             mode='lines+markers',
                             name='Доход',
                             line=dict(color='royalblue', width=2),
                             ),
                  row=2, col=1
                  )
    cost_df = mean_stats_df['Расход'].reset_index()
    fig.add_trace(go.Scatter(x=cost_df['Дата'],
                             y=cost_df['Расход'],
                             mode='lines+markers',
                             name='Расход',
                             line=dict(color='firebrick', width=2),
                             ),
                  row=2, col=1
                  )

    # График изменения капитал
    capital_df = mean_stats_df['Капитал'].reset_index()
    fig.add_trace(go.Scatter(x=capital_df['Дата'],
                             y=capital_df['Капитал'],
                             mode='lines+markers',
                             name='Капитал',
                             line=dict(color='green', width=2),
                             ),
                  row=3, col=1
                  )

    fig.update_layout(
        height=1000,
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
    if return_pdf:
        fig.write_image(config.IMAGE_TO_BOT_PATH, scale=1, width=1200, height=1000)
    else:
        fig.write_html(os.path.join(config.REPORTS_PATH, f"Основной отчет в валюте {currency}.html"))
        fig.show()
