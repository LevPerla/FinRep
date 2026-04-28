from src.data.validation import validate_all_data
from src.data.get_finance import update_fx_cache_interactive
from src.reports.main_report import create_main_report
from src.reports.month_report import create_month_report
from src.reports.year_report import create_year_report

CURRENCY = 'RUB'
YEAR = '2026'
MONTH = '04'
FX_NETWORK_ENABLED = True
VALIDATE_DATA = True

if VALIDATE_DATA:
    validate_all_data()

# Run manually only when you want to compare providers and curate data/rates/fx_rates.csv.
# update_fx_cache_interactive(['RUB', 'KZT', 'EUR', 'GBP'], '2026-01-01', '2026-04-28')
 
create_main_report(currency=CURRENCY, fx_network_enabled=FX_NETWORK_ENABLED)
create_year_report(year=YEAR, currency=CURRENCY, fx_network_enabled=FX_NETWORK_ENABLED)
create_month_report(year=YEAR, currency=CURRENCY, month=MONTH, fx_network_enabled=FX_NETWORK_ENABLED)


# create_year_report(year='2025', currency=CURRENCY)
# create_year_report(year='2024', currency=CURRENCY)
# create_year_report(year='2023', currency=CURRENCY)
