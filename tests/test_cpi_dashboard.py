import os
from decimal import Decimal

import pandas as pd

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src import config
from src.dashboard.main_data import _real_asset_capital_data
from src.data.sqlite_store import initialize_database, save_cpi_observations


def _seed_cpi(database, observations):
    save_cpi_observations(
        database,
        currency="RUB",
        observations=observations,
        source_version="test-release",
        payload_sha256="a" * 64,
        fetched_at="2026-04-01T00:00:00Z",
        published_on="2026-04-01",
    )


def test_real_asset_capital_uses_selected_base_month_and_leaves_gaps(
        tmp_path, monkeypatch):
    database = tmp_path / "finrep.sqlite3"
    initialize_database(database)
    _seed_cpi(database, [
        {"period": "2026-01", "index_value": "100"},
        {"period": "2026-03", "index_value": "120"},
    ])
    monkeypatch.setattr(config, "active_database_path", lambda: database)
    monkeypatch.setattr(config, "use_sqlite_storage", lambda: True)
    balance = pd.DataFrame(
        {"Капитал по активам": [Decimal("5000000"), Decimal("5200000"), Decimal("5500000")]},
        index=pd.to_datetime(["2026-01-31", "2026-02-28", "2026-03-31"]),
    )

    result = _real_asset_capital_data(balance, "RUB", "2026-03")

    assert result.attrs["base_period"] == "2026-03"
    assert result.attrs["status"] == "partial"
    assert result.attrs["missing_periods"] == ["2026-02"]
    assert result.loc[0, "Реальная стоимость"] == 6000000
    assert pd.isna(result.loc[1, "Реальная стоимость"])
    assert result.loc[2, "Реальная стоимость"] == 5500000
