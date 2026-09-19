from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import re

import pandas as pd


DEFAULT_LOCALE = "ru"
SUPPORTED_LOCALES = ("ru", "en")
LOCALE_STORAGE_KEY = "dashboard-locale"
LOCALE_TIMESTAMP_STORAGE_KEY = "dashboard-locale-timestamp"
DYNAMIC_TRANSLATION_KEYS = frozenset({"dashboard.theme_toggle"})

REPORT_TEXT_EN = {
    "Суммарные расходы и крупнейшие покупки": "Total expenses and largest purchases",
    "Пунктирные линии — топ-15 покупок за всю историю. Наведите курсор или коснитесь линии, чтобы прочитать комментарий.": "Dotted lines mark the top 15 purchases over the full history. Hover over or tap a line to read the comment.",
    "Аллокация расходов по месяцам": "Monthly expense allocation",
    "Доли рассчитаны из сумм расходов внутри каждого месяца.": "Shares are calculated from expense totals within each month.",
    "Доли не определены: итог месяца отсутствует или не положителен.": "Shares are undefined: the monthly total is missing or not positive.",
    "Отрицательные доли отражают корректировки расходов.": "Negative shares reflect expense adjustments.",
    "Доля, %": "Share, %",
    "Аналитика расходов": "Expense analytics",
    "Расходы по категориям за месяц": "Monthly expenses by category",
    "Вся история. Год и месяц в панели не ограничивают этот отчёт.": "Full history. The year and month controls do not limit this report.",
    "Нет расходных операций": "No expense transactions",
    "Нет данных за месяцы:": "No data for months:",
    "Пропуски не считаются нулевыми расходами.": "Missing months are not treated as zero expenses.",
    "Не удалось загрузить аналитику расходов.": "Unable to load expense analytics.",
    # Dataset and chart titles.
    "Ключевые метрики": "Key metrics",
    "Итоги по годам": "Yearly totals",
    "Топ-15 самых больших покупок за всю историю": "Top 15 largest purchases",
    "Курсы валют": "Exchange rates",
    "Курсы валют и конвертация": "Exchange rates and conversion",
    "Доходы и расходы": "Income and expenses",
    "Динамика доходов и расходов": "Income and expense trend",
    "Динамика доходов/расходов": "Income and expense trend",
    "Денежный поток": "Cash flow",
    "Норма сбережений": "Savings rate",
    "Динамика нормы сбережений": "Savings rate trend",
    "Динамика капитала": "Capital trend",
    "Валютная переоценка": "FX revaluation",
    "Валютная структура активов": "Asset currency allocation",
    "Динамика аллокации активов по валютам": "Asset allocation by currency",
    "Изменение курсов валют": "Exchange-rate changes",
    "Динамика курсов валют": "Exchange-rate trend",
    "Итоги по кварталам": "Quarterly totals",
    "Распределение расходов": "Expense distribution",
    "Доходы по месяцам": "Monthly income",
    "Расходы по месяцам": "Monthly expenses",
    "Описательные статистики": "Summary statistics",
    "Капитал по месяцам": "Monthly capital",
    "Транзакции": "Transactions",
    "Суммарные показатели": "Summary metrics",
    "Дебиторская задолженность": "Receivables",
    "Кредиторская задолженность": "Payables",
    "Распределение по счетам": "Allocation by account",
    "Цели года": "Annual goals",
    "Прогноз капитала на 12 месяцев": "12-month capital forecast",
    "Факт с начала прошлого года и прогноз на 12 месяцев": "Actual since last year and 12-month forecast",
    "Финансовый запас по денежному потоку": "Cash-flow runway",
    "Валютные сценарии": "Currency scenarios",
    "Нет данных за выбранный год": "No data for the selected year",
    "Статистика по годам": "Yearly statistics",
    "Дельты": "Net cash flow",
    "Информация о курсах валют": "Exchange-rate information",
    "Нет данных о курсах валют": "No exchange-rate data",
    "Ошибка загрузки курсов валют": "Could not load exchange rates",
    # Table columns, metrics, statuses and legend labels.
    "Год": "Year",
    "Месяц": "Month",
    "Квартал": "Quarter",
    "Дата": "Date",
    "Дата оценки": "Valuation date",
    "Показатель": "Metric",
    "Значение": "Value",
    "Сумма": "Amount",
    "Суммарно": "Total",
    "Среднее": "Average",
    "Медиана": "Median",
    "Ст. отклонение": "Std. deviation",
    "Минимум": "Minimum",
    "Максимум": "Maximum",
    "Процент": "Share",
    "Процент дохода": "Income ratio",
    "Статистика": "Statistic",
    "Статус": "Status",
    "Статус ID": "Status ID",
    "Детали": "Details",
    "Источник": "Source",
    "Источник целей": "Goal source",
    "Тип": "Type",
    "Валюта": "Currency",
    "Курс": "Rate",
    "Обратный": "Inverse",
    "Обратный курс": "Inverse rate",
    "Изм.": "Change",
    "Изменение (%)": "Change (%)",
    "Категория": "Category",
    "Комментарий": "Comment",
    "Счет": "Account",
    "Исходная сумма": "Original amount",
    "В валюте отчета": "In report currency",
    "Общий доход": "Total income",
    "Общий расход": "Total expenses",
    "Доход": "Income",
    "Расход": "Expenses",
    "Сбережения": "Savings",
    "Дельта": "Net cash flow",
    "Баланс": "Monthly balance",
    "Сальдо": "Net result",
    "Капитал": "Capital",
    "Капитал cash-flow": "Cash-flow capital",
    "Капитал по cash-flow": "Cash-flow capital",
    "Капитал по активам": "Asset-based capital",
    "Капитал по денежному потоку": "Cash-flow capital",
    "Инвестиции": "Investments",
    "Расхождение с активами": "Asset reconciliation gap",
    "Погашение деб. зад.": "Receivable repayments",
    "Погашение кред. зад.": "Payable repayments",
    "Доход минус расход": "Income minus expenses",
    "Доход месяца": "Monthly income",
    "Расход месяца": "Monthly expenses",
    "Денежный поток месяца": "Monthly cash flow",
    "Валютная переоценка месяца": "Monthly FX revaluation",
    "Финансовый запас по активам": "Asset-based runway",
    "Финансовый запас по денежному потоку": "Cash-flow runway",
    "Средний доход/мес": "Average monthly income",
    "Средний расход/мес": "Average monthly expenses",
    "Средний расход": "Average expenses",
    "Факт": "Actual",
    "Цель": "Goal",
    "Прогноз": "Forecast",
    "Отклонение": "Variance",
    "Прогресс (%)": "Progress (%)",
    "Runway, мес.": "Runway, months",
    "Runway, лет": "Runway, years",
    "Сценарий": "Scenario",
    "Что меняется": "Change description",
    "Пара курса": "Currency pair",
    "Курс сценария": "Scenario rate",
    "Шок выбранной валюты (%)": "Selected-currency shock (%)",
    "Изменение капитала": "Capital change",
    "Всего": "Total",
    "Всего в валюте": "Total in currency",
    "Всего в валюте,%": "Share by currency, %",
    "В норме": "On track",
    "Нет данных": "No data",
    "Положительный": "Positive",
    "Отрицательный": "Negative",
    "Высокий уровень": "Strong",
    "Стоит проверить": "Review",
    "Низкий уровень": "Low",
    "Требует сверки": "Reconciliation needed",
    "Источник: активы": "Source: assets",
    "Источник: денежный поток": "Source: cash flow",
    "рассчитано": "calculated",
    "не рассчитано": "not calculated",
    "не задано": "not set",
    "Без изменения курсов": "No exchange-rate change",
    # Report layout and empty/error states.
    "Не удалось загрузить данные основного отчета.": "Could not load the overview report.",
    "Не удалось загрузить данные годового отчета.": "Could not load the year report.",
    "Не удалось загрузить данные месячного отчета.": "Could not load the month report.",
    "Не удалось загрузить данные плана и прогноза.": "Could not load planning and forecast data.",
    "Показатели выбранного месяца недоступны. История и показатели с указанной последней датой остаются видимыми.": "Metrics for the selected month are unavailable. History and metrics with their latest available date remain visible.",
    "Первый запуск": "First run",
    "Добавьте первые операции": "Add your first transactions",
    "После сохранения месяца здесь появятся баланс, динамика расходов и показатели для сверки.": "After you save the month, this page will show your balance, expense trend, and reconciliation metrics.",
    "Откройте раздел «Ввод данных».": "Open Add data.",
    "Загрузите банковскую выписку или добавьте операцию вручную.": "Upload a bank statement or add a transaction manually.",
    "Проверьте Preview и нажмите «Сохранить месяц».": "Review the preview and select Save month.",
    "Перейти к вводу данных": "Go to Add data",
    "На телефоне: Ещё → Ввод данных.": "On mobile: More → Add data.",
    "Сверка": "Reconciliation",
    "Устойчивость": "Resilience",
    "Задолженности": "Debts",
    "Капитал и активы": "Capital and assets",
    "Прочие показатели": "Other metrics",
    "Год без операций": "Year with no transactions",
    "Выберите другой год или добавьте и сохраните операции за этот период.": "Select another year or add and save transactions for this period.",
    "Финансовый запас по денежному потоку, месяцев": "Cash-flow runway, months",
    "Финансовый запас по денежному потоку, лет": "Cash-flow runway, years",
    "Месяц не сохранён": "Month not saved",
    "Выбранный месяц ещё не создан. Добавьте или импортируйте операции, проверьте Preview и сохраните месяц.": "The selected month has not been created yet. Add or import transactions, review the preview, and save the month.",
    "Нет данных для отображения.": "No data to display.",
    "Открыть детализацию транзакций за день": "Open transaction details for this day",
    "В этот день нет ненулевых транзакций.": "There are no non-zero transactions on this day.",
    "Транзакции за день": "Transactions for the day",
    # Metric explanations.
    "Все операции категории «Доход» за выбранный месяц": "All Income transactions for the selected month",
    "Все расходные операции за выбранный месяц": "All expense transactions for the selected month",
    "Операции категории «Сбережения» за выбранный месяц": "Savings transactions for the selected month",
    "Доход минус расход за выбранный месяц": "Income minus expenses for the selected month",
    "Cash-flow месяца с учетом сбережений и долговых операций": "Monthly cash flow including savings and debt transactions",
    "Новые суммы, выданные в долг за выбранный месяц": "New amounts lent during the selected month",
    "Возвраты ранее выданных долгов за выбранный месяц": "Repayments of amounts previously lent",
    "Новые заимствования за выбранный месяц": "New borrowings during the selected month",
    "Погашения ранее полученных долгов за выбранный месяц": "Repayments of previous borrowings",
    "Накопленный cash-flow на конец выбранного месяца": "Cumulative cash flow at the end of the selected month",
    "Стоимость assets snapshot; инвестиции учтены в последнем доступном месяце": "Asset snapshot value; investments are included in the latest available month",
    "Операции категории «Инвестиции» за выбранный месяц": "Investment transactions for the selected month",
    "Assets snapshot минус накопленный cash-flow капитал": "Asset snapshot minus cumulative cash-flow capital",
    "Изменение активов сверх cash-flow месяца: валютная и рыночная переоценка": "Change in assets beyond monthly cash flow: FX and market revaluation",
    "Нет снимка активов за выбранный месяц.": "No asset snapshot for the selected month.",
    # Data entry presentation. Category names and stored values stay unchanged.
    "Активы": "Assets",
    "Ручной ввод транзакции": "Add a transaction manually",
    "Импорт банковского PDF": "Import a bank PDF",
    "Проверка и сохранение месяца": "Review and save the month",
    "Действие": "Action",
    "Направление": "Direction",
    "Статус банка": "Bank status",
    "Причина skip": "Skip reason",
    "Дубль в CSV": "Duplicate in CSV",
    "Дубль в staging": "Duplicate in staging",
    "Детали PDF": "PDF details",
    "Счёт банка": "Bank account",
    "Заменяет pending": "Replaces pending",
    "Несколько pending": "Multiple pending matches",
    "Ревизия staging": "Staging revision",
    "Загрузить": "Load",
    "Добавить строку": "Add row",
    "Удалить выбранные": "Delete selected",
    "Применить": "Apply",
    "Добавить": "Add",
    "Перетащи Kaspi, BCC или Ozon PDF сюда": "Drop a Kaspi, BCC, or Ozon PDF here",
    "или нажми для выбора файла": "or select a file",
    "Операции из PDF появятся здесь. Дубли среди черновиков и сохранённых операций будут пропущены.": "PDF transactions will appear here. Duplicates among drafts and saved transactions will be skipped.",
    "Категории: клик — одна ячейка, Shift+клик — диапазон, Ctrl/Cmd+клик — несколько; Ctrl/Cmd+C и Ctrl/Cmd+V — копировать и вставить.": "Categories: click one cell, Shift+click a range, or Ctrl/Cmd+click multiple cells; use Ctrl/Cmd+C and Ctrl/Cmd+V to copy and paste.",
    "Период Preview": "Preview period",
    "Выбери месяц выписки": "Select statement month",
    "Сохранить месяц": "Save month",
    "Проверь импорт выше и нажми Preview. Без загруженной выписки используется выбранный период отчёта. Данные месяца изменятся только после нажатия «Сохранить месяц».": "Review the import above and select Preview. Without an uploaded statement, the selected report period is used. Monthly data changes only after you select Save month.",
    "Заполни дату, категорию, валюту и сумму.": "Enter the date, category, currency, and amount.",
    "Черновик добавлен.": "Draft added.",
    "Черновик уже был добавлен; повтор не создан.": "This draft was already added; no duplicate was created.",
    "Сначала нажми Preview, затем подтверди экспорт.": "Select Preview first, then confirm saving.",
    "Выписка содержит несколько месяцев: выбери период перед Preview.": "The statement contains multiple months: select a period before Preview.",
    "Выбранный период не соответствует строкам текущей выписки.": "The selected period does not match the current statement rows.",
    "В импорте отсутствует дата операции.": "The import has no transaction date.",
    "В импорте есть строка с некорректной датой.": "The import contains a row with an invalid date.",
    "Выбери строки активов для удаления.": "Select asset rows to delete.",
    "PDF не выбран или не содержит данных.": "No PDF was selected or the file contains no data.",
    "Не удалось прочитать PDF. Проверь файл и попробуй снова.": "Could not read the PDF. Check the file and try again.",
    "Не удалось разобрать банковскую выписку. Проверь формат файла.": "Could not parse the bank statement. Check the file format.",
    "В PDF не найден текст с операциями. Проверь содержимое файла.": "No transaction text was found in the PDF. Check the file contents.",
    "Выписка распознана, но операции в ней не найдены.": "The statement was recognized, but no transactions were found.",
    "Этот PDF не похож на поддерживаемую выписку Kaspi, BCC или Ozon Банка.": "This PDF does not look like a supported Kaspi, BCC, or Ozon Bank statement.",
    "Некорректное действие импорта: выбери import или skip.": "Invalid import action. Select import or skip.",
    "Есть возможные дубли без решения: для каждой строки review выбери import или skip.": "Some possible duplicates require a decision. Select import or skip for every review row.",
    "История операций изменилась после Preview: построй Preview заново и проверь возможные дубли.": "Transaction history changed after Preview. Create a new Preview and review possible duplicates.",
    "Банковское поступление нельзя сохранить как расход: выбери «Сбережения», «Доход» или другую категорию поступления.": "A bank credit cannot be saved as an expense. Select Savings, Income, or another credit category.",
    "Структура Preview изменилась: построй Preview заново.": "The Preview structure changed. Create a new Preview.",
    "Структура дат Preview изменилась: построй Preview заново.": "The Preview date structure changed. Create a new Preview.",
    "Нет черновиков со статусом draft/ready для выбранного месяца.": "There are no draft or ready transactions for the selected month.",
    "Preview пустой: сначала нажми Preview или заполни таблицу.": "Preview is empty. Select Preview first or fill in the table.",
    "В preview нет колонки Дата.": "Preview has no Date column.",
    "Preview не содержит ревизию staging: построй Preview заново.": "Preview has no staging revision. Create a new Preview.",
    "Preview создан для другого режима данных: построй Preview заново.": "Preview was created for another data mode. Create a new Preview.",
    "Preview создан для другого периода: построй Preview заново.": "Preview was created for another period. Create a new Preview.",
    "Preview устарел: данные изменились, построй Preview заново.": "Preview is stale because the data changed. Create a new Preview.",
    "Месяц сохранён, но итоговые показатели сейчас недоступны. Открой сверку, чтобы повторить расчёт.": "The month was saved, but summary metrics are currently unavailable. Open reconciliation to run the calculation again.",
    "Перейти к сверке": "Go to reconciliation",
    "внутренние переводы": "internal transfers",
    "уже добавлены ранее": "previously added",
    "возможные дубли сохранённых операций": "possible duplicates of saved transactions",
    "неоднозначные pending-операции": "ambiguous pending transactions",
    "исключены вручную": "excluded manually",
    "уже обработаны": "already processed",
}

