from __future__ import annotations


def clear_valuation_caches() -> None:
    from src.dashboard.main_data import clear_main_dashboard_cache
    from src.model.create_tables import clear_table_cache

    clear_table_cache()
    clear_main_dashboard_cache()
