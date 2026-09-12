import warnings

import pandas as pd
from pandas.testing import assert_frame_equal

from src.dashboard import month_data, planning_data


def _without_dtype_warnings(function):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = function()

    matching = [
        warning
        for warning in caught
        if "incompatible dtype" in str(warning.message).lower()
    ]
    assert matching == []
    return result


def test_month_summary_formats_money_without_mutating_numeric_source():
    source = pd.DataFrame(
        [
            {
                "Показатель": "Доход",
                "Значение": 338_000.0,
                "Статус": "ok",
                "Детали": "synthetic",
                "Тип": "money",
            }
        ]
    )
    before = source.copy(deep=True)

    display = _without_dtype_warnings(
        lambda: month_data._format_summary_metrics(source, "RUB")
    )

    assert display.loc[0, "Значение"] == "338 000.00₽"
    assert "Тип" not in display.columns
    assert_frame_equal(source, before)


def test_planning_goals_format_money_and_percent_without_mutating_source():
    source = pd.DataFrame(
        [
            {
                "Показатель": "Капитал",
                "Факт": 2_358_663.26,
                "Цель": 1_500_000.0,
                "Отклонение": 858_663.26,
                "Прогресс (%)": 157.244217,
                "Тип": "money",
            },
            {
                "Показатель": "Норма сбережений",
                "Факт": 75.0,
                "Цель": 80.0,
                "Отклонение": -5.0,
                "Прогресс (%)": 93.75,
                "Тип": "percent",
            },
        ]
    )
    before = source.copy(deep=True)

    display = _without_dtype_warnings(
        lambda: planning_data._format_goals_progress(source, "RUB")
    )

    assert display.loc[0, ["Факт", "Цель", "Отклонение"]].tolist() == [
        "2 358 663.26₽",
        "1 500 000.00₽",
        "858 663.26₽",
    ]
    assert display.loc[1, ["Факт", "Цель", "Отклонение"]].tolist() == [
        "75.00%",
        "80.00%",
        "-5.00%",
    ]
    assert display["Прогресс (%)"].tolist() == ["157.24%", "93.75%"]
    assert_frame_equal(source, before)
