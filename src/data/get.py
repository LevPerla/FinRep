import pandas as pd
import numpy as np
import os
from functools import lru_cache

from src import config
from src.data.staging import TRANSACTION_BOUNDARY_RE, sanitize_transaction_comment


TRANSACTION_COLUMNS = ['Дата', 'Категория', 'Валюта', 'Значение', 'Комментарий', 'Год', 'Квартал', 'Месяц']
ASSET_COLUMNS = ['Счет', 'Валюта', 'Значение', 'Год', 'Квартал', 'Месяц']


def _empty_frame(columns):
    data = {column: pd.Series(dtype='object') for column in columns}
    if 'Дата' in data:
        data['Дата'] = pd.Series(dtype='datetime64[ns]')
    if 'Значение' in data:
        data['Значение'] = pd.Series(dtype='float64')
    return pd.DataFrame(data)


def _split_transaction_values(value):
    return TRANSACTION_BOUNDARY_RE.split(str(value))


def _parse_transaction_value(value):
    normalized = (str(value)
                  .replace(',', '.')
                  .replace('\\xa0', '')
                  .replace('\xa0', '')
                  .replace(' ₽', ''))
    parts = normalized.split('|', 2)
    amount = parts[0]
    currency = parts[1] if len(parts) >= 2 else 'RUB'
    comment = sanitize_transaction_comment(parts[2]) if len(parts) == 3 else np.nan
    return {
        'Значение': float(amount),
        'Валюта': currency.upper(),
        'Комментарий': comment,
    }


def get_transactions():
    if config.use_sqlite_storage():
        return _get_transactions_sqlite_cached(str(config.active_database_path())).copy(deep=True)
    return _get_transactions_cached(str(config.active_data_path("transactions_info"))).copy(deep=True)


@lru_cache(maxsize=1)
def _get_transactions_sqlite_cached(database_path: str):
    from src.data.sqlite_store import connect_database

    with connect_database(database_path) as connection:
        rows = connection.execute("""SELECT v.occurred_on, v.category_name_ru,
            v.currency_code, v.amount_minor, v.comment, c.minor_unit,
            'cash' AS row_kind, '' AS event_kind, '' AS side
            FROM v_cash_transactions v JOIN currencies c ON c.code = v.currency_code
            UNION ALL
            SELECT e.occurred_on, '', e.currency_code, e.amount_minor, e.comment,
            c.minor_unit, 'debt', e.event_kind, e.side
            FROM debt_cash_events e JOIN currencies c ON c.code = e.currency_code
            UNION ALL
            SELECT e.occurred_on, 'Инвестиции', e.currency_code,
            CASE e.flow_kind WHEN 'withdrawal' THEN -e.amount_minor ELSE e.amount_minor END,
            e.comment, c.minor_unit, 'investment', e.flow_kind, ''
            FROM investment_cash_events e JOIN currencies c ON c.code = e.currency_code
            ORDER BY occurred_on""").fetchall()
    if not rows:
        return _empty_frame(TRANSACTION_COLUMNS)
    data = pd.DataFrame([dict(row) for row in rows])
    data["Дата"] = pd.to_datetime(data.pop("occurred_on"))
    debt_categories = {
        ("issue", "receivable"): "Дебиторская задолженность",
        ("repayment", "receivable"): "Погашение деб. зад.",
        ("issue", "liability"): "Кредиторская задолженность",
        ("repayment", "liability"): "Погашение кред. зад.",
    }
    data["Категория"] = data.apply(
        lambda row: debt_categories[(row["event_kind"], row["side"])]
        if row["row_kind"] == "debt" else row["category_name_ru"], axis=1)
    data["Валюта"] = data.pop("currency_code")
    data["Значение"] = data.apply(
        lambda row: float(row["amount_minor"] / (10 ** row["minor_unit"])), axis=1)
    data["Комментарий"] = data.pop("comment").replace("", np.nan)
    data["Год"] = data["Дата"].dt.year.astype(str)
    data["Квартал"] = data["Дата"].dt.quarter.astype(str)
    data["Месяц"] = data["Дата"].dt.month.astype(str)
    return data[TRANSACTION_COLUMNS]


