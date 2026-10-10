import pandas as pd
import pytest

from src.data import get_finance


def test_nbk_rates_are_converted_to_usd_and_shared_between_currencies(monkeypatch):
    calls = []

    def get(_url, *, params, timeout):
        calls.append((params["fdate"], timeout))

        class Response:
            content = (f"<rates><date>{params['fdate']}</date>"
                       "<item><title>USD</title><description>500</description><quant>1</quant></item>"
                       "<item><title>EUR</title><description>550</description><quant>1</quant></item>"
                       "<item><title>RUB</title><description>600</description><quant>100</quant></item>"
                       "</rates>").encode()

            def raise_for_status(self):
                pass

        return Response()

    monkeypatch.setattr(get_finance.requests, "get", get)
    monkeypatch.setattr(get_finance, "_NBK_DAILY_CACHE", {})
    day = pd.Timestamp("2025-04-18")

    with get_finance.fx_network_mode(True):
        eur = get_finance._fetch_nbk_usd_rate("EUR", day, day)
        rub = get_finance._fetch_nbk_usd_rate("RUB", day, day)
        kzt = get_finance._fetch_nbk_usd_rate("KZT", day, day)

    assert eur.iloc[0] == pytest.approx(1.1)
    assert rub.iloc[0] == pytest.approx(0.012)
    assert kzt.iloc[0] == pytest.approx(0.002)
    assert calls == [("18.04.2025", (3, 5))]


def test_nbk_rejects_rate_for_wrong_date(monkeypatch):
    class Response:
        content = b"<rates><date>17.04.2025</date></rates>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(get_finance.requests, "get", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(get_finance, "_NBK_DAILY_CACHE", {})

    with pytest.raises(ValueError, match="different rate date"):
        get_finance._nbk_daily_rates(pd.Timestamp("2025-04-18"))


def test_second_provider_fills_only_first_provider_gaps(monkeypatch):
    first = pd.Timestamp("2025-04-18")
    second = pd.Timestamp("2025-04-21")
    saved = {}
    monkeypatch.setattr(get_finance.config, "FX_PROVIDER_ORDER", ["yfinance", "nbk"])
    monkeypatch.setattr(get_finance, "_missing_dates",
                        lambda *_args: [day for day in (first, second) if day not in saved])
    monkeypatch.setattr(get_finance, "_fetchable_missing_dates",
                        lambda _currency, days: days)
    monkeypatch.setattr(get_finance, "_PROVIDERS", {
        "yfinance": lambda *_args: pd.Series({first: 0.01}),
        "nbk": lambda *_args: pd.Series({first: 0.02, second: 0.03}),
    })
    monkeypatch.setattr(get_finance, "_append_cache_rows",
                        lambda _currency, rates, source: saved.update({
                            day: (rate, source) for day, rate in rates.items()}))

    with get_finance.fx_network_mode(True):
        get_finance._ensure_currency_cached("RUB", first, second)

    assert saved == {first: (0.01, "yfinance"), second: (0.03, "nbk")}
