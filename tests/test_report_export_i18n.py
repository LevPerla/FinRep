import inspect
import os
from io import BytesIO

import pandas as pd
import plotly.graph_objects as go
from openpyxl import load_workbook

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.app import _dataframe_to_xlsx_bytes
from src.dashboard.app import create_app
from src.dashboard.i18n import (
    DEFAULT_LOCALE,
    localize_export_dataframe,
    localize_figure,
)
from src.reports.main_report import create_main_report
from src.reports.month_report import create_month_report
from src.reports.year_report import create_year_report


def test_xlsx_localization_preserves_raw_data_categories_and_numeric_types():
    raw = pd.DataFrame(
        [
            {
                "Показатель": "Доход",
                "Категория": "Прочее",
                "Значение": 125.5,
                "Комментарий": "=1+1",
            }
        ]
    )

    localized = localize_export_dataframe(raw, "en")
    workbook = load_workbook(
        BytesIO(_dataframe_to_xlsx_bytes(localized, "Key metrics")),
        data_only=False,
    )
    sheet = workbook.active

    assert list(localized.columns) == ["Metric", "Category", "Value", "Comment"]
    assert localized.iloc[0].to_dict() == {
        "Metric": "Income",
        "Category": "Прочее",
        "Value": 125.5,
        "Comment": "=1+1",
    }
    assert raw.iloc[0].to_dict()["Показатель"] == "Доход"
    assert sheet.title == "Key metrics"
    assert sheet["C2"].data_type == "n"
    assert sheet["D2"].data_type == "s"


def test_legacy_figure_localization_changes_presentation_on_a_copy():
    raw = go.Figure(
        go.Table(
            header={"values": ["Дата", "Доход"]},
            cells={"values": [["2026-05"], [100]]},
        )
    )
    raw.update_layout(
        title="Основной отчет в валюте RUB",
        annotations=[{"text": "Статистика по годам", "x": 0.5, "y": 1}],
    )

    localized = localize_figure(raw, "en")

    assert localized is not raw
    assert localized.layout.title.text == "Overview report in RUB"
    assert localized.layout.annotations[0].text == "Yearly statistics"
    assert list(localized.data[0].header.values) == ["Date", "Income"]
    assert raw.layout.title.text == "Основной отчет в валюте RUB"
    assert list(raw.data[0].header.values) == ["Дата", "Доход"]


def test_legacy_report_entrypoints_default_to_russian_locale():
    for report in (create_main_report, create_year_report, create_month_report):
        assert inspect.signature(report).parameters["locale"].default == DEFAULT_LOCALE


def test_dataset_download_callback_receives_dashboard_locale():
    app = create_app()
    key = next(key for key in app.callback_map if '"type":"dataset-download"' in key)

    assert any(
        item["id"] == "dashboard-locale" for item in app.callback_map[key]["state"]
    )