@lru_cache(maxsize=1)
def _get_transactions_cached(transactions_root: str):
    if not os.path.isdir(transactions_root):
        return _empty_frame(TRANSACTION_COLUMNS)
    transactions_df = pd.DataFrame()
    for folder_name in os.listdir(transactions_root):
        if folder_name == '.DS_Store':
            continue
        for file_name in os.listdir(os.path.join(transactions_root, folder_name)):
            # print(file_name)
            if file_name == '.DS_Store' or '.backup_' in file_name:
                continue
            month_df = pd.read_csv(os.path.join(transactions_root, folder_name, file_name), sep=';',
                                   decimal=',',
                                   parse_dates=True,
                                   dayfirst=True,
                                   index_col='Дата')
            month_df = month_df.rename(columns={'Долги (у меня)': 'Дебиторская задолженность',
                                                'Крупные покупки/ Поездки': 'Поездки'},
                                       errors='ignore')
            month_df = month_df.reset_index().melt(id_vars='Дата', var_name='Категория')

            month_df['value'] = month_df['value'].apply(_split_transaction_values)
            month_df = month_df.explode('value')
            month_df['value'] = month_df['value'].apply(_parse_transaction_value)

            month_df['Валюта'] = month_df['value'].apply(lambda x: x['Валюта'])
            month_df['Значение'] = month_df['value'].apply(lambda x: x['Значение'])
            month_df['Комментарий'] = month_df['value'].apply(lambda x: x['Комментарий'])
            month_df.drop('value', axis=1, inplace=True)

            month_df['Год'] = month_df['Дата'].apply(lambda x: x.year).astype(str)
            month_df['Квартал'] = month_df['Дата'].apply(lambda x: x.quarter).astype(str)
            month_df['Месяц'] = month_df['Дата'].apply(lambda x: x.month).astype(str)

            assert len(
                set(month_df['Валюта'].unique()) - config.UNIQUE_TICKERS.keys()) == 0, 'Есть недопустимые тикеры валют'
            transactions_df = pd.concat([transactions_df, month_df], axis=0)
    if transactions_df.empty:
        return _empty_frame(TRANSACTION_COLUMNS)
    return transactions_df.reset_index().drop('index', axis=1)


def get_assets():
    if config.use_sqlite_storage():
        return _get_assets_sqlite_cached(str(config.active_database_path())).copy(deep=True)
    return _get_assets_cached(str(config.active_data_path("assets_info"))).copy(deep=True)


@lru_cache(maxsize=1)
def _get_assets_sqlite_cached(database_path: str):
    from src.data.sqlite_store import connect_database

    with connect_database(database_path) as connection:
        rows = connection.execute("""SELECT v.period, v.account_name, v.currency_code,
            v.amount_minor, c.minor_unit FROM v_asset_snapshots v
            JOIN currencies c ON c.code = v.currency_code ORDER BY v.period, v.id""").fetchall()
    if not rows:
        return _empty_frame(ASSET_COLUMNS)
    data = pd.DataFrame([dict(row) for row in rows])
    periods = pd.PeriodIndex(data.pop("period"), freq="M")
    data["Счет"] = data.pop("account_name")
    data["Валюта"] = data.pop("currency_code")
    data["Значение"] = data.apply(
        lambda row: float(row["amount_minor"] / (10 ** row["minor_unit"])), axis=1)
    data["Год"] = periods.year.astype(str)
    data["Квартал"] = periods.quarter.astype(str)
    data["Месяц"] = periods.month.astype(str)
    return data[ASSET_COLUMNS]


