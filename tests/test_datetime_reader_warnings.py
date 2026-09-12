import warnings

import pandas as pd

from src import config
from src.data import get
from src.reports import month_report


def _write_ambiguous_month(root):
    year_dir = root / "2026"
    year_dir.mkdir()
    (year_dir / "2026_04.csv").write_text(
        "Дата;Прочее\n03.04.2026;10|RUB|ambiguous date\n",
        encoding="utf-8",
    )


def _call_without_infer_datetime_warning(function):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = function()

    matching = [
        warning
        for warning in caught
        if "infer_datetime_format" in str(warning.message)
    ]
    assert matching == []
    return result


def test_transaction_reader_keeps_day_first_parsing_without_deprecation(tmp_path):
    _write_ambiguous_month(tmp_path)
    get._get_transactions_cached.cache_clear()

    transactions = _call_without_infer_datetime_warning(
        lambda: get._get_transactions_cached(str(tmp_path))
    )

    assert transactions.loc[0, "Дата"] == pd.Timestamp("2026-04-03")


def test_legacy_month_reader_stops_emitting_datetime_deprecation(tmp_path, monkeypatch):
    _write_ambiguous_month(tmp_path)
    monkeypatch.setattr(config, "TRANSACTIONS_INFO_PATH", str(tmp_path))

    month = _call_without_infer_datetime_warning(
        lambda: month_report._get_month_transactions("2026", "04")
    )

    assert month.loc[0, "Прочее"] == "10|RUB|ambiguous date"
