from src.data.get import get_transactions, get_assets, get_investments
from src.data.get_finance import get_act_moex, get_actual_rates
from src.model.create_tables import create_invest_tbl, get_capital_by_month, get_act_receivables, get_act_liabilities
from src import config, utils
import numpy as np
import pandas as pd

pd.set_option('display.max_rows', 1000)
pd.set_option('display.max_columns', 100)

from src.reports.year_report import create_year_report
from src.reports.month_report import create_month_report
from src.reports.main_report import create_main_report

transactions_df = get_transactions()
assets_df = get_assets()

create_main_report(transactions_df, currency='RUB')
create_year_report(transactions_df, year='2022', currency='RUB')
create_month_report(transactions_df, assets_df, year='2022', month='11', currency='RUB')
