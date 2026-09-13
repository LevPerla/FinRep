import os
import re

os.environ.setdefault("FINREP_DASH_PASSWORD", "test-password")
os.environ.setdefault("FINREP_DASH_SECRET_KEY", "test-session-secret")

from src.dashboard.app import (
    _localized_text_values,
    _resolve_locale_update,
    create_app,
)
from src.dashboard.auth import LOGIN_TEMPLATE
from src.dashboard.i18n import (
    DEFAULT_LOCALE,
    DYNAMIC_TRANSLATION_KEYS,
    LOCALE_STORAGE_KEY,
    LOCALE_TIMESTAMP_STORAGE_KEY,
    SUPPORTED_LOCALES,
    TRANSLATIONS,
    normalize_locale,
    tr,
    translation_payload,
)


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
