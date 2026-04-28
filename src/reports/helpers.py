import plotly.graph_objects as go

from src.data.exchange_rates_info import get_exchange_rates_info


def add_table(fig, data, row, col, header_color='paleturquoise', cell_color='lavender',
              font=None, **table_kwargs):
    fig.add_trace(
        go.Table(
            header=dict(values=list(data.columns),
                        fill_color=header_color,
                        align='left'),
            cells=dict(values=[data[column] for column in data.columns],
                       fill_color=cell_color,
                       align='left',
                       font=font),
            **table_kwargs,
        ),
        row=row,
        col=col,
    )


def add_message_table(fig, title, message, row, col, header_color='lightblue', cell_color='lightcyan'):
    fig.add_trace(
        go.Table(
            header=dict(values=[title],
                        fill_color=header_color,
                        align='left'),
            cells=dict(values=[[message]],
                       fill_color=cell_color,
                       align='left'),
        ),
        row=row,
        col=col,
    )


def add_exchange_rates_table(fig, currency, row, col):
    try:
        exchange_rates_df = get_exchange_rates_info(currency)
        if exchange_rates_df.empty:
            add_message_table(fig, 'Информация о курсах валют', 'Нет данных о курсах валют', row, col)
            return
        add_table(
            fig,
            exchange_rates_df,
            row=row,
            col=col,
            header_color='lightblue',
            cell_color='lightcyan',
        )
    except Exception as e:
        print(f"Error adding exchange rates info: {e}")
        add_message_table(fig, 'Информация о курсах валют', 'Ошибка загрузки курсов валют', row, col)
