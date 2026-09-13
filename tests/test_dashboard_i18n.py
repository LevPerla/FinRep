import os
import re

import pandas as pd
import plotly.graph_objects as go

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.app import (
    _localized_column_defs,
    _localized_text_values,
    _month_report_layout,
    _resolve_locale_update,
    _year_report_layout,
    create_app,
)
from src.dashboard.auth import LOGIN_TEMPLATE
from src.dashboard.i18n import (
    DEFAULT_LOCALE,
    DYNAMIC_TRANSLATION_KEYS,
    LOCALE_STORAGE_KEY,
    LOCALE_TIMESTAMP_STORAGE_KEY,
    REPORT_TEXT_EN,
    SUPPORTED_LOCALES,
    TRANSLATIONS,
    localize_report_datasets,
    normalize_locale,
    report_column_label,
    report_text,
    tr,
    translation_payload,
)
from src.dashboard.main_data import DashboardDataset


def _layout_component(node, component_id):
    if isinstance(node, dict):
        if node.get("props", {}).get("id") == component_id:
            return node
        for value in node.values():
            found = _layout_component(value, component_id)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _layout_component(value, component_id)
            if found is not None:
                return found
    elif getattr(node, "id", None) == component_id:
        return node
    elif hasattr(node, "children"):
        return _layout_component(node.children, component_id)
    return None


def _layout_i18n_keys(node) -> set[str]:
    keys = set()
    if isinstance(node, dict):
        component_id = node.get("props", {}).get("id")
        if isinstance(component_id, dict) and component_id.get("type") in {"i18n-text", "i18n-tab-label"}:
            keys.add(component_id["key"])
        for value in node.values():
            keys.update(_layout_i18n_keys(value))
    elif isinstance(node, list):
        for value in node:
            keys.update(_layout_i18n_keys(value))
    return keys


def test_translation_catalog_has_the_same_keys_for_every_locale():
    reference_keys = set(TRANSLATIONS[DEFAULT_LOCALE])

    assert SUPPORTED_LOCALES == ("ru", "en")
    assert reference_keys
    assert all(set(TRANSLATIONS[locale]) == reference_keys for locale in SUPPORTED_LOCALES)


def test_unknown_locale_falls_back_to_russian_without_mutating_catalog():
    payload = translation_payload()
    payload["ru"]["dashboard.title"] = "changed"

    assert normalize_locale("de") == "ru"
    assert normalize_locale(None) == "ru"
    assert tr("dashboard.settings", "de") == "Параметры"
    assert tr("missing.key", "en") == "missing.key"
    assert TRANSLATIONS["ru"]["dashboard.title"] == "Финансы"


def test_locale_resolution_prefers_user_selection_and_otherwise_stored_value():
    assert _resolve_locale_update("dashboard-locale-select", "en", "ru") == "en"
    assert _resolve_locale_update("dashboard-locale-select", None, "en") == "en"
    assert _resolve_locale_update("dashboard-locale", None, "en") == "en"
    assert _resolve_locale_update(None, None, "invalid") == "ru"


def test_dashboard_locale_store_is_local_and_selector_stays_inside_settings():
    app = create_app()
    client = app.server.test_client()
    client.post("/login", data={"data_mode": "test"})
    layout = client.get("/_dash-layout").get_json()

    store = _layout_component(layout, "dashboard-locale")
    settings = _layout_component(layout, "dashboard-settings")
    selector = _layout_component(settings, "dashboard-locale-select")

    assert store["props"]["storage_type"] == "local"
    assert store["props"]["data"] == "ru"
    assert selector["props"]["options"] == [
        {"label": "RU", "value": "ru"},
        {"label": "EN", "value": "en"},
    ]
    assert _layout_component(layout, "dashboard-currency")["props"]["value"] == "RUB"


def test_every_login_and_dashboard_chrome_key_exists_in_the_catalog():
    app = create_app()
    client = app.server.test_client()
    client.post("/login", data={"data_mode": "test"})
    layout = client.get("/_dash-layout").get_json()
    template_keys = set(re.findall(r'data-i18n(?:-aria-label)?="([^"]+)"', LOGIN_TEMPLATE))
    used_keys = template_keys | _layout_i18n_keys(layout)

    assert used_keys
    assert used_keys <= set(TRANSLATIONS[DEFAULT_LOCALE]) | DYNAMIC_TRANSLATION_KEYS


def test_dashboard_chrome_translation_changes_labels_but_not_keys():
    component_ids = [
        {"type": "i18n-text", "key": "dashboard.settings"},
        {"type": "i18n-text", "key": "nav.main.desktop"},
        {"type": "i18n-text", "key": "dashboard.theme_toggle"},
    ]

    assert _localized_text_values(component_ids, "ru", "dark") == [
        "Параметры",
        "Основной отчет",
        "Светлая",
    ]
    assert _localized_text_values(component_ids, "en", "light") == [
        "Settings",
        "Overview",
        "Dark",
    ]
    assert [component["key"] for component in component_ids] == [
        "dashboard.settings",
        "nav.main.desktop",
        "dashboard.theme_toggle",
    ]


