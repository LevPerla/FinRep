# from src.telegram_bot.bot import bot
#
# bot.polling(none_stop=True, interval=0)

from src.reports.main_report import create_main_report
from src.reports.month_report import create_month_report
from src.reports.year_report import create_year_report

CURRENCY = 'RUB'
YEAR = '2023'
MONTH = '06'

create_main_report(currency=CURRENCY)
create_year_report(year=YEAR, currency=CURRENCY)
create_month_report(year=YEAR, currency=CURRENCY, month=MONTH)
