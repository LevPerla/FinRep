from __future__ import annotations

import pandas as pd
import pytest

from src.dashboard import main_data
from src.data import crypto, get_finance, investment_calculations, proccess
from src.model import create_tables


def _cache(*rows) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": date,
                "currency": currency,
                "usd_rate": rate,
                "source": source,
                "fetched_at": f"{date}T12:00:00",
            }
            for date, currency, rate, source in rows
        ]
    ).assign(date=lambda data: pd.to_datetime(data["date"]))


def test_future_rate_does_not_fill_earlier_day(monkeypatch):
    rates = _cache(("2026-02-01", "RUB", 0.013, "CBR"))
    monkeypatch.setattr(get_finance, "_read_cache", lambda: rates)

    result = get_finance._series_from_cache(
        "RUB", pd.Timestamp("2026-01-31"), pd.Timestamp("2026-02-01")
    )

    assert pd.isna(result.loc["2026-01-31"])
    assert result.loc["2026-02-01"] == pytest.approx(0.013)


@pytest.mark.parametrize(
    ("rate_date", "expected"),
    [("2026-01-03", 0.013), ("2026-01-02", None)],
)
def test_previous_rate_has_seven_day_limit(monkeypatch, rate_date, expected):
    rates = _cache((rate_date, "RUB", 0.013, "CBR"))
    monkeypatch.setattr(get_finance, "_read_cache", lambda: rates)

    result = get_finance._series_from_cache(
        "RUB", pd.Timestamp("2026-01-10"), pd.Timestamp("2026-01-10")
    ).iloc[0]

    if expected is None:
        assert pd.isna(result)
    else:
        assert result == pytest.approx(expected)


def test_cross_rate_uses_each_previous_leg_and_exposes_metadata(monkeypatch):
    rates = _cache(
        ("2026-01-03", "EUR", 1.2, "ECB"),
        ("2026-01-09", "RUB", 0.012, "CBR"),
    )
    monkeypatch.setattr(get_finance, "_read_cache", lambda: rates)
    monkeypatch.setattr(get_finance, "_ensure_cache_file", lambda: None)
    monkeypatch.setattr(get_finance, "_ensure_currency_cached", lambda *_: None)

    info = get_finance.get_fx_rate_info("EUR", "RUB", "2026-01-10")

    assert info["rate"] == pytest.approx(100.0)
    assert [(leg["currency"], leg["rate_date"].date().isoformat(), leg["source"]) for leg in info["legs"]] == [
        ("EUR", "2026-01-03", "ECB"),
        ("RUB", "2026-01-09", "CBR"),
    ]


def test_stale_rate_metadata_keeps_actual_date_and_source(monkeypatch):
    rates = _cache(("2026-01-02", "RUB", 0.012, "CBR"))
    monkeypatch.setattr(get_finance, "_read_cache", lambda: rates)
    monkeypatch.setattr(get_finance, "_ensure_cache_file", lambda: None)
    monkeypatch.setattr(get_finance, "_ensure_currency_cached", lambda *_: None)

    info = get_finance.get_fx_rate_info("USD", "RUB", "2026-01-10")

    assert info["rate"] is None
    assert info["legs"][1]["rate_date"].date().isoformat() == "2026-01-02"
    assert info["legs"][1]["source"] == "stale:CBR"
    assert "CBR 2026-01-02" in info["source"]


def test_historical_conversion_does_not_use_latest_cache_fallback(monkeypatch):
    transactions = pd.DataFrame(
        [{"Дата": pd.Timestamp("2026-01-01"), "Валюта": "USD", "Значение": 100.0}]
    )
    monkeypatch.setattr(
        proccess,
        "get_rates",
        lambda **_: pd.DataFrame(
            {"USDRUB=X": [float("nan")]}, index=pd.DatetimeIndex(["2026-01-01"], name="Дата")
        ),
    )

    with pytest.raises(ValueError, match="Нет курса USD → RUB.*2026-01-01"):
        proccess.convert_transaction(transactions, "RUB", "Значение")


