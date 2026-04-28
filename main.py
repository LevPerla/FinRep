# from src.telegram_bot.bot import bot

# bot.polling(none_stop=True, interval=0)

from src.reports.main_report import create_main_report
from src.reports.month_report import create_month_report
from src.reports.year_report import create_year_report

CURRENCY = 'RUB'
YEAR = '2026'
MONTH = '04'
FX_NETWORK_ENABLED = True
 
create_main_report(currency=CURRENCY, fx_network_enabled=FX_NETWORK_ENABLED)
create_year_report(year=YEAR, currency=CURRENCY, fx_network_enabled=FX_NETWORK_ENABLED)
create_month_report(year=YEAR, currency=CURRENCY, month=MONTH, fx_network_enabled=FX_NETWORK_ENABLED)


# create_year_report(year='2025', currency=CURRENCY)
# create_year_report(year='2024', currency=CURRENCY)
# create_year_report(year='2023', currency=CURRENCY)