REPORT_VALUE_COLUMNS = {
    "Показатель",
    "Статус",
    "Детали",
    "Статистика",
    "Тип",
    "Источник",
    "Сценарий",
    "Что меняется",
    "Квартал",
}

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
        "report.main.overview": "Обзор",
        "report.main.expenses": "Расходы",
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
        "report.main.overview": "Overview",
        "report.main.expenses": "Expenses",
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


def report_text(value: object, locale: str | None = None) -> object:
    """Translate report presentation text while leaving data/category keys intact."""
    if normalize_locale(locale) != "en" or not isinstance(value, str):
        return value
    if value in REPORT_TEXT_EN:
        return REPORT_TEXT_EN[value]

    patterns = (
        (r"^Топ-15 самых больших покупок за (\d{4}) год$", r"Top 15 largest purchases in \1"),
        (r"^1 валюта в (.+)$", r"1 unit in \1"),
        (r"^([^:]+): доход минус расход$", r"\1: income minus expenses"),
        (r"^([^:]+): денежный поток / доход$", r"\1: cash flow / income"),
        (r"^([^:]+): изменение стоимости из-за курсов валют$", r"\1: value change caused by exchange rates"),
        (r"^([^,]+), выбранный месяц$", r"\1, selected month"),
        (r"^([^,]+), последний доступный месяц$", r"\1, latest available month"),
        (r"^([^,]+), нет сохранённых данных$", r"\1, no saved data"),
        (r"^([^,]+), средний расход за 12 месяцев: (.+)$", r"\1, 12-month average expenses: \2"),
        (r"^(.+); данные на (.+)$", r"\1; data as of \2"),
        (r"^Капитал по активам / средний расход за последние 12 месяцев(.*)$", r"Asset-based capital / average expenses over the last 12 months\1"),
        (r"^Капитал по денежному потоку / средний расход за последние 12 месяцев(.*)$", r"Cash-flow capital / average expenses over the last 12 months\1"),
        (r"^Последний снимок активов минус капитал по денежному потоку(.*)$", r"Latest asset snapshot minus cash-flow capital\1"),
        (r"^Показатель «(.+)» за выбранный месяц$", r"Metric “\1” for the selected month"),
        (r"^(.+) укрепляется на (\d+)% к остальным валютам$", r"\1 strengthens by \2% against other currencies"),
        (r"^(.+) слабеет на (\d+)% к остальным валютам$", r"\1 weakens by \2% against other currencies"),
        (r"^Нет курса (.+) → (.+) на ([^.]+)\. Зависимый итог недоступен\.$", r"No \1 → \2 rate is available for \3. The dependent total is unavailable."),
        (r"^Месяц (.+) сохранён\.$", r"Month \1 saved."),
        (r"^Preview построен для (.+)\.$", r"Preview created for \1."),
        (r"^Недопустимые валюты активов: (.+)$", r"Unsupported asset currencies: \1"),
        (r"^Некорректная сумма у активов: (.+)$", r"Invalid asset amount for: \1"),
        (r"^Некорректная сумма актива: (.+)$", r"Invalid asset amount: \1"),
        (r"^Недопустимая валюта актива: (.+)$", r"Unsupported asset currency: \1"),
        (r"^PDF слишком большой: максимум (.+)\.$", r"The PDF is too large. Maximum size: \1."),
        (r"^PDF содержит (\d+) стр\.; максимум (\d+)\.$", r"The PDF has \1 pages. Maximum: \2."),
        (r"^Основной отчет в валюте (.+)$", r"Overview report in \1"),
        (r"^Отчет за (\d{4}) год в валюте (.+)$", r"Report for \1 in \2"),
        (r"^Отчет за (\d{2}) месяц (\d{4}) года, в валюте (.+)$", r"Report for \2-\1 in \3"),
    )
    for pattern, replacement in patterns:
        if re.match(pattern, value):
            return re.sub(pattern, replacement, value)

    replacements = {
        "Последний доступный снимок активов": "Latest available asset snapshot",
        "Накопленный денежный поток за доступную историю": "Cumulative cash flow over available history",
        "Капитал:": "Capital:",
        "Изменение:": "Change:",
        " мес.": " months",
        " лет": " years",
    }
    translated = value
    for source, target in replacements.items():
        translated = translated.replace(source, target)
    return translated