def test_current_transaction_conversion_uses_current_rate(monkeypatch):
    transactions = pd.DataFrame(
        [{"Дата": pd.Timestamp("2025-01-01"), "Валюта": "USD", "Значение": 100.0}]
    )
    monkeypatch.setattr(proccess, "get_actual_fx_rate", lambda *_: 90.0)
    monkeypatch.setattr(
        proccess,
        "get_rates",
        lambda **_: pytest.fail("historical rate lookup must not run for current conversion"),
    )

    converted = proccess.convert_transaction(
        transactions,
        "RUB",
        "Значение",
        use_current_rate=True,
    )

    assert converted.loc[0, "Значение"] == 9000.0


@pytest.mark.parametrize("target_currency", ["USD", "EUR", "KZT"])
def test_future_transaction_uses_latest_available_date_without_changing_transaction_date(
    target_currency,
    monkeypatch,
):
    transactions = pd.DataFrame(
        [{"Дата": pd.Timestamp("2026-09-20"), "Валюта": "RUB", "Значение": 9000.0}]
    )
    requested_ranges = []
    monkeypatch.setattr(
        proccess,
        "get_current_fx_date",
        lambda: pd.Timestamp("2026-09-13"),
    )

    def rates(*, tickers, min_date, max_date):
        requested_ranges.append((tickers, pd.Timestamp(min_date), pd.Timestamp(max_date)))
        return pd.DataFrame(
            {f"RUB{target_currency}=X": [1 / 90]},
            index=pd.DatetimeIndex(["2026-09-13"], name="Дата"),
        )

    monkeypatch.setattr(proccess, "get_rates", rates)

    converted = proccess.convert_transaction(transactions, target_currency, "Значение")

    assert requested_ranges == [
        ([f"RUB{target_currency}=X"], pd.Timestamp("2026-09-13"), pd.Timestamp("2026-09-13"))
    ]
    assert converted.loc[0, "Дата"] == pd.Timestamp("2026-09-20")
    assert converted.loc[0, "Значение"] == 100.0


@pytest.mark.parametrize("module", [investment_calculations, crypto])
def test_current_portfolio_does_not_use_stale_fallback(module, monkeypatch):
    monkeypatch.setattr(module, "get_actual_fx_rate", lambda *_: None)

    with pytest.raises(ValueError, match="Нет курса USD → RUB"):
        module._conversion_rate("USD", "RUB")


@pytest.mark.parametrize("module", [investment_calculations, crypto])
def test_portfolio_price_uses_rate_as_of_price_date(module, monkeypatch):
    monkeypatch.setattr(module, "get_actual_fx_rate", lambda *_: pytest.fail("current rate must not be used"))
    monkeypatch.setattr(module, "get_fx_rate_as_of", lambda *args: 91.0 if args[2] == "2026-05-31" else None)

    assert module._conversion_rate("USD", "RUB", "2026-05-31") == 91.0


def test_static_sample_uses_its_latest_cache_date(tmp_path, monkeypatch):
    rates = _cache(
        ("2026-05-30", "RUB", 0.011, "sample"),
        ("2026-05-31", "KZT", 0.0021, "sample"),
    )
    monkeypatch.setattr(get_finance.config, "SAMPLE_DATA_PATH", str(tmp_path))
    monkeypatch.setattr(get_finance.config, "active_data_path", lambda *_: tmp_path)
    monkeypatch.setattr(get_finance, "_read_cache", lambda: rates)

    assert get_finance._current_fx_date() == pd.Timestamp("2026-05-31")


def test_rate_info_uses_current_valuation_date_by_default(monkeypatch):
    monkeypatch.setattr(
        get_finance,
        "_current_fx_date",
        lambda: pd.Timestamp("2026-05-31"),
    )

    info = get_finance.get_fx_rate_info("USD", "USD")

    assert info["rate"] == 1.0
    assert info["rate_date"] == pd.Timestamp("2026-05-31")


@pytest.mark.parametrize(
    ("module", "function_name"),
    [(create_tables, "_get_fx_rate_as_of"), (main_data, "_fx_rate_as_of")],
)
def test_as_of_helpers_do_not_use_latest_fallback(module, function_name, monkeypatch):
    monkeypatch.setattr(module, "get_fx_rates", lambda *_: pd.DataFrame())

    assert getattr(module, function_name)("USD", "RUB", "2026-01-01") is None
