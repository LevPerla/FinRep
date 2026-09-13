from __future__ import annotations


DEFAULT_LOCALE = "ru"
SUPPORTED_LOCALES = ("ru", "en")
LOCALE_STORAGE_KEY = "dashboard-locale"
LOCALE_TIMESTAMP_STORAGE_KEY = "dashboard-locale-timestamp"
DYNAMIC_TRANSLATION_KEYS = frozenset({"dashboard.theme_toggle"})

TRANSLATIONS: dict[str, dict[str, str]] = {
    "ru": {
        "app.title": "Финансы",
        "auth.page_title": "FinRep — вход",
        "auth.password": "Пароль",
        "auth.password_help_label": "Что делать, если пароль не установлен",
        "auth.show_password": "Показать пароль",
        "auth.hide_password": "Скрыть пароль",
        "auth.live_submit": "Войти в LIVE",
        "auth.live_unavailable": "LIVE недоступен: задайте FINREP_DASH_PASSWORD и FINREP_DASH_SECRET_KEY в .env.",
        "auth.demo_submit": "Открыть demo без пароля",
        "auth.demo_note": "Demo использует только read-only sample_data.",
        "auth.error": "LIVE недоступен или введен неверный пароль.",
        "auth.help_title": "Как настроить пароль",
        "auth.help_intro": "Чтобы открыть LIVE с вашими данными:",
        "auth.help_env_file_prefix": "Создайте файл",
        "auth.help_env_file_suffix": "в корне проекта (можно скопировать",
        "auth.help_password_prefix": "Задайте свой пароль:",
        "auth.help_secret_prefix": "Добавьте стабильный случайный ключ сессий:",
        "auth.help_secret_suffix": "Его можно получить командой",
        "auth.help_restart": "Перезапустите приложение и войдите с новым паролем.",
        "auth.help_demo": "Demo по-прежнему доступно без пароля и использует только read-only sample_data.",
        "auth.help_done": "Понятно",
        "action.close": "Закрыть",
        "action.close.mobile_more": "Закрыть",
        "action.close.month_modal": "Закрыть",
        "dashboard.title": "Финансы",
        "dashboard.settings": "Параметры",
        "dashboard.refresh": "Обновить",
        "dashboard.refresh_fx": "Обновить курс",
        "dashboard.logout": "Выйти",
        "dashboard.theme_light": "Светлая",
        "dashboard.theme_dark": "Темная",
        "dashboard.mode_test": "ДЕМО",
        "dashboard.mode_live": "LIVE",
        "dashboard.locale_label": "Язык интерфейса",
        "dashboard.export_live_only": "PNG/PDF доступны только в LIVE.",
        "nav.primary_label": "Основные разделы",
        "nav.main.desktop": "Основной отчет",
        "nav.year.desktop": "Годовой отчет",
        "nav.month.desktop": "Месячный отчет",
        "nav.planning.desktop": "План и прогноз",
        "nav.input.desktop": "Ввод данных",
        "nav.debts.desktop": "Долги · Beta",
        "nav.investments.desktop": "Инвестиции · Beta",
        "nav.main.mobile": "Основной отчет",
        "nav.year.mobile": "Годовой отчет",
        "nav.month.mobile": "Месячный отчет",
        "nav.planning.mobile": "План",
        "nav.more.mobile": "Ещё",
        "nav.input.mobile": "Ввод данных",
        "nav.debts.mobile": "Долги · Beta",
        "nav.investments.mobile": "Инвестиции · Beta",
        "nav.more_title": "Другие разделы",
    },
    "en": {
        "app.title": "Finance",
        "auth.page_title": "FinRep — sign in",
        "auth.password": "Password",
        "auth.password_help_label": "What to do if no password is configured",
        "auth.show_password": "Show password",
        "auth.hide_password": "Hide password",
        "auth.live_submit": "Sign in to LIVE",
        "auth.live_unavailable": "LIVE is unavailable: set FINREP_DASH_PASSWORD and FINREP_DASH_SECRET_KEY in .env.",
        "auth.demo_submit": "Open demo without a password",
        "auth.demo_note": "Demo uses read-only sample_data only.",
        "auth.error": "LIVE is unavailable or the password is incorrect.",
        "auth.help_title": "How to configure a password",
        "auth.help_intro": "To open LIVE with your data:",
        "auth.help_env_file_prefix": "Create a",
        "auth.help_env_file_suffix": "file in the project root (you can copy",
        "auth.help_password_prefix": "Set your password:",
        "auth.help_secret_prefix": "Add a stable random session key:",
        "auth.help_secret_suffix": "You can generate it with",
        "auth.help_restart": "Restart the application and sign in with the new password.",
        "auth.help_demo": "Demo remains available without a password and uses read-only sample_data only.",
        "auth.help_done": "Got it",
        "action.close": "Close",
        "action.close.mobile_more": "Close",
        "action.close.month_modal": "Close",
        "dashboard.title": "Finance",
        "dashboard.settings": "Settings",
        "dashboard.refresh": "Refresh",
        "dashboard.refresh_fx": "Refresh rates",
        "dashboard.logout": "Sign out",
        "dashboard.theme_light": "Light",
        "dashboard.theme_dark": "Dark",
        "dashboard.mode_test": "DEMO",
        "dashboard.mode_live": "LIVE",
        "dashboard.locale_label": "Interface language",
        "dashboard.export_live_only": "PNG/PDF are available in LIVE only.",
        "nav.primary_label": "Main sections",
        "nav.main.desktop": "Overview",
        "nav.year.desktop": "Year report",
        "nav.month.desktop": "Month report",
        "nav.planning.desktop": "Plan and forecast",
        "nav.input.desktop": "Add data",
        "nav.debts.desktop": "Debts · Beta",
        "nav.investments.desktop": "Investments · Beta",
        "nav.main.mobile": "Overview",
        "nav.year.mobile": "Year",
        "nav.month.mobile": "Month",
        "nav.planning.mobile": "Plan",
        "nav.more.mobile": "More",
        "nav.input.mobile": "Add data",
        "nav.debts.mobile": "Debts · Beta",
        "nav.investments.mobile": "Investments · Beta",
        "nav.more_title": "More sections",
    },
}


def normalize_locale(locale: str | None) -> str:
    return locale if locale in SUPPORTED_LOCALES else DEFAULT_LOCALE


def tr(key: str, locale: str | None = None) -> str:
    normalized = normalize_locale(locale)
    return TRANSLATIONS[normalized].get(key, TRANSLATIONS[DEFAULT_LOCALE].get(key, key))


def translation_payload() -> dict[str, dict[str, str]]:
    return {locale: values.copy() for locale, values in TRANSLATIONS.items()}