def report_column_label(column: str, locale: str | None = None) -> str:
    if normalize_locale(locale) != "en":
        return column
    if column.startswith("В валюте отчета ("):
        return column.replace("В валюте отчета", "In report currency", 1)
    return str(REPORT_TEXT_EN.get(column, column))


def localize_report_datasets(datasets: dict, locale: str | None) -> dict:
    """Return presentation-localized dataset copies without changing raw dataframes."""
    if normalize_locale(locale) != "en":
        return datasets

    localized = {}
    for dataset_id, dataset in datasets.items():
        display = dataset.display_dataframe
        if display is not None:
            display = display.copy(deep=True)
            value_columns = REPORT_VALUE_COLUMNS.intersection(display.columns)
            if dataset.id == "planning_goals":
                value_columns = value_columns - {"Показатель"}
            if dataset.id in {"yearly_stats", "planning_runway"}:
                value_columns = value_columns | {"Год", "Runway, мес.", "Runway, лет"}.intersection(display.columns)
            if dataset.id == "cockpit_metrics":
                value_columns = value_columns | {"Значение"}.intersection(display.columns)
            if dataset.id == "month_assets":
                value_columns = value_columns | {"Счет"}.intersection(display.columns)
            for column in value_columns:
                display[column] = display[column].map(lambda value: report_text(value, "en"))
        figure = localize_figure(dataset.figure, "en")
        localized[dataset_id] = replace(
            dataset,
            title=str(report_text(dataset.title, "en")),
            display_dataframe=display,
            figure=figure,
        )
    return localized