def test_login_uses_the_same_browser_locale_key_and_returns_english_error():
    app = create_app()
    client = app.server.test_client()

    login_html = client.get("/login").get_data(as_text=True)
    response = client.post(
        "/login",
        data={"password": "wrong", "data_mode": "live", "locale": "en"},
    )
    english_html = response.get_data(as_text=True)

    assert LOCALE_STORAGE_KEY in login_html
    assert LOCALE_TIMESTAMP_STORAGE_KEY in login_html
    assert "JSON.stringify(value)" in login_html
    assert 'data-locale="ru"' in login_html
    assert 'data-locale="en"' in login_html
    assert 'name="locale"' in login_html
    assert response.status_code == 401
    assert '<html lang="en">' in english_html
    assert "LIVE is unavailable or the password is incorrect." in english_html
    assert "Sign in to LIVE" in english_html


def test_report_translation_keeps_category_values_and_internal_columns_stable():
    raw = pd.DataFrame(
        [
            {
                "Показатель": "Доход",
                "Статус": "В норме",
                "Категория": "Доход",
                "Значение": 125.5,
            }
        ]
    )
    display = raw.copy(deep=True)
    figure = go.Figure(go.Bar(x=["Доход"], y=[125.5], name="Доход"))
    figure.update_layout(title="Динамика доходов и расходов")
    dataset = DashboardDataset(
        id="example",
        title="Ключевые метрики",
        dataframe=raw,
        display_dataframe=display,
        figure=figure,
    )

    localized = localize_report_datasets({"example": dataset}, "en")["example"]

    assert localized.dataframe is raw
    pd.testing.assert_frame_equal(localized.dataframe, raw)
    assert list(localized.display_dataframe.columns) == list(display.columns)
    assert localized.display_dataframe.loc[0, "Показатель"] == "Income"
    assert localized.display_dataframe.loc[0, "Статус"] == "On track"
    assert localized.display_dataframe.loc[0, "Категория"] == "Доход"
    assert localized.display_dataframe.loc[0, "Значение"] == 125.5
    assert localized.title == "Key metrics"
    assert localized.figure.layout.title.text == "Income and expense trend"
    assert localized.figure.data[0].name == "Income"
    assert list(localized.figure.data[0].x) == ["Доход"]
    assert dataset.title == "Ключевые метрики"
    assert dataset.figure.data[0].name == "Доход"


def test_report_text_handles_dynamic_copy_and_russian_fallback():
    assert report_text("Топ-15 самых больших покупок за 2026 год", "en") == "Top 15 largest purchases in 2026"
    assert report_text("2026-05: доход минус расход", "en") == "2026-05: income minus expenses"
    assert report_text("USD укрепляется на 10% к остальным валютам", "en") == "USD strengthens by 10% against other currencies"
    assert report_text("Нет курса EUR → RUB на 2026-09-30. Зависимый итог недоступен.", "en") == (
        "No EUR → RUB rate is available for 2026-09-30. The dependent total is unavailable."
    )
    assert report_text("Пища", "en") == "Пища"
    assert report_text("Доход", "ru") == "Доход"
    assert report_column_label("В валюте отчета (RUB)", "en") == "In report currency (RUB)"


def test_localized_grid_headers_keep_raw_fields_for_callbacks_and_styles():
    data = pd.DataFrame([{"Показатель": "Капитал", "Цель": "100.00₽"}])
    dataset = DashboardDataset(id="planning_goals", title="Цели года", dataframe=data, display_dataframe=data)

    columns = _localized_column_defs(dataset, data, "dark", read_only=False, locale="en")

    assert [(column["field"], column["headerName"]) for column in columns] == [
        ("Показатель", "Metric"),
        ("Цель", "Goal"),
    ]
    assert columns[1]["editable"] is True
    assert "Capital" in columns[0]["valueFormatter"]["function"]

    localized = localize_report_datasets({"planning_goals": dataset}, "en")["planning_goals"]
    assert localized.display_dataframe.loc[0, "Показатель"] == "Капитал"
    assert localized.dataframe.loc[0, "Показатель"] == "Капитал"


def test_report_empty_states_switch_language_without_changing_route_values():
    month_dataset = DashboardDataset(
        id="month_empty",
        title="Месяц не сохранён",
        dataframe=pd.DataFrame([{"Год": "2026", "Месяц": "09", "Валюта": "RUB"}]),
    )
    year_dataset = DashboardDataset(
        id="year_empty",
        title="Нет данных за выбранный год",
        dataframe=pd.DataFrame([{"Год": "2026", "Валюта": "RUB"}]),
    )

    month_layout = _month_report_layout({"month_empty": month_dataset}, "dark", locale="en")
    year_layout = _year_report_layout({"year_empty": year_dataset}, "dark", locale="en")
    month_link = _layout_component(month_layout, "month-empty-input-link")

    assert "Month not saved" in str(month_layout)
    assert "No data for 2026-09" in str(month_layout)
    assert month_link.href == "?currency=RUB&year=2026&month=09&tab=input"
    assert "Year with no transactions" in str(year_layout)
    assert "No data for 2026" in str(year_layout)


def test_report_translation_dictionary_has_no_empty_english_labels():
    assert REPORT_TEXT_EN
    assert all(source and translation for source, translation in REPORT_TEXT_EN.items())