@lru_cache(maxsize=1)
def _get_assets_cached(assets_root: str):
    if not os.path.isdir(assets_root):
        return _empty_frame(ASSET_COLUMNS)
    assets_df = pd.DataFrame()
    for folder_name in os.listdir(assets_root):
        if folder_name == '.DS_Store':
            continue
        for file_name in os.listdir(os.path.join(assets_root, folder_name)):
            # print(file_name)
            if file_name == '.DS_Store':
                continue
            month_df = pd.read_csv(os.path.join(assets_root, folder_name, file_name),
                                   sep=';', decimal=',', index_col='Счет')

            for col_name in month_df.columns:
                month_df[col_name] = (month_df[col_name].astype(str)
                                      .str.replace(',', '.')
                                      .str.replace('\\xa0', '')
                                      .str.replace('\xa0', '')
                                      .apply(lambda x: x.split('|') if len(x.split('|')) == 2 else [x, 'RUB'])
                                      .apply(lambda x: {'Значение': float(x[0]),
                                                        'Валюта': x[1].upper()})
                                      )
            month_df = month_df.reset_index()
            month_df['Дата'] = pd.Period(file_name.split('.')[0].replace('_', '-'))
            month_df['Валюта'] = month_df['Сумма'].apply(lambda x: x['Валюта'])
            month_df['Значение'] = month_df['Сумма'].apply(lambda x: x['Значение'])

            month_df['Год'] = month_df['Дата'].apply(lambda x: x.year).astype(str)
            month_df['Квартал'] = month_df['Дата'].apply(lambda x: x.quarter).astype(str)
            month_df['Месяц'] = month_df['Дата'].apply(lambda x: x.month).astype(str)
            month_df.drop(['Сумма', 'Дата'], axis=1, inplace=True)

            assert len(
                set(month_df['Валюта'].unique()) - config.UNIQUE_TICKERS.keys()) == 0, 'Есть недопустимые тикеры валют'

            assets_df = pd.concat([assets_df, month_df], axis=0)
    if assets_df.empty:
        return _empty_frame(ASSET_COLUMNS)
    return assets_df.reset_index().drop('index', axis=1)


def get_investments():
    if config.use_sqlite_storage():
        return _get_investments_sqlite_cached(str(config.active_database_path())).copy(deep=True)
    return _get_investments_cached(str(config.active_data_path("investments", "investments.csv"))).copy(deep=True)


@lru_cache(maxsize=1)
def _get_investments_sqlite_cached(database_path: str):
    from src.data.sqlite_store import connect_database

    with connect_database(database_path) as connection:
        rows = connection.execute("""SELECT t.occurred_on, t.operation, i.asset_type,
            i.ticker, t.quantity_text, t.unit_price_text, t.price_currency_code,
            t.fee_minor, c.minor_unit, t.account_label, t.comment
            FROM investment_trades t JOIN instruments i ON i.id = t.instrument_id
            JOIN currencies c ON c.code = t.price_currency_code
            ORDER BY t.occurred_on, t.id""").fetchall()
    if not rows:
        return pd.DataFrame(columns=[
            "Тип_транзакции", "Актив", "Тикер", "Количество", "Дата", "Цена", "Валюта"])
    data = pd.DataFrame([dict(row) for row in rows])
    result = pd.DataFrame({
        "Тип_транзакции": data["operation"].map({"buy": "Покупка", "sell": "Продажа"}),
        "Актив": data["asset_type"].map(
            {"stocks": "Акции", "funds": "Фонды", "crypto": "Крипто"}),
        "Тикер": data["ticker"],
        "Количество": pd.to_numeric(data["quantity_text"]),
        "Дата": pd.to_datetime(data["occurred_on"]),
        "Цена": pd.to_numeric(data["unit_price_text"]),
        "Валюта": data["price_currency_code"],
        "Комиссия": data.apply(
            lambda row: float(row["fee_minor"] / (10 ** row["minor_unit"])), axis=1),
        "Счет": data["account_label"],
        "Комментарий": data["comment"],
    })
    return result


@lru_cache(maxsize=1)
def _get_investments_cached(investments_path: str):
    data = pd.read_csv(investments_path, sep=';', decimal=',')
    data['Дата'] = data['Дата'].astype('datetime64[ns]')
    data['Валюта'] = data['Цена'].apply(lambda x: x.split('|')[1])
    data['Цена'] = data['Цена'].apply(lambda x: x.split('|')[0].replace(',', '.')).astype(float)
    return data


def clear_data_cache():
    _get_transactions_cached.cache_clear()
    _get_assets_cached.cache_clear()
    _get_investments_cached.cache_clear()
    _get_transactions_sqlite_cached.cache_clear()
    _get_assets_sqlite_cached.cache_clear()
    _get_investments_sqlite_cached.cache_clear()

if __name__ == '__main__':
    tmp_df = get_transactions()
    # tmp_df = get_assets()
    # tmp_df = get_investments()

    print(tmp_df.head(50))