def localize_export_dataframe(data: pd.DataFrame, locale: str | None) -> pd.DataFrame:
    """Localize spreadsheet presentation while preserving numeric cells and data keys."""
    localized = data.copy(deep=True)
    if normalize_locale(locale) != "en":
        return localized
    for column in REPORT_VALUE_COLUMNS.intersection(localized.columns):
        localized[column] = localized[column].map(lambda value: report_text(value, "en"))
    return localized.rename(columns=lambda column: report_column_label(str(column), "en"))


def localize_figure(figure, locale: str | None):
    """Return a presentation-localized Plotly figure copy."""
    if figure is None:
        return None
    if normalize_locale(locale) != "en":
        return figure
    localized = deepcopy(figure)
    if localized.layout.title and localized.layout.title.text:
        localized.layout.title.text = report_text(localized.layout.title.text, "en")
    for axis_name in ("xaxis", "yaxis"):
        axis = getattr(localized.layout, axis_name, None)
        if axis and axis.title and axis.title.text:
            axis.title.text = report_text(axis.title.text, "en")
    for annotation in localized.layout.annotations or ():
        if annotation.text:
            annotation.text = report_text(annotation.text, "en")
    for trace in localized.data:
        if getattr(trace, "name", None):
            trace.name = report_text(trace.name, "en")
        if getattr(trace, "hovertemplate", None):
            trace.hovertemplate = report_text(trace.hovertemplate, "en")
        customdata = getattr(trace, "customdata", None)
        meta = getattr(trace, "meta", None)
        user_comments = isinstance(meta, dict) and meta.get("user_comments")
        if customdata is not None and not user_comments:
            trace.customdata = _translate_nested_values(customdata)
        header = getattr(trace, "header", None)
        if header is not None and getattr(header, "values", None) is not None:
            header.values = [report_column_label(str(value), "en") for value in header.values]
    return localized


def _translate_nested_values(value):
    if isinstance(value, str):
        return report_text(value, "en")
    if isinstance(value, pd.DataFrame):
        return value.map(lambda item: report_text(item, "en"))
    if isinstance(value, pd.Series):
        return value.map(lambda item: report_text(item, "en"))
    if hasattr(value, "tolist"):
        value = value.tolist()
    if isinstance(value, (list, tuple)):
        return [[_translate_nested_values(item) for item in row] if isinstance(row, (list, tuple)) else _translate_nested_values(row) for row in value]
    return value
