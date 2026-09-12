import pandas as pd

from src.dashboard import main_data, planning_data


def _balance(asset_capital=1100.0):
    return pd.DataFrame(
        {
            "Доход": [200.0, 100.0],
            "Расход": [100.0, 200.0],
            "Дельта": [100.0, -100.0],
            "Капитал": [900.0, 1000.0],
            "Капитал по активам": [1050.0, asset_capital],
            "Расхождение с активами": [150.0, asset_capital - 1000.0],
            "Валютная переоценка": [0.0, 0.0],
        },
        index=pd.to_datetime(["2026-01-31", "2026-02-28"]),
    )


def test_main_runway_explicitly_uses_asset_snapshot():
    metrics = main_data._cockpit_metrics(_balance(), "RUB", "2026", "02").set_index("Показатель")

    assert metrics.loc["Капитал по активам", "Значение"] == 1100.0
    assert metrics.loc["Капитал по активам", "Детали"] == "Последний доступный snapshot активов"
    assert metrics.loc["Runway по активам", "Значение"] == 1100.0 / 150.0
    assert metrics.loc["Runway по активам", "Детали"] == (
        "Капитал по активам / средний расход за последние 12 месяцев"
    )


def test_main_runway_names_cash_flow_fallback():
    balance = _balance(asset_capital=float("nan"))
    balance["Капитал по активам"] = float("nan")

    metrics = main_data._cockpit_metrics(balance, "RUB", "2026", "02").set_index("Показатель")

    assert metrics.loc["Капитал по cash-flow", "Значение"] == 1000.0
    assert metrics.loc["Runway по cash-flow", "Значение"] == 1000.0 / 150.0


def test_planning_runway_explicitly_uses_cash_flow_capital():
    runway = planning_data._runway(_balance()).iloc[0]

    assert runway["Капитал по cash-flow"] == 1000.0
    assert runway["Средний расход"] == 150.0
    assert runway["Runway, мес."] == 1000.0 / 150.0


def test_savings_rate_display_clamp_remains_intentional():
    metrics = main_data._cockpit_metrics(_balance(), "RUB", "2026", "02").set_index("Показатель")

    assert metrics.loc["Норма сбережений", "Значение"] == 0.0
